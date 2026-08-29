# DR overview

Generic guidance for using restore-engine in disaster recovery. Site-specific topology (UCS, Ceph layout, NIC bonding) belongs in your internal runbooks — not in this repo.

## What PBS restore is (and is not)

- Each PBS snapshot is a **full logical recovery point** (chunk references). Backup uploads can be incremental; **restore reconstructs the selected snapshot** onto target storage.
- There is **no supported “differential restore”** that patches only changed blocks onto an existing production VM disk via the PVE restore API.
- **Live-restore** can make a guest usable earlier while the full image still streams; it is not a delta merge into old disks.

restore-engine orchestrates PBS → PVE restores, plans, drills, and assurance. It does not implement a custom chunk-patch failback writer.

## Recommended DR shape

1. **Production:** PBS backups from PVE (schedule + verify jobs on PBS).
2. **DR site:** PBS **sync/replicate** so snapshots are local before you need them.
3. **Disaster:** restore-engine from local PBS — ordered **plans**, optional **live-restore** where RTO matters.
4. **Assurance / drills:** scratch location, powered-off or isolated, **sampled/rotated** — not nightly full-estate rewrites to production storage.
5. **Optional:** tier-1 warm pre-stage (powered off); refresh when PBS snapshot id changes.

**RPO** is “last successful backup + sync,” not continuous replication of live disks.

## How restore-engine fits

| Capability | Use in DR |
|------------|-----------|
| **Groups** | Define which VMs belong in a recovery batch (tags, VMIDs, name patterns, ranges) |
| **Locations** | Target cluster node(s), storage, VMID start, DR vs normal mode, isolation |
| **Plans** | Ordered groups + location; **Check** before run |
| **Drills** | Prove restore path without leaving VMs on production L2 |
| **Assurance** | Policy (QGA, HTTP, max RTO) on drill outcomes |
| **Reports** | Readiness / run evidence (Markdown/HTML) |

See [README](../README.md) workflow and [infra-metrics.md](infra-metrics.md) for observability during large restores.

## Safety defaults

- Use **readiness Check** and `require_verified_to_run` before production recovery runs.
- Prefer **powered-off drills** on shared L2; use network **unlink/remap** or **isolated** locations before power-on.
- **DR mode** keeps source VMIDs — confirm explicitly; do not run against a live cluster without a runbook.

## Further reading

- [README](../README.md) — production checklist, PCI passthrough notes
- [infra-metrics.md](infra-metrics.md) — Grafana/Netdata/OTel during restores
- [gap-analysis.md](gap-analysis.md) — feature vs test coverage
