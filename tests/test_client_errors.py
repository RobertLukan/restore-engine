"""Tests for client_errors helpers."""

import ssl

from client_errors import public_error_message, tls_client_context


def test_tls_client_context_minimum_version():
    ctx = tls_client_context(verify=True)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_tls_client_context_can_disable_verify():
    ctx = tls_client_context(verify=False)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_public_error_message_uses_class_name_only():
    try:
        raise ValueError("secret internal detail")
    except ValueError as exc:
        assert public_error_message(exc) == "ValueError"
        assert public_error_message(exc, prefix="failed") == "failed: ValueError"
        assert "secret" not in public_error_message(exc)
