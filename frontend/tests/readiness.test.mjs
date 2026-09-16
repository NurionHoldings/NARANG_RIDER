import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/readiness.ts", import.meta.url), "utf8");
test("branch readiness screen is fail-closed and names external gates", () => {
  for (const label of ["출시 차단", "제휴사 공식 API", "지도 공급자 Sandbox", "보험 연동 Sandbox", "법률 전문가 승인", "현장 파일럿"]) assert.match(source, new RegExp(label));
});
test("screen disclaims compliance and cannot trigger merge or deployment", () => {
  assert.match(source, /법률 준수, 보안 인증 또는 제휴 인증을 의미하지 않습니다/);
  assert.match(source, /운영자 승인 전 실행할 수 없습니다/);
  assert.doesNotMatch(source, /fetch\(|POST|deploy\(|merge\(/);
});
