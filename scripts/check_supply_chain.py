"""Offline supply-chain consistency and provenance policy checker."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "supply-chain" / "components.json"
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
ACTION = re.compile(r"uses:\s*([^\s@]+)@([^\s]+)")


class SupplyChainError(ValueError):
    """A stable offline policy violation."""


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _inventory() -> tuple[dict, dict[tuple[str, str, str], dict]]:
    document = _load_json(INVENTORY)
    items = document.get("components", [])
    index = {}
    for item in items:
        key = (item["ecosystem"], item["name"].lower(), item["version"])
        if key in index:
            raise SupplyChainError(f"duplicate inventory component: {key}")
        index[key] = item
        if not item.get("source") or not item.get("license"):
            raise SupplyChainError(f"missing provenance: {key}")
    return document, index


def _python_locks(index: dict[tuple[str, str, str], dict]) -> None:
    for lock_name, scope in (
        ("requirements-runtime.lock", "runtime"),
        ("requirements-dev.lock", "development"),
    ):
        for raw in (ROOT / lock_name).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = PIN.fullmatch(line)
            if not match:
                raise SupplyChainError(f"unpinned or URL Python dependency: {line}")
            name, version = match.groups()
            item = index.get(("python", name.lower(), version))
            if item is None:
                raise SupplyChainError(f"Python dependency lacks provenance: {line}")
            if lock_name == "requirements-runtime.lock" and item["scope"] != scope:
                raise SupplyChainError(f"runtime scope mismatch: {line}")


def _npm_lock(index: dict[tuple[str, str, str], dict]) -> None:
    package = _load_json(ROOT / "frontend" / "package.json")
    lock = _load_json(ROOT / "frontend" / "package-lock.json")
    root = lock["packages"][""]
    if root.get("devDependencies", {}) != package.get("devDependencies", {}):
        raise SupplyChainError("npm manifest and lock disagree")
    for path, item in lock["packages"].items():
        if not path.startswith("node_modules/"):
            continue
        name = path.removeprefix("node_modules/")
        version = item.get("version", "")
        recorded = index.get(("npm", name.lower(), version))
        if recorded is None:
            raise SupplyChainError(f"npm dependency lacks provenance: {name}@{version}")
        resolved = item.get("resolved", "")
        if not resolved.startswith("https://registry.npmjs.org/") or not item.get("integrity"):
            raise SupplyChainError(f"npm source/integrity invalid: {name}@{version}")
        if item.get("hasInstallScript") and not recorded.get("package_scripts"):
            raise SupplyChainError(f"unreviewed npm install script: {name}@{version}")
        if recorded.get("native_binary") and not recorded.get("native_review"):
            raise SupplyChainError(f"unreviewed native binary: {name}@{version}")


def _containers_and_actions(index: dict[tuple[str, str, str], dict]) -> None:
    dockerfiles = list(ROOT.glob("Dockerfile*"))
    for path in dockerfiles:
        for image in re.findall(r"^FROM\s+([^\s]+)", path.read_text(), re.MULTILINE):
            image = image.split("@", 1)[0]
            name, _, version = image.partition(":")
            name = name.rsplit("/", 1)[-1]
            if ("container", name.lower(), version) not in index:
                raise SupplyChainError(f"container lacks provenance: {image}")
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        for name, version in ACTION.findall(workflow.read_text()):
            if ("github-action", name.lower(), version) not in index:
                raise SupplyChainError(f"action lacks provenance: {name}@{version}")


def _license_policy(document: dict) -> None:
    policy = document["policy"]
    allow = set(policy["allow"])
    review = set(policy["review"])
    deny = set(policy["deny_without_written_approval"])
    for item in document["components"]:
        license_id = item["license"]
        if license_id in deny and not item.get("release_blocker"):
            raise SupplyChainError(f"denied license is not blocked: {item['name']}")
        if license_id not in allow | review | deny | {"PostgreSQL"}:
            raise SupplyChainError(f"unclassified license: {license_id}")
        if license_id in review and not item.get("release_blocker"):
            raise SupplyChainError(f"review license missing blocker: {item['name']}")


def _vendor_and_source_scan() -> None:
    forbidden_dirs = {"vendor", "vendored", "third_party", "node_modules"}
    for path in ROOT.rglob("*"):
        if path.is_dir() and path.name.lower() in forbidden_dirs:
            raise SupplyChainError(f"vendored tree requires source record: {path.relative_to(ROOT)}")
    marker = re.compile(r"copied\s+from|source:\s*https?://", re.IGNORECASE)
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "frontend" / "src").rglob("*.ts")):
        if marker.search(path.read_text(encoding="utf-8")):
            raise SupplyChainError(f"copied-source marker requires provenance: {path.relative_to(ROOT)}")


def artifact_manifest() -> dict:
    paths = [
        "pyproject.toml",
        "requirements-runtime.lock",
        "requirements-dev.lock",
        "frontend/package.json",
        "frontend/package-lock.json",
        "Dockerfile",
        "supply-chain/components.json",
        "THIRD_PARTY_NOTICE.md",
    ]
    subjects = []
    for name in paths:
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        subjects.append({"name": name, "digest": {"sha256": digest}})
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": subjects,
        "predicate": {
            "buildDefinition": {"buildType": "NARANG_RIDER/offline-manifest/v1"},
            "runDetails": {"builder": {"id": "local-ci-untrusted"}},
            "certification": False,
            "signed": False,
        },
    }


def check() -> dict:
    document, index = _inventory()
    _python_locks(index)
    _npm_lock(index)
    _containers_and_actions(index)
    _license_policy(document)
    _vendor_and_source_scan()
    blockers = sorted(
        f"{item['ecosystem']}:{item['name']}:{item['release_blocker']}"
        for item in document["components"]
        if item.get("release_blocker")
    )
    if document.get("release_status") != ("BLOCKED" if blockers else "READY"):
        raise SupplyChainError("release status disagrees with blockers")
    result = artifact_manifest()
    result["release_status"] = document["release_status"]
    result["blockers"] = blockers
    return result


if __name__ == "__main__":
    output = check()
    destination = ROOT / "build" / "supply-chain-provenance.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(f"supply-chain inventory verified; release={output['release_status']}")
