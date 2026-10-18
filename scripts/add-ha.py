#!/usr/bin/env python3
"""Add every QEMU VM in the Proxmox cluster to HA.

Run as root on any cluster node:

    python3 add-ha.py --dry-run
    python3 add-ha.py
    python3 add-ha.py --yes

The list is always printed first. Running guests are added as HA state
"started". Stopped guests are added as "stopped", so HA does not power
them on. Templates, guests already in HA, and guests with an unknown
power state are skipped. LXC containers are not added.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NODES = Path("/etc/pve/nodes")
HA_RESOURCES = Path("/etc/pve/ha/resources.cfg")
SECTION_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")
HA_VM_RE = re.compile(r"^vm:\s*(\d+)\s*$", re.MULTILINE)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.geteuid() != 0:
        print("Run this as root on a Proxmox VE node.", file=sys.stderr)
        return 1
    if not NODES.is_dir():
        print("/etc/pve/nodes is missing. Run this on a Proxmox VE node.", file=sys.stderr)
        return 1
    if shutil.which("ha-manager") is None:
        print("ha-manager not found. Run this on a Proxmox VE node.", file=sys.stderr)
        return 1

    print("Scanning the cluster for QEMU guests...", flush=True)
    pending, skipped = collect()
    print_report(pending, skipped)
    if not pending:
        return 0
    if args.dry_run:
        print()
        print("Dry run: nothing was added to HA.")
        return 0

    print()
    print("No HA group is set. Any online node may run these guests.")
    print("Stopped guests are added as HA state stopped and stay off.")
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "Refusing to change HA without a terminal. "
                "Re-run with --yes after reviewing the list above.",
                file=sys.stderr,
            )
            return 1
        print()
        try:
            answer = input(f"Type yes to add the {len(pending)} guest(s) listed above to HA: ")
        except EOFError:
            answer = ""
        if answer != "yes":
            print("Aborted. Nothing was added to HA.")
            return 1

    failed = 0
    added = 0
    for guest in pending:
        print(f"Adding vm:{guest.vmid} ({guest.name}) as {guest.ha_state}.", flush=True)
        result = run(
            ["ha-manager", "add", f"vm:{guest.vmid}", "--state", guest.ha_state],
            timeout=30,
        )
        if result.returncode == 0:
            added += 1
            continue
        failed += 1
        detail = (result.stderr or result.stdout or "ha-manager add failed").strip()
        print(f"Failed to add vm:{guest.vmid}: {detail}", file=sys.stderr)

    print()
    print(f"Added {added}, failed {failed}, of {len(pending)}.")
    return 1 if failed else 0


class Guest:
    def __init__(self, vmid: int, node: str, name: str, status: str, ha_state: str, reason: str = ""):
        self.vmid = vmid
        self.node = node
        self.name = name
        self.status = status
        self.ha_state = ha_state
        self.reason = reason


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add every QEMU VM in the cluster to HA. "
        "The list is always printed before anything is changed."
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="Print the list and exit")
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Add the listed guests after printing them, without a prompt",
    )
    return parser.parse_args(argv)


def collect() -> tuple[list[Guest], list[Guest]]:
    statuses = cluster_statuses()
    already = ha_vmids(HA_RESOURCES.read_text(encoding="utf-8", errors="replace") if HA_RESOURCES.is_file() else "")
    pending: list[Guest] = []
    skipped: list[Guest] = []
    for nodedir in sorted(path for path in NODES.iterdir() if path.is_dir()):
        qemu_dir = nodedir / "qemu-server"
        if not qemu_dir.is_dir():
            continue
        node = nodedir.name
        for conf in sorted(qemu_dir.glob("*.conf")):
            guest = guest_from_conf(node, conf, statuses, already)
            if guest is None:
                continue
            if guest.reason:
                skipped.append(guest)
            else:
                pending.append(guest)
    pending.sort(key=lambda guest: (guest.node, guest.vmid))
    skipped.sort(key=lambda guest: (guest.reason, guest.node, guest.vmid))
    return pending, skipped


def guest_from_conf(
    node: str,
    conf: Path,
    statuses: dict[tuple[str, int], str],
    already: set[int],
) -> Guest | None:
    if not conf.stem.isdigit():
        return None
    values = conf_values(conf)
    vmid = int(conf.stem)
    name = values.get("name") or "-"
    status = statuses.get((node, vmid), "unknown")
    if values.get("template") == "1":
        return Guest(vmid, node, name, status, "", "template")
    if vmid in already:
        return Guest(vmid, node, name, status, "", "already in HA")
    ha_state = ha_state_for(status)
    if ha_state is None:
        return Guest(vmid, node, name, status, "", "unknown power state")
    return Guest(vmid, node, name, status, ha_state)


def ha_state_for(status: str) -> str | None:
    """Match HA state to the current power state so stopped VMs stay off."""
    if status in {"running", "paused"}:
        return "started"
    if status == "stopped":
        return "stopped"
    return None


def print_report(pending: list[Guest], skipped: list[Guest]) -> None:
    print("QEMU guests that will be added to HA:")
    print()
    if not pending:
        print("  (none)")
    else:
        print(f"  {'VMID':<8}  {'NODE':<20}  {'STATUS':<10}  {'HA STATE':<10}  NAME")
        for guest in pending:
            print(
                f"  {guest.vmid:<8}  {guest.node:<20}  {guest.status:<10}  "
                f"{guest.ha_state:<10}  {guest.name}"
            )
    print()
    print(f"{len(pending)} QEMU guest(s) to add.")
    if not skipped:
        return
    print()
    print("Skipped:")
    print()
    print(f"  {'VMID':<8}  {'NODE':<20}  {'STATUS':<10}  {'REASON':<22}  NAME")
    for guest in skipped:
        print(
            f"  {guest.vmid:<8}  {guest.node:<20}  {guest.status:<10}  "
            f"{guest.reason:<22}  {guest.name}"
        )


def conf_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        match = SECTION_RE.match(line)
        if match and match.group(1) not in values:
            values[match.group(1)] = match.group(2).strip()
    return values


def ha_vmids(text: str) -> set[int]:
    return {int(match) for match in HA_VM_RE.findall(text)}


def cluster_statuses() -> dict[tuple[str, int], str]:
    result = run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pvesh failed").strip()
        print(f"Could not read cluster status ({detail}). Those guests will be skipped.", file=sys.stderr)
        return {}
    return statuses_from_resources(result.stdout)


def statuses_from_resources(raw: str) -> dict[tuple[str, int], str]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        return {}
    statuses: dict[tuple[str, int], str] = {}
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "qemu":
            continue
        node = str(item.get("node") or "")
        try:
            vmid = int(item["vmid"])
        except (KeyError, TypeError, ValueError):
            continue
        statuses[(node, vmid)] = str(item.get("status") or "unknown")
    return statuses


def run(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if not stderr:
            stderr = f"timed out after {timeout}s"
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        return subprocess.CompletedProcess(args, 124, stdout, stderr)


def _self_check() -> None:
    assert ha_state_for("running") == "started"
    assert ha_state_for("paused") == "started"
    assert ha_state_for("stopped") == "stopped"
    assert ha_state_for("unknown") is None
    assert ha_vmids("vm: 100\n\tstate started\nvm:101\nct: 50\n") == {100, 101}
    resources = json.dumps(
        {
            "data": [
                {"type": "qemu", "node": "pve-a", "vmid": 10, "status": "running"},
                {"type": "lxc", "node": "pve-a", "vmid": 20, "status": "stopped"},
                {"type": "qemu", "node": "pve-b", "vmid": 11, "status": "stopped"},
            ]
        }
    )
    assert statuses_from_resources(resources) == {("pve-a", 10): "running", ("pve-b", 11): "stopped"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        _self_check()
        print("ok")
        raise SystemExit(0)
    raise SystemExit(main())
