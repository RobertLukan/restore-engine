#!/usr/bin/env python3
"""Destroy QEMU guests matching restore-engine + a plan-run batch tag.

Run as root on any cluster node:

    python3 cleanup.py --batch re-a1b2c3d4e5f6 --dry-run
    python3 cleanup.py --batch a1b2c3d4e5f6
    python3 cleanup.py --batch re-a1b2c3d4e5f6 --yes

Requires ``--batch`` (``re-<id>`` or bare hex). Guests must carry both the
``restore-engine`` ownership tag and the matching ``re-<batch>`` tag (or a
description ``run=`` whose plan-run id derives to that batch).

LXC containers are never deleted. PBS backups are left alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TAG = "restore-engine"
BATCH_PREFIX = "re-"
NODES = Path("/etc/pve/nodes")
HA_RESOURCES = Path("/etc/pve/ha/resources.cfg")
UPID_RE = re.compile(r"UPID:[^\s\"]+")
SECTION_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")
_HEX_RE = re.compile(r"[^0-9a-fA-F]")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.geteuid() != 0:
        print("Run this as root on a Proxmox VE node.", file=sys.stderr)
        return 1
    if not NODES.is_dir():
        print("/etc/pve/nodes is missing. Run this on a Proxmox VE node.", file=sys.stderr)
        return 1
    if shutil.which("pvesh") is None:
        print("pvesh not found. Run this on a Proxmox VE node.", file=sys.stderr)
        return 1

    try:
        batch_id = normalize_batch_id(args.batch)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    batch_tag = f"{BATCH_PREFIX}{batch_id}"

    print(f"Scanning /etc/pve for tags '{TAG}' + '{batch_tag}'...", flush=True)
    qemu, lxc = collect_guests(batch_id=batch_id)
    print_report(qemu, lxc, batch_tag=batch_tag)
    if not qemu:
        return 0
    if args.dry_run:
        print()
        print("Dry run: nothing was deleted.")
        return 0

    print()
    print("The QEMU guests listed above will be destroyed.")
    print("Running guests are hard-stopped first.")
    print(
        "Disks are removed, including unreferenced disks. "
        "Backup, replication, and HA entries for these VMIDs are purged."
    )
    print("PBS backups are not deleted.")
    print("Destroy waits for each PVE delete task (large disks may take minutes).")
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "Refusing to destroy without a terminal. "
                "Re-run with --yes after reviewing the list above.",
                file=sys.stderr,
            )
            return 1
        print()
        try:
            answer = input(
                f"Type the batch id '{batch_tag}' to destroy the {len(qemu)} guest(s) listed above: "
            )
        except EOFError:
            answer = ""
        if normalize_batch_id(answer) != batch_id and answer.strip() != batch_tag:
            print("Aborted. Nothing was deleted.")
            return 1

    failed = 0
    destroyed = 0
    for guest in qemu:
        print()
        print(f"---- VMID {guest.vmid} ({guest.name}) on {guest.node} ----")
        conf = qemu_conf(guest.node, guest.vmid)
        if not conf.is_file():
            print("Already gone.")
            destroyed += 1
            continue
        if ha_managed(guest.vmid):
            print(f"HA manages vm:{guest.vmid}; setting state to stopped.")
            result = run(["ha-manager", "set", f"vm:{guest.vmid}", "--state", "stopped"])
            if result.returncode != 0:
                print(f"Failed to set HA state for vm:{guest.vmid}.", file=sys.stderr)
                if result.stderr.strip():
                    print(result.stderr.strip(), file=sys.stderr)
                failed += 1
                continue
        status = guest_status(guest.node, guest.vmid)
        if status not in {"stopped", "unknown", ""}:
            print(f"Stopping VMID {guest.vmid} on {guest.node} ({status}).")
            if not stop_guest(guest.node, guest.vmid):
                failed += 1
                continue
        print(f"Destroying VMID {guest.vmid} on {guest.node}.")
        if destroy_guest(guest.node, guest.vmid, timeout=args.destroy_timeout):
            print(f"Destroyed VMID {guest.vmid} ({guest.name}) on {guest.node}.")
            destroyed += 1
        else:
            print(
                f"Failed to destroy VMID {guest.vmid} ({guest.name}) on {guest.node}.",
                file=sys.stderr,
            )
            failed += 1

    print()
    print(f"Destroyed {destroyed}, failed {failed}, of {len(qemu)}.")
    return 1 if failed else 0


class Guest:
    def __init__(self, vmid: int, node: str, name: str, status: str = "", flags: str = ""):
        self.vmid = vmid
        self.node = node
        self.name = name
        self.status = status
        self.flags = flags


def normalize_batch_id(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text.startswith(BATCH_PREFIX):
        text = text[len(BATCH_PREFIX) :]
    text = _HEX_RE.sub("", text)
    if not text:
        raise ValueError("batch id is required (e.g. re-a1b2c3d4e5f6)")
    return text


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find QEMU guests tagged restore-engine + a batch tag and destroy them. "
        "The list is always printed before anything is deleted."
    )
    parser.add_argument(
        "--batch",
        required=True,
        help="Batch id or tag (re-<12hex> or bare hex). Required.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print the list and exit",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Delete after printing the list, without a prompt",
    )
    parser.add_argument(
        "--destroy-timeout",
        type=int,
        default=0,
        help="Seconds to wait per delete task (0 = unlimited)",
    )
    return parser.parse_args(argv)


def collect_guests(*, batch_id: str) -> tuple[list[Guest], list[Guest]]:
    qemu: list[Guest] = []
    lxc: list[Guest] = []
    for nodedir in sorted(p for p in NODES.iterdir() if p.is_dir()):
        node = nodedir.name
        qemu_dir = nodedir / "qemu-server"
        if qemu_dir.is_dir():
            for conf in sorted(qemu_dir.glob("*.conf")):
                guest = qemu_from_conf(node, conf, batch_id=batch_id)
                if guest is not None:
                    qemu.append(guest)
        lxc_dir = nodedir / "lxc"
        if lxc_dir.is_dir():
            for conf in sorted(lxc_dir.glob("*.conf")):
                guest = lxc_from_conf(node, conf)
                if guest is not None:
                    lxc.append(guest)
    qemu.sort(key=lambda guest: (guest.node, guest.vmid))
    lxc.sort(key=lambda guest: (guest.node, guest.vmid))
    if qemu:
        print(f"Found {len(qemu)} QEMU guest(s). Reading status...", flush=True)
        statuses = cluster_statuses()
        for guest in qemu:
            guest.status = statuses.get((guest.node, guest.vmid), "unknown")
    return qemu, lxc


def qemu_from_conf(node: str, conf: Path, *, batch_id: str) -> Guest | None:
    if not conf.stem.isdigit():
        return None
    values = conf_values(conf)
    if not tag_in(values.get("tags", ""), TAG):
        return None
    if not matches_batch(values, batch_id):
        return None
    name = values.get("name") or "-"
    flags = "template" if values.get("template") == "1" else ""
    return Guest(int(conf.stem), node, name, flags=flags)


def matches_batch(values: dict[str, str], batch_id: str) -> bool:
    batch_tag = f"{BATCH_PREFIX}{batch_id}"
    if tag_in(values.get("tags", ""), batch_tag):
        return True
    desc = values.get("description") or ""
    if f"run=" not in desc:
        return False
    for part in re.split(r"[\n;]", desc):
        if "run=" not in part:
            continue
        token = part.split("run=", 1)[1].split(";")[0].strip()
        if token and batch_id_from_plan_run_id(token) == batch_id:
            return True
    return False


def batch_id_from_plan_run_id(plan_run_id: str) -> str:
    return _HEX_RE.sub("", str(plan_run_id or ""))[:12].lower()


def lxc_from_conf(node: str, conf: Path) -> Guest | None:
    if not has_tag(conf, TAG):
        return None
    if not conf.stem.isdigit():
        return None
    name = conf_values(conf).get("hostname") or "-"
    return Guest(int(conf.stem), node, name)


def print_report(qemu: list[Guest], lxc: list[Guest], *, batch_tag: str) -> None:
    print(f"QEMU guests tagged '{TAG}' + '{batch_tag}':")
    print()
    if not qemu:
        print("  (none)")
    else:
        print(f"  {'VMID':<8}  {'NODE':<20}  {'STATUS':<10}  {'FLAGS':<10}  NAME")
        for guest in qemu:
            print(
                f"  {guest.vmid:<8}  {guest.node:<20}  {guest.status:<10}  "
                f"{(guest.flags or '-'):<10}  {guest.name}"
            )
    print()
    print(f"{len(qemu)} QEMU guest(s).")
    if not lxc:
        return
    print()
    print(f"LXC containers tagged '{TAG}' are skipped (restore-engine never deletes LXC):")
    print()
    print(f"  {'VMID':<8}  {'NODE':<20}  HOSTNAME")
    for guest in lxc:
        print(f"  {guest.vmid:<8}  {guest.node:<20}  {guest.name}")


def has_tag(path: Path, tag: str = TAG) -> bool:
    """Whole-token match. 'restore-engine-old' does not match."""
    return tag_in(conf_values(path).get("tags", ""), tag)


def tag_in(raw: str, tag: str = TAG) -> bool:
    if not raw:
        return False
    want = tag.lower()
    for token in re.split(r"[;,]", raw):
        if token.strip().lower() == want:
            return True
    return False


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


def qemu_conf(node: str, vmid: int) -> Path:
    return NODES / node / "qemu-server" / f"{vmid}.conf"


def cluster_statuses() -> dict[tuple[str, int], str]:
    """One cluster query for every guest status. Per-VM pvesh calls stall the listing."""
    result = pvesh(
        ["get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pvesh failed").strip()
        print(f"Could not read cluster status ({detail}). Showing status as unknown.", file=sys.stderr)
        return {}
    return statuses_from_resources(result.stdout)


def statuses_from_resources(raw: str) -> dict[tuple[str, int], str]:
    text = raw.strip()
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


def guest_status(node: str, vmid: int) -> str:
    result = pvesh(
        ["get", f"/nodes/{node}/qemu/{vmid}/status/current", "--output-format", "json"],
        timeout=15,
    )
    if result.returncode != 0:
        return "unknown"
    status = json_field(result.stdout, "status")
    return status or "unknown"


def json_field(raw: str, field: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        return ""
    value = data.get(field)
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)


def ha_managed(vmid: int) -> bool:
    if not HA_RESOURCES.is_file():
        return False
    try:
        text = HA_RESOURCES.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    pattern = re.compile(rf"^vm:\s*{vmid}\s*$", re.MULTILINE)
    return pattern.search(text) is not None


def stop_guest(node: str, vmid: int) -> bool:
    pvesh(["create", f"/nodes/{node}/qemu/{vmid}/status/stop", "--timeout", "90"])
    return wait_until_stopped(node, vmid, 95)


def wait_until_stopped(node: str, vmid: int, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    next_note = time.monotonic() + 10
    while True:
        status = guest_status(node, vmid)
        if status in {"stopped", "unknown", ""}:
            return True
        now = time.monotonic()
        if now >= next_note:
            print(f"  still waiting for VMID {vmid} on {node} to stop ({status})...", flush=True)
            next_note = now + 10
        if now >= deadline:
            print(
                f"Timed out waiting for VMID {vmid} on {node} to stop (still {status}).",
                file=sys.stderr,
            )
            return False
        time.sleep(2)


def destroy_guest(node: str, vmid: int, *, timeout: int = 0) -> bool:
    for attempt in range(1, 4):
        result = pvesh(
            [
                "delete",
                f"/nodes/{node}/qemu/{vmid}",
                "--purge",
                "1",
                "--destroy-unreferenced-disks",
                "1",
            ]
        )
        err = result.stderr or ""
        if result.returncode != 0 and any(
            needle in err
            for needle in ("destroy-unreferenced-disks", "unknown parameter", "extra parameter")
        ):
            result = pvesh(["delete", f"/nodes/{node}/qemu/{vmid}", "--purge", "1"])
            err = result.stderr or ""
        if result.returncode != 0:
            if re.search(r"running", err, re.IGNORECASE) and attempt < 3:
                print(f"VMID {vmid} on {node} is still running; stopping again.", file=sys.stderr)
                pvesh(["create", f"/nodes/{node}/qemu/{vmid}/status/stop", "--timeout", "60"])
                time.sleep(2)
                continue
            if err.strip():
                print(err.strip(), file=sys.stderr)
            return False
        upid = extract_upid(result.stdout)
        if not upid:
            if not qemu_conf(node, vmid).exists():
                return True
            # No UPID — still poll until gone (timeout 0 = unlimited).
            return wait_destroyed(node, vmid, "", timeout)
        if wait_destroyed(node, vmid, upid, timeout):
            return True
        status = guest_status(node, vmid)
        if attempt < 3 and status not in {"stopped", "unknown"}:
            print(
                f"Destroy task failed while VMID {vmid} is {status}; stopping and retrying.",
                file=sys.stderr,
            )
            pvesh(["create", f"/nodes/{node}/qemu/{vmid}/status/stop", "--timeout", "60"])
            time.sleep(2)
            continue
        return False
    return False


def wait_destroyed(node: str, vmid: int, upid: str, timeout: int) -> bool:
    conf = qemu_conf(node, vmid)
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    next_note = time.monotonic() + 10
    while True:
        if not conf.exists():
            return True
        now = time.monotonic()
        if now >= next_note:
            print(f"  still destroying VMID {vmid} on {node}...", flush=True)
            next_note = now + 10
        if upid:
            result = pvesh(
                ["get", f"/nodes/{node}/tasks/{upid}/status", "--output-format", "json"]
            )
            state = json_field(result.stdout, "status") if result.returncode == 0 else ""
            exitstatus = json_field(result.stdout, "exitstatus") if result.returncode == 0 else ""
            if state == "stopped":
                if exitstatus == "OK" or (
                    exitstatus and exitstatus.upper().startswith("WARNINGS:")
                ):
                    for _ in range(5):
                        if not conf.exists():
                            return True
                        time.sleep(1)
                    return True
                print(f"Task {upid} failed: {exitstatus or 'unknown error'}", file=sys.stderr)
                return False
        if deadline is not None and time.monotonic() >= deadline:
            print(
                f"Timed out waiting for VMID {vmid} on {node} to be destroyed.",
                file=sys.stderr,
            )
            return False
        time.sleep(2)


def extract_upid(text: str) -> str:
    match = UPID_RE.search(text or "")
    return match.group(0) if match else ""


def pvesh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run(["pvesh", *args], timeout=timeout)


def run(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if not stderr:
            stderr = f"timed out after {timeout}s"
        return subprocess.CompletedProcess(args, 124, stdout, stderr)


def _self_check() -> None:
    """Local checks for tag parsing. Not used on the Proxmox node."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def write(name: str, *lines: str) -> Path:
            path = root / name
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return path

        assert has_tag(write("exact.conf", "name: web", "tags: restore-engine"))
        assert has_tag(write("multi.conf", "tags: lab;restore-engine;drill"))
        assert has_tag(write("comma.conf", "tags: lab,restore-engine"))
        assert has_tag(write("case.conf", "tags: Restore-Engine"))
        assert has_tag(write("space.conf", "tags: foo; restore-engine ; bar"))
        assert not has_tag(write("prefix.conf", "tags: restore-engine-old"))
        assert not has_tag(write("suffix.conf", "tags: my-restore-engine"))
        assert not has_tag(write("num.conf", "tags: restore-engine2"))
        assert not has_tag(write("desc.conf", "description: restore-engine:job=abc", "tags: production"))
        assert not has_tag(write("none.conf", "name: plain"))
        assert conf_values(write("named.conf", "name: db one", "template: 1"))["name"] == "db one"
        assert json_field('{"status":"running"}', "status") == "running"
        wrapped = '{"data":{"status":"stopped","exitstatus":"OK"}}'
        assert json_field(wrapped, "status") == "stopped"
        assert json_field(wrapped, "exitstatus") == "OK"
        assert extract_upid("UPID:pve1:0000A1:001234:64ABC:qmdestroy:120:root@pam:") == (
            "UPID:pve1:0000A1:001234:64ABC:qmdestroy:120:root@pam:"
        )
        resources = json.dumps(
            {
                "data": [
                    {"type": "qemu", "node": "pve-a", "vmid": 10, "status": "running"},
                    {"type": "lxc", "node": "pve-a", "vmid": 20, "status": "stopped"},
                    {"type": "qemu", "node": "pve-b", "vmid": 11, "status": "stopped"},
                ]
            }
        )
        found = statuses_from_resources(resources)
        assert found == {("pve-a", 10): "running", ("pve-b", 11): "stopped"}
        assert normalize_batch_id("re-a1b2c3d4e5f6") == "a1b2c3d4e5f6"
        assert normalize_batch_id("A1B2C3D4E5F6") == "a1b2c3d4e5f6"
        assert matches_batch(
            {"tags": "restore-engine;re-a1b2c3d4e5f6", "description": ""},
            "a1b2c3d4e5f6",
        )
        assert matches_batch(
            {
                "tags": "restore-engine",
                "description": "restore-engine:job=x;run=a1b2c3d4e5f6-0000-0000-0000-000000000000",
            },
            "a1b2c3d4e5f6",
        )
        assert not matches_batch(
            {"tags": "restore-engine;re-ffffffffffff", "description": ""},
            "a1b2c3d4e5f6",
        )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        _self_check()
        print("ok")
        raise SystemExit(0)
    raise SystemExit(main())
