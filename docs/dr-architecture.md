# DR architecture notes

Decisions from restore-engine design discussions (vendor-neutral where possible). Guidance for operators — not a commitment to implement every option.

**Estate context (typical):** ~4 hyperconverged PVE/Ceph nodes; ~60–70 TiB virtual; many 100–200 GiB app VMs; a few ~4 TiB guests; SQL VMs ~8 TiB gross with largest disk ~2 TiB. PBS at the same site/VLAN. Native SQL DR may come later; for now restores are full VM-from-PBS.

---

## What PBS restore is (and is not)

- Each PBS snapshot is a **full logical recovery point** (chunk references). Backup *uploads* can be incremental via dirty bitmaps; **restore reconstructs the selected snapshot** onto target storage.
- There is **no supported “differential restore”** that patches only changed blocks onto an existing production VM disk via the PVE restore API.
- **Live-restore** can make a guest usable earlier while the full image still streams; it is not a delta merge into old disks.

restore-engine orchestrates PBS → PVE restores, plans, drills, and assurance. It does not invent a custom chunk-patch failback writer (high risk, unsupported).

---

## Recommended shape (Ceph production, no RBD mirror)

1. Prod: Ceph → PBS backups only (no fleet-wide RBD mirror).
2. DR: PBS **pull sync** so snapshots are local.
3. Disaster: restore-engine from **local PBS**, priority order, live-restore where RTO matters.
4. Assurance/drills: **scratch** pool, sampled/rotated — not nightly full-estate rewrites.
5. Optional: small **tier-1 warm pre-stage** (powered off); refresh only when PBS snapshot id changes.
6. Optional later: native SQL / narrow mirror for the two large SQL VMs only.

**RPO** is “last successful backup + sync,” not seconds behind live Ceph.

---

## Production networking (current)

Hyperconverged nodes use **Cisco UCS Manager (UCSM)** with **Fabric Interconnects (FI)**. Typical layout:

| Role | vNICs | Linux bonding (prod today) |
|------|-------|------------------------------|
| Management | 2 | **active/standby** |
| Corosync | 2 | **active/standby** |
| Ceph internal (cluster) | 2 | Often TLB/ALB (candidate to change) |
| Ceph external (public) | 2 | Often TLB/ALB (candidate to change) |
| VM uplink | 2 | Often TLB/ALB (candidate to change) |
| Backup / PBS | 2 | Often TLB/ALB (candidate to change) |

**12 vNICs → 6 bonds.**

### Cisco FI and bonding

- The two uplinks of a pair typically go to **different FIs** (fabric A / fabric B) for **HA**, not as one LACP LAG. FIs in this design **do not support LACP** on those host uplinks the way a Nexus does.
- **`balance-tlb` / `balance-alb`** across two FI-facing NICs is a poor production fit: asymmetric paths, awkward failover, hard to reason about. Prefer **not** to use TLB/ALB toward two FIs.
- **active/standby** for mgmt and corosync is a **valid production choice** (HA only; one link active).
- On UCS, **vNIC Fabric Failover** is the intended HA mechanism: preferred fabric A or B, failover to the other; Linux sees a stable NIC. That can replace a second bonded slave for HA.

### Production hardening direction (optional, rolling)

For roles still on TLB/ALB (Ceph, VM, backup):

1. UCSM: keep **one** vNIC per role, set preferred Fabric ID, **Enable Failover**.
2. Spread preferred fabric across roles so both FIs carry traffic when healthy.
3. PVE: remove TLB/ALB bond; bridge on the single vNIC.
4. Leave mgmt/corosync active/standby until a quiet window if they are stable; optionally collapse later to one vNIC + fabric failover.

Where the **switch supports LACP** (e.g. Nexus), Linux **`802.3ad`** bonds remain appropriate — that is separate from FI-facing pairs.

---

## DR networking (target)

DR nodes have (planned):

