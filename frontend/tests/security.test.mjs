import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

const api = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/app.ts", import.meta.url), "utf8");
const html = readdirSync(new URL("..", import.meta.url)).filter(x => x.endsWith(".html"))
  .map(x => readFileSync(new URL(`../${x}`, import.meta.url), "utf8")).join("\n");

test("session uses cookies, CSRF and no-store without browser token persistence", () => {
  assert.match(api, /credentials: "include"/);
  assert.match(api, /X-CSRF-Token/);
  assert.match(api, /cache: "no-store"/);
  assert.doesNotMatch(api + app + html, /localStorage|sessionStorage|serviceWorker/);
});

test("four semantic Korean role portals are present", () => {
  for (const role of ["merchant", "rider", "customer", "branch-ops"]) assert.match(html, new RegExp(`data-role="${role}"`));
  assert.match(html, /lang="ko"/);
  assert.match(app, /서버 권한 검증이 최종 기준/);
});

test("sensitive rendering has redaction and route guard boundaries", () => {
  assert.match(api, /연락처 보호/);
  assert.match(api, /정밀위치 보호/);
  assert.match(api, /ROUTE_GUARD_DENIED/);
});
