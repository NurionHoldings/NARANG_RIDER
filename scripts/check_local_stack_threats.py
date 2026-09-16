"""Static guard for accidental production-like local-stack configuration."""

from __future__ import annotations

from pathlib import Path


def main() -> int:
    text = Path("compose.local.yml").read_text(encoding="utf-8")
    forbidden = ("0.0.0.0:", "DEBUG=true", "latest", "${HOME}", "/var/run/docker.sock")
    found = [value for value in forbidden if value in text]
    required = ("NARANG_ENV: local-synthetic", "read_only: true", "internal: true", "127.0.0.1:18000")
    missing = [value for value in required if value not in text]
    if found or missing:
        raise SystemExit(f"local stack threat check failed: found={found}, missing={missing}")
    print("local stack threat checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
