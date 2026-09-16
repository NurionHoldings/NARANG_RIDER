#!/usr/bin/env python3
"""API-free stacked-branch ancestry and declared open-PR inventory verifier."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", default="release/stack-inventory.json")
    args = parser.parse_args()
    data = json.loads(Path(args.inventory).read_text())
    failures = []
    previous = None
    for item in data["pull_requests"]:
        if item["state"] != "open" or item["merged"]:
            failures.append(f"PR #{item['number']} inventory must remain open and unmerged")
        ref = f"origin/{item['branch']}"
        if git("rev-parse", "--verify", ref).returncode:
            failures.append(f"missing fetched ref: {ref}")
        if previous and git("merge-base", "--is-ancestor", previous, ref).returncode:
            failures.append(f"stack ancestry mismatch: {previous} !< {ref}")
        previous = ref
    if failures:
        raise SystemExit("\n".join(failures))
    print(json.dumps({"verified": len(data["pull_requests"]), "mode": "api-free-declared-inventory"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
