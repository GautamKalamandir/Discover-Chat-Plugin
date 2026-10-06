import logging

import pytest

from app.core.config import Settings


def settings_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in (
        "CONVERSATION_RETENTION_HOURS",
        "AUDIT_RETENTION_HOURS",
        "CLEANUP_INTERVAL_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_env(monkeypatch)

    assert settings.conversation_retention_hours == 12
    assert settings.audit_retention_hours == 2160
    assert settings.cleanup_interval_minutes == 15


@pytest.mark.parametrize(("value", "expected"), [("24", 24), (" 48 ", 48), ("1", 1)])
def test_configured_hours_are_used(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    settings = settings_from_env(monkeypatch, CONVERSATION_RETENTION_HOURS=value)

    assert settings.conversation_retention_hours == expected


@pytest.mark.parametrize("value", ["0", "-5", "abc", "12.5", "", "99999999999"])
def test_invalid_hours_fall_back_to_12(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    settings = settings_from_env(monkeypatch, CONVERSATION_RETENTION_HOURS=value)

    assert settings.conversation_retention_hours == 12


def test_invalid_value_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    settings_from_env(monkeypatch, AUDIT_RETENTION_HOURS="forever")

    assert "AUDIT_RETENTION_HOURS='forever'" in caplog.text
    assert "fallback 2160" in caplog.text


def test_audit_and_conversation_windows_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_env(
        monkeypatch, CONVERSATION_RETENTION_HOURS="24", AUDIT_RETENTION_HOURS="720"
    )

    assert (settings.conversation_retention_hours, settings.audit_retention_hours) == (24, 720)
