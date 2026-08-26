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

DR target (failover vs `active-backup` vs TLB/ALB, unique service profiles): see **UCS: fabric failover vs Linux bonding** below.

---

## UCS: fabric failover vs Linux bonding

FIs do **not** present host LACP the way Nexus does. Two vNICs pinned to FI-A and FI-B are **HA**, not a LAG.

| Mechanism | On FI-facing 40 G? |
|-----------|---------------------|
| **UCS vNIC fabric failover** (one vNIC, preferred fabric) | **Yes** — intended HA; OS sees one NIC / MAC |
| Two vNICs + **`balance-tlb` / `balance-alb`** | **No** — asymmetric paths; ALB MAC/CAM games on FIs; **TLB does not add RX** (restore ingest is RX) |
| Two vNICs + Linux **`active-backup`** | Only if you need host ARP path-check (“FI up, northbound dead”). **Disable** fabric failover on those vNICs — never run both |
| Linux **`802.3ad` (LACP)** | **Nexus only** (3×10, and PBS if recabled there) |

**Do not mix** fabric failover and a Linux bond on the same vNICs.

Failover’s cost vs dual-vNIC bonding: **no aggregation on that vNIC when healthy** (active/standby for that role), HA is opaque to Linux/Netdata, and UCS does not fail over on “FI up / Nexus uplinks dead.” Dual+TLB does **not** fix that and does not double PBS RX.

Healthy **throughput** comes from **splitting roles across preferred fabrics** (PBS prefer A, Ceph cluster prefer B), not from bonding one role across A+B. One FI death: both roles share the survivor (degraded, service up). Do **not** hard-pin without failover.

**Unique service profiles per server** do not change this. Failover is a per-vNIC property on each profile. Share a **LAN connectivity policy** / updating template so all four nodes match; keep the **same** preferred-fabric pattern on every node. Unique SPs only make drift (vNIC order → Linux names) more likely — bonding would make that worse.

---

## DR networking (target)

DR nodes (planned):

- **2×40 Gbit/s** → two different FIs (HA, **no** LACP on FI; fabric failover)
- **4×10 Gbit/s** → Cisco Nexus (**LACP supported**); **3×10 split across both Nexus** (vPC/MLAG), **1×10** to a single Nexus

Do **not** clone all 12 production vNICs. Do **not** split the remaining 10 Gs as “2×10 Ceph public + 3×10 VM” — only three 10 Gs remain after corosync.

### Target assignment (per hyperconverged DR node)

| Ports | Config | Role |
|-------|--------|------|
| **40 G #1** | Prefer FI-A, **Fabric Failover on**, **no** Linux bond | **PBS / backup** data + **mgmt VLAN** (separate subnets) |
| **40 G #2** | Prefer FI-B, **Fabric Failover on**, **no** Linux bond | **Ceph internal (cluster)** — OSD↔OSD replication |
| **3×10 G** | Linux **`802.3ad` (LACP)** → **both Nexus** | VLANs: **Ceph external (public)**, **VM uplink**, **corosync ring1** |
| **1×10 G** | Standalone, one Nexus | **Corosync ring0 only** |

```text
PBS  --40G backup-->  PVE  --40G cluster-->  other OSDs
                        │
                        └── 3×10 LACP (both Nexus): Ceph public / VMs / corosync ring1
```

Use **all three** leftover 10 Gs in **one** LACP. A 2×10 Ceph-only bond would cap public at ~20 Gbit/s during restore while a dedicated VM 10 G sat idle. Guest traffic is small in a restore window; Ceph public is not.

### VLANs on the 3×10 LACP

| VLAN | Role |
|------|------|
| Ceph **external** (public) | RBD client (PVE → primary OSD); Ceph mons |
| VM uplink | Guests |
| Corosync **ring1** | Second knet ring; **QoS/CoS**; **MTU 1500** — **not** the Ceph public subnet |

### VLANs on backup 40 G

| VLAN | Role |
|------|------|
| PBS / backup | Restore ingest (PVE RX) |
| Mgmt | SSH, PVE API — **not** the PBS subnet |

Mgmt on the backup **physical** is acceptable as a **separate VLAN + QoS**: FI path survives Nexus trouble; traffic is tiny TCP (unlike corosync). Costs: that NIC can sit at ~40 G RX during restore (QoS required); bouncing the backup vNIC can drop SSH — keep **CIMC / UCSM / KVM** as real OOB.

Do **not** let hostname/default route pull extra bulk onto this NIC:

- Proxmox **live migration** → VM (3×10) network, not backup/mgmt IP
- Ceph mons / public → Ceph external VLAN on 3×10
- restore-engine / browsers → **mgmt** IP, not the PBS VLAN IP

Simpler alternative: mgmt VLAN on the 3×10 next to VM (conventional HCI; less FI vs Nexus diversity for SSH).

### Why this split (utilization + write amplification)

