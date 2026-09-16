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

test("rider portal explains incident protection boundaries", () => {
  assert.match(app, /사고·보험 보호/);
  assert.match(app, /거절 불이익은 없습니다/);
  assert.match(app, /미분쟁 수익은 계속 지급 대상/);
  assert.match(app, /사진만으로 과실·보상 거절을 확정하지 않습니다/);
});

test("notification center keeps lockscreen payload minimal and rights intact", () => {
  assert.match(app, /알림센터/);
  assert.match(app, /잠금화면에는 주소·정밀위치·라이더 정보·주문내용을 표시하지 않습니다/);
  assert.match(app, /환불·이의제기 등 권리를 제한하지 않습니다/);
});

test("map-free route status protects location and ARKAON advice boundaries", () => {
  assert.match(app, /정밀위치나 라이더 정보 없이 대략적인 진행상태만 확인합니다/);
  assert.match(app, /주소와 정밀위치는 표시하거나 저장하지 않습니다/);
  assert.match(app, /보수에 불리하지 않은 보수적 공개견적/);
  assert.match(app, /배차 배제·보수 삭감·제재의 근거가 아닙니다/);
  assert.match(app, /우회 주행만으로 과실을 판단하지 않습니다/);
});

test("operations labels synthetic simulation as non-production evidence", () => {
  assert.match(app, /합성 검증 보고서/);
  assert.match(app, /실데이터가 아닌 부하·경제성·악용 가설/);
});
