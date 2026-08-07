"""Backup inventory grouping helpers (UI parity).

The Backups table groups snapshots client-side: one row per VMID with a
snapshot dropdown. This module mirrors that logic for unit tests.
"""

from __future__ import annotations

from typing import Any


def group_backups_by_vmid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group PBS backup rows by ``vmid``; snapshots newest-first; default = latest.

    Returns a list of groups sorted by VMID::

        {
          "vmid": int,
          "name": str,           # from latest snapshot
          "source_label": str,
          "snapshots": [row, ...],  # newest timestamp first
          "latest": row,
        }
    """
    by_vmid: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            vmid = int(row["vmid"])
        except (KeyError, TypeError, ValueError):
            continue
        by_vmid.setdefault(vmid, []).append(row)

    groups: list[dict[str, Any]] = []
    for vmid in sorted(by_vmid.keys()):
        snaps = sorted(
            by_vmid[vmid],
            key=lambda r: str(r.get("timestamp") or ""),
            reverse=True,
        )
        latest = snaps[0]
        groups.append(
            {
                "vmid": vmid,
                "name": latest.get("name") or f"vm-{vmid}",
                "source_label": latest.get("source_label") or latest.get("datastore") or "",
                "snapshots": snaps,
                "latest": latest,
            }
        )
    return groups
