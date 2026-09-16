from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path

from narang_rider.research_watch import ArkaonResearchWatch, ResearchCandidate, ResearchSource

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "arkaon-research-watch-evidence.json"
NOW = datetime(2026, 9, 16, tzinfo=UTC)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def normalize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def main() -> None:
    watch = ArkaonResearchWatch(frozenset({"official.synthetic.invalid"}))
    source = ResearchSource(
        "source-1", "https://official.synthetic.invalid/release", "합성 공식 발표", "합성사",
        NOW - timedelta(days=1), NOW, NOW + timedelta(days=14), digest("content"), True,
    )
    value = watch.quarantine(
        ResearchCandidate(
            "candidate-1", "DELIVERY", source, "합성 기능", "신규", "사용성 개선",
            ("개인정보",), ("합성 계약시험",),
        )
    )
    value = watch.verify_evidence(value.candidate_id, now=NOW)
    value = watch.request_eternian_review(value.candidate_id)
    value = watch.record_eternian_review(
        value.candidate_id, review_digest=digest("eternian-review"), accepted=True
    )
    payload = normalize(asdict(value))
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(encoded)
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(sha256(encoded).hexdigest() + "\n")
    if payload["automatic_learning"] or payload["production_change_allowed"]:
        raise SystemExit("research watch escaped advisory boundary")


if __name__ == "__main__":
    main()
