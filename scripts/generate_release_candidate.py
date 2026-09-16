#!/usr/bin/env python3
"""Verify traceability and emit a deterministic blocked RC report."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from narang_rider.release_candidate import (
    EvidenceItem,
    EvidenceStatus,
    ReleaseCandidateEvaluator,
    ReleaseRequirement,
    TraceabilityVerifier,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--traceability", default="release/traceability.json")
    parser.add_argument("--evidence", default="release/evidence.json")
    parser.add_argument("--output", default="build/release-candidate-report.json")
    args = parser.parse_args()
    trace_data = json.loads(Path(args.traceability).read_text())
    evidence_data = json.loads(Path(args.evidence).read_text())
    requirements = [ReleaseRequirement(
        item["id"], item["title"], tuple(item["domain"]), tuple(item["api"]), tuple(item["db"]),
        tuple(item["frontend"]), tuple(item["tests"]), tuple(item["threat_controls"]))
        for item in trace_data["requirements"]]
    trace = TraceabilityVerifier.verify(requirements, known_refs=trace_data["known_refs"])
    if not trace.complete:
        raise SystemExit(f"traceability incomplete: missing={trace.missing_coverage} orphan={trace.orphan_refs}")
    evidence = [EvidenceItem(item["id"], item["category"], item.get("digest"),
                             EvidenceStatus(item["status"]), reference=item.get("reference"))
                for item in evidence_data["evidence"]]
    candidate = ReleaseCandidateEvaluator().evaluate(version=trace_data["release"],
        commit_sha=os.getenv("GITHUB_SHA", "LOCAL_SYNTHETIC_HEAD"), traceability=trace,
        evidence=evidence, now=datetime.now(UTC))
    output = {
        "version": candidate.version,
        "commit_sha": candidate.commit_sha,
        "verdict": candidate.verdict.value,
        "blockers": candidate.blockers,
        "limitations": candidate.limitations,
        "compliance_claim": candidate.compliance_claim,
        "traceability_digest": candidate.traceability_digest,
        "evidence_digest": candidate.evidence_digest,
        "requirements": trace.requirement_count,
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    if candidate.verdict.value != "BLOCKED":
        raise SystemExit("pre-production RC must remain BLOCKED until external approvals are supplied")
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
