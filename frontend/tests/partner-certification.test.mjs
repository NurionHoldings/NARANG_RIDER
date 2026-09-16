import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/partnerCertification.ts", import.meta.url), "utf8");

test("admin report discloses provisional offline evidence", () => {
  assert.match(source, /제휴사 Sandbox 적합성/);
  assert.match(source, /실제 제휴사 인증·운영 연결을 의미하지 않습니다/);
  assert.match(source, /live certified 표시는 항상 거짓/);
});

test("admin report renders only evidence digest, not payload", () => {
  assert.match(source, /evidenceDigest/);
  assert.doesNotMatch(source, /rawPayload|requestPayload|secretValue/);
});
