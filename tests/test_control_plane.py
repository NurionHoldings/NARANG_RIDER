from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.control_plane import (
    BranchOnboardingService,
    BranchPolicyOverride,
    BranchPolicyResolver,
    NationalOperatingPolicy,
    ReadinessEvidence,
    ReadinessItem,
)
from narang_rider.network import (
    Branch,
    BranchRegistry,
    BranchStatus,
    BranchType,
)

NOW = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)


def registry():
    value = BranchRegistry()
    value.register(
        Branch("hq", BranchType.HEADQUARTERS, "본사", None, BranchStatus.ACTIVE, (), 1, NOW)
    )
    value.register(
        Branch(
            "central",
            BranchType.REGIONAL_BRANCH,
            "중부",
            "hq",
            BranchStatus.ACTIVE,
            (),
            1,
            NOW,
        )
    )
    value.register(
        Branch(
            "new-hub",
            BranchType.LOCAL_HUB,
            "신규허브",
            "central",
            BranchStatus.PROVISIONING,
            (),
            1,
            NOW,
        )
    )
    return value


def national_policy():
    return NationalOperatingPolicy(
        policy_id="national-v1",
        version=1,
        minimum_base_pay_won=3_000,
        maximum_evidence_retention=timedelta(days=7),
        maximum_settlement_delay_hours=24,
        forbidden_ai_authorities=frozenset(
            {"PAY_REDUCTION", "ACCOUNT_SUSPENSION", "RETALIATORY_DISPATCH"}
        ),
        effective_from=NOW,
    )


def override(**changes):
    values = {
        "override_id": "override-1",
        "branch_id": "new-hub",
        "national_policy_id": "national-v1",
        "national_policy_version": 1,
        "base_pay_won": 3_500,
        "evidence_retention": timedelta(days=5),
        "settlement_delay_hours": 12,
        "requested_by": "hub-manager",
        "approved_by_hq": "hq-policy",
        "approved_by_branch": "hub-owner",
        "effective_from": NOW,
    }
    values.update(changes)
    return BranchPolicyOverride(**values)


def test_branch_inherits_national_safety_floor() -> None:
    resolver = BranchPolicyResolver(
        branches=registry(),
        national_policy=national_policy(),
    )
    effective = resolver.resolve("new-hub")

    assert effective.base_pay_won == 3_000
    assert "PAY_REDUCTION" in effective.forbidden_ai_authorities
    assert effective.override_id is None


@pytest.mark.parametrize(
    "changes,error",
    (
        ({"base_pay_won": 2_999}, "BRANCH_CANNOT_LOWER_MINIMUM_PAY"),
        (
            {"evidence_retention": timedelta(days=8)},
            "BRANCH_CANNOT_EXTEND_EVIDENCE_RETENTION",
        ),
        ({"settlement_delay_hours": 25}, "BRANCH_CANNOT_DELAY_SETTLEMENT"),
        ({"national_policy_version": 0}, "STALE_NATIONAL_POLICY_VERSION"),
    ),
)
def test_branch_override_cannot_weaken_national_floor(changes, error) -> None:
    resolver = BranchPolicyResolver(
        branches=registry(),
        national_policy=national_policy(),
    )
    with pytest.raises(ValueError, match=error):
        resolver.register_override(override(**changes))


def test_valid_local_improvement_keeps_forbidden_ai_authorities() -> None:
    resolver = BranchPolicyResolver(
        branches=registry(),
        national_policy=national_policy(),
    )
    resolver.register_override(override())
    effective = resolver.resolve("new-hub")

    assert effective.base_pay_won == 3_500
    assert effective.evidence_retention == timedelta(days=5)
    assert effective.settlement_delay_hours == 12
    assert effective.forbidden_ai_authorities == national_policy().forbidden_ai_authorities


def readiness(item, *, expires_at=None):
    return ReadinessEvidence(
        item=item,
        evidence_reference=f"vault:readiness:{item.value}",
        verified_by="independent-reviewer",
        verified_at=NOW,
        expires_at=expires_at,
    )


def test_branch_activation_requires_every_readiness_item() -> None:
    branches = registry()
    onboarding = BranchOnboardingService(branches=branches)
    for item in tuple(ReadinessItem)[:-1]:
        onboarding.record(branch_id="new-hub", evidence=readiness(item))

    result = onboarding.evaluate(branch_id="new-hub", now=NOW)
    assert not result.ready
    assert result.missing_items == (ReadinessItem.POLICY_ACCEPTANCE,)
    with pytest.raises(ValueError, match="BRANCH_NOT_READY"):
        onboarding.activate(
            branch_id="new-hub",
            now=NOW,
            expected_policy_version=1,
            hq_approver="hq",
            branch_approver="hub",
        )


def test_expired_insurance_blocks_activation() -> None:
    onboarding = BranchOnboardingService(branches=registry())
    for item in ReadinessItem:
        onboarding.record(
            branch_id="new-hub",
            evidence=readiness(
                item,
                expires_at=(
                    NOW + timedelta(hours=1)
                    if item is ReadinessItem.INSURANCE_COVERAGE
                    else None
                ),
            ),
        )
    result = onboarding.evaluate(branch_id="new-hub", now=NOW + timedelta(hours=2))

    assert not result.ready
    assert result.expired_items == (ReadinessItem.INSURANCE_COVERAGE,)


def test_ready_branch_requires_independent_hq_and_branch_approval() -> None:
    branches = registry()
    onboarding = BranchOnboardingService(branches=branches)
    for item in ReadinessItem:
        onboarding.record(branch_id="new-hub", evidence=readiness(item))
    with pytest.raises(ValueError, match="BRANCH_ACTIVATION_DUAL_APPROVAL_REQUIRED"):
        onboarding.activate(
            branch_id="new-hub",
            now=NOW,
            expected_policy_version=1,
            hq_approver="same",
            branch_approver="same",
        )
    activated = onboarding.activate(
        branch_id="new-hub",
        now=NOW,
        expected_policy_version=1,
        hq_approver="hq",
        branch_approver="hub",
    )
    assert activated.status is BranchStatus.ACTIVE


def test_raw_readiness_document_is_never_stored() -> None:
    with pytest.raises(ValueError, match="VERIFIED_VAULT_EVIDENCE_REQUIRED"):
        readiness = ReadinessEvidence(
            item=ReadinessItem.LEGAL_AUTHORITY,
            evidence_reference="business-registration-number:raw",
            verified_by="reviewer",
            verified_at=NOW,
        )
        BranchOnboardingService(branches=registry()).record(
            branch_id="new-hub",
            evidence=readiness,
        )
