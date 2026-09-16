"""Validate fail-closed mobility plaza policies and emit synthetic CI evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from narang_rider.mobility_plaza import (
    ArkaonPlazaSupervisor,
    AuthorityEvidence,
    PartnerApplication,
    PartnerGovernance,
    PlazaVertical,
)

ROOT = Path(__file__).resolve().parents[1]


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def main() -> None:
    policy_path = ROOT / "config/mobility-plaza-governance.json"
    authority_path = ROOT / "config/mobility-plaza-authority-registry.json"
    policy = json.loads(policy_path.read_text())
    registry = json.loads(authority_path.read_text())
    if policy["status"] != "BLOCKED" or policy["environment"] != "SYNTHETIC_ONLY":
        raise SystemExit("mobility plaza must remain blocked and synthetic-only")
    if registry["status"] != "HUMAN_REVIEW_REQUIRED":
        raise SystemExit("authority registry cannot claim legal approval")

    now = datetime(2026, 9, 16, tzinfo=UTC)
    governance = PartnerGovernance()
    application = PartnerApplication(
        "synthetic-partner", "합성 파트너", frozenset({PlazaVertical.MAINTENANCE}), None,
        digest(b"contract"), digest(b"disclosure"), "https://partner.synthetic.invalid/api",
    )
    governance.register(application)
    governance.submit_evidence(application.partner_id, digest(b"evidence"))
    governance.accept_contract_test(application.partner_id, digest(b"contract-test"))
    governance.ethernian_review(application.partner_id, "review:synthetic")
    governance.operator_decide(application.partner_id, "operator:synthetic")
    partner = governance.enable_sandbox(application.partner_id)
    authorities = tuple(
        AuthorityEvidence(
            f"source-{index}", item["url"], item["title"], item["authority"], now,
            now + timedelta(days=30), digest(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()),
            False,
        )
        for index, item in enumerate(registry["sources"], start=1)
    )
    report = ArkaonPlazaSupervisor().assess(
        partner=partner,
        listing=None,
        authorities=authorities,
        policy_digest=digest(policy_path.read_bytes()),
        contract_test_digest=digest(b"contract-test"),
        observations=(),
        now=now,
    )
    artifact = report.as_ci_artifact()
    artifact["authority_registry_digest"] = digest(authority_path.read_bytes())
    output = ROOT / "build/mobility-plaza-governance.json"
    output.parent.mkdir(exist_ok=True)
    payload = json.dumps(artifact, ensure_ascii=False, sort_keys=True, default=str, indent=2) + "\n"
    output.write_text(payload)
    (output.with_suffix(".json.sha256")).write_text(digest(payload.encode()) + "\n")


if __name__ == "__main__":
    main()
