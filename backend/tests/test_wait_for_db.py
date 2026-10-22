"""`python -m app.jobs.wait_for_db`: the container waits for the database before migrating."""

import socket

import pytest

from app.jobs import wait_for_db


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_it_waits_until_the_database_answers() -> None:
    answers = iter([False, False, True])
    clock = Clock()

    assert wait_for_db.wait(
        "postgres",
        5432,
        timeout=10,
        check=lambda h, p: next(answers),
        clock=clock,
        sleep=clock.sleep,
    )
    assert clock.now == 2.0


def test_it_gives_up_after_the_timeout() -> None:
    clock = Clock()

    assert not wait_for_db.wait(
        "postgres", 5432, timeout=3, check=lambda h, p: False, clock=clock, sleep=clock.sleep
    )
    assert clock.now == 3.0


def test_a_name_that_does_not_resolve_is_not_reachable() -> None:
    assert not wait_for_db.reachable("no-such-host.invalid", 5432, timeout=0.5)


def test_an_open_port_is_reachable() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert wait_for_db.reachable("127.0.0.1", server.getsockname()[1])


def test_failure_names_host_and_port_but_never_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Settings:
        database_url = "postgresql+asyncpg://discover:s3cret@db.example:6543/discover"

    monkeypatch.setattr(wait_for_db, "get_settings", lambda: Settings())
    monkeypatch.setattr(wait_for_db, "wait", lambda *a, **k: False)

    assert wait_for_db.main(["--timeout", "1"]) == 1
    error = capsys.readouterr().err
    assert "db.example:6543" in error and "s3cret" not in error
