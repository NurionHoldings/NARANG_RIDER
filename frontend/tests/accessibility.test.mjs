import assert from "node:assert/strict";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../src/app.ts", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const ux = readFileSync(new URL("../src/korean-ux.ts", import.meta.url), "utf8");

test("deterministic portal accessibility audit passes offline", () => {
  const output = execFileSync(process.execPath, ["scripts/accessibility-audit.mjs"], {
    cwd: new URL("..", import.meta.url), encoding: "utf8",
  });
  assert.match(output, /PASS/);
});

test("mandatory customer language is exact and rights stay independent", () => {
  assert.match(ux, /개봉 전 외관 이상이 있으면 먼저 신고해 주세요\./);
  assert.match(app, /안내 확인 여부나 선택 동의 거부만으로 환불·이의제기·배차·보수 권리를 제한하지 않습니다/);
});

test("consent starts unticked and dangerous work needs explicit confirmation", () => {
  assert.doesNotMatch(app, /checked(?:=|\s)/);
  assert.match(app, /type="checkbox" required/);
  assert.match(app, /class="danger"/);
});

test("reflow touch focus motion and non-color states are explicit", () => {
  for (const token of ["min-height:44px", "max-width:20rem", "prefers-reduced-motion:reduce", "overflow-wrap:anywhere", "오류: ", "상태: "]) assert.match(css, new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
});
