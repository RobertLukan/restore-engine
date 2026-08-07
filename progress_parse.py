"""Parse Proxmox restore task logs and estimate throughput / ETA."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# PBS restore logs look like:
#   progress 55% (read 26575110144 bytes, zeroes = 82% (21961375744 bytes), duration 202 sec)
# "read" includes sparse zeros; wire payload ≈ read − zeroes.
#
# Live-restore (QEMU block-job) logs look like:
#   restore-drive-scsi0: transferred 24.2 GiB of 45.0 GiB (53.86%) in 4m 14s
#   restore-drive-scsi0: transferred 348.3 MiB of 45.0 GiB (0.76%) in 10s
_PROGRESS_PCT_RE = re.compile(
    r"\bprogress\s*[:=]?\s*(\d{1,3}(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_ZEROES_RE = re.compile(
    r"zeroes?\s*=\s*(\d{1,3}(?:\.\d+)?)\s*%\s*\((\d+)\s*bytes\)",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"\bduration\s+(\d+(?:\.\d+)?)\s*sec",
    re.IGNORECASE,
)
_BYTES_PAIR_RE = re.compile(
    r"(?:read|transferred|written|restored|downloaded)\s+(\d+)\s*(?:/\s*(\d+))?\s*bytes",
    re.IGNORECASE,
)
_MIB_PAIR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:MiB|MB)\s*(?:/\s*(\d+(?:\.\d+)?)\s*(?:MiB|MB))?",
    re.IGNORECASE,
)
_LIVE_XFER_RE = re.compile(
    r"transferred\s+(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\s+of\s+"
    r"(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\s*\((\d+(?:\.\d+)?)%\)"
    r"(?:\s+in\s+((?:\d+\s*h\s*)?(?:\d+\s*m\s*)?(?:\d+\s*s)?|\d+\s*s))?"
    r"(?:\s*\((\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|MB|KB)/s\))?",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|MB)/s",
    re.IGNORECASE,
)
_SIZE_UNIT_BYTES = {
    "b": 1,
    "kib": 1024,
    "kb": 1000,
    "mib": 1024**2,
    "mb": 1024**2,  # QEMU / PVE progress uses binary-ish MB≈MiB
    "gib": 1024**3,
    "gb": 1024**3,
    "tib": 1024**4,
    "tb": 1024**4,
}
_NET_MIB_RE = re.compile(
    r"(?:download(?:ed)?|network|recv(?:eived)?|fetched)\s+(\d+(?:\.\d+)?)\s*(?:MiB|MB)",
    re.IGNORECASE,
)
_NET_BYTES_RE = re.compile(
    r"(?:download(?:ed)?|network|recv(?:eived)?|fetched)\s+(\d+)\s*bytes",
    re.IGNORECASE,
)
_COMPLETE_RE = re.compile(
    r"restore\s+image\s+complete\s*\([^)]*bytes\s*=\s*(\d+)[^)]*"
    r"(?:duration\s*=\s*(\d+(?:\.\d+)?)[^)]*)?"
    r"(?:speed\s*=\s*(\d+(?:\.\d+)?)\s*(?:MiB|MB)/s)?",
    re.IGNORECASE,
)


@dataclass
class ParsedProgress:
    percent: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    speed_bps: float | None = None
    network_bytes_done: int | None = None
    zeroes_bytes: int | None = None
    zeroes_percent: float | None = None
    duration_sec: float | None = None
    status_text: str = ""


def _mib_to_bytes(value: float) -> int:
    return int(value * 1024 * 1024)


def _size_to_bytes(value: float, unit: str) -> int:
    mult = _SIZE_UNIT_BYTES.get(unit.strip().lower())
    if mult is None:
        raise ValueError(f"unknown size unit: {unit}")
    return int(float(value) * mult)


def parse_duration_token(text: str) -> float | None:
    """Parse QEMU-style durations: ``10s``, ``1m``, ``4m 14s``, ``1h 2m 3s``."""
    raw = (text or "").strip()
    if not raw:
        return None
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+)\s*([hms])", raw, flags=re.IGNORECASE):
        matched = True
        n = int(amount)
        u = unit.lower()
        if u == "h":
            total += n * 3600
        elif u == "m":
            total += n * 60
        else:
            total += n
    return total if matched else None


def estimate_network_bytes_done(
    *,
    backup_size_bytes: int | None,
    percent: float | None,
    bytes_done: int | None,
    bytes_total: int | None,
) -> int | None:
    """Fallback when logs lack zeroes: scale PBS backup size by restore %."""
    if not backup_size_bytes or backup_size_bytes <= 0:
        return None
    if percent is not None and 0 <= percent <= 100:
        return int(backup_size_bytes * (percent / 100.0))
    if bytes_done is not None and bytes_total and bytes_total > 0:
        return int(backup_size_bytes * min(1.0, max(0.0, bytes_done / bytes_total)))
    return None


def parse_restore_progress(log_lines: list[str]) -> ParsedProgress:
    """Extract the best progress signal from recent task log lines."""
    result = ParsedProgress()
    for raw in log_lines:
        line = str(raw or "").strip()
        if not line:
            continue
        text = line
        result.status_text = text[:240]
        live_matched = False

        # Live-restore block-job lines (must run before generic MiB/bytes parsers).
        for match in _LIVE_XFER_RE.finditer(text):
            live_matched = True
            try:
                result.bytes_done = _size_to_bytes(float(match.group(1)), match.group(2))
                result.bytes_total = _size_to_bytes(float(match.group(3)), match.group(4))
                pct = float(match.group(5))
            except ValueError:
                continue
            if 0 <= pct <= 100:
                result.percent = pct
            if match.group(6):
                dur = parse_duration_token(match.group(6))
                if dur is not None:
                    result.duration_sec = dur
            if match.group(7) and match.group(8):
                try:
                    result.speed_bps = float(_size_to_bytes(float(match.group(7)), match.group(8)))
                except ValueError:
                    pass

        # Prefer explicit "progress N%" — never treat "zeroes = N%" as progress.
        for match in _PROGRESS_PCT_RE.finditer(text):
            try:
                pct = float(match.group(1))
            except ValueError:
                continue
            if 0 <= pct <= 100:
                result.percent = pct

        for match in _ZEROES_RE.finditer(text):
            try:
                result.zeroes_percent = float(match.group(1))
                result.zeroes_bytes = int(match.group(2))
            except ValueError:
                continue

        for match in _DURATION_RE.finditer(text):
            try:
                result.duration_sec = float(match.group(1))
            except ValueError:
                continue

        if not live_matched:
            for match in _BYTES_PAIR_RE.finditer(text):
                try:
                    done = int(match.group(1))
                except ValueError:
                    continue
                result.bytes_done = done
                if match.group(2):
                    try:
                        result.bytes_total = int(match.group(2))
                    except ValueError:
                        pass

            for match in _MIB_PAIR_RE.finditer(text):
                # Skip pairs that are only inside "zeroes = …" contexts if we already
                # have a read counter; MiB lines without "read" are still useful.
                if "zeroes" in text.lower() and _BYTES_PAIR_RE.search(text):
                    continue
                try:
                    done = _mib_to_bytes(float(match.group(1)))
                except ValueError:
                    continue
                result.bytes_done = done
                if match.group(2):
                    try:
                        result.bytes_total = _mib_to_bytes(float(match.group(2)))
                    except ValueError:
                        pass

        for match in _COMPLETE_RE.finditer(text):
            try:
                result.bytes_done = int(match.group(1))
            except (TypeError, ValueError):
                pass
            if match.group(2):
                try:
                    result.duration_sec = float(match.group(2))
                except ValueError:
                    pass
            if match.group(3):
                try:
                    result.speed_bps = float(match.group(3)) * 1024 * 1024
                except ValueError:
                    pass

        if not live_matched:
            for match in _RATE_RE.finditer(text):
                # Gross rate from logs; may include sparse jumps on non-live restores.
                try:
                    result.speed_bps = float(_size_to_bytes(float(match.group(1)), match.group(2)))
                except ValueError:
                    continue

        for match in _NET_BYTES_RE.finditer(text):
            try:
                result.network_bytes_done = int(match.group(1))
            except ValueError:
                continue
        for match in _NET_MIB_RE.finditer(text):
            try:
                result.network_bytes_done = _mib_to_bytes(float(match.group(1)))
            except ValueError:
                continue

        # Wire payload ≈ non-zero bytes processed (PBS sparse / --skip-zero).
        if (
            result.network_bytes_done is None
            and result.bytes_done is not None
            and result.zeroes_bytes is not None
        ):
            result.network_bytes_done = max(0, int(result.bytes_done) - int(result.zeroes_bytes))

        # Prefer wall-clock averages from the log duration when present.
        if result.duration_sec and result.duration_sec > 0:
            if result.bytes_done is not None and (result.speed_bps is None or result.speed_bps <= 0):
                result.speed_bps = float(result.bytes_done) / result.duration_sec

        if result.percent is None and result.bytes_done is not None and result.bytes_total:
            result.percent = min(100.0, 100.0 * result.bytes_done / result.bytes_total)

    return result


def parse_iso(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def compute_eta_sec(
    *,
    progress: float | None,
    bytes_done: int | None,
    bytes_total: int | None,
    speed_bps: float | None,
    restore_started_at: str,
    now: datetime | None = None,
    min_elapsed_sec: float = 15.0,
    min_samples_hint: int = 2,
    sample_count: int = 0,
) -> float | None:
    """Return ETA seconds, or None if estimate is too early / unavailable."""
    clock = now or datetime.now(timezone.utc)
    started = parse_iso(restore_started_at)
    elapsed = (clock - started).total_seconds() if started else 0.0
    if elapsed < min_elapsed_sec and sample_count < min_samples_hint:
        return None

    if bytes_total and bytes_done is not None and speed_bps and speed_bps > 0:
        remaining = max(0, bytes_total - bytes_done)
        return remaining / speed_bps

    if progress is not None and 0 < progress < 100 and elapsed > 0:
        return elapsed * (100.0 - progress) / progress

    return None


def ema(previous: float | None, sample: float, alpha: float = 0.35) -> float:
    if previous is None or previous <= 0:
        return sample
    return alpha * sample + (1.0 - alpha) * previous


def estimate_network_speed_bps(
    *,
    gross_speed_bps: float | None,
    backup_size_bytes: int | None,
    bytes_total: int | None,
    network_bytes_done: int | None,
    restore_started_at: str,
    now: datetime | None = None,
    log_network_bytes: int | None = None,
    prev_log_network_bytes: int | None = None,
    tick_dt_sec: float = 0.0,
    prev_network_speed_bps: float | None = None,
    duration_sec: float | None = None,
    from_zeroes: bool = False,
    disk_sparsity_ratio: float | None = None,
) -> float | None:
    """Estimate realistic wire throughput (bytes/s).

    Prefer ``(read − zeroes) / duration`` from PBS restore logs. For live-restore
    (no zeroes), scale gross virtual rate by sampled fidx non-zero fraction.
    Density scaling is only a fallback when zeroes/sparsity are absent.
    """
    # 1) Non-zero bytes / log duration (best for sparse images).
    if (
        from_zeroes
        and network_bytes_done is not None
        and duration_sec
        and duration_sec > 0
        and network_bytes_done >= 0
    ):
        avg = float(network_bytes_done) / duration_sec
        if avg >= 0:
            return ema(prev_network_speed_bps, avg) if avg > 0 else 0.0

    # 2) Explicit download counters / non-zero byte deltas between ticks.
    if (
        log_network_bytes is not None
        and prev_log_network_bytes is not None
        and tick_dt_sec > 0
        and log_network_bytes >= prev_log_network_bytes
    ):
        instant = (log_network_bytes - prev_log_network_bytes) / tick_dt_sec
        if instant > 0:
            return ema(prev_network_speed_bps, instant)

    # 3) Live-restore / no zeroes: scale virtual gross by fidx sparsity.
    if (
        not from_zeroes
        and disk_sparsity_ratio is not None
        and gross_speed_bps
        and gross_speed_bps > 0
    ):
        sparse = max(0.0, min(1.0, float(disk_sparsity_ratio)))
        scaled = gross_speed_bps * sparse
        if scaled >= 0:
            return ema(prev_network_speed_bps, scaled) if scaled > 0 else 0.0

    # 4) Density-scale gross speed: net ≈ gross × (PBS size / virtual disk size).
    if (
        not from_zeroes
        and gross_speed_bps
        and gross_speed_bps > 0
        and backup_size_bytes
        and backup_size_bytes > 0
        and bytes_total
        and bytes_total > 0
    ):
        density = min(1.0, backup_size_bytes / bytes_total)
        scaled = gross_speed_bps * density
        if scaled > 0:
            return ema(prev_network_speed_bps, scaled)

    # 5) Session average from estimated payload transferred.
    if network_bytes_done and network_bytes_done > 0:
        started = parse_iso(restore_started_at)
        clock = now or datetime.now(timezone.utc)
        if started:
            elapsed = (clock - started).total_seconds()
            if elapsed >= 8:
                return network_bytes_done / elapsed

    return prev_network_speed_bps if prev_network_speed_bps and prev_network_speed_bps > 0 else None


def metrics_mapping_from_tick(
    *,
    parsed: ParsedProgress,
    restore_started_at: str,
    prev_bytes_done: int | None,
    prev_speed_bps: float | None,
    sample_count: int,
    tick_dt_sec: float,
    backup_size_bytes: int = 0,
    prev_network_bytes: int | None = None,
    prev_network_speed_bps: float | None = None,
    wire_compression_ratio: float | None = None,
    disk_sparsity_ratio: float | None = None,
) -> dict[str, str]:
    """Build Redis hash fields for one progress tick."""
    out: dict[str, str] = {}
    if parsed.status_text:
        out["pve_status_text"] = parsed.status_text[:240]

    percent = parsed.percent
    bytes_done = parsed.bytes_done
    bytes_total = parsed.bytes_total
    speed = parsed.speed_bps
    from_zeroes = parsed.zeroes_bytes is not None and parsed.bytes_done is not None

    if parsed.zeroes_bytes is not None:
        out["zeroes_bytes"] = str(int(parsed.zeroes_bytes))
    if parsed.zeroes_percent is not None:
        out["zeroes_percent"] = str(parsed.zeroes_percent)

    if bytes_done is not None:
        out["bytes_done"] = str(int(bytes_done))
        if prev_bytes_done is not None and tick_dt_sec > 0 and bytes_done >= prev_bytes_done:
            instant = (bytes_done - prev_bytes_done) / tick_dt_sec
            # Live-restore logs ~1 line/sec; a backlog poll can span many seconds of
            # progress in one tick and produce absurd instant rates — ignore those.
            max_plausible = 2 * 1024**3  # 2 GiB/s ceiling
            if parsed.speed_bps and parsed.speed_bps > 0:
                max_plausible = max(max_plausible, float(parsed.speed_bps) * 4)
            if 0 < instant <= max_plausible:
                speed = ema(prev_speed_bps, instant)
    if (
        (speed is None or speed <= 0)
        and parsed.duration_sec
        and parsed.duration_sec > 0
        and bytes_done is not None
        and bytes_done > 0
    ):
        speed = float(bytes_done) / float(parsed.duration_sec)
    if bytes_total is not None:
        out["bytes_total"] = str(int(bytes_total))
    if speed is not None and speed > 0:
        out["speed_bps"] = str(int(speed))
    elif prev_speed_bps and prev_speed_bps > 0:
        speed = prev_speed_bps
        out["speed_bps"] = str(int(speed))

    if percent is not None:
        out["progress"] = str(int(max(0, min(100, round(percent)))))

    # Logical non-zero (read − zeroes), then scale by sampled PBS compression for wire.
    logical_done = parsed.network_bytes_done
    sparsity = None
    if disk_sparsity_ratio is not None and disk_sparsity_ratio >= 0:
        sparsity = max(0.0, min(1.0, float(disk_sparsity_ratio)))

    if logical_done is None and not from_zeroes:
        # Live-restore has no zeroes= lines — estimate from fidx non-zero fraction.
        if sparsity is not None and bytes_done is not None:
            logical_done = int(bytes_done * sparsity)
        else:
            logical_done = estimate_network_bytes_done(
                backup_size_bytes=backup_size_bytes or None,
                percent=percent,
                bytes_done=bytes_done if bytes_done is not None else prev_bytes_done,
                bytes_total=bytes_total,
            )

    ratio = None
    if wire_compression_ratio is not None and wire_compression_ratio >= 0:
        ratio = max(0.0, min(1.0, float(wire_compression_ratio)))

    net_done = logical_done
    if net_done is not None and ratio is not None:
        net_done = int(net_done * ratio)
    if net_done is not None:
        out["network_bytes_done"] = str(int(net_done))
    if logical_done is not None:
        out["nonzero_bytes_done"] = str(int(logical_done))

    logical_speed = estimate_network_speed_bps(
        gross_speed_bps=speed,
        backup_size_bytes=backup_size_bytes or None,
        bytes_total=bytes_total,
        network_bytes_done=logical_done,
        restore_started_at=restore_started_at,
        log_network_bytes=parsed.network_bytes_done,
        prev_log_network_bytes=prev_network_bytes if parsed.network_bytes_done is not None else None,
        tick_dt_sec=tick_dt_sec,
        prev_network_speed_bps=None if ratio is not None else prev_network_speed_bps,
        duration_sec=parsed.duration_sec,
        from_zeroes=from_zeroes,
        disk_sparsity_ratio=sparsity,
    )
    if logical_speed is not None and logical_speed >= 0:
        out["nonzero_speed_bps"] = str(int(logical_speed))

    net_speed = logical_speed
    if net_speed is not None and ratio is not None:
        net_speed = float(net_speed) * ratio
        if prev_network_speed_bps and prev_network_speed_bps > 0 and net_speed > 0:
            net_speed = ema(prev_network_speed_bps, net_speed)

    if net_speed is not None and net_speed >= 0:
        out["network_speed_bps"] = str(int(net_speed))

    eta = compute_eta_sec(
        progress=percent,
        bytes_done=bytes_done if bytes_done is not None else prev_bytes_done,
        bytes_total=bytes_total,
        speed_bps=speed,
        restore_started_at=restore_started_at,
        sample_count=sample_count,
    )
    if eta is not None and eta >= 0:
        out["eta_sec"] = str(int(eta))
    return out


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
