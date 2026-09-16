#!/usr/bin/env python3
"""Generate a non-sensitive weekly blocker snapshot."""
from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path
from scripts.validate_external_blockers import load_registry, registry_digest, validate_registry

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--registry", default="config/external-blockers.json")
    parser.add_argument("--output", default="artifacts/external-blockers-weekly.json")
    args=parser.parse_args()
    data=load_registry(args.registry); validate_registry(data)
    output={"generated_at":datetime.now(timezone.utc).isoformat(),"release_gate":data["release_gate"],"registry_digest":registry_digest(data),
      "blockers":[{"id":b["id"],"title":b["title"],"owner_role":b["owner_role"],"status":b["status"],"risk":b["risk"],"dependencies":b["dependencies"],"evidence_expires_at":b["evidence_expires_at"]} for b in data["blockers"]]}
    target=Path(args.output); target.parent.mkdir(parents=True,exist_ok=True); target.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return 0
if __name__=="__main__": raise SystemExit(main())
