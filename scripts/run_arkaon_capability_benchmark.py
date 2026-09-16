"""Emit deterministic synthetic ARKAON capability evidence for CI."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from narang_rider.arkaon_capability import (
    BenchmarkAnswer,
    BenchmarkCase,
    CapabilityBenchmark,
    CapabilityDomain,
    CapabilityProfile,
    MetricThresholds,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    raw = json.loads((ROOT / "config/arkaon-capability-profile.json").read_text())
    profile = CapabilityProfile(
        raw["profile_id"], raw["version"], frozenset(CapabilityDomain),
        MetricThresholds(**raw["thresholds"]), frozenset(raw["reusable_platforms"]),
        raw["synthetic_only"], raw["shared_data_allowed"], raw["production_status"],
    )
    cases = tuple(
        BenchmarkCase(
            f"synthetic-{domain.value}", domain, "NARANG_RIDER", frozenset({"expected"}),
            sha256(f"synthetic:{domain.value}".encode()).hexdigest(),
            regression_present=domain is CapabilityDomain.REGRESSION_BREAKING_CHANGE,
        )
        for domain in CapabilityDomain
    )
    answers = tuple(
        BenchmarkAnswer(
            case.case_id, frozenset({"expected"}), frozenset({"expected"}),
            detected_regression=case.regression_present,
        )
        for case in cases
    )
    report = CapabilityBenchmark().evaluate(profile=profile, cases=cases, answers=answers)
    if not report.passed:
        raise SystemExit("synthetic capability benchmark failed")
    output = ROOT / "build/arkaon-capability-benchmark.json"
    output.parent.mkdir(exist_ok=True)
    payload = json.dumps(report.as_ci_artifact(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.write_text(payload)
    (output.with_suffix(".json.sha256")).write_text(sha256(payload.encode()).hexdigest() + "\n")


if __name__ == "__main__":
    main()
