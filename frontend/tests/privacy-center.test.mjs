import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/privacy-center.ts", import.meta.url), "utf8");
test("subject center exposes six rights without AI denial", () => {
  for (const right of ["열람", "정정", "삭제", "처리정지", "내보내기", "이의"]) assert.match(source, new RegExp(right));
  assert.match(source, /아르카온이 거절하지 않습니다/);
});
test("ops view exposes reviewed expiring retention and human breach decision", () => {
  assert.match(source, /미리보기 → 독립 검토 → 실행/);
  assert.match(source, /무기한 일괄 보존은 허용하지 않습니다/);
  assert.match(source, /법적 결론은 자동 확정하지 않습니다/);
});
test("browser source never stores tokens or raw privacy data", () => {
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|phone|address|latitude|longitude/);
});
