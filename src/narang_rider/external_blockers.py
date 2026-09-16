"""Validate external release blockers without reading external evidence bodies."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ALLOWED = {"PENDING", "IN_PROGRESS", "BLOCKED", "VERIFIED"}
REF_RE = re.compile(r"^(vault-ref|evidence-ref|sha256):[A-Za-z0-9._:/-]+$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FORBIDDEN_KEYS = {"secret", "token", "password", "api_key", "private_key", "phone", "address", "resident_id", "email"}
REQUIRED_IDS = [f"EXT-{n:02d}" for n in range(1, 8)]

class BlockerValidationError(ValueError):
    pass

def load_registry(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

def validate_registry(data: dict[str, Any], *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    blockers = data.get("blockers", [])
    ids = [item.get("id") for item in blockers]
    if ids != REQUIRED_IDS:
        raise BlockerValidationError("external blocker IDs/order must be EXT-01..EXT-07")
    by_id = {item["id"]: item for item in blockers}
    if data.get("release_gate") != "BLOCKED":
        raise BlockerValidationError("release gate must remain BLOCKED until operator release procedure")
    for item in blockers:
        _reject_sensitive_keys(item)
        status = item.get("status")
        if status not in ALLOWED:
            raise BlockerValidationError(f"{item['id']}: invalid status")
        for dep in item.get("dependencies", []):
            if dep not in by_id or by_id[dep]["sequence"] >= item["sequence"]:
                raise BlockerValidationError(f"{item['id']}: invalid dependency {dep}")
            if status == "VERIFIED" and by_id[dep]["status"] != "VERIFIED":
                raise BlockerValidationError(f"{item['id']}: dependency {dep} not VERIFIED")
        refs = item.get("evidence_refs", [])
        if any(not isinstance(ref, str) or not REF_RE.fullmatch(ref) for ref in refs):
            raise BlockerValidationError(f"{item['id']}: unsafe evidence reference")
        if status == "VERIFIED":
            _validate_verified(item, now)
        elif item.get("evidence_digest") and not DIGEST_RE.fullmatch(item["evidence_digest"]):
            raise BlockerValidationError(f"{item['id']}: invalid digest")

def _validate_verified(item: dict[str, Any], now: datetime) -> None:
    required = ("evidence_refs", "evidence_digest", "evidence_generated_at", "evidence_expires_at", "ethernian_review_ref")
    if any(not item.get(key) for key in required):
        raise BlockerValidationError(f"{item['id']}: VERIFIED without complete evidence")
    if not DIGEST_RE.fullmatch(item["evidence_digest"]):
        raise BlockerValidationError(f"{item['id']}: invalid evidence digest")
    if not REF_RE.fullmatch(item["ethernian_review_ref"]):
        raise BlockerValidationError(f"{item['id']}: unsafe Ethernian review reference")
    expiry = datetime.fromisoformat(item["evidence_expires_at"])
    generated = datetime.fromisoformat(item["evidence_generated_at"])
    if expiry <= generated or expiry <= now:
        raise BlockerValidationError(f"{item['id']}: evidence expired")
    if item.get("operator_required") and not item.get("operator_approval_ref"):
        raise BlockerValidationError(f"{item['id']}: operator approval required")
    if item.get("operator_approval_ref") and not REF_RE.fullmatch(item["operator_approval_ref"]):
        raise BlockerValidationError(f"{item['id']}: unsafe operator approval reference")

def _reject_sensitive_keys(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in FORBIDDEN_KEYS:
                raise BlockerValidationError(f"forbidden sensitive field: {path}{key}")
            _reject_sensitive_keys(child, f"{path}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}{index}.")

def registry_digest(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()

