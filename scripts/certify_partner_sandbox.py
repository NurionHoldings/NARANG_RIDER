"""Emit deterministic offline partner certification reports."""

from __future__ import annotations

import argparse
import json

from narang_rider.partner_sandbox import PartnerKind, certify_all, default_profiles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=[kind.value for kind in PartnerKind])
    args = parser.parse_args()
    profiles = default_profiles()
    if args.profile:
        profiles = tuple(profile for profile in profiles if profile.kind.value == args.profile)
    reports = certify_all(profiles)
    print(json.dumps([report.as_dict() for report in reports], ensure_ascii=False, indent=2))
    # BLOCKED is expected for named provisional profiles; only contract failures fail CI.
    return 1 if any(report.verdict.value == "FAIL" for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
