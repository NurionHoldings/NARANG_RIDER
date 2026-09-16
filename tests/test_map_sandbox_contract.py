from hashlib import sha256

import pytest

from narang_rider.map_sandbox_contract import (
    ContractRejected,
    FailureMode,
    MapSandboxContractRunner,
    SandboxContract,
    SyntheticRequest,
    SyntheticResponse,
    sandbox_release_gate,
)


class Adapter:
    def __init__(self, response=None):
        self.response = response or SyntheticResponse(
            200, {"distance_m": 1200, "duration_s": 360}
        )
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        return self.response


def contract(**changes):
    values = {
        "provider_id": "synthetic-map",
        "fixture_version": "2026-09-16.1",
        "allowed_capability": "route",
        "allowed_host": "synthetic-map.invalid",
        "allowed_path": "/v1/route",
        "allowed_request_fields": frozenset({"origin_token", "destination_token"}),
        "required_response_fields": frozenset({"distance_m", "duration_s"}),
        "maximum_requests": 1,
        "evidence_digest": sha256(b"synthetic-contract").hexdigest(),
    }
    values.update(changes)
    return SandboxContract(**values)


def request(**changes):
    values = {
        "capability": "route",
        "url": "https://synthetic-map.invalid/v1/route",
        "fields": {"origin_token": "synthetic:o", "destination_token": "synthetic:d"},
        "credential_ref": "vault-ref:synthetic:map-contract",
    }
    values.update(changes)
    return SyntheticRequest(**values)


def test_valid_synthetic_contract_passes_but_never_activates():
    adapter = Adapter()
    report = MapSandboxContractRunner().verify(
        contract=contract(), adapter=adapter, request=request()
    )
    assert report.passed and adapter.calls == 1
    assert not report.production_activation_allowed
    assert report.as_ci_artifact()["schema"] == "narang.map-sandbox-contract.v1"
    assert sandbox_release_gate(report)["production_status"] == "BLOCKED"


@pytest.mark.parametrize(
    ("changes", "violation"),
    [
        ({"capability": "motorcycle_route"}, "capability_not_allowlisted"),
        ({"url": "https://evil.example/v1/route"}, "endpoint_not_allowlisted"),
        ({"url": "https://synthetic-map.invalid/v1/../admin"}, "endpoint_not_allowlisted"),
        ({"url": "https://synthetic-map.invalid/v1/route?debug=1"}, "endpoint_not_allowlisted"),
        ({"fields": {"origin_token": "x", "debug": True}}, "request_field_not_allowlisted"),
    ],
)
def test_non_allowlisted_request_is_rejected_before_adapter_call(changes, violation):
    adapter = Adapter()
    report = MapSandboxContractRunner().verify(
        contract=contract(), adapter=adapter, request=request(**changes)
    )
    assert violation in report.violations
    assert adapter.calls == 0


@pytest.mark.parametrize("field", ["rider_id", "order_id", "address", "precise_location"])
def test_personal_data_fields_are_blocked_before_call(field):
    adapter = Adapter()
    report = MapSandboxContractRunner().verify(
        contract=contract(allowed_request_fields=frozenset({field})),
        adapter=adapter,
        request=request(fields={field: "real-data"}),
    )
    assert "personal_data_exposed" in report.violations
    assert report.personal_data_exposed
    assert adapter.calls == 0


def test_raw_or_non_synthetic_credential_is_blocked():
    adapter = Adapter()
    report = MapSandboxContractRunner().verify(
        contract=contract(), adapter=adapter, request=request(credential_ref="secret-key")
    )
    assert "credential_boundary_failed" in report.violations
    assert report.credential_exposed and adapter.calls == 0


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (FailureMode.TIMEOUT, "provider_timeout"),
        (FailureMode.QUOTA_EXHAUSTED, "provider_quota_exhausted"),
        (FailureMode.KEY_REVOKED, "provider_key_revoked"),
        (FailureMode.MALFORMED_RESPONSE, "provider_malformed_response"),
    ],
)
def test_provider_failures_fail_closed(mode, expected):
    adapter = Adapter(SyntheticResponse(503, {}, mode))
    report = MapSandboxContractRunner().verify(
        contract=contract(), adapter=adapter, request=request()
    )
    assert expected in report.violations
    assert "unexpected_status" in report.violations
    assert not report.passed


def test_response_schema_and_response_pii_are_rejected():
    adapter = Adapter(SyntheticResponse(200, {"distance_m": 1, "address": "real"}))
    report = MapSandboxContractRunner().verify(
        contract=contract(), adapter=adapter, request=request()
    )
    assert "response_schema_mismatch" in report.violations
    assert "response_personal_data_exposed" in report.violations


@pytest.mark.parametrize(
    "changes",
    [
        {"synthetic_only": False},
        {"allowed_host": "api.real-provider.com"},
        {"allowed_path": "/../admin"},
        {"maximum_requests": 0},
        {"evidence_digest": "bad"},
    ],
)
def test_unsafe_contract_definition_is_rejected(changes):
    with pytest.raises(ContractRejected):
        MapSandboxContractRunner().verify(
            contract=contract(**changes), adapter=Adapter(), request=request()
        )


def test_report_digest_is_deterministic():
    first = MapSandboxContractRunner().verify(
        contract=contract(), adapter=Adapter(), request=request()
    )
    second = MapSandboxContractRunner().verify(
        contract=contract(), adapter=Adapter(), request=request()
    )
    assert first.report_digest == second.report_digest
