from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict
from enum import Enum
from hashlib import sha256
from pathlib import Path

from narang_rider.contributor_guidance import ArkaonContributorGuide

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "arkaon-contributor-guidance.json"


def changed_paths(base: str) -> tuple[str, ...]:
    if not base or set(base) == {"0"}:
        base = "HEAD^"
    verify = subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if verify.returncode != 0:
        base = "HEAD^"
    command = ["git", "diff", "--name-only", "--diff-filter=ACMRT", f"{base}...HEAD"]
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return tuple(line for line in result.stdout.splitlines() if line)


def normalize(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def markdown(payload: dict[str, object]) -> str:
    lines = ["## ARKAON 사전 보완 가이드", ""]
    lines.append(f"필수 미해결 항목: **{payload['required_open_count']}개**")
    lines.append("")
    items = payload["items"]
    if not items:
        lines.append("경로별 추가 안내가 없습니다. 공통 안전기준과 PR 체크리스트는 계속 적용됩니다.")
    for item in items:
        mark = "✅" if item["satisfied"] else "⚠️"
        lines.extend(
            [
                f"- {mark} **{item['title']}** (`{item['rule_id']}`)",
                f"  - 담당: `{item['owner_role']}`",
                f"  - 안내: {item['instruction']}",
            ]
        )
    lines.extend(
        [
            "",
            "> 이 보고서는 안내용입니다. ARKAON은 승인·병합·배포하거나 실패 기준을 완화할 수 없습니다.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="main")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    paths = tuple(args.paths) or changed_paths(args.base)
    report = ArkaonContributorGuide().analyze(paths)
    payload = normalize(asdict(report))
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(encoded)
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(sha256(encoded).hexdigest() + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(markdown(payload))
    else:
        print(markdown(payload), end="")


if __name__ == "__main__":
    main()
