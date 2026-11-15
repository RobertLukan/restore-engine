#!/usr/bin/env python3
"""Add every QEMU VM in the Proxmox cluster to HA.

Run as root on any cluster node:

    python3 add-ha.py --dry-run
    python3 add-ha.py
    python3 add-ha.py --yes
    python3 add-ha.py --start-all

The list is always printed first. A running guest is always HA state
"started" and is never switched to "stopped". Stopped guests are added as
"stopped", so HA does not power them on. Re-running the script reads one
cluster power-state list: if a guest is on but its HA state is still
stopped, that state is changed to started. --start-all sets
stopped guests to HA state "started", which powers them on.

Templates and LXC containers are skipped. No HA group is set.
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
HA_HEADER_RE = re.compile(r"^(vm|ct):\s*(\d+)\s*$")
HA_STATE_RE = re.compile(r"^\s*state\s+(\S+)\s*$")
ALIVE = {"running", "paused"}


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
    pending, skipped = collect(start_all=args.start_all)
    print_report(pending, skipped, start_all=args.start_all)
    if not pending:
        return 0
    if args.dry_run:
        print()
        print("Dry run: nothing was changed.")
        return 0

    print()
    print("No HA group is set. Any online node may run these guests.")
    if args.start_all:
        print("Stopped guests will be set to HA state started, which powers them on.")
    else:
        print("A running guest is never given HA state stopped.")
        print("Stopped guests are added as HA state stopped and stay off.")
    if not confirm(args.yes, len(pending), start_all=args.start_all):
        return 1

    failed = 0
    changed = 0
    for guest in pending:
        if not apply_guest(guest):
            failed += 1
            continue
        changed += 1

    print()
    print(f"Changed {changed}, failed {failed}, of {len(pending)}.")
    return 1 if failed else 0


class Guest:
    def __init__(
        self,
        vmid: int,
        node: str,
        name: str,
        status: str,
        ha_state: str,
        action: str = "",
        reason: str = "",
    ):
        self.vmid = vmid
        self.node = node
        self.name = name
        self.status = status
        self.ha_state = ha_state
        self.action = action
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
        help="Apply the listed changes after printing them, without a prompt",
    )
    parser.add_argument(
        "--start-all",
        action="store_true",
        help="Set stopped guests to HA state started, which powers them on",
    )
    return parser.parse_args(argv)


def confirm(assume_yes: bool, count: int, *, start_all: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Refusing to change HA without a terminal. "
            "Re-run with --yes after reviewing the list above.",
            file=sys.stderr,
        )
        return False
    verb = "start" if start_all else "update"
    print()
    try:
        answer = input(f"Type yes to {verb} the {count} guest(s) listed above: ")
    except EOFError:
        answer = ""
    if answer != "yes":
        print("Aborted. Nothing was changed.")
        return False
    return True


def collect(*, start_all: bool) -> tuple[list[Guest], list[Guest]]:
    print("Reading cluster power state...", flush=True)
    by_node, by_vmid = cluster_statuses()
    print("Reading HA config and guest files...", flush=True)
    ha = ha_states(HA_RESOURCES.read_text(encoding="utf-8", errors="replace") if HA_RESOURCES.is_file() else "")
    pending: list[Guest] = []
    skipped: list[Guest] = []
    for nodedir in sorted(path for path in NODES.iterdir() if path.is_dir()):
        qemu_dir = nodedir / "qemu-server"
        if not qemu_dir.is_dir():
            continue
        node = nodedir.name
        for conf in sorted(qemu_dir.glob("*.conf")):
            if not conf.stem.isdigit():
                continue
            values = conf_values(conf)
            vmid = int(conf.stem)
            guest = plan_guest(
                vmid=vmid,
                node=node,
                name=values.get("name") or "-",
                template=values.get("template") == "1",
                status=resolve_status(node, vmid, by_node, by_vmid),
                ha_state=ha.get(vmid),
                start_all=start_all,
            )
            if guest.reason:
                skipped.append(guest)
            else:
                pending.append(guest)
    pending.sort(key=lambda guest: (guest.node, guest.vmid))
    skipped.sort(key=lambda guest: (guest.reason, guest.node, guest.vmid))
    return pending, skipped


def plan_guest(
    *,
    vmid: int,
    node: str,
    name: str,
    template: bool,
    status: str,
    ha_state: str | None,
    start_all: bool,
) -> Guest:
    """Decide one guest. A running guest is never planned as HA state stopped."""
    if template:
        return Guest(vmid, node, name, status, "", reason="template")
    in_ha = ha_state is not None
    alive = status in ALIVE
    if alive:
        if ha_state == "stopped" or not in_ha:
            action = "set" if in_ha else "add"
            return Guest(vmid, node, name, status, "started", action=action)
        return Guest(vmid, node, name, status, ha_state or "", reason="already in HA")
    if start_all and (status == "stopped" or ha_state == "stopped"):
        action = "set" if in_ha else "add"
        return Guest(vmid, node, name, status, "started", action=action)
    if status == "stopped" and not in_ha and not start_all:
        return Guest(vmid, node, name, status, "stopped", action="add")
    if status == "stopped" and in_ha:
        return Guest(vmid, node, name, status, ha_state or "", reason="already in HA")
    return Guest(vmid, node, name, status, "", reason="unknown power state")


def promote_if_running(guest: Guest, live: str) -> Guest:
    """HA requested stopped, but the guest is on. Request started instead."""
    if live not in ALIVE:
        return guest
    return Guest(guest.vmid, guest.node, guest.name, live, "started", action="set")


def resolve_status(
    node: str,
    vmid: int,
    by_node: dict[tuple[str, int], str],
    by_vmid: dict[int, str],
) -> str:
    """Prefer a running report from any node over a stopped one."""
    local = by_node.get((node, vmid), "")
    other = by_vmid.get(vmid, "")
    if local in ALIVE or other in ALIVE:
        return local if local in ALIVE else other
    if local == "stopped" or other == "stopped":
        return "stopped"
    return local or other or "unknown"


def apply_guest(guest: Guest) -> bool:
    state = guest.ha_state
    if state == "stopped":
        live = live_status(guest.node, guest.vmid)
        if live in ALIVE:
            print(
                f"vm:{guest.vmid} is {live}; keeping HA state started.",
                flush=True,
            )
            state = "started"
            if guest.action == "add":
                # Already guarded by plan_guest. Keep add, but never pass stopped.
                pass
        elif live != "stopped":
            print(
                f"Refusing to set vm:{guest.vmid} to HA state stopped "
                f"(power state is {live or 'unknown'}).",
                file=sys.stderr,
            )
            return False
    print(f"{guest.action} vm:{guest.vmid} ({guest.name}) as {state}.", flush=True)
    cmd = ["ha-manager", guest.action, f"vm:{guest.vmid}", "--state", state]
    result = run(cmd, timeout=30)
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "ha-manager failed").strip()
    print(f"Failed to {guest.action} vm:{guest.vmid}: {detail}", file=sys.stderr)
    return False


def print_report(pending: list[Guest], skipped: list[Guest], *, start_all: bool) -> None:
    title = "QEMU guests that will be started:" if start_all else "QEMU guests that will be updated in HA:"
    print(title)
    print()
    if not pending:
        print("  (none)")
    else:
        print(f"  {'VMID':<8}  {'NODE':<20}  {'STATUS':<10}  {'ACTION':<6}  {'HA STATE':<10}  NAME")
        for guest in pending:
            print(
                f"  {guest.vmid:<8}  {guest.node:<20}  {guest.status:<10}  "
                f"{guest.action:<6}  {guest.ha_state:<10}  {guest.name}"
            )
    print()
    print(f"{len(pending)} QEMU guest(s).")
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


def ha_states(text: str) -> dict[int, str]:
    """Map VMID to the requested HA state. Missing state defaults to started."""
    states: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        header = HA_HEADER_RE.match(line)
        if header:
            kind, raw_id = header.group(1), header.group(2)
            current = int(raw_id) if kind == "vm" else None
            if current is not None:
                states[current] = "started"
            continue
        if current is None:
            continue
        state = HA_STATE_RE.match(line)
        if state:
            states[current] = state.group(1)
    return states


def cluster_statuses() -> tuple[dict[tuple[str, int], str], dict[int, str]]:
    result = run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pvesh failed").strip()
        print(f"Could not read cluster status ({detail}). Those guests will be skipped.", file=sys.stderr)
        return {}, {}
    return statuses_from_resources(result.stdout)


def statuses_from_resources(raw: str) -> tuple[dict[tuple[str, int], str], dict[int, str]]:
    text = (raw or "").strip()
    if not text:
        return {}, {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}, {}
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        return {}, {}
    by_node: dict[tuple[str, int], str] = {}
    by_vmid: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "qemu":
            continue
        node = str(item.get("node") or "")
        try:
            vmid = int(item["vmid"])
        except (KeyError, TypeError, ValueError):
            continue
        status = str(item.get("status") or "unknown")
        by_node[(node, vmid)] = status
        if by_vmid.get(vmid) not in ALIVE:
            by_vmid[vmid] = status
        if status in ALIVE:
            by_vmid[vmid] = status
    return by_node, by_vmid


def live_status(node: str, vmid: int) -> str:
    result = run(
        ["pvesh", "get", f"/nodes/{node}/qemu/{vmid}/status/current", "--output-format", "json"],
        timeout=15,
    )
    if result.returncode != 0:
        return "unknown"
    text = (result.stdout or "").strip()
    if not text:
        return "unknown"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "unknown"
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        return "unknown"
    status = data.get("status")
    qmpstatus = data.get("qmpstatus")
    if status in ALIVE or qmpstatus in ALIVE:
        return "running"
    if status is None or isinstance(status, (dict, list)):
        return "unknown"
    return str(status) or "unknown"


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
    running = plan_guest(
        vmid=1, node="n", name="web", template=False, status="running", ha_state=None, start_all=False
    )
    assert running.action == "add" and running.ha_state == "started" and running.reason == ""
    stopped = plan_guest(
        vmid=2, node="n", name="db", template=False, status="stopped", ha_state=None, start_all=False
    )
    assert stopped.action == "add" and stopped.ha_state == "stopped"
    repair = plan_guest(
        vmid=3, node="n", name="app", template=False, status="running", ha_state="stopped", start_all=False
    )
    assert repair.action == "set" and repair.ha_state == "started"
    stale = plan_guest(
        vmid=7, node="n", name="late", template=False, status="stopped", ha_state="stopped", start_all=False
    )
    assert stale.reason == "already in HA"
    promoted = promote_if_running(stale, "running")
    assert promoted.action == "set" and promoted.ha_state == "started" and promoted.status == "running"
    assert promote_if_running(stale, "stopped").reason == "already in HA"
    already = plan_guest(
        vmid=4, node="n", name="ok", template=False, status="running", ha_state="started", start_all=False
    )
    assert already.reason == "already in HA"
    start = plan_guest(
        vmid=5, node="n", name="off", template=False, status="stopped", ha_state="stopped", start_all=True
    )
    assert start.action == "set" and start.ha_state == "started"
    assert plan_guest(
        vmid=6, node="n", name="t", template=True, status="stopped", ha_state=None, start_all=True
    ).reason == "template"

    by_node = {("other", 9): "running", ("n", 9): "stopped"}
    by_vmid = {9: "running"}
    assert resolve_status("n", 9, by_node, by_vmid) == "running"

    text = "vm: 100\n\tstate stopped\nvm: 101\nct: 50\n\tstate stopped\n"
    assert ha_states(text) == {100: "stopped", 101: "started"}
    resources = json.dumps(
        {
            "data": [
                {"type": "qemu", "node": "pve-a", "vmid": 10, "status": "running"},
                {"type": "qemu", "node": "pve-b", "vmid": 10, "status": "stopped"},
                {"type": "lxc", "node": "pve-a", "vmid": 20, "status": "stopped"},
            ]
        }
    )
    nodes, vmids = statuses_from_resources(resources)
    assert nodes[("pve-a", 10)] == "running"
    assert vmids[10] == "running"
    assert 20 not in vmids


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        _self_check()
        print("ok")
        raise SystemExit(0)
    raise SystemExit(main())
