"""Deterministic offline security checks used by CI."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from narang_rider.release_security import migration_inventory


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    migration_inventory(ROOT / "migrations")
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    if lock.get("lockfileVersion") != 3 or not lock.get("packages", {}).get("node_modules/typescript", {}).get("integrity"):
        fail("frontend dependency lock is incomplete")
    requirements = (ROOT / "requirements-dev.lock").read_text().splitlines()
    if any(line and not line.startswith("#") and "==" not in line for line in requirements):
        fail("Python development dependencies must be exactly pinned")
    docker = (ROOT / "Dockerfile").read_text()
    for marker in (" AS builder", "USER 10001:10001", "COPY --from=builder"):
        if marker not in docker:
            fail(f"container hardening marker missing: {marker}")
    secret_pattern = re.compile(r"(?i)(password|client_secret|private_key|access_token)\s*[:=]\s*['\"][^'\"]{8,}")
    for base in (ROOT / "src", ROOT / "frontend" / "src"):
        for path in base.rglob("*"):
            if path.is_file() and secret_pattern.search(path.read_text(errors="ignore")):
                fail(f"possible embedded secret: {path.relative_to(ROOT)}")
    frontend = "\n".join(path.read_text() for path in (ROOT / "frontend").glob("*.html"))
    frontend += (ROOT / "frontend" / "src" / "app.ts").read_text()
    if re.search(r"\beval\s*\(|new\s+Function\s*\(", frontend):
        fail("CSP-incompatible dynamic JavaScript")
    print("offline security checks passed")


if __name__ == "__main__":
    main()
