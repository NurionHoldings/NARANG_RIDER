# ai배비·도시락.store 독립 커넥터

조회·작성일: 2026-09-16

이 커넥터는 각 파트너의 기존 POS·배달플랫폼·배달대행사 흐름을 교체하지 않는다.
파트너 주문을 NARANG canonical order로 매핑하고, 기존 흐름 옆에 중복 차단과 상태
콜백을 덧붙인다. 공개 API 명세나 실제 자격증명은 제공받지 않았으므로 현재 구현은
명시적 capability 계약과 mock contract test이며, 운영 연결 전 파트너 승인 명세로
필드 매핑을 검증해야 한다.

## 안전 경계

- ai배비와 도시락.store는 런타임·재시도 큐·dead-letter를 각각 소유한다.
- 인증정보와 콜백 주소는 `vault:` 참조만 허용하며 코드와 이벤트에 원문을 넣지 않는다.
- inbound/outbound idempotency key 재사용 시 payload가 달라지면 fail-closed 한다.
- 가게 직접 호출과 라이더회사 호출은 하나의 전역 order lock을 공유하여 중복 호출을 막는다.
- 콜백은 한 파트너가 연속 실패해도 다른 파트너의 전송 상태를 변경하지 않는다.
- dead-letter는 재처리 대기 상태이며 자동 주문취소·보수환수·계정제재를 일으키지 않는다.

## 운영 연결 전 필수 확인

1. 각 파트너가 서명한 주문·취소·상태 콜백 스키마와 버전 정책
2. 인증 방식, 키 회전, webhook 서명 및 replay 허용시간
3. timeout·rate limit·retry-after·장애 공지 계약
4. 테스트 환경에서 mock contract test와 동일한 provider certification
5. 개인정보 처리위탁·보유기간·파기 및 국내 법적 검토

