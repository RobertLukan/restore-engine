"""Tests for restore progress parsing and ETA helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from progress_parse import compute_eta_sec, parse_restore_progress


def test_parse_percent_and_bytes() -> None:
    parsed = parse_restore_progress(
        [
            "starting restore",
            "progress 42% (read 419430400 / 1000000000 bytes)",
        ]
    )
    assert parsed.percent == 42.0
    assert parsed.bytes_done == 419430400
    assert parsed.bytes_total == 1000000000


def test_parse_mib_rate() -> None:
    parsed = parse_restore_progress(["transferred 128.5 MiB / 512 MiB (55.0 MiB/s)"])
    assert parsed.bytes_done is not None
    assert parsed.bytes_total is not None
    assert parsed.speed_bps is not None
    assert parsed.speed_bps > 50 * 1024 * 1024


def test_eta_from_bytes() -> None:
    started = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    eta = compute_eta_sec(
        progress=50.0,
        bytes_done=500,
        bytes_total=1000,
        speed_bps=50,
        restore_started_at=started,
        sample_count=3,
    )
    assert eta == 10.0


def test_eta_from_percent() -> None:
    started = (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat()
    eta = compute_eta_sec(
        progress=25.0,
        bytes_done=None,
        bytes_total=None,
        speed_bps=None,
        restore_started_at=started,
        sample_count=5,
        now=datetime.now(timezone.utc),
    )
    assert eta is not None
    assert eta > 100  # 40 * 75/25 = 120


def test_eta_too_early() -> None:
    started = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    eta = compute_eta_sec(
        progress=10.0,
        bytes_done=None,
        bytes_total=None,
        speed_bps=None,
        restore_started_at=started,
        sample_count=1,
        min_elapsed_sec=15,
    )
    assert eta is None


def test_parse_pbs_progress_with_zeroes() -> None:
    """Real PBS restore line: progress must not pick up zeroes=N%."""
    from progress_parse import metrics_mapping_from_tick

    line = (
        "progress 55% (read 26575110144 bytes, zeroes = 82% (21961375744 bytes), "
        "duration 202 sec)"
    )
    parsed = parse_restore_progress([line])
    assert parsed.percent == 55.0
    assert parsed.bytes_done == 26575110144
    assert parsed.zeroes_bytes == 21961375744
    assert parsed.zeroes_percent == 82.0
    assert parsed.duration_sec == 202.0
    useful = 26575110144 - 21961375744
    assert parsed.network_bytes_done == useful
    mapping = metrics_mapping_from_tick(
        parsed=parsed,
        restore_started_at=(datetime.now(timezone.utc) - timedelta(seconds=202)).isoformat(),
        prev_bytes_done=None,
        prev_speed_bps=None,
        sample_count=3,
        tick_dt_sec=3.0,
        backup_size_bytes=48_000_000_000,
    )
    assert mapping["progress"] == "55"
    assert int(mapping["network_bytes_done"]) == useful
    # ~22.8 MB/s logical non-zero when no compression ratio applied.
    net = int(mapping["network_speed_bps"])
    assert 20 * 1024 * 1024 < net < 26 * 1024 * 1024


def test_all_zeroes_image_network_near_zero() -> None:
    line = "progress 72% (read 775946240 bytes, zeroes = 100% (775946240 bytes), duration 5 sec)"
    parsed = parse_restore_progress([line])
    assert parsed.network_bytes_done == 0
    from progress_parse import metrics_mapping_from_tick

    mapping = metrics_mapping_from_tick(
        parsed=parsed,
        restore_started_at=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        prev_bytes_done=None,
        prev_speed_bps=None,
        sample_count=2,
        tick_dt_sec=1.0,
    )
    assert int(mapping["network_speed_bps"]) == 0


def test_estimate_network_from_backup_size() -> None:
    from progress_parse import estimate_network_bytes_done, estimate_network_speed_bps, metrics_mapping_from_tick, ParsedProgress

    assert estimate_network_bytes_done(
        backup_size_bytes=1000, percent=25.0, bytes_done=None, bytes_total=None
    ) == 250

    # Density: 40 MiB/s gross, backup is 1/4 of virtual → ~10 MiB/s network.
    gross = 40 * 1024 * 1024
    net = estimate_network_speed_bps(
        gross_speed_bps=gross,
        backup_size_bytes=20 * 1024 * 1024 * 1024,
        bytes_total=80 * 1024 * 1024 * 1024,
        network_bytes_done=None,
        restore_started_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    )
    assert net is not None
    assert abs(net - 10 * 1024 * 1024) < 1024  # ~10 MiB/s

    started = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    # 40 MiB/s for 2s → 80 MiB virtual progress; density 20/80 GiB = 0.25 → ~10 MiB/s net.
    virtual_total = 80 * 1024**3
    backup = 20 * 1024**3
    delta = 80 * 1024 * 1024
    mapping = metrics_mapping_from_tick(
        parsed=ParsedProgress(
            percent=50.0,
            bytes_done=delta,
            bytes_total=virtual_total,
            speed_bps=gross,
        ),
        restore_started_at=started,
        prev_bytes_done=0,
        prev_speed_bps=gross,
        sample_count=3,
        tick_dt_sec=2.0,
        backup_size_bytes=backup,
        prev_network_bytes=None,
        prev_network_speed_bps=None,
    )
    assert "network_speed_bps" in mapping
    # Density-scaled ~10 MiB/s, not 40.
    assert int(mapping["network_speed_bps"]) < 15 * 1024 * 1024
    assert int(mapping["network_speed_bps"]) > 5 * 1024 * 1024


def test_parse_live_restore_gib() -> None:
    """Live-restore QEMU block-job lines use GiB + (percent%), not PBS progress/zeroes."""
    from progress_parse import metrics_mapping_from_tick

    line = "restore-drive-scsi0: transferred 24.2 GiB of 45.0 GiB (53.86%) in 4m 14s"
    parsed = parse_restore_progress([line])
    assert parsed.percent == 53.86
    assert parsed.bytes_done is not None
    assert abs(parsed.bytes_done - int(24.2 * 1024**3)) < 1024**2
    assert parsed.bytes_total is not None
    assert abs(parsed.bytes_total - int(45.0 * 1024**3)) < 1024**2
    assert parsed.duration_sec == 4 * 60 + 14
    assert parsed.speed_bps is not None
    # ~102 MiB/s average from 24.2 GiB / 254s
    assert 90 * 1024**2 < parsed.speed_bps < 120 * 1024**2

    mapping = metrics_mapping_from_tick(
        parsed=parsed,
        restore_started_at=(datetime.now(timezone.utc) - timedelta(seconds=254)).isoformat(),
        prev_bytes_done=None,
        prev_speed_bps=None,
        sample_count=5,
        tick_dt_sec=3.0,
        backup_size_bytes=48_000_000_000,
        wire_compression_ratio=0.31,
        disk_sparsity_ratio=0.125,
    )
    assert mapping["progress"] == "54"
    assert int(mapping["bytes_done"]) == parsed.bytes_done
    assert int(mapping["bytes_total"]) == parsed.bytes_total
    # Non-zero ≈ 24.2 GiB × 0.125; wire ≈ that × 0.31.
    nonzero = int(mapping["nonzero_bytes_done"])
    assert abs(nonzero - int(24.2 * 1024**3 * 0.125)) < 1024**2
    wire = int(mapping["network_speed_bps"])
    # Gross ~102 MiB/s × 0.125 × 0.31 ≈ 4 MiB/s
    assert 2 * 1024**2 < wire < 8 * 1024**2


def test_parse_live_restore_mib_of_gib_then_gib() -> None:
    """Regression: MiB-of-GiB lines must not freeze bytes_done after crossing into GiB."""
    lines = [
        "restore-drive-scsi0: transferred 348.3 MiB of 45.0 GiB (0.76%) in 10s",
        "restore-drive-scsi0: transferred 24.2 GiB of 45.0 GiB (53.86%) in 4m 14s",
    ]
    parsed = parse_restore_progress(lines)
    assert parsed.percent == 53.86
    assert parsed.bytes_done is not None
    assert parsed.bytes_done > 20 * 1024**3
    assert parsed.bytes_total is not None
    assert abs(parsed.bytes_total - int(45.0 * 1024**3)) < 1024**2


def test_parse_live_restore_zero_bytes() -> None:
    parsed = parse_restore_progress(
        ["restore-drive-scsi0: transferred 0.0 B of 45.0 GiB (0.00%) in 0s"]
    )
    assert parsed.percent == 0.0
    assert parsed.bytes_done == 0
    assert parsed.bytes_total is not None
    assert parsed.bytes_total > 40 * 1024**3
