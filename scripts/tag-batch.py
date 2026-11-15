#!/usr/bin/env python3
"""Add a plan-run batch tag to QEMU guests that already carry restore-engine.

Run as root on any cluster node:

    python3 tag-batch.py --batch re-a1b2c3d4e5f6 --dry-run
    python3 tag-batch.py --batch a1b2c3d4e5f6
    python3 tag-batch.py --batch re-a1b2c3d4e5f6 --yes

Only guests with the ownership tag ``restore-engine`` are considered.
Guests that already have the target ``re-<batch>`` tag are skipped.
Guests that already have a *different* ``re-*`` batch tag are skipped
unless ``--force-retag`` is set (other tags are preserved; other ``re-*``
tags are replaced).

Templates and LXC are skipped. Tags are applied with ``pvesh`` on each
guest's owning node so cluster VMs (not only local) are updated. After
tagging, use the Cleanup tab / ``cleanup.py --batch …`` with the same
batch id (dual-tag gate).
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

TAG = "restore-engine"
BATCH_PREFIX = "re-"
NODES = Path("/etc/pve/nodes")
SECTION_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")
_HEX_RE = re.compile(r"[^0-9a-fA-F]")
_BATCH_TAG_RE = re.compile(r"^re-[0-9a-fA-F]+$", re.IGNORECASE)


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
        if args.new_batch:
            batch_id = secrets.token_hex(6)
        else:
            batch_id = normalize_batch_id(args.batch or "")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    batch_tag = f"{BATCH_PREFIX}{batch_id}"

    print(
        f"Scanning /etc/pve for '{TAG}' guests to tag with '{batch_tag}'"
        + (" (force-retag)" if args.force_retag else "")
        + "...",
        flush=True,
    )
    pending, skipped = collect(batch_id=batch_id, force_retag=args.force_retag)
    print_report(pending, skipped, batch_tag=batch_tag)
    if not pending:
        print()
        print(f"Batch id for Cleanup: {batch_tag}")
        return 0
    if args.dry_run:
        print()
        print("Dry run: nothing was changed.")
        print(f"Batch id for Cleanup: {batch_tag}")
        return 0

    print()
    print(f"Will add tag '{batch_tag}' to the {len(pending)} guest(s) listed above.")
    print("Other non-batch tags are preserved. LXC and templates are not touched.")
    if not confirm(args.yes, batch_tag, len(pending)):
        return 1

    failed = 0
    changed = 0
    for guest in pending:
        print(f"---- VMID {guest.vmid} ({guest.name}) on {guest.node} ----", flush=True)
        if apply_tags(guest.node, guest.vmid, guest.new_tags):
            print(f"Tagged VMID {guest.vmid}: {';'.join(guest.new_tags)}")
            changed += 1
        else:
            print(f"Failed to tag VMID {guest.vmid}.", file=sys.stderr)
            failed += 1

    print()
    print(f"Tagged {changed}, failed {failed}, of {len(pending)}.")
    print(f"Batch id for Cleanup: {batch_tag}")
    return 1 if failed else 0


class Guest:
    def __init__(
        self,
        vmid: int,
        node: str,
        name: str,
        tags: list[str],
        new_tags: list[str] | None = None,
        reason: str = "",
        action: str = "",
    ):
        self.vmid = vmid
        self.node = node
        self.name = name
        self.tags = tags
        self.new_tags = new_tags or []
        self.reason = reason
        self.action = action


def normalize_batch_id(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text.startswith(BATCH_PREFIX):
        text = text[len(BATCH_PREFIX) :]
    text = _HEX_RE.sub("", text)
    if not text:
        raise ValueError("batch id is required (e.g. re-a1b2c3d4e5f6) or pass --new-batch")
    return text


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Add re-<batch> to QEMU guests that already have the restore-engine tag. "
            "The list is always printed before anything is changed."
        )
    )
    parser.add_argument(
        "--batch",
        help="Batch id or tag (re-<12hex> or bare hex).",
    )
    parser.add_argument(
        "--new-batch",
        action="store_true",
        help="Generate a random 12-hex batch id (prints it for Cleanup).",
    )
    parser.add_argument(
        "--force-retag",
        action="store_true",
        help="Replace existing re-* batch tags with the target batch (keeps other tags).",
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="Print the list and exit")
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Apply after printing the list, without a prompt",
    )
    args = parser.parse_args(argv)
    if not args.new_batch and not args.batch:
        parser.error("provide --batch or --new-batch")
    if args.new_batch and args.batch:
        parser.error("use either --batch or --new-batch, not both")
    return args


def confirm(assume_yes: bool, batch_tag: str, count: int) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Refusing to change tags without a terminal. "
            "Re-run with --yes after reviewing the list above.",
            file=sys.stderr,
        )
        return False
    print()
    try:
        answer = input(f"Type the batch id '{batch_tag}' to tag the {count} guest(s): ")
    except EOFError:
        answer = ""
    try:
        if normalize_batch_id(answer) == normalize_batch_id(batch_tag):
            return True
    except ValueError:
        pass
    if answer.strip() == batch_tag:
        return True
    print("Aborted. Nothing was changed.")
    return False


def collect(*, batch_id: str, force_retag: bool) -> tuple[list[Guest], list[Guest]]:
    want = f"{BATCH_PREFIX}{batch_id}"
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
            tags = parse_tag_list(values.get("tags", ""))
            name = values.get("name") or "-"
            vmid = int(conf.stem)
            guest = plan_guest(
                vmid=vmid,
                node=node,
                name=name,
                tags=tags,
                template=values.get("template") == "1",
                want_batch=want,
                force_retag=force_retag,
            )
            if guest.reason:
                skipped.append(guest)
            else:
                pending.append(guest)
    pending.sort(key=lambda g: (g.node, g.vmid))
    skipped.sort(key=lambda g: (g.reason, g.node, g.vmid))
    return pending, skipped


def plan_guest(
    *,
    vmid: int,
    node: str,
    name: str,
    tags: list[str],
    template: bool,
    want_batch: str,
    force_retag: bool,
) -> Guest:
    if template:
        return Guest(vmid, node, name, tags, reason="template")
    tag_lower = {t.lower() for t in tags}
    if TAG.lower() not in tag_lower:
        return Guest(vmid, node, name, tags, reason="no restore-engine tag")
    if want_batch.lower() in tag_lower:
        return Guest(vmid, node, name, tags, reason="already has target batch")
    other_batches = [t for t in tags if _BATCH_TAG_RE.match(t)]
    if other_batches and not force_retag:
        return Guest(
            vmid,
            node,
            name,
            tags,
            reason=f"other batch ({','.join(other_batches)}; use --force-retag)",
        )
    new_tags = merge_tags(tags, want_batch, force_retag=force_retag)
    action = "retag" if other_batches else "add"
    return Guest(vmid, node, name, tags, new_tags=new_tags, action=action)


def merge_tags(existing: list[str], want_batch: str, *, force_retag: bool) -> list[str]:
    """Keep non-batch tags; ensure restore-engine + target batch; drop other re-* when force."""
    out: list[str] = []
    seen: set[str] = set()

    def add(tag: str) -> None:
        key = tag.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(tag)

    for tag in existing:
        if _BATCH_TAG_RE.match(tag):
            if force_retag:
                continue
            add(tag)
            continue
        add(tag)
    add(TAG)
    add(want_batch)
    return out


def parse_tag_list(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[;,]", raw or ""):
        tag = token.strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def print_report(pending: list[Guest], skipped: list[Guest], *, batch_tag: str) -> None:
    print(f"QEMU guests to tag with '{batch_tag}' (already have '{TAG}'):")
    print()
    if not pending:
        print("  (none)")
    else:
        print(f"  {'VMID':<8}  {'NODE':<16}  {'ACTION':<8}  {'TAGS':<40}  NAME")
        for guest in pending:
            print(
                f"  {guest.vmid:<8}  {guest.node:<16}  {guest.action:<8}  "
                f"{';'.join(guest.tags):<40}  {guest.name}"
            )
    print()
    print(f"{len(pending)} guest(s) will be tagged.")
    interesting = [g for g in skipped if g.reason != "no restore-engine tag"]
    if not interesting:
        return
    print()
    print("Skipped (managed / already tagged / conflict / template):")
    print()
    print(f"  {'VMID':<8}  {'NODE':<16}  {'REASON':<44}  NAME")
    for guest in interesting:
        print(f"  {guest.vmid:<8}  {guest.node:<16}  {guest.reason:<44}  {guest.name}")


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


def apply_tags(node: str, vmid: int, tags: list[str]) -> bool:
    """Set tags via cluster API on the guest's owning node (qm only works locally)."""
    joined = ";".join(tags)
    result = pvesh(
        ["set", f"/nodes/{node}/qemu/{vmid}/config", "--tags", joined],
        timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pvesh failed").strip()
        if detail:
            print(detail, file=sys.stderr)
        return False
    return True


def pvesh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run(["pvesh", *args], timeout=timeout)


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if not stderr:
            stderr = f"timed out after {timeout}s"
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)


