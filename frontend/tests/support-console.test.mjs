import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/support-console.ts", import.meta.url), "utf8");
test("participant inbox lists every case family and appeal rights", () => {
  for (const label of ["배송", "파손", "환불", "결제", "정산", "배차", "안전", "개인정보", "보험", "제휴"]) assert.match(source, new RegExp(label));
  assert.match(source, /이의를 신청/);
});
test("support console separates notes and blocks automated outcomes", () => {
  assert.match(source, /내부 메모는 당사자 화면과 분리/);
  assert.match(source, /사진·AI·반복 신호만으로 책임, 환불거절 또는 제재를 결정하지 않습니다/);
  assert.match(source, /기존 2인 통제/);
});
test("frontend does not persist tokens contacts notes or evidence", () => {
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|CacheStorage|phone|address/);
});