- VM-only use of a 40 G wastes a fat pipe while Ceph is starved.
- PBS can approach ~40 Gbit/s (~5 GB/s) **into one node**; that will **not** fit through 3×10 if **public + cluster** share that bond, especially with RF3.
- Critical restore flows: **PBS RX** (backup 40 G) and **OSD replica** traffic (cluster 40 G). Public on 3×10 still carries PVE → primary OSD (see restore path below).
- Spread preferred fabric so both 40 Gs work when healthy.

### Corosync (two knet rings)

The dedicated 10 G lands on **one** Nexus only. The 3×10 LACP is already dual-homed — that is ring1, **not** a VLAN on a 40 G.

| Ring | Path | Survives |
|------|------|----------|
| **ring0** | Dedicated 1×10 → Nexus-A | Nexus-B down; LACP degraded; FI/40 G issues |
| **ring1** | VLAN on 3×10 LACP | Nexus-A / that 10 G cable down (bond still has members on B) |

Use two **knet rings**, not a Linux bond of 10 G + LACP (bond failover can lose the token). **Do not** put corosync on either 40 G (those pipes fill during restore). **Do not** put ring1 on Ceph public or Ceph cluster.

### DR cutover checklist

1. Cable 3×10 LACP across **both** Nexus; VLANs for Ceph public, VM, corosync ring1 (+ QoS); bring OSDs up.
2. Dedicated 10 G corosync ring0; form/join cluster; add ring1 on the LACP VLAN.
3. UCSM: two 40 G vNICs, opposite fabric, failover on; PVE: two bridges, **no** 40 G bond.
4. PBS VLAN + mgmt VLAN on the backup 40 G bridge; set PVE **migration** off that IP; test throughput.
5. Run restore-engine drills.

---

## Ceph networks (public vs cluster)

| Name here | Ceph term | What it carries |
|-----------|-----------|-----------------|
| **External** | `public_network` | Clients ↔ OSDs (**RBD** reads/writes), mons |
| **Internal** | `cluster_network` | OSD↔OSD **replication, backfill, recovery**, OSD heartbeats |

If `cluster_network` is unset, replicas share **public** (worse during restore).

**RBD mirroring** (site-to-site) is client-like → **public**, not cluster. Local RF3 (`size=3`) is **not** mirroring.

---

## Restore data path (PBS → PVE → Ceph RF3)

PBS does **not** talk to Ceph. PVE **terminates** the backup stream, then **writes a new RBD**.

```text
PBS  --backup 40G-->  PVE (qemu / pbs client)
                         |
                         | Ceph public (3×10 LACP)
                         v
                    primary OSD
                         |
                         | Ceph cluster (40G)
                         +--> replica OSD
                         +--> replica OSD     (size=3)
```

| Hop | Network |
|-----|---------|
| PBS → PVE | Dedicated **backup 40 G** |
| PVE (`librbd` / QEMU) → primary OSD | Ceph **external** |
| Primary OSD → other OSDs | Ceph **internal** |

Not on the data path: VM uplink (except guest after boot), corosync, mgmt (API only).

The client sends each write only to the **primary**. On **4-node HCI**, primaries are spread, so most client writes **leave the restoring node on public**. The cluster net is used only by **OSDs** (replicas), never by QEMU.

Rough factors, useful restore rate **W** (balanced CRUSH, 4 nodes, size=3):

| | One node restoring | Four nodes restoring (each at W) |
|--|--------------------|----------------------------------|
| PBS RX | W | W |
| Public TX/RX | ~0.75 W | ~0.75 W each way |
| Cluster TX/RX | ~0.5 W | **~2 W** each way |

Live-restore uses the **same** pipes (PBS read and RBD write overlap); it does not add bandwidth.

---

## Restore speed expectations (order of magnitude)

Numbers are **planning ceilings**, not guarantees. Real limits: CPU (older M6-class hosts), OSD disks, PBS cold cache, concurrent jobs, Proxmox/`bwlimit`/PBS traffic control.

### Link raw rates

| Path | Approx raw |
|------|------------|
| 1×40 Gbit/s | ~5 GB/s |
| 3×10 G LACP | ~3.75 GB/s each way (full duplex) |
| 1×10 Gbit/s | ~1.25 GB/s |

Useful = unique RBD bytes written (one copy). RF3 then stores three copies: cluster ≈ **(N−1)×** useful from primaries.

| Pool | Cluster vs useful (rough) | Implication |
|------|---------------------------|-------------|
| **size=3** | ~**2×** useful | Four-way restore: **cluster 40 G** binds before per-node PBS 40 G |
| **size=2** | ~**1×** useful | Faster first land; less durability in the window |
| Local **ZFS** (no Ceph repl) | **~1×** to local disks | Fastest first land; node-local until migrate |

With **PBS on 40 G and all Ceph on 3×10 only**, expect **worse** (shared ~30 Gbit/s for public + replication).

