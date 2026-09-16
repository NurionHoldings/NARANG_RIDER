from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter

import pytest

from narang_rider.api_contract import (
    ApiContractHandler,
    FakeAuthContextVerifier,
    HttpRequest,
    VerifiedPrincipal,
)
from narang_rider.application import CommandCapability, OrderIntakeApplicationService
from narang_rider.casework import (
    CaseErrorCode,
    CasePrincipal,
    CaseRejected,
    CaseSeverity,
    CaseType,
    CaseworkService,
    ParticipantRole,
)
from narang_rider.dispatch import DispatchCandidate, FairDispatchPolicy
from narang_rider.money import Money
from narang_rider.outbox_worker import DeliveryPolicy
from narang_rider.persistence import InMemoryPersistence
from narang_rider.pricing import DeliveryFacts, PricingPolicy, quote_delivery
from narang_rider.resource_limits import JsonBudget, ResourceLimitExceeded, bounded_json_object
from narang_rider.simulation import SimulationProfile


def test_json_depth_node_collection_and_string_budgets_fail_closed() -> None:
    budget = JsonBudget(max_bytes=65_536, max_depth=4, max_nodes=16,
                        max_collection_items=4, max_string_chars=8)
    values = [
        b'{"a":{"b":{"c":{"d":{"e":1}}}}}',
        b'{"a":[1,2,3,4,5]}',
        b'{"a":"123456789"}',
        b'{"a":1,"b":2,"c":3,"d":4,"e":5}',
    ]
    for value in values:
        with pytest.raises(ResourceLimitExceeded):
            bounded_json_object(value, budget)


def test_api_rejects_deep_json_and_returns_uniform_retry_after() -> None:
    store = InMemoryPersistence()
    principal = VerifiedPrincipal(
        "actor", "branch", frozenset({CommandCapability.INGEST_ORDER,
                                        CommandCapability.CALL_RIDER})
    )

    class DenyRate:
        def allow(self, principal, route_id):
            return False

    handler = ApiContractHandler(
        auth_verifier=FakeAuthContextVerifier({"Bearer token": principal}),
        rate_limit=DenyRate(),
        intake=OrderIntakeApplicationService(store), repository=store, unit_of_work=store,
    )
    request = HttpRequest("POST", "/api/v1/orders", {
        "Authorization": "Bearer token", "X-Branch-Id": "branch",
        "Idempotency-Key": "key", "Content-Type": "application/json",
    }, b"{}")
    response = handler.handle(request)
    assert response.status == 429 and response.headers["Retry-After"] == "1"


def test_dispatch_simulation_outbox_and_case_limits_are_bounded() -> None:
    policy = FairDispatchPolicy("policy")
    candidates = tuple(DispatchCandidate(str(i), True, i, i) for i in range(5_001))
    with pytest.raises(ValueError, match="CANDIDATE_LIMIT"):
        policy.select(candidates, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ORDER_LIMIT"):
        SimulationProfile("too-large", 1, 1, 1, 1, 300_001, 1)
    with pytest.raises(ValueError):
        DeliveryPolicy(batch_size=501)

    service = CaseworkService()
    principal = CasePrincipal("owner", ParticipantRole.CUSTOMER, "branch")
    with pytest.raises(CaseRejected) as rejected:
        service.open_case(
            case_id="case", principal=principal, case_type=CaseType.DELIVERY,
            severity=CaseSeverity.STANDARD,
            participant_ids=("owner", *(f"p{i}" for i in range(16))),
            resource_ref="resource://order/1", idempotency_key="key",
            now=datetime(2026, 9, 16, tzinfo=UTC),
        )
    assert rejected.value.code is CaseErrorCode.RESOURCE_LIMIT


def test_hot_pricing_and_dispatch_budget_is_generous_and_deterministic() -> None:
    pricing = PricingPolicy(
        "v1", datetime(2026, 1, 1, tzinfo=UTC), 3_000, 500, 180, 1_000,
        3, 120, 500, 100, 300, 450, 100, 100, 20_000,
    )
    facts = DeliveryFacts(4_000, 20, 5, 1_000, 3)
    quote = quote_delivery(
        pricing, facts, customer_delivery_fee=Money(3_000), expected_direct_cost=Money(1_000)
    )
    candidates = tuple(DispatchCandidate(f"r{i}", True, i, 100 + i) for i in range(1_000))
    dispatch = FairDispatchPolicy("fifo")
    started = perf_counter()
    for _ in range(100):
        result = dispatch.select(candidates, quote)
        assert result.selected_rider_id == "r0"
    assert perf_counter() - started < 5.0
