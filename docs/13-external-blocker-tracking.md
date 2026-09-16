# 외부 차단조건 실행 추적

이 문서는 출시후보의 외부 입력 7개를 실행 가능한 검증 단위로 추적한다. 단일 진실원본은 `config/external-blockers.json`이다.

## 상태 규칙

- `PENDING`: 착수 전
- `IN_PROGRESS`: 안전한 증거 수집 중
- `BLOCKED`: 선행조건·외부응답·위험 해소 대기
- `VERIFIED`: 증거 참조, SHA-256 다이제스트, 유효기간, 에테르니언 독립 검토가 모두 유효하며 필요한 경우 운영자 승인까지 존재

`VERIFIED`는 Issue 종료나 출시승인을 뜻하지 않는다. Issue, PR, 배포, 출시를 자동으로 닫거나 실행하지 않는다. 출시 게이트는 운영자 최종 승인 전까지 항상 `BLOCKED`다.

## 증거 취급

저장소와 GitHub Issue에는 `evidence-ref:`, `vault-ref:`, `sha256:` 형식의 비밀 없는 참조만 기록한다. API 키, 토큰, 인증서 원문, 개인정보, 실제 위치·결제·보험자료는 금지한다. 증거 만료 시 재검증 전까지 통과로 취급하지 않는다.

## 운영 보기

`narang_rider.external_readiness.external_readiness_api()`는 읽기 전용 요약을 제공한다. `python scripts/external_blocker_snapshot.py`는 주간 검토용 비민감 스냅샷을 로컬에서 생성한다. 외부 연락·Issue 종료·출시·병합·배포는 수행하지 않는다.
