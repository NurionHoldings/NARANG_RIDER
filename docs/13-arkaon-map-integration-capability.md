# ARKAON 지도 연동 역량 및 라이더 내비게이션 인계

조회일: 2026-09-16
상태: 내부 구현 완료, 실제 공급자 인증·키·Sandbox·법률 검토는 **BLOCKED**

## 원칙

ARKAON은 공식 문서를 찾아 비교하고 요구사항과 어댑터 초안을 제안할 수 있다. 검색 결과가
곧 운영 지식이 되지는 않는다. 공식 출처, 변경 차이, 회귀시험, Canary, 롤백 증거를 사람이
검토하고 에테르니언 서명 지침과 운영자 승인을 거쳐야 승격된다.

ARKAON은 지도나 위치정보를 근거로 배차 배제, 보수 변경, 제재, 보험 결정, 라이더 감시,
경로 이탈 부정판정을 할 수 없다. 오토바이 경로는 공급자의 공식 지원이 확인된 경우에만
표시하며, 자동차 경로를 오토바이 안전경로로 바꾸어 표현하지 않는다.

## 공식 출처 등록부

아래 내용은 clean-room 방식으로 기능 존재 여부와 검토 지점만 재작성했다. 가격·약관·쿼터·
표시의무는 수시 변경될 수 있어 계약 전 원문 재검토가 필요하다.

| 공급자/플랫폼 | 공식 문서 | 확인 내용 | 운영 판정 |
|---|---|---|---|
| Kakao Navi | [Kakao Navi iOS](https://developers.kakao.com/docs/en/kakaonavi/ios) | SDK가 내비 앱 실행 URL과 미설치 시 설치 안내 흐름을 제공하고 iOS allowlist 설정을 요구함 | SDK 계약·키·약관·표시의무 재검토 전 BLOCKED |
| Kakao Map | [Kakao Map concepts](https://developers.kakao.com/docs/en/kakaomap/common) | 위치 데이터 REST API와 지도 표시 SDK 제공 | 내비 인계와 별개로 인증 필요 |
| NAVER Cloud Maps | [Maps 개요](https://api.ncloud-docs.com/docs/application-maps-overview) | 동적/정적 지도, 지오코딩, 역지오코딩, Directions 5/15 제공 | IAM/API Gateway 인증 및 계약 검토 전 BLOCKED |
| NAVER Cloud Directions | [Directions 5](https://api.ncloud-docs.com/docs/en/application-maps-directions5) | 자동차 경로 입력과 경유지 계약 제공 | 오토바이 적합성 근거 없음; motorcycle BLOCKED |
| TMAP | [TMAP API](https://tmapapi.tmapmobility.com/) | 지도, POI, 지오코딩, 교통, 경로, 다중 경유지 기능 안내 | 정확한 호출 계약·Navi SDK·상업약관 인증 전 BLOCKED |
| TMAP Mobility | [기업 API/SDK](https://www.tmapmobility.com/service/corporate/api) | 기업용 API/SDK 제공 사실 확인 | 계약·쿼터·귀속표시 확인 필요 |
| Android | [App Links](https://developer.android.com/training/app-links) | 검증된 웹 링크의 앱 연결 플랫폼 경계 | 패키지/호스트 검증과 fallback 시험 필요 |
| Apple | [Supporting universal links](https://developer.apple.com/documentation/xcode/supporting-universal-links-in-your-app) | 연관 도메인 기반 링크 경계 | AASA·권한·fallback 시험 필요 |
| Apple Maps | [Map Links](https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/MapLinks/MapLinks.html) | Apple Maps로 넘기는 지도 링크 계약 | 현재 공식성·최신성 재확인 전 BLOCKED |

모든 출처의 조회일은 2026-09-16이다. 라이선스 전문의 적용 판단, 캐시/파생데이터 허용 범위,
로고·귀속표시, API 키의 앱/서버 제한, 쿼터와 재판매 금지는 법무·공급자 계약 검토가 끝나기
전까지 terms_review=BLOCKED로 유지한다.

## 라이더 흐름

1. 라이더가 매 배송마다 설치된 지도 앱 또는 시스템 기본 지도를 명시적으로 선택한다.
2. 15분 이내 세션과 일회용 목적지 토큰을 검증한다.
3. 목적지는 Vault/위치 경계에서 일시 해석하고 도메인·로그·분석에 저장하지 않는다.
4. 인증된 어댑터가 scheme/host/path/query allowlist를 적용해 외부 앱으로 인계한다.
5. 앱 미설치 시 시스템 지도 열기 또는 안전한 복사 fallback을 제공한다.
6. 외부 앱 이동, 법규·현장안전 우선, 백그라운드 추적 미시작을 분명히 알린다.
7. 돌아오면 기존 배송업무 화면에서 계속한다. 경로 이탈은 제재 신호로 사용하지 않는다.

## 역량 진화 절차

SearchProvider(공식 도메인만) → 출처 해시 → 기존 버전과 차이 → 사람 검토 → 에테르니언
서명 지침 → 오프라인 계약/악용 회귀시험 → 제한 Canary → 운영자 승격 → 롤백 가능 상태

웹 검색 결과를 곧바로 코드·설정·운영으로 반영하는 자율학습은 금지한다. 오래됐거나 출처가
사라진 지식은 fail-closed로 중지한다.

## 추적성

| 요구사항 | 구현 | 시험 |
|---|---|---|
| 버전형 공식 지식 | MapProviderKnowledgeRegistry | stale/provisional/BLOCKED 거부 |
| ARKAON 검색·비교·초안 | MapIntegrationAssistant | 운영 활성화 false |
| 목적지 비저장 인계 | RiderNavigationHandoffService | 로그·재해석·TTL 시험 |
| URL/Intent 방어 | CertifiedHandoffConfig | scheme/host/path/query 공격 시험 |
| 오토바이 과장 금지 | motorcycle_support_verified | 공식 근거 없으면 거부 |
| 지식 업데이트 | propose_knowledge_update | 출처 없음 거부·사람 검토 필수 |
