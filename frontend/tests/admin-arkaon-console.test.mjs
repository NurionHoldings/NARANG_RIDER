import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/admin-arkaon-console.ts", import.meta.url), "utf8");

test("admin console supports conversation and bounded partial change instructions", () => {
  for (const phrase of ["대화", "부분 수정지시", "허용 파일경로", "수정안 미리보기", "시험계획", "위험"])
    assert.match(source, new RegExp(phrase));
});

test("every mutation stage promises guidance refresh", () => {
  for (const stage of ["수정안 생성", "미리보기 완료", "운영자 승인", "검토 브랜치 반영"])
    assert.match(source, new RegExp(stage));
  assert.match(source, /때마다 새 버전으로 갱신/);
});

test("console warns that approval is not merge deployment or production mutation", () => {
  assert.match(source, /main 병합·실제 배포·운영 변경은 실행되지 않습니다/);
  assert.match(source, /비밀번호·토큰·주민등록번호·실제 개인정보를 입력하지 마십시오/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB/);
});
