# #040 로컬·CI 전체 실행환경

이 환경은 `local-synthetic` 전용이다. 운영 배포, 실제 주문, 결제·송금, 운영 자격증명, 개인정보 처리를 지원하지 않는다. App ASGI, PostgreSQL 16, one-shot migration, Outbox worker, 정적 frontend, mock OIDC/JWKS·제휴사·알림·지도·결제참조 공급자를 격리된 Docker network에서 재현한다.

## Linux/macOS

```bash
docker compose -f compose.local.yml up --build --wait
docker compose -f compose.local.yml exec -T app python scripts/local_full_stack_smoke.py --base-url http://127.0.0.1:8000 --output /tmp/local-full-stack-report.json
docker compose -f compose.local.yml down --volumes --remove-orphans
```

## Windows PowerShell

```powershell
docker compose -f compose.local.yml up --build --wait
docker compose -f compose.local.yml exec -T app python scripts/local_full_stack_smoke.py --base-url http://127.0.0.1:8000 --output /tmp/local-full-stack-report.json
docker compose -f compose.local.yml down --volumes --remove-orphans
```

호스트 공개 포트는 loopback의 `18000`(합성 API), `18080`(정적 화면)뿐이다. PostgreSQL과 mock 공급자는 호스트에 공개하지 않는다. DB는 tmpfs이고 종료 시 데이터가 사라진다. 고정 문자열은 운영 비밀이 아니라 합성 로컬 값이다. 로그/보고서는 원문 payload 없이 digest와 통과여부만 남긴다.

## 합성 시나리오

Mock 로그인 → 점주 주문 → 라이더 제안·수락·픽업·완료 → 마스킹 고객 증거·신고 → 균형원장·정산 → 알림 Outbox 순서로 실행한다. HQ·권역·지역지사와 점주·라이더·고객 주체는 `config://seed/*` 참조로만 seed한다.
