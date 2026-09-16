# DB 마이그레이션·복구 독립감사

## 판정

합성 데이터 기반의 fresh up, 순차 적용, 트랜잭션 실패주입, 재시작, 복구 불변식은 PASS다. 운영 DB 백업·시점복구 훈련은 수행하지 않았고 출시 판정은 **BLOCKED**다.

## 보강 결과

- 0001~0005의 순서·중복·SHA-256을 `migrations/manifest.json`에 고정했다.
- 모든 up migration은 단일 트랜잭션과 5초 잠금·60초 실행 제한을 사용한다.
- 신규 테이블의 RLS `ENABLE/FORCE`, 지사 정책, 애플리케이션 grant 보존, 인덱스 유효성을 PostgreSQL 16에서 검사한다.
- 금융원장·감사기록은 변경·삭제가 거부된다. 누락됐던 관제·사고·알림 감사 테이블에도 불변 trigger를 추가했다.
- 현재 down 스크립트는 금융·감사 이력을 삭제할 수 있어 모두 `forward-only`로 분류했다. 운영 runner는 승인이나 백업이 있어도 down을 거부하며 forward repair만 허용한다.
- 합성 snapshot→upgrade→restore→replay는 원장 합계, 행 개수·digest, 지사 격리, Outbox 상태를 비교한다.
- 합성 시점복구 marker와 보고서 checksum을 생성한다.
- 중간 실패는 전체 rollback되고, 마지막 확정 version부터 다시 시작함을 검증한다.

## Expand-contract 기준

실제 대용량 변경은 nullable 추가·이중 읽기/쓰기·backfill·검증·제약 확정·구필드 제거를 서로 다른 배포로 나눈다. 기존 행을 검사하는 제약은 가능한 경우 `NOT VALID` 후 별도 `VALIDATE CONSTRAINT`로 적용한다. 현재 0001~0005는 신규 테이블 생성뿐이므로 해당 전략을 억지로 적용하지 않는다.

## 남은 외부 검증

운영과 동급인 격리 환경에서 암호화 백업, 실제 WAL 시점복구, 대용량 lock 시간, 연결 pool, 장애 시 RPO/RTO를 사람이 검증해야 한다. 이 문서와 합성 보고서는 운영 데이터 복구 증명이 아니다.