- **2×40 Gbit/s** → two different FIs (HA, no LACP on FI)
- **4×10 Gbit/s** → Cisco Nexus (**LACP supported**)

Do **not** clone all 12 production vNICs. Keep DR simpler.

### Target assignment (per hyperconverged DR node)

| Ports | Config | Role |
|-------|--------|------|
| **40 G #1** | Prefer FI-A, **Fabric Failover on**, **no** Linux bond | **PBS / backup** (restore ingest RX) |
| **40 G #2** | Prefer FI-B, **Fabric Failover on**, **no** Linux bond | **Ceph cluster** (replication TX) |
| **3×10 G** | Linux bond **`802.3ad` (LACP)** → Nexus | **Ceph public + VM uplink + mgmt** (VLAN-separated) |
| **1×10 G** | Standalone | **Corosync only** |

```text
PBS  --40G-->  node  --40G cluster-->  other OSDs
                 │
                 └── 3×10 LACP: Ceph public / VMs / mgmt
```

### Why this split (utilization + write amplification)

- During a restore blast, **VM-only use of a 40 G** wastes a fat pipe while Ceph is starved.
- **PBS can approach ~40 Gbit/s (~5 GB/s)** into the node; that will **not** “fit” through **3×10 G (~3.75 GB/s)** if **all** Ceph public + cluster traffic shares that LACP bond — especially with replication.
- On hyperconverged restore, critical flows are **PBS RX** and **Ceph cluster TX** (replicas to other nodes). Put those on the **two 40 G** links (opposite preferred fabrics + failover).
- Put **public / VM / mgmt** on Nexus LACP; guest traffic is usually small vs restore.

### Fabric failover vs “use both 40 Gs”

- **HA:** each 40 G vNIC has fabric failover so one FI death does not kill that role.
- **Throughput when healthy:** PBS and Ceph cluster prefer **different** FIs so both 40 Gs work.
- If one FI fails, both roles share the surviving FI (degraded bandwidth, service up). Do **not** hard-pin roles to FIs **without** failover.

### DR cutover checklist

1. Build 3×10 LACP + Ceph public VLANs; bring OSDs up.
2. Bring dedicated 10 G corosync; form/join cluster.
3. UCSM: two 40 G vNICs, opposite fabric, failover on; PVE: two bridges, no 40 G bond.
4. Place PBS VLAN on the backup 40 G bridge; test throughput.
5. Run restore-engine drills.

---

## Restore speed expectations (order of magnitude)

Numbers are **planning ceilings**, not guarantees. Real limits: CPU (older M6-class hosts), OSD disks, PBS cold cache, concurrent jobs, Proxmox/`bwlimit`/PBS traffic control.

### Link raw rates

| Path | Approx raw |
|------|------------|
| 1×40 Gbit/s | ~5 GB/s |
| 3×10 G LACP | ~3.75 GB/s |
| 1×10 Gbit/s | ~1.25 GB/s |

### Ceph replication tax (simplified)

For **size = N** (replica count), a primary OSD roughly sends **(N−1)×** useful data to peers on the **cluster** network (plus public/client overhead when not local).

| Pool | Cluster TX vs useful write (rough) | Implication |
|------|--------------------------------------|-------------|
| **size=3** (typical prod) | ~**2×** useful | Cluster often limits before a single 40 G PBS RX |
| **size=2** | ~**1×** useful | Less amp; faster first land; less durability in the window |
| Local **ZFS** (no Ceph repl) | **~1×** to local disks | Fastest first land; node-local until migrate |

With **40 G PBS + 40 G cluster** and size=3, a **per-node network-bound useful** restore rate on the order of **~15–20 Gbit/s (~2–2.5 GB/s)** is a more honest ceiling than “5 GB/s” (cluster ≈ useful×2). Disks/CPU often land lower.

With **PBS on 40 G and all Ceph on 3×10 only**, expect **worse** (shared ~30 Gbit/s for public + replication).

### Whole-estate wall time

