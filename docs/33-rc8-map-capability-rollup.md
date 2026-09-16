# 0.1.0-rc.8 지도 역량 롤업 독립검토

판정: **BLOCKED / REVIEW ONLY**
소스 HEAD: 04797a004b429c0a7b6405b388c233d2cb6ca859
초기 main: 8ea1e56774d9de350b9b8455e58fa98a95f94c14

## 정확한 계보

- 기능 #063은 GitHub PR #63의 외부 차단조건 추적기다.
- 기능 #065는 GitHub PR #72의 ARKAON 지도 지식·라이더 인계다.
- 기능 #066은 GitHub PR #73의 기기호환·실패복구다.
- GitHub #64~#70은 PR이 아니라 EXT-01~EXT-07 이슈다.
- PR #71은 rc.7 검토 Snapshot이며 보존한다. #067 계보의 기능 소스나 병합 선조로 가장하지 않는다.
- 이번 검토 PR은 초기 main부터 #063 소스, #065, #066을 포함한 단일 직선 계보다.

## 내부 증거

| 증거 | SHA-256 | 판정 |
|---|---|---|
| 지도 공식지식·ARKAON 경계 | 0743f3d949d1d30ded3bd3d12132c1180df994cbd7a6446132f177b839db8164 | PASS |
| 모바일 기기 bridge | b892b49ead66768219ba1d111d50c1f0b345236bae3e72000d227bfaddf98723 | PASS |
| 잠정 공급자 등록부 | 1c21c0938f805537347fe35f7b4b805ac6983b5606e1015927fb71ca338b0521 | PROVISIONAL |
| 내비 인계 API | 1e3de5606d0999b6c930a63f10c77830e7b1eff8a3ef1d68286c3c72b62e45a5 | PASS |
| 기기 실행 API | a0efae63068ab6891b1f0e61a9788e7b1eff8a3ef1d68286c3c72b62e45a5 | PASS |

PASS는 내부 합성 계약의 재현만 의미한다. 공급자 공식 인증, 법률 적합성, 실제 안전경로,
물리기기 호환 또는 출시 승인이 아니다.

## EXT-02 지도 차단조건

[Issue #65](https://github.com/NurionHoldings/NARANG_RIDER/issues/65)에 아래 증거가 없으므로
출시를 차단한다.

- 공급자 선정과 Sandbox 승인
- 최신 공식 scheme, App Link, Intent, Universal Link, package/host/path/query 계약
- API 키 제한, 귀속표시, 캐시·파생데이터·재판매·쿼터·DPA/SLA 검토
- Android/iOS 최소·현재·최신 실제기기 시험
- 설치·미설치·업데이트·OS 제한·offline·복귀·fallback
- 보조기기와 한글 오류 안내 시험
- Canary·중지·rollback 및 에테르니언 심사
- 운영자 명시적 승격

## 독립심사 결론

ARKAON은 공식 자료 검색, 비교, 체크리스트, 어댑터와 시험 제안만 할 수 있다. 공급자를
VERIFIED로 표시하거나 앱을 설치·자동실행·백그라운드 실행할 수 없다. 지도와 경로는 배차
배제, 보수, 제재, 보험, 감시, 경로이탈 부정판정에 사용하지 않는다.

main 병합, 자동병합, 배포, 운영 키, 위치정보, 외부 지도 호출은 모두 금지 상태다.
