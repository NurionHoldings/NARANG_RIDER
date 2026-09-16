"""Verify the review-only rollup without granting merge or release authority."""

from __future__ import annotations

import argparse
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
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    failures: list[str] = []

    expected_prs = list(range(1, 47))
    if manifest["included_pull_requests"] != expected_prs:
        failures.append("included PRs must be the contiguous range 1..46")
    if manifest["version"] != "0.1.0-rc.2" or manifest["purpose"] != "REVIEW_ONLY":
        failures.append("rollup identity or purpose mismatch")
    if manifest["release_verdict"] != "BLOCKED":
        failures.append("rollup release verdict must remain BLOCKED")
    if not all(
        manifest[field]
        for field in ("operator_approval_required", "auto_merge_forbidden", "deployment_forbidden")
    ):
        failures.append("operator/merge/deployment safety locks are required")

    main_commit = manifest["expected_initial_main_commit"]
    source_head = manifest["source_head"]
    if git("cat-file", "-e", f"{main_commit}^{{commit}}").returncode:
        failures.append("expected initial main commit is unavailable")
    if git("cat-file", "-e", f"{source_head}^{{commit}}").returncode:
        failures.append("declared source head is unavailable")
    if git("merge-base", "--is-ancestor", main_commit, source_head).returncode:
        failures.append("source head does not descend from expected initial main")
    if git("merge-base", "--is-ancestor", source_head, args.head).returncode:
        failures.append("rollup HEAD does not include declared source head")
    merge_commits = git("rev-list", "--merges", f"{main_commit}..{args.head}")
    if merge_commits.returncode or merge_commits.stdout.strip():
        failures.append("rollup history contains merge commits or cannot be inspected")

    evidence_by_category = {item["category"]: item["status"] for item in evidence["evidence"]}
    for blocker in manifest["blockers"]:
        if evidence_by_category.get(blocker) not in {"missing", "expired", "fail"}:
            failures.append(f"blocker unexpectedly satisfied or absent: {blocker}")

    report = {
        "version": manifest["version"],
        "purpose": manifest["purpose"],
        "source_head": source_head,
        "expected_main": main_commit,
        "included_pr_count": len(manifest["included_pull_requests"]),
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
