from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path

from narang_rider.contract_lifecycle import ContractLifecycleService, EntitlementMandate
from narang_rider.electronic_contract import ContractParty, ContractStage, ElectronicContract

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "contract-lifecycle-evidence.json"


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def party(party_id: str) -> ContractParty:
    return ContractParty(
        party_id, f"synthetic-{party_id}", f"registry:{party_id}", f"signer:{party_id}",
        digest(f"authority:{party_id}"),
    )


def normalize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return [normalize(item) for item in sorted(value)]
    if isinstance(value, tuple):
        return [normalize(item) for item in value]
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    return value


def main() -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    contract = ElectronicContract(
        "synthetic-contract", "template", "1", digest("template"), party("company"),
        party("partner"), (("scope", "synthetic"),), (("scope", "synthetic"),),
        digest("document"), now - timedelta(days=1), now + timedelta(days=365),
        stage=ContractStage.AUTO_EXECUTED, execution_digest=digest("execution"),
    )
    mandate = EntitlementMandate(
        "synthetic-mandate", contract.contract_id, contract.execution_digest or "",
        contract.partner.party_id, "MOTORCYCLE_MARKET",
        frozenset({"CONTRACT_READ", "LISTING_WRITE", "SUPPORT_REQUEST"}),
        "operator:synthetic", now - timedelta(minutes=1), now + timedelta(days=30),
    )
    service = ContractLifecycleService()
    lifecycle = service.activate(
        lifecycle_id="synthetic-lifecycle", contract=contract, mandate=mandate,
        starts_at=now, ends_at=now + timedelta(days=180),
    )
    bundle = service.evidence_bundle(lifecycle.lifecycle_id)
    payload = normalize(
        {
            **bundle,
            "lifecycle": asdict(lifecycle),
            "audit": [asdict(event) for event in service.audit],
        }
    )
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(encoded)
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(sha256(encoded).hexdigest() + "\n")


if __name__ == "__main__":
    main()
