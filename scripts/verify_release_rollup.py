"""Verify the rc.10 review-only rollup without granting release authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="release/rollup-manifest.json")
    parser.add_argument("--evidence", default="release/evidence.json")
    parser.add_argument("--output", default="build/release-rollup-report.json")
    parser.add_argument("--actions", default="release/operator-next-actions.json")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    actions = json.loads(Path(args.actions).read_text(encoding="utf-8"))
    failures: list[str] = []

    expected_prs = [*range(1, 64), 72, 73, 75, 76, 78, 79, 80, 81, 82, 83]
    if manifest["included_pull_requests"] != expected_prs:
        failures.append("included PR lineage mismatch for rc.10")
    if manifest["version"] != "0.1.0-rc.10" or manifest["purpose"] != "REVIEW_ONLY":
        failures.append("rollup identity or purpose mismatch")
    if manifest["release_verdict"] != "BLOCKED":
        failures.append("rollup verdict must remain BLOCKED")
    if not all(
        manifest[field]
        for field in ("operator_approval_required", "auto_merge_forbidden", "deployment_forbidden")
    ):
        failures.append("operator, merge, and deployment locks are required")

    main_commit = manifest["expected_initial_main_commit"]
    source_head = manifest["source_head"]
    if git("cat-file", "-e", f"{main_commit}^{{commit}}").returncode:
        failures.append("expected initial main commit unavailable")
    if git("cat-file", "-e", f"{source_head}^{{commit}}").returncode:
        failures.append("declared source head unavailable")
    if git("merge-base", "--is-ancestor", main_commit, source_head).returncode:
        failures.append("source does not descend from initial main")
    if git("merge-base", "--is-ancestor", source_head, args.head).returncode:
        failures.append("rollup HEAD does not include source head")
    merges = git("rev-list", "--merges", f"{main_commit}..{args.head}")
    if merges.returncode or merges.stdout.strip():
        failures.append("rollup history contains merge commits")
    commit_count = git("rev-list", "--count", f"{main_commit}..{args.head}")
    if commit_count.returncode or int(commit_count.stdout.strip() or "0") < 73:
        failures.append("rollup history is unexpectedly short")

    expected_mapping = {
        "065": 72, "066": 73, "068": 75, "069": 76,
        "071": 78, "072": 79, "073": 80, "074": 81, "075": 82, "076": 83,
    }
    mapping = manifest.get("feature_pr_mapping", {})
    for feature, pull_request in expected_mapping.items():
        if mapping.get(feature, {}).get("pull_request") != pull_request:
            failures.append(f"feature #{feature} must map to PR #{pull_request}")
    if manifest.get("external_blocker_issue", {}).get("issue") != 65:
        failures.append("map external blockers must remain tied to issue #65")

    artifact_hashes = {
        "critical_coverage_gap_inventory": "audit/coverage-gap-report.json",
        "deterministic_openapi_contract": "api/openapi.json",
        "accessibility_independent_audit": "docs/25-korean-ux-accessibility-audit.md",
        "migration_recovery_drill": "build/migration-recovery-drill.json",
        "external_provider_intake_packet": "docs/28-external-provider-sandbox-intake.md",
        "professional_review_packet": "docs/29-professional-independent-review-packet.md",
        "map_knowledge_contract": "src/narang_rider/map_integration.py",
        "mobile_navigation_synthetic_matrix": "src/narang_rider/mobile_navigation.py",
        "route_choice_safety_cost": "src/narang_rider/route_choice.py",
        "arkaon_map_competency": "src/narang_rider/map_competency.py",
        "map_document_monitor": "src/narang_rider/map_document_monitor.py",
        "map_sandbox_contract": "src/narang_rider/map_sandbox_contract.py",
        "device_certification_evidence": "src/narang_rider/device_certification_evidence.py",
        "route_quality_shadow": "src/narang_rider/route_quality_shadow.py",
        "map_provider_resilience": "src/narang_rider/map_provider_resilience.py",
        "map_release_gate": "src/narang_rider/map_release_gate.py",
    }
    evidence_items = {item["category"]: item for item in evidence["evidence"]}
    for category, path in artifact_hashes.items():
        item = evidence_items.get(category)
        if item is None or item.get("status") != "pass":
            failures.append(f"required evidence is not passing: {category}")
            continue
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if item.get("digest") != actual:
            failures.append(f"evidence digest is stale: {category}")

    for blocker in manifest["blockers"]:
        if evidence_items.get(blocker, {}).get("status") not in {"missing", "expired", "fail"}:
            failures.append(f"blocker unexpectedly satisfied or absent: {blocker}")

    action_rows = actions.get("actions", [])
    required_fields = {
        "order", "id", "title", "owner", "status", "evidence", "expiry", "blocker"
    }
    if actions.get("release") != manifest["version"] or actions.get("verdict") != "BLOCKED":
        failures.append("operator actions identity or verdict mismatch")
    if len(action_rows) != 7 or [row.get("order") for row in action_rows] != list(range(1, 8)):
        failures.append("operator actions must remain the ordered seven-step plan")
    for row in action_rows:
        if not required_fields.issubset(row) or row.get("status") != "PENDING":
            failures.append("operator action is incomplete or not PENDING")

    report = {
        "version": manifest["version"],
        "purpose": manifest["purpose"],
        "source_head": source_head,
        "expected_main": main_commit,
        "included_pr_count": len(manifest["included_pull_requests"]),
        "history_commit_count": int(commit_count.stdout.strip() or "0"),
        "release_verdict": "BLOCKED",
        "operator_approval_required": True,
        "failures": failures,
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
