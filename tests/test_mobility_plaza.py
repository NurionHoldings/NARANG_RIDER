from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from narang_rider.mobility_plaza import (
    ArkaonObservation,
    ArkaonPlazaSupervisor,
    AuthorityEvidence,
    ComparisonCandidate,
    ComparisonFactor,
    DataBoundaryRequest,
    ListingGovernance,
    ListingStage,
    MonitoringVerdict,
    PartnerApplication,
    PartnerGovernance,
    PartnerStage,
    PlazaListing,
    PlazaRejected,
    PlazaVertical,
    compare_listings,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def application(vertical=PlazaVertical.INSTALLMENT_FINANCE):
    return PartnerApplication(
        "partner-1", "합성 파트너", frozenset({vertical}), "evidence-ref:license-1",
        digest("contract"), digest("disclosure"), "https://finance.synthetic.invalid/api",
    )


def enabled_partner(vertical=PlazaVertical.INSTALLMENT_FINANCE):
    governance = PartnerGovernance()
    governance.register(application(vertical))
    governance.submit_evidence("partner-1", digest("evidence"))
    governance.accept_contract_test("partner-1", digest("contract-test"))
    governance.ethernian_review("partner-1", "review:ethernian:1")
    governance.operator_decide("partner-1", "decision:operator:1")
    return governance, governance.enable_sandbox("partner-1")


def listing(**changes):
    values = {
        "listing_id": "listing-1",
        "partner_id": "partner-1",
        "vertical": PlazaVertical.INSTALLMENT_FINANCE,
        "title": "합성 이륜차 할부 비교상품",
        "terms_digest": digest("terms"),
        "evidence_refs": ("evidence-ref:terms-1",),
        "commission_bps": 150,
        "sponsored": True,
    }
    values.update(changes)
    return PlazaListing(**values)


def visible_listing(**changes):
    value = listing(**changes)
    governance = ListingGovernance()
    _, partner = enabled_partner(value.vertical)
    governance.register(value, partner)
    governance.submit_for_review(value.listing_id)
    return governance.expose_in_sandbox(value.listing_id, "operator:list:1")


def authority(**changes):
    values = {
        "evidence_id": "law-1",
        "source_url": "https://www.law.go.kr/example",
        "title": "합성 검토용 공식 근거",
        "authority": "LAW_GO_KR",
        "accessed_at": NOW,
        "expires_at": NOW + timedelta(days=30),
        "content_digest": digest("law"),
        "legal_interpretation_approved": True,
    }
    values.update(changes)
    return AuthorityEvidence(**values)


def test_regulated_partner_requires_license_and_invalid_sandbox_only():
    with pytest.raises(PlazaRejected, match="license"):
        PartnerApplication(
            "p", "x", frozenset({PlazaVertical.INSURANCE}), None,
            digest("c"), digest("d"), "https://insurance.synthetic.invalid/api",
        )
    with pytest.raises(PlazaRejected, match="invalid"):
        PartnerApplication(
            "p", "x", frozenset({PlazaVertical.MAINTENANCE}), None,
            digest("c"), digest("d"), "https://real-provider.example/api",
        )


def test_partner_lifecycle_separates_arkaon_review_operator_and_sandbox():
    governance, partner = enabled_partner()
    assert partner.stage is PartnerStage.SANDBOX_ENABLED
    with pytest.raises(PlazaRejected):
        governance.enable_sandbox("partner-1")
    suspended = governance.suspend("partner-1", "evidence expired")
    assert suspended.stage is PartnerStage.SUSPENDED
    assert governance.offboard("partner-1", "operator:offboard:1").stage is PartnerStage.OFFBOARDED


def test_same_review_and_operator_reference_is_forbidden():
    governance = PartnerGovernance()
    governance.register(application())
    governance.submit_evidence("partner-1", digest("e"))
    governance.accept_contract_test("partner-1", digest("t"))
    governance.ethernian_review("partner-1", "same")
    with pytest.raises(PlazaRejected, match="separate"):
        governance.operator_decide("partner-1", "same")


def test_listing_never_allows_real_application_or_production_visibility():
    with pytest.raises(PlazaRejected):
        listing(real_application_allowed=True)
    with pytest.raises(PlazaRejected):
        listing(production_visible=True)
    assert visible_listing().stage is ListingStage.SANDBOX_VISIBLE


def test_comparison_score_ignores_commission_and_sponsorship_but_discloses_both():
    high_commission = visible_listing(commission_bps=900, sponsored=True)
    low_commission = visible_listing(
        listing_id="listing-2", commission_bps=0, sponsored=False, terms_digest=digest("terms-2")
    )
    factors = (ComparisonFactor("total_cost", 80, 60), ComparisonFactor("consumer_terms", 90, 40))
    results = compare_listings((ComparisonCandidate(high_commission, factors), ComparisonCandidate(low_commission, factors)))
    assert [item.score for item in results] == [84, 84]
    assert results[0].listing_id == "listing-1"
    assert results[0].commission_bps == 900 and results[0].sponsored


def test_cross_platform_sensitive_data_mixing_is_forbidden():
    with pytest.raises(PlazaRejected, match="mixing"):
        DataBoundaryRequest("NARANG_RIDER", "MJN", frozenset({"order", "credit"}), "comparison").validate()
    DataBoundaryRequest("NARANG_RIDER", "NARANG_RIDER", frozenset({"identity"}), "user-requested quote").validate()


@pytest.mark.parametrize(
    "action",
    ["APPROVE_CREDIT", "UNDERWRITE_INSURANCE", "BIND_CONTRACT", "MOVE_MONEY", "AUTO_SANCTION", "ACTIVATE_PRODUCTION"],
)
def test_arkaon_forbidden_authorities_are_rejected(action):
    with pytest.raises(PlazaRejected, match="forbidden"):
        ArkaonObservation("o", "partner-1", None, "drift", "HIGH", digest("o"), NOW, action)


def test_arkaon_monitoring_is_evidence_bound_human_review_only_and_deterministic():
    _, partner = enabled_partner()
    item = visible_listing()
    supervisor = ArkaonPlazaSupervisor()
    arguments = {
        "partner": partner,
        "listing": item,
        "authorities": (authority(),),
        "policy_digest": digest("policy"),
        "contract_test_digest": digest("test"),
        "observations": (),
        "now": NOW,
    }
    first = supervisor.assess(**arguments)
    second = supervisor.assess(**arguments)
    assert first == second
    assert first.verdict is MonitoringVerdict.PASS_FOR_HUMAN_REVIEW
    assert first.requires_human_review
    assert not first.automatic_enforcement_allowed
    assert not first.production_change_allowed

    changed_terms = supervisor.assess(**{**arguments, "listing": visible_listing(terms_digest=digest("changed"))})
    assert changed_terms.snapshot_digest != first.snapshot_digest


def test_stale_authority_or_critical_drift_blocks_without_auto_enforcement():
    _, partner = enabled_partner()
    critical = ArkaonObservation(
        "obs-1", "partner-1", "listing-1", "terms_digest_changed", "CRITICAL",
        digest("observation"), NOW, "PROPOSE_SUSPENSION",
    )
    report = ArkaonPlazaSupervisor().assess(
        partner=partner,
        listing=visible_listing(),
        authorities=(authority(expires_at=NOW),),
        policy_digest=digest("policy"),
        contract_test_digest=digest("test"),
        observations=(critical,),
        now=NOW,
    )
    assert report.verdict is MonitoringVerdict.BLOCKED
    assert not report.automatic_enforcement_allowed


def test_unapproved_legal_interpretation_stays_in_human_review():
    _, partner = enabled_partner()
    report = ArkaonPlazaSupervisor().assess(
        partner=partner,
        listing=None,
        authorities=(authority(legal_interpretation_approved=False),),
        policy_digest=digest("policy"),
        contract_test_digest=digest("test"),
        observations=(),
        now=NOW,
    )
    assert report.verdict is MonitoringVerdict.HUMAN_REVIEW
    assert report.blockers == ("legal_review_pending:law-1",)
