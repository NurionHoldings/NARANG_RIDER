"""Verify the review-backed critical decision inventory for #054.

This is intentionally not a numeric coverage calculator. coverage.py is not a
pinned project dependency, so the audit records executable decision evidence,
source hashes, and explicitly reviewed deferrals without inventing percentages.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "audit" / "coverage-gap-report.json"
VALID_DISPOSITIONS = {"covered", "deferred-reviewed"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def main() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    if report.get("method") != "static-critical-decision-inventory":
        raise SystemExit("coverage gap audit method is missing or misleading")
    if report.get("numeric_coverage_claimed") is not False:
        raise SystemExit("numeric coverage must not be claimed without pinned coverage.py")

    modules = report.get("critical_modules", [])
    if not modules:
        raise SystemExit("critical module inventory is empty")
    for module in modules:
        path = ROOT / module["path"]
        if digest(path) != module["sha256"]:
            raise SystemExit(f"stale critical source hash: {module['path']}")
        gaps = module.get("decisions", [])
        if not gaps:
            raise SystemExit(f"critical module has no decision inventory: {module['path']}")
        for decision in gaps:
            disposition = decision.get("disposition")
            if disposition not in VALID_DISPOSITIONS:
                raise SystemExit(f"unreviewed critical gap: {module['path']}:{decision.get('id')}")
            if disposition == "covered":
                evidence = decision.get("evidence", [])
                if not evidence:
                    raise SystemExit(f"covered decision lacks evidence: {decision['id']}")
                for item in evidence:
                    test_path_text, separator, function = item.partition("::")
                    if not separator or function not in test_functions(ROOT / test_path_text):
                        raise SystemExit(f"missing test evidence: {item}")
            elif not decision.get("rationale"):
                raise SystemExit(f"reviewed deferral lacks rationale: {decision['id']}")

    if report.get("unreviewed_critical_gaps") != 0:
        raise SystemExit("critical gap inventory must contain zero unreviewed gaps")
    print(f"coverage gap inventory valid: {len(modules)} critical modules, 0 unreviewed gaps")


if __name__ == "__main__":
    main()
