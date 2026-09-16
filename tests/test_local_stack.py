import asyncio
import json

import pytest

from narang_rider.local_stack import STEPS, SyntheticState, app


def test_deterministic_full_synthetic_scenario() -> None:
    state = SyntheticState()
    for step in STEPS:
        payload = {"masked_media_ref": "config://proof/masked"} if step == "reported" else {}
        state.advance(step, payload)
    report = state.report()
    assert report["complete"] is True
    assert report["ledger_balanced"] is True
    assert report["outbox_delivered"] is True
    assert report["raw_pii_present"] is False
    assert len(report["evidence_digest"]) == 64


@pytest.mark.parametrize("field", ["address", "phone", "latitude", "longitude", "customer_name"])
def test_raw_pii_fails_closed(field: str) -> None:
    with pytest.raises(ValueError, match="RAW_PII_REJECTED"):
        SyntheticState().advance("submitted", {field: "synthetic-but-forbidden"})


def test_asgi_health_is_no_store() -> None:
    messages = [{"type": "http.request", "body": b"", "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "path": "/health/live", "method": "GET"}, receive, send))
    assert sent[0]["status"] == 200
    assert (b"cache-control", b"no-store") in sent[0]["headers"]
    assert json.loads(sent[1]["body"])["mode"] == "synthetic-local-only"
