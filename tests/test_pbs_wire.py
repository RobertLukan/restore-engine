"""Tests for PBS wire compression helpers."""

from __future__ import annotations

import hashlib
import struct

from pbs_wire import _parse_fidx, parse_job_backup_ref
from progress_parse import ParsedProgress, metrics_mapping_from_tick
from datetime import datetime, timedelta, timezone


def test_parse_job_backup_ref() -> None:
    source_id, pbs_id, epoch = parse_job_backup_ref(
        "main/idrija4tb/root|vm/109/2026-08-07T09:06:06Z"
    )
    assert source_id == "main/idrija4tb/root"
    assert pbs_id == "109"
    assert epoch == 1786093566


def test_parse_fidx_chunk_size_and_digests() -> None:
    chunk = 4 * 1024 * 1024
    zero = hashlib.sha256(b"\x00" * chunk).digest()
    other = hashlib.sha256(b"\x01" * chunk).digest()
    header = bytearray(4096)
    # size @64, chunk_size @72
    struct.pack_into("<Q", header, 64, chunk * 3)
    struct.pack_into("<Q", header, 72, chunk)
    body = bytes(header) + zero + other + other
    chunk_size, digests = _parse_fidx(body)
    assert chunk_size == chunk
    assert digests == [zero, other, other]


def test_metrics_apply_wire_compression_ratio() -> None:
    line = (
        "progress 55% (read 26575110144 bytes, zeroes = 82% (21961375744 bytes), "
        "duration 202 sec)"
    )
    from progress_parse import parse_restore_progress

    parsed = parse_restore_progress([line])
    useful = 26575110144 - 21961375744
    started = (datetime.now(timezone.utc) - timedelta(seconds=202)).isoformat()
    mapping = metrics_mapping_from_tick(
        parsed=parsed,
        restore_started_at=started,
        prev_bytes_done=None,
        prev_speed_bps=None,
        sample_count=3,
        tick_dt_sec=3.0,
        wire_compression_ratio=0.40,
    )
    assert int(mapping["nonzero_bytes_done"]) == useful
    assert int(mapping["network_bytes_done"]) == int(useful * 0.40)
    # Logical ~22.8 MB/s → wire ~9.1 MB/s
    wire = int(mapping["network_speed_bps"])
    assert 8 * 1024 * 1024 < wire < 11 * 1024 * 1024


def test_live_restore_uses_sparsity_not_backup_size() -> None:
    """Live-restore has no zeroes=; PBS archive size ≈ virtual size so density fails."""
    from progress_parse import parse_restore_progress

    line = "restore-drive-scsi0: transferred 22.8 GiB of 45.0 GiB (50.62%) in 3m 34s"
    parsed = parse_restore_progress([line])
    started = (datetime.now(timezone.utc) - timedelta(seconds=214)).isoformat()
    # Without sparsity, backup_size≈virtual would treat nearly all bytes as payload.
    mapping = metrics_mapping_from_tick(
        parsed=parsed,
        restore_started_at=started,
        prev_bytes_done=None,
        prev_speed_bps=None,
        sample_count=5,
        tick_dt_sec=3.0,
        backup_size_bytes=48_318_382_895,
        wire_compression_ratio=0.30,
        disk_sparsity_ratio=1436 / 11520,  # ~12.5% non-zero chunks
    )
    assert int(mapping["nonzero_bytes_done"]) < 4 * 1024**3
    wire = int(mapping["network_speed_bps"])
    # ~ (22.8GiB/214s)*0.125*0.30 ≈ 4 MiB/s → ~32 Mbit/s
    assert 1 * 1024**2 < wire < 10 * 1024**2
