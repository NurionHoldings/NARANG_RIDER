import dataclasses

import pytest

from narang_rider.partner_sandbox import (
    REQUIRED_CAPABILITIES,
    PartnerKind,
    PartnerProfile,
    PartnerSandboxHarness,
    Verdict,
    certify_all,
    default_profiles,
)


@pytest.mark.parametrize("profile", default_profiles(), ids=lambda profile: profile.kind)
def test_provider_neutral_contract_suite(profile: PartnerProfile) -> None:
    report = PartnerSandboxHarness(profile).run()
    expected = Verdict.BLOCKED if profile.provisional else Verdict.PASS
    assert report.verdict is expected
    assert report.live_certified is False
    assert len(report.evidence_digest) == 64
    assert all(len(check.evidence_digest) == 64 for check in report.checks)
    assert "payload" not in report.as_dict()
    assert not any(check.verdict is Verdict.FAIL for check in report.checks)


def test_two_named_profiles_are_explicitly_provisional() -> None:
    reports = {report.profile: report for report in certify_all()}
    assert reports[PartnerKind.AI_BAEBI].verdict is Verdict.BLOCKED
    assert reports[PartnerKind.DOSIRAK_STORE].verdict is Verdict.BLOCKED
    assert all(report.live_certified is False for report in reports.values())


@pytest.mark.parametrize("field", ["auth_ref", "webhook_key_ref"])
def test_profile_requires_vault_references(field: str) -> None:
    profile = default_profiles()[0]
    invalid = dataclasses.replace(profile, **{field: "plaintext-secret"})
    with pytest.raises(ValueError, match="VAULT_REFERENCE_REQUIRED"):
        invalid.validate()


def test_missing_capability_fails_closed() -> None:
    profile = dataclasses.replace(
        default_profiles()[0], capabilities=REQUIRED_CAPABILITIES - {"settlement.reconcile"}
    )
    report = PartnerSandboxHarness(profile).run()
    assert report.verdict is Verdict.FAIL
    assert report.checks[0].code == "REQUIRED_CAPABILITY_MISSING"


def test_report_never_contains_fixture_payload_or_secrets() -> None:
    rendered = str(PartnerSandboxHarness(default_profiles()[0]).run().as_dict())
    assert "not-a-real-secret" not in rendered
    assert "vault:pii" not in rendered
    assert "order_id" not in rendered
