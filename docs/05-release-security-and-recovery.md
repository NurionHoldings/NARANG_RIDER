# 출시 전 보안·복구 기준

## 배포 승인과 롤백

배포는 기본 비활성이다. CI 성공만으로 배포하지 않으며 운영자와 독립 검토자 2인의 승인을
Release Manifest에 기록한다. Manifest에는 정확한 커밋, API·프런트엔드·마이그레이션 버전,
SBOM, 불변 아티팩트 SHA-256, 이전 검증 커밋을 기록한다. 승인 전에는 staging도 운영
자격증명을 사용하지 않는다.

배포 전 smoke test는 `/health/live`, `/health/ready`, 인증 실패, 지사 간 접근 차단, 주문 접수
멱등성, 원장 균형, outbox 중복 방지, 정적 파일 CSP를 확인한다. 실패하면 트래픽을 이전
아티팩트로 전환한다. 이미 실행된 DB migration은 역방향 파괴 DDL로 되돌리지 않고 호환되는
이전 애플리케이션으로 전환한 뒤 별도 승인된 복구 migration을 적용한다.

## 백업과 합성 복구

- 목표 RPO: 원장·정산 5분, 나머지 운영 데이터 15분
- 목표 RTO: 핵심 주문·안전·정산 60분, 전체 운영 4시간
- 암호화 백업은 운영 DB와 다른 장애영역에 보관하며 서비스 계정에 삭제권한을 주지 않는다.
- 매일 합성 데이터 복구, 매월 격리환경 전체 복구훈련을 수행한다.
- 복구 후 migration 버전, 레코드 수, 원장 합계, outbox 멱등키, RLS를 검증한다.
- 실데이터 복구는 사고 티켓과 독립 2인 승인 없이는 실행하지 않는다.

## 키 교체

JWKS는 새 키 공개 → 캐시 전파 확인 → 새 키 서명 → 구 키 검증 유예 → 폐기 순서로 교체한다.
웹훅은 현재·차기 비밀을 제한 기간 함께 검증하고 callback ID 재생 차단을 유지한다. 유출 시
비밀 참조를 새 버전으로 교체하고 기존 키를 즉시 폐기하며 영향 기간을 감사한다. 키 값은
환경변수·로그·manifest·CI 산출물에 기록하지 않는다.

## 무중단 migration

모든 변경은 expand → backfill → dual-read 검증 → cutover → contract 순서다. 신규 컬럼은 먼저
nullable 또는 안전한 기본값으로 추가한다. 인덱스는 운영 차단을 피하는 방식으로 생성한다.
contract 단계는 구버전 트래픽이 0이고 복구 창이 끝난 뒤 별도 release에서 승인한다. 동일
release에서 컬럼 이름 변경·삭제·타입 축소를 금지한다.

## 컨테이너 운영

이미지는 다단계·비 root 사용자로 빌드한다. 운영 오케스트레이터는 root filesystem을 읽기
전용으로 두고 `/tmp`만 용량 제한 tmpfs로 제공하며 Linux capability를 모두 제거한다. 비밀은
이미지·빌드 인자에 넣지 않고 `config://` 참조를 런타임 resolver에 전달한다.

## 위협모델 보강

| 위협 | 방어선 | 검증 |
|---|---|---|
| SSRF | 사용자가 URL을 지정하지 못하며 provider endpoint는 승인된 config 참조 | 임의 URL 입력 거부 |
| SQL/명령 주입 | 매개변수 쿼리, shell 호출 금지, DTO allowlist | 특수문자·경계 테스트 |
| XSS/CSP | 텍스트 렌더링, 외부 스크립트 금지, `eval` 금지 | 로컬 CSP 호환 검사 |
| CSRF | 쿠키 세션과 서버 검증 CSRF 토큰 | 누락·불일치 요청 거부 |
| 공급망 | 정확 버전 lock, npm integrity, SBOM, 최소 이미지 | 오프라인 lock 검사 |
| 지사 탈출 | 복합키·강제 RLS·서버 principal branch | PostgreSQL 교차지사 시험 |
| 자격증명 유출 | config 참조, 로그 redaction, secret scan | startup·manifest·로그 시험 |
| 배포권한 오용 | 배포 기본 비활성, 독립 2인 승인 | 미승인 release 거부 |
