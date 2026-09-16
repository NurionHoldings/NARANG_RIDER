"""Render operator checklists; this command never contacts a provider."""

from __future__ import annotations

import argparse
import json

from narang_rider.external_intake import ProviderKind, operator_checklist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=[kind.value for kind in ProviderKind] + ["all"],
        default="all",
    )
    args = parser.parse_args()
    kinds = list(ProviderKind) if args.provider == "all" else [ProviderKind(args.provider)]
    print(json.dumps([operator_checklist(kind) for kind in kinds], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
