"""Client-safe error text and TLS defaults for HTTP/API responses."""

from __future__ import annotations

import ssl


def tls_client_context(*, verify: bool = True) -> ssl.SSLContext:
    """TLS client context with TLS 1.2+ only."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def public_error_message(exc: BaseException, *, prefix: str = "") -> str:
    """Return exception type only — avoid leaking str(exc) / traceback text to clients."""
    label = exc.__class__.__name__
    if prefix:
        return f"{prefix}: {label}"
    return label