- **Non-zero** bytes (Backups → **Estimate sizes**) drive effort more than gross PBS archive size.
- Many **100–200 GiB** apps: parallelize across all 4 nodes; often minutes–tens of minutes each at good concurrency.
- **≤2 TiB** disks (even inside an “8 TiB” SQL VM): plan per disk; hours-class for dense data.
- Full **~60–70 TiB** virtual estate to durable Ceph size=3: typically **many hours to >1 day** at realistic sustained rates — optimize **business RTO waves**, not one flat clock.

### restore-engine knobs

- `max_concurrent_restores`, multi-node targets, **bwlimit = 0**, clear datacenter/PBS throttles for the window.
- Live-restore improves **time-to-usable**, not always time-to-full-disk.
- Pause Ceph scrub/backfill during the blast when safe.

---

## Interim storage: Ceph size=2 vs size=3 vs ZFS

| Approach | First-land speed | HA / mobility during land | Later cost | Notes |
|----------|------------------|---------------------------|------------|--------|
| Straight **Ceph size=3** | Slowest first land | Full shared storage | None | Best end state |
| **Ceph size=2** pool, then move to size=3 | Faster | Full shared storage | Second full write | Prefer **move disk** over in-place `size` 2→3 (cluster-wide backfill at 60–70 TiB is painful) |
| **ZFS land**, then move to Ceph size=3 | Often fastest first land | Node-local until migrate | Second full write | Best for fat SQL disks / landing server |
| In-place grow pool 2→3 | Faster first land | Full | **Heavy backfill** | Avoid for whole estate |

**Practical hybrid**

- App VMs (100–200 GiB) → Ceph size=3 (or size=2 only if you need mobility on day one).
- SQL / largest disks → ZFS or Ceph size=2 interim → migrate to size=3 when stable.
- Optional **landing server** (no OSDs, local NVMe/ZFS): max gain when hyperconverged nodes would otherwise fight PBS RX + OSD + replication on the same CPUs/NICs.

---

## ZFS layout for DR landing: mirror vs RAIDZ

Use ZFS **only as DR landing/scratch**, not as long-term shared prod replacement for Ceph.

| Layout | When to use | Pros | Cons |
|--------|-------------|------|------|
| **Mirror** (or striped mirrors, dRAID mirrors) | **Preferred for VM disks / RBD-like random write** | Better random IOPS, simpler replace, predictable latency | Lower usable % than RAIDZ |
| **RAIDZ1/Z2** | Bulk capacity, sequential, backups | Better capacity efficiency | Poor fit for many small random VM writes; resilver heavier; often worse latency under restore+boot |

**Recommendation for restore landing (SQL/fat VMs):** **mirrors** (e.g. multiple mirror vdevs), optionally with **special** metadata vdev on Optane/fast NVMe if available. Use RAIDZ for a **scratch dump/archive** dataset if needed — not for the primary “boot the SQL VM here” pool.

Optane: useful as **special**/SLOG or a small fast pool; it will not hold the whole estate alone.

---

## NVMe wear

- Full reconstruct drills rewrite roughly whole disks; daily full-estate Assure ages drives quickly.
- Split **scratch (proof)** vs **warm (staging)**.
- Prefer PBS sync for freshness; write PVE/Ceph NVMe for real DR or rare proof.

---

## Deferred ideas

| Idea | Role | Status |
|------|------|--------|
| Snapshot-to-snapshot `.fidx` diff | Analytics | Deferred |
| Custom differential failback onto existing VM | True delta | Out of scope |
| Fleet Ceph RBD mirror | Warm DR | Optional; ops risk — consider **SQL-only** later |
| Native SQL AG / log shipping | Bypass 8 TiB VM restore | Future |

---

## Product actions (restore-engine)

- **Non-zero size estimate** (from `.fidx`): on-demand beside gross size — Backups UI **Estimate sizes** / `POST /api/backups/estimate-size`.
- Multi-node restore, concurrency, live-restore, bwlimit — use for DR waves; see README Performance tuning.
