import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const api = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/app.ts", import.meta.url), "utf8");
const flows = readFileSync(new URL("../src/workflows.ts", import.meta.url), "utf8");

test("all four operational journeys are wired to typed routes", () => {
  for (const label of ["주문 초안 저장", "공개견적 확인", "배차 수락", "픽업 완료", "마스킹 배송사진 확인", "외관 이상 신고", "준비상태 확인", "정산 독립 승인", "Sandbox 보고서"]) assert.match(flows, new RegExp(label));
});

test("active form idempotency and double-click control stay memory-only", () => {
  assert.match(flows, /private idempotencyKey: string \| null/);
  assert.match(flows, /if \(this\.pending\)/);
  assert.match(flows, /crypto\.randomUUID/);
  assert.doesNotMatch(api + app + flows, /localStorage|sessionStorage|indexedDB|CacheStorage/);
});

test("offline retry is explicit and optimistic conflict refreshes", () => {
  assert.match(flows, /retryExplicitly/);
  assert.match(flows, /자동 재전송하지 않습니다/);
  assert.match(flows, /status === 409\) await this\.refresh/);
});

test("safe Korean messages cover auth validation conflict and throttling", () => {
  for (const status of ["401", "403", "409", "422", "429"]) assert.match(flows, new RegExp(`${status}:`));
  assert.doesNotMatch(flows, /error\.stack|JSON\.stringify\(error\)/);
});

test("DOM contract has labels errors live regions focus and retry control", () => {
  assert.match(app, /aria-live="polite"/);
  assert.match(app, /role="alert"/);
  assert.match(app, /aria-describedby="form-help form-error"/);
  assert.match(app, /다시 시도/);
  assert.match(app, /\.focus\(\)/);
});

test("production factory has no demo session or token bypass", () => {
  assert.match(app, /ServerSession/);
  assert.doesNotMatch(app, /DemoSession|demo-token|session-bound-demo/);
});
