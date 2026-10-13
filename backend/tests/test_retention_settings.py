import logging
from datetime import time

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


# --- /summary reports (ADR 0015) -----------------------------------------------------------------

_REPORT_ENV = (
    "REPORT_TIMEZONE",
    "REPORT_RETENTION_DAYS",
    "REPORT_CLEANUP_TIME",
    "REPORT_BUILD_TIMEOUT_SECONDS",
    "REPORT_DOWNLOAD_LINK_SECONDS",
    "REPORT_LOGO_PATH",
)


def report_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in _REPORT_ENV:
        monkeypatch.delenv(name, raising=False)
    return settings_from_env(monkeypatch, **env)


def test_report_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = report_settings(monkeypatch)

    assert settings.report_timezone == "Asia/Kolkata"
    assert settings.report_retention_days == 7
    assert settings.report_cleanup_time == "02:00"
    assert settings.report_cleanup_clock == time(2, 0)
    assert settings.report_build_timeout_seconds == 1800  # many-model reports
    assert settings.report_model_concurrency == 4
    assert (settings.report_trend_days, settings.report_inactive_lookback_days) == (30, 7)
    assert settings.report_download_link_seconds == 60
    assert settings.report_logo_path is None
    assert str(settings.report_zone) == "Asia/Kolkata"


def test_configured_report_values_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = report_settings(
        monkeypatch,
        REPORT_TIMEZONE="Europe/London",
        REPORT_RETENTION_DAYS="14",
        REPORT_CLEANUP_TIME="3:5",
    )

    assert settings.report_timezone == "Europe/London"
    assert settings.report_retention_days == 14
    assert settings.report_cleanup_time == "03:05"


@pytest.mark.parametrize("value", ["Mars/Olympus", "../../etc/passwd", "", "   "])
def test_unknown_time_zones_fall_back_to_asia_kolkata(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    assert report_settings(monkeypatch, REPORT_TIMEZONE=value).report_timezone == "Asia/Kolkata"


@pytest.mark.parametrize("value", ["25:00", "02:60", "2am", "02", "", "-1:00"])
def test_invalid_cleanup_times_fall_back_to_02_00(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    assert report_settings(monkeypatch, REPORT_CLEANUP_TIME=value).report_cleanup_time == "02:00"


@pytest.mark.parametrize("value", ["0", "-1", "a week"])
def test_invalid_retention_days_fall_back_to_7(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    assert report_settings(monkeypatch, REPORT_RETENTION_DAYS=value).report_retention_days == 7


def test_invalid_report_timezone_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    report_settings(monkeypatch, REPORT_TIMEZONE="Nowhere/Town")

    assert "REPORT_TIMEZONE='Nowhere/Town'" in caplog.text
