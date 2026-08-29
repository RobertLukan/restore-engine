"""Startup guards for ui.session_secret."""

import pytest

from main import validate_session_secret


def test_validate_session_secret_ok_when_password_unset():
    validate_session_secret({})
    validate_session_secret({"ui": {}})


def test_validate_session_secret_ok_with_strong_secret():
    validate_session_secret(
        {
            "ui": {
                "password": "secret",
                "session_secret": "a" * 32,
            }
        }
    )


def test_validate_session_secret_rejects_missing():
    with pytest.raises(RuntimeError, match="ui.session_secret is required"):
        validate_session_secret({"ui": {"password": "secret"}})


def test_validate_session_secret_rejects_short():
    with pytest.raises(RuntimeError, match="at least 32"):
        validate_session_secret(
            {"ui": {"password": "secret", "session_secret": "too-short"}}
        )


def test_validate_session_secret_rejects_dev_placeholder():
    with pytest.raises(RuntimeError, match="default dev placeholder"):
        validate_session_secret(
            {
                "ui": {
                    "password": "secret",
                    "session_secret": "dev-insecure-session-secret-change-me",
                }
            }
        )
