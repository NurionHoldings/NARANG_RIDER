"""Validate the external release blocker registry."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from narang_rider.external_blockers import load_registry, registry_digest, validate_registry

if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "config/external-blockers.json")
    document = load_registry(target)
    validate_registry(document)
    print(registry_digest(document))
