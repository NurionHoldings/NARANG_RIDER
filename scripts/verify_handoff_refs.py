from pathlib import Path

from narang_rider.handoff import RUNBOOKS, verify_handoff_references


def main() -> None:
    missing = verify_handoff_references(Path("."))
    if missing:
        raise SystemExit("stale handoff references: " + ", ".join(missing))
    if len(RUNBOOKS) != 10:
        raise SystemExit("handoff scenario inventory changed without review")
    print(f"handoff references verified: {len(RUNBOOKS)} runbooks")


if __name__ == "__main__":
    main()
