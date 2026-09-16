# #039 제휴사 Sandbox 적합성 인증 하니스

## 판정 경계

이 하니스는 네트워크·운영 자격증명·개인정보 없이 NARANG RIDER의 공급자 중립 계약만 검증한다. `ai배비`와 `도시락.store` 프로필은 공식 API 명세를 제공받기 전까지 **잠정(provisional)** 이며 모든 내부 계약 검사가 통과해도 최종 판정은 `BLOCKED`이다. `live_certified`는 항상 `false`다. 실제 제휴사 인증·승인·운영 연결을 주장하는 자료로 사용할 수 없다.

## Clean-room 출처 기록

- 작성일/조회일: 2026-09-16
- 외부 제휴사 문서·코드·트래픽: 사용하지 않음
- 입력 근거: NARANG RIDER 내부 canonical order/callback/정산 계약과 합성 필드
- `ai배비`, `도시락.store`: 명칭과 연동 목표만 식별자로 사용. 엔드포인트·필드·서명 규격을 추정하지 않음
- fixture: 실제 주문·주소·전화번호가 없는 합성 데이터. 원본 payload는 인증 보고서에 포함하지 않음

## 검증 범위

manifest·capability 협상, 버전/media type, Vault 참조, 인증·웹훅 서명 주입, 주문 생성·취소·상태·비용견적·라이더 호출·증거알림·정산대사, 멱등·재전송·충돌·순서역전, timeout·retry·circuit·DLQ, rate-limit header, clock skew·키 교체를 동일 suite로 실행한다. 가게 직접 호출과 라이더회사 호출의 중복, 비마스킹 배송증거, 원문 개인정보, 자동 라이더 환수는 fail-closed로 거부한다.

## 보고서

`PASS`는 generic 계약 suite의 통과, `FAIL`은 필수 계약 실패, `BLOCKED`는 내부 suite는 통과했지만 공식 공급자 명세가 없어 실연동 승격이 금지된 상태다. 보고서는 check ID, 판정, 코드, SHA-256 증거 digest만 포함한다. payload·비밀정보·개인 식별자는 포함하지 않는다.

```bash
PYTHONPATH=src python scripts/certify_partner_sandbox.py
PYTHONPATH=src python scripts/certify_partner_sandbox.py --profile ai-baebi
```
