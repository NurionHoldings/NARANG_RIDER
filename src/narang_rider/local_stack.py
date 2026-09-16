"""Synthetic-only ASGI application used by the reproducible local stack."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

SYNTHETIC_ORDER = "syn-order-0001"
STEPS = ("submitted", "offered", "accepted", "picked_up", "delivered", "reported", "settled", "notified")
FORBIDDEN = ("address", "phone", "latitude", "longitude", "customer_name")


@dataclass
class SyntheticState:
    step: int = -1
    audit: list[str] = field(default_factory=list)
    outbox_delivered: bool = False

    def advance(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in STEPS:
            raise ValueError("UNKNOWN_ACTION")
        if any(field in payload for field in FORBIDDEN):
            raise ValueError("RAW_PII_REJECTED")
        expected = self.step + 1
        if STEPS[expected] != action:
            raise ValueError("INVALID_TRANSITION")
        if action == "reported" and not str(payload.get("masked_media_ref", "")).startswith("config://"):
            raise ValueError("MASKED_PROOF_REQUIRED")
        self.step = expected
        digest = hashlib.sha256(f"{SYNTHETIC_ORDER}:{action}".encode()).hexdigest()
        self.audit.append(digest)
        self.outbox_delivered = action == "notified"
        return {"order_id": SYNTHETIC_ORDER, "state": action, "audit_digest": digest}

    def report(self) -> dict[str, Any]:
        return {
            "scenario": SYNTHETIC_ORDER,
            "complete": self.step == len(STEPS) - 1,
            "audit_count": len(self.audit),
            "ledger_balanced": self.step >= STEPS.index("settled"),
            "outbox_delivered": self.outbox_delivered,
            "raw_pii_present": False,
            "evidence_digest": hashlib.sha256("".join(self.audit).encode()).hexdigest(),
        }


STATE = SyntheticState()
MAX_SYNTHETIC_BODY_BYTES = 65_536


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes) or len(body) + len(chunk) > MAX_SYNTHETIC_BODY_BYTES:
            result = {"error": "PAYLOAD_TOO_LARGE"}
            encoded = json.dumps(result, separators=(",", ":")).encode()
            await send({"type": "http.response.start", "status": 413, "headers": [
                (b"content-type", b"application/json"), (b"cache-control", b"no-store")
            ]})
            await send({"type": "http.response.body", "body": encoded})
            return
        body += chunk
        if not message.get("more_body"):
            break
    path = scope.get("path", "")
    method = scope.get("method", "GET")
    status = 200
    try:
        payload = json.loads(body or b"{}")
        if path in {"/health/live", "/health/ready"}:
            result = {"status": "ok", "mode": "synthetic-local-only"}
        elif path == "/mock/token" and method == "POST":
            result = {"access_token": "synthetic.unsigned.local", "issuer": "config://mock-oidc"}
        elif path.startswith("/scenario/") and method == "POST":
            result = STATE.advance(path.rsplit("/", 1)[-1], payload)
        elif path == "/scenario/report":
            result = STATE.report()
        elif path.startswith("/mock/"):
            result = {"provider": path.split("/")[2], "reference": "config://synthetic/mock"}
        else:
            status, result = 404, {"error": "NOT_FOUND"}
    except (ValueError, json.JSONDecodeError) as error:
        status, result = 409, {"error": str(error)}
    encoded = json.dumps(result, separators=(",", ":")).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": encoded})


async def worker() -> None:
    while True:
        await asyncio.sleep(5)


def main() -> None:
    if os.environ.get("NARANG_ENV") != "local-synthetic":
        raise SystemExit("LOCAL_STACK_PRODUCTION_GUARD")
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker())
        return
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
