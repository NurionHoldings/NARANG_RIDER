from datetime import UTC, datetime

import pytest

from narang_rider.compatibility import LegacyStatus
from narang_rider.connector import CallerType
from narang_rider.partner_connectors import (
    DeliveryState,
    IndependentConnectorHub,
    PartnerConnectorConfig,
    PartnerEnvelope,
    ai_baebi_config,
    dosirak_store_config,
)


def hub() -> IndependentConnectorHub:
    return IndependentConnectorHub((ai_baebi_config(), dosirak_store_config()))


def envelope(event_id: str = "evt-1") -> PartnerEnvelope:
    return PartnerEnvelope(
        event_id=event_id,
        external_order_id="partner-order-1",
        merchant_id="merchant-1",
        amount_won=18_000,
        pickup_ref="vault:locations/pickup-1",
        dropoff_ref="vault:locations/dropoff-1",
    )


@pytest.mark.parametrize("partner_id", ["ai-baebi", "dosirak-store"])
def test_partner_contract_maps_to_canonical_order(partner_id: str) -> None:
    connector = hub()
    order = connector.ingest(partner_id, envelope(), canonical_id="nara-order-1")

    assert order.canonical_id == "nara-order-1"
    assert order.source_refs == (f"{partner_id}:partner-order-1",)


def test_inbound_replay_is_idempotent_and_conflict_is_blocked() -> None:
    connector = hub()
    first = connector.ingest("ai-baebi", envelope(), canonical_id="nara-order-1")
    replay = connector.ingest("ai-baebi", envelope(), canonical_id="ignored")
    assert replay == first

    changed = PartnerEnvelope(**{**envelope().__dict__, "amount_won": 19_000})
    with pytest.raises(ValueError, match="INBOUND_IDEMPOTENCY_CONFLICT"):
        connector.ingest("ai-baebi", changed, canonical_id="nara-order-2")


def test_existing_partner_sources_are_deduplicated_into_one_order() -> None:
    connector = hub()
    first = connector.ingest("ai-baebi", envelope("ai-event"), canonical_id="nara-1")
    second = connector.ingest(
        "dosirak-store", envelope("dosirak-event"), canonical_id="nara-2"
    )
    assert first.canonical_id == second.canonical_id == "nara-1"
    assert set(second.source_refs) == {
        "ai-baebi:partner-order-1",
        "dosirak-store:partner-order-1",
    }


def test_merchant_and_company_rider_calls_cannot_duplicate_one_order() -> None:
    connector = hub()
    now = datetime(2026, 9, 16, tzinfo=UTC)
    connector.request_rider(
        "ai-baebi",
        call_id="call-merchant",
        order_id="nara-1",
        caller_type=CallerType.MERCHANT,
        caller_id="merchant-1",
        idempotency_key="merchant-key",
        payload={"fee": 4_000},
        created_at=now,
    )
    with pytest.raises(ValueError, match="DUPLICATE_ACTIVE_RIDER_CALL"):
        connector.request_rider(
            "dosirak-store",
            call_id="call-company",
            order_id="nara-1",
            caller_type=CallerType.RIDER_COMPANY,
            caller_id="company-1",
            idempotency_key="company-key",
            payload={"fee": 4_500},
            created_at=now,
        )


def test_outbound_replay_and_conflict_contract() -> None:
    connector = hub()
    first = connector.publish_status(
        "ai-baebi",
        event_id="out-1",
        canonical_order_id="nara-1",
        status=LegacyStatus.DISPATCHED,
        idempotency_key="status-key",
    )
    replay = connector.publish_status(
        "ai-baebi",
        event_id="out-ignored",
        canonical_order_id="nara-1",
        status=LegacyStatus.DISPATCHED,
        idempotency_key="status-key",
    )
    assert replay == first
    with pytest.raises(ValueError, match="OUTBOUND_IDEMPOTENCY_CONFLICT"):
        connector.publish_status(
            "ai-baebi",
            event_id="out-2",
            canonical_order_id="nara-1",
            status=LegacyStatus.DELIVERED,
            idempotency_key="status-key",
        )


def test_retry_dead_letter_and_partner_failure_isolation() -> None:
    connector = hub()
    for partner in ("ai-baebi", "dosirak-store"):
        connector.publish_status(
            partner,
            event_id=f"{partner}-out",
            canonical_order_id="nara-1",
            status=LegacyStatus.RECEIVED,
            idempotency_key=f"{partner}-key",
        )
    for _ in range(3):
        failed = connector.record_delivery(
            "ai-baebi", "ai-baebi-key", delivered=False, error_code="HTTP_503"
        )

    assert failed.state is DeliveryState.DEAD_LETTER
    assert len(connector.dead_letters("ai-baebi")) == 1
    healthy = connector.record_delivery(
        "dosirak-store", "dosirak-store-key", delivered=True
    )
    assert healthy.state is DeliveryState.DELIVERED
    assert connector.dead_letters("dosirak-store") == ()


def test_auth_material_must_be_vault_references() -> None:
    with pytest.raises(ValueError, match="PARTNER_AUTH_VAULT_REQUIRED"):
        PartnerConnectorConfig(
            "unsafe", "unsafe", "plain-secret", "vault:callback", frozenset()
        )