def _self_test() -> None:
    assert normalize_batch_id("re-A1b2C3d4e5f6") == "a1b2c3d4e5f6"
    assert normalize_batch_id("a1b2c3d4e5f6") == "a1b2c3d4e5f6"
    assert parse_tag_list("lab; restore-engine ;re-abc") == ["lab", "restore-engine", "re-abc"]
    g = plan_guest(
        vmid=100,
        node="pve",
        name="x",
        tags=["restore-engine"],
        template=False,
        want_batch="re-aaaaaaaaaaaa",
        force_retag=False,
    )
    assert g.action == "add" and "re-aaaaaaaaaaaa" in g.new_tags
    g2 = plan_guest(
        vmid=101,
        node="pve",
        name="y",
        tags=["restore-engine", "re-bbbbbbbbbbbb"],
        template=False,
        want_batch="re-aaaaaaaaaaaa",
        force_retag=False,
    )
    assert g2.reason.startswith("other batch")
    g3 = plan_guest(
        vmid=102,
        node="pve",
        name="z",
        tags=["lab", "restore-engine", "re-bbbbbbbbbbbb"],
        template=False,
        want_batch="re-aaaaaaaaaaaa",
        force_retag=True,
    )
    assert g3.action == "retag"
    assert "lab" in g3.new_tags
    assert "re-aaaaaaaaaaaa" in g3.new_tags
    assert "re-bbbbbbbbbbbb" not in g3.new_tags
    g4 = plan_guest(
        vmid=103,
        node="pve",
        name="prod",
        tags=["production"],
        template=False,
        want_batch="re-aaaaaaaaaaaa",
        force_retag=False,
    )
    assert g4.reason == "no restore-engine tag"
    print("self-test ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        _self_test()
        raise SystemExit(0)
    raise SystemExit(main())
