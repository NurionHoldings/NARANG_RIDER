from narang_rider.compatibility import CompatibilityGateway, LegacyStatus, PartnerOrder


def partner(provider, external):
    return PartnerOrder(
        provider, external, "merchant-1", 25000,
        "vault:pickup:1", "vault:dropoff:1", LegacyStatus.RECEIVED
    )


def test_existing_platform_and_agency_orders_merge():
    gateway = CompatibilityGateway()
    first = gateway.ingest(partner("pos", "p1"), canonical_id="order-1")
    merged = gateway.ingest(partner("agency", "a1"), canonical_id="order-2")
    assert merged.canonical_id == first.canonical_id
    assert merged.source_refs == ("agency:a1", "pos:p1")


def test_status_callback_returns_to_every_source():
    gateway = CompatibilityGateway()
    gateway.ingest(partner("pos", "p1"), canonical_id="order-1")
    gateway.ingest(partner("agency", "a1"), canonical_id="order-2")
    callbacks = gateway.callback("order-1", LegacyStatus.DELIVERED)
    assert callbacks == (("agency:a1", LegacyStatus.DELIVERED), ("pos:p1", LegacyStatus.DELIVERED))
