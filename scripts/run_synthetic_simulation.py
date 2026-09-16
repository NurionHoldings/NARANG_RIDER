#!/usr/bin/env python3
"""Run a bounded, synthetic-only NARANG RIDER simulation."""

from __future__ import annotations

import argparse
import sys

from narang_rider.simulation import MANUAL_NATIONAL_PROFILE, SMOKE_PROFILE, run_simulation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("smoke", "manual-national"), default="smoke")
    args = parser.parse_args()
    profile = SMOKE_PROFILE if args.profile == "smoke" else MANUAL_NATIONAL_PROFILE
    report = run_simulation(profile)
    sys.stdout.write(report.to_json() + "\n")
    return 0 if report.capacity_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