### Four nodes in parallel (this NIC layout, size=3)

Per node, cluster load ≈ **2× W** each way on a 40 G (~5 GB/s) cap → **W ≈ 2.5 GB/s/node** (~20 Gbit/s useful).

| | Network math |
|--|----------------|
| Per node useful | **~2.5 GB/s** (~20 Gbit/s) |
| **4-node aggregate useful** | **~10 GB/s** (~80 Gbit/s) |
| Per-node PBS 40 G | Only ~20 Gbit/s needed (half idle) |
| Public 3×10 | ~0.75×2.5 GB/s ≈ 1.9 GB/s — **headroom** |
| OSD media writes cluster-wide | **~3× useful** ≈ 30 GB/s (~7.5 GB/s/node) |

**One node restoring:** cluster on that node is only ~0.5 W, so **PBS 40 G** and **public 3×10** cap first → **~5 GB/s** useful on that node. Four-way is **slower per node**, **faster in total**.

PBS must **transmit ~80 Gbit/s** cluster-wide to hit the four-node network cap:

| PBS uplinks | Who limits 4-way restore |
|-------------|---------------------------|
| **2×40 G** and TX actually spreads (four PVE IPs) | Tie: PBS ≈ Ceph cluster (~10 GB/s useful) |
| **1×40 G** or TLB stuck on one slave | **PBS** → ~1.25 GB/s/node, ~5 GB/s total |

### Honest sustained

Use **~40–60%** of line-rate for planning (M6 CPU, PBS chunk reconstruct, Ceph overhead, LACP hash):

| | Network math | Planning |
|--|----------------|----------|
| 4 nodes, RF3, PBS can do 80 Gbit/s | ~10 GB/s | **~4–6 GB/s** useful (~15–20 TB/h) |
| Same, PBS only 40 Gbit/s | ~5 GB/s | **~2–3 GB/s** (~7–11 TB/h) |

- **Non-zero** bytes (Backups → **Estimate sizes**) drive effort more than gross PBS archive size.
- Many **100–200 GiB** apps: parallelize across all 4 nodes; often minutes–tens of minutes each at good concurrency.
- **≤2 TiB** disks (even inside an “8 TiB” SQL VM): plan per disk; hours-class for dense data.
- Full **~60–70 TiB** virtual estate to durable Ceph size=3: typically **many hours to >1 day** — optimize **business RTO waves**, not one flat clock.

### restore-engine knobs

- `max_concurrent_restores`, multi-node targets, **bwlimit = 0**, clear datacenter/PBS throttles for the window.
- Live-restore improves **time-to-usable**, not always time-to-full-disk.
- Pause Ceph scrub/backfill during the blast when safe.

---

## PBS cabling: stay on FI vs Nexus LACP

LACP on Nexus is how PBS can **offer** ~80 Gbit/s. Whether to recable depends on the **path to PVE backup 40 Gs**, not on LACP as an idea.

If PBS and PVE backup vNICs both stay on **FIs**, restore can stay **on-fabric** (PBS → FI → PVE) and never use Nexus.

If **only PBS** moves to Nexus:

```text
PBS  --LACP 2×40-->  Nexus  --FI uplinks-->  FI  -->  PVE backup 40G
```

All restore hairpins on **FI northbound**. If those uplinks are thin or shared with VM/Ceph public, LACP can **lose** more than it gains. Recable only if FI uplinks are **≥80 Gbit/s** with headroom, **or** PVE backup NICs also land on Nexus.

On FI, PBS cannot LACP: two 40 G vNICs = two fabrics (prefer A/B + failover), **not** one 80 G LAG. **Do not** use TLB/ALB toward FIs.

**Can the PBS M6 fill ~80 Gbit/s?** 21 NVMe + ~1 TB RAM / L2ARC can likely **read** that. Restore is usually limited by **parallel streams**, **PVE decrypt/decompress CPU**, then PBS CPU/TLS. **One job is often ~10–25 Gbit/s**, not 80. Even perfect PBS TX only **ties** the RF3 cluster 40 G cap (~10 GB/s useful); it does not double restore.

Measure before recabling:

1. `iperf3` with many streams to **all four** PVE backup IPs (path/NIC test).
2. One fat VM restore, then 4–8 in parallel; watch PBS TX, PVE backup RX, Ceph cluster, CPU.

If iperf is ~80 Gbit/s but restore is ~20 Gbit/s with idle NICs → CPU/jobs, not cabling. If iperf is ~40 Gbit/s with one slave busy → path/TLB; Nexus LACP (with a fat path) may help. If recabling: **2×40 G LACP across both Nexus**, PBS off Ceph/VM VLANs.

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
- **Infra metrics**: optional Grafana Compose profile + Netdata (lab install / prod optional) + PVE/PBS OpenTelemetry — see [infra-metrics.md](infra-metrics.md).
