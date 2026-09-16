import json
from pathlib import Path

import pytest

from scripts.check_supply_chain import ROOT, SupplyChainError, artifact_manifest, check


def test_repository_supply_chain_inventory_is_consistent():
    result = check()
    assert result["release_status"] == "BLOCKED"
    assert result["blockers"]
    assert all("UNKNOWN" not in blocker for blocker in result["blockers"])


def test_artifact_manifest_is_deterministic_and_uses_sha256():
    assert artifact_manifest() == artifact_manifest()
    for subject in artifact_manifest()["subject"]:
        assert len(subject["digest"]["sha256"]) == 64


def test_provenance_is_explicitly_not_signed_or_certified():
    predicate = artifact_manifest()["predicate"]
    assert predicate["certification"] is False
    assert predicate["signed"] is False


def test_every_inventory_item_has_classification_and_source():
    inventory = json.loads((ROOT / "supply-chain/components.json").read_text())
    assert inventory["components"]
    for item in inventory["components"]:
        assert item["scope"]
        assert isinstance(item["direct"], bool)
        assert item["source"].startswith("https://") or item["source"].startswith("docker.io/")
        assert item["license"]


def test_unrecorded_python_dependency_fails_closed(tmp_path, monkeypatch):
    original = Path.read_text

    def altered(path, *args, **kwargs):
        if path == ROOT / "requirements-runtime.lock":
            return "mystery==1.0\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", altered)
    with pytest.raises(SupplyChainError, match="lacks provenance"):
        check()


def test_notice_does_not_claim_legal_approval():
    notice = (ROOT / "THIRD_PARTY_NOTICE.md").read_text()
    assert "not legal advice" in notice
    assert "does not assert compatibility or compliance" in notice
