"""Authorization service against a real database; Power BI simulated by FakeProbe.

User A (from the brief): Sales, HR, Inventory allowed — Finance, Manpower not.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.messages import GENERIC_DENIAL
from app.authz.service import AuthorizationService
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import AuditEvent, AuditOutcome
from tests.authz_helpers import REGISTRY, Clock, FakeProbe, request_ctx, seed_registry

pytestmark = pytest.mark.integration

USER_A_GRANTS = {"user-a": {"sales-ds", "hr-ds", "inventory-ds", "draft-ds"}}


@pytest.fixture
async def env(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[AuthorizationService, FakeProbe, Clock, async_sessionmaker[AsyncSession]]:
    await seed_registry(committed_sessionmaker)
    probe = FakeProbe({k: set(v) for k, v in USER_A_GRANTS.items()})
    clock = Clock()
    service = AuthorizationService(
        committed_sessionmaker, probe, Settings(_env_file=None), clock=clock
    )
    return service, probe, clock, committed_sessionmaker


Env = tuple[AuthorizationService, FakeProbe, Clock, async_sessionmaker[AsyncSession]]


async def audit_events(sm: async_sessionmaker[AsyncSession]) -> list[AuditEvent]:
    async with sm() as session:
        return list((await session.scalars(select(AuditEvent).order_by(AuditEvent.id))).all())


# --- G1: allowed set ----------------------------------------------------------------------------


async def test_allowed_set_contains_only_permitted_enabled_models(env: Env) -> None:
    service, probe, _, _ = env

    authz = await service.build_context(request_ctx())

    assert sorted(authz.allowed) == ["hr-ds", "inventory-ds", "sales-ds"]
    # The disabled model is never even asked about, although Power BI would allow it.
    assert "draft-ds" not in probe.probed


async def test_decisions_are_cached_within_ttl(env: Env) -> None:
    service, probe, clock, _ = env
    await service.build_context(request_ctx())

    clock.advance(minutes=1)
    await service.build_context(request_ctx())

    assert len(probe.calls) == 1


async def test_denied_expires_after_2_minutes_and_allowed_after_10(env: Env) -> None:
    service, probe, clock, _ = env
    await service.build_context(request_ctx())

    clock.advance(minutes=3)
    await service.build_context(request_ctx())
    assert probe.calls[-1] == ["finance-ds", "manpower-ds"]  # only denied rows re-checked

    clock.advance(minutes=1)  # 4 min: allowed rows (10 min) still trusted
    await service.build_context(request_ctx())
    assert len(probe.calls) == 2

    clock.advance(minutes=7)  # 11 min: allowed rows expired too (denied rows refreshed at 3 min)
    await service.build_context(request_ctx())
    assert {"hr-ds", "inventory-ds", "sales-ds"} <= set(probe.calls[-1])


async def test_cache_is_shared_between_backend_instances(env: Env) -> None:
    service, probe, clock, sm = env
    await service.build_context(request_ctx())
    other_instance = AuthorizationService(sm, probe, Settings(_env_file=None), clock=clock)

    authz = await other_instance.build_context(request_ctx())

    assert len(probe.calls) == 1
    assert sorted(authz.allowed) == ["hr-ds", "inventory-ds", "sales-ds"]


async def test_power_bi_not_answering_fails_closed_and_is_not_cached(env: Env) -> None:
    service, probe, _, _ = env
    probe.unknown = {"sales-ds"}

    authz = await service.build_context(request_ctx())
    assert not authz.is_allowed("sales-ds")
    assert authz.unverified == frozenset({"sales-ds"})

    probe.unknown = set()
    authz = await service.build_context(request_ctx())
    assert authz.is_allowed("sales-ds")


# --- G2: explicit checks ------------------------------------------------------------------------


async def test_scenario_1_allowed_model_passes_and_is_audited(env: Env) -> None:
    service, _, _, sm = env
    authz = await service.build_context(request_ctx())

    await service.assert_allowed(authz, ["sales-ds"])

    events = await audit_events(sm)
    assert [(e.outcome, e.pbi_dataset_id) for e in events] == [(AuditOutcome.ALLOW, "sales-ds")]


async def test_scenario_2_denied_model_gets_generic_message(env: Env) -> None:
    service, _, _, sm = env
    authz = await service.build_context(request_ctx())

    with pytest.raises(AppError) as excinfo:
        await service.assert_allowed(authz, ["finance-ds"])

    assert excinfo.value.status_code == 403
    assert excinfo.value.message == GENERIC_DENIAL
    event = (await audit_events(sm))[-1]
    assert (event.outcome, event.reason) == (AuditOutcome.DENY, "model_not_allowed")


async def test_scenario_6_cross_model_request_is_denied_as_a_whole(env: Env) -> None:
    service, _, _, sm = env
    authz = await service.build_context(request_ctx())

    with pytest.raises(AppError) as excinfo:
        await service.assert_allowed(authz, ["sales-ds", "finance-ds"])

    assert excinfo.value.message == GENERIC_DENIAL
    event = (await audit_events(sm))[-1]
    assert event.details == {
        "model_ids": ["sales-ds", "finance-ds"],
        "denied_model_ids": ["finance-ds"],
    }


@pytest.mark.parametrize("dataset_id", ["does-not-exist", "draft-ds", "finance-ds"])
async def test_scenario_11_unknown_disabled_and_forbidden_look_identical(
    env: Env, dataset_id: str
) -> None:
    service, probe, _, _ = env
    authz = await service.build_context(request_ctx())
    calls_before = len(probe.calls)

    with pytest.raises(AppError) as excinfo:
        await service.assert_allowed(authz, [dataset_id])

    assert (excinfo.value.code, excinfo.value.message) == ("model_access_denied", GENERIC_DENIAL)
    assert len(probe.calls) == calls_before  # unregistered/disabled ids are never sent to Power BI


async def test_generic_denial_never_names_the_model(env: Env) -> None:
    service, _, _, _ = env
    authz = await service.build_context(request_ctx())

    with pytest.raises(AppError) as excinfo:
        await service.assert_allowed(authz, ["finance-ds"])

    name, domain, _ = REGISTRY["finance-ds"]
    for secret in ("finance-ds", name, domain or name):
        assert secret.lower() not in excinfo.value.message.lower()


async def test_scenario_4_newly_granted_access_works_immediately(env: Env) -> None:
    service, probe, clock, _ = env
    await service.build_context(request_ctx())  # Finance cached as denied
    probe.grants["user-a"].add("finance-ds")  # admin grants access in Power BI
    clock.advance(seconds=30)  # still inside the 2-minute denied TTL

    authz = await service.build_context(request_ctx())
    assert not authz.is_allowed("finance-ds")  # list still from cache...
    authz = await service.assert_allowed(authz, ["finance-ds"])  # ...explicit ask re-checks live

    assert authz.is_allowed("finance-ds")
    assert (await service.build_context(request_ctx())).is_allowed("finance-ds")  # cache updated


async def test_model_denied_live_in_this_request_is_not_rechecked(env: Env) -> None:
    service, probe, _, _ = env
    authz = await service.build_context(request_ctx())  # live probe this request

    with pytest.raises(AppError):
        await service.assert_allowed(authz, ["finance-ds"])

    assert len(probe.calls) == 1


async def test_unverifiable_model_returns_503_not_a_denial(env: Env) -> None:
    service, probe, _, _ = env
    probe.unknown = {"sales-ds"}
    authz = await service.build_context(request_ctx())

    with pytest.raises(AppError) as excinfo:
        await service.assert_allowed(authz, ["sales-ds"])

    assert (excinfo.value.status_code, excinfo.value.code) == (503, "access_check_unavailable")


# --- scenarios 5 and 20: revocation -------------------------------------------------------------


async def test_scenario_5_revocation_reported_by_power_bi_takes_effect_at_once(env: Env) -> None:
    service, probe, clock, sm = env
    authz = await service.build_context(request_ctx())
    probe.grants["user-a"].discard("sales-ds")  # admin revokes in Power BI

    await service.on_power_bi_denied(authz, "sales-ds")  # a real query was rejected
    clock.advance(seconds=10)  # well inside the 10-minute allowed TTL

    assert not (await service.build_context(request_ctx())).is_allowed("sales-ds")
    assert (await audit_events(sm))[-1].event_type == "authz.revoked"


async def test_scenario_20_follow_up_after_revocation_is_denied(env: Env) -> None:
    service, probe, clock, _ = env
    first_turn = await service.build_context(request_ctx())
    await service.assert_allowed(first_turn, ["sales-ds"])
    probe.grants["user-a"].discard("sales-ds")

    clock.advance(minutes=11)  # cached "allowed" expired
    follow_up = await service.build_context(request_ctx())

    with pytest.raises(AppError):
        await service.assert_allowed(follow_up, ["sales-ds"])


async def test_users_do_not_share_decisions(env: Env) -> None:
    service, probe, _, sm = env
    probe.grants["user-b"] = {"finance-ds"}

    a = await service.build_context(request_ctx("user-a"))
    b = await service.build_context(request_ctx("user-b"))

    assert not a.is_allowed("finance-ds")
    assert sorted(b.allowed) == ["finance-ds"]
    async with sm() as session:
        rows = await session.scalar(text("SELECT count(*) FROM user_model_access"))
    assert rows == 10  # 5 enabled models x 2 users
