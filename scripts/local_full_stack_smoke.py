"""Drive the local HTTP stack end to end and emit a redacted report."""

from __future__ import annotations

import argparse
import json
import urllib.request


def request(base: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--output", default="local-full-stack-report.json")
    args = parser.parse_args()
    token = request(args.base_url, "/mock/token", {})
    if token.get("issuer") != "config://mock-oidc":
        raise SystemExit("mock login failed")
    actions = ("submitted", "offered", "accepted", "picked_up", "delivered", "reported", "settled", "notified")
    for action in actions:
        payload = {"masked_media_ref": "config://proof/masked"} if action == "reported" else {}
        request(args.base_url, f"/scenario/{action}", payload)
    report = request(args.base_url, "/scenario/report")
    required = {"complete": True, "ledger_balanced": True, "outbox_delivered": True, "raw_pii_present": False}
    if any(report.get(key) != value for key, value in required.items()):
        raise SystemExit("full stack evidence failed")
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True, indent=2)
    print(json.dumps({"verdict": "PASS", "evidence_digest": report["evidence_digest"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
