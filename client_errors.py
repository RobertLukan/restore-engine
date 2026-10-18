"""Client-safe error text and TLS defaults for HTTP/API responses."""

from __future__ import annotations

import errno
import ssl


def tls_client_context(*, verify: bool = True) -> ssl.SSLContext:
    """TLS client context with TLS 1.2+ only."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SAFE_ERRNO_HINTS = {
    errno.ECONNREFUSED: "connection refused",
    errno.ENETUNREACH: "network unreachable",
    errno.EHOSTUNREACH: "no route to host",
    errno.ETIMEDOUT: "timed out",
}


def _safe_network_hint(exc: BaseException) -> str:
    """Extract a short, non-sensitive routing hint from connection failures."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        en = getattr(cur, "errno", None)
        if isinstance(en, int) and en in _SAFE_ERRNO_HINTS:
            return _SAFE_ERRNO_HINTS[en]
        # httpx wraps OSError as __cause__ / args
        args = getattr(cur, "args", ())
        for arg in args:
            if isinstance(arg, OSError) and isinstance(getattr(arg, "errno", None), int):
                hint = _SAFE_ERRNO_HINTS.get(arg.errno)
                if hint:
                    return hint
            if isinstance(arg, int) and arg in _SAFE_ERRNO_HINTS:
                return _SAFE_ERRNO_HINTS[arg]
        cur = cur.__cause__ or cur.__context__
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        return "timed out"
    return ""


def public_error_message(exc: BaseException, *, prefix: str = "") -> str:
    """Return a client-safe error label (no secrets / raw exception text).

    For connection failures, append a short routing hint (e.g. no route to host)
    so operators can tell VPN/Docker networking issues from bad credentials.
    """
    label = exc.__class__.__name__
    hint = _safe_network_hint(exc)
    if hint:
        label = f"{label} ({hint})"
    if prefix:
        return f"{prefix}: {label}"
    return label
