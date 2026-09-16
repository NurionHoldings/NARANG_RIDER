# 지도 앱 기기 호환성과 실패복구 인증

상태: **BLOCKED**  
기준일: 2026-09-16

코드와 합성 시험은 Android, iOS, 모바일 웹의 공통 계약을 검증한다. 실제 기기, 공급자 공식
scheme/host/path/query, 설치·업데이트 흐름을 확인하기 전에는 어떤 공급자도 운영 인증으로
승격하지 않는다.

## 플랫폼 계약

- Android: 공식 근거가 VERIFIED인 App Link 또는 Intent 형태만 후보로 삼는다. 임의 package,
  component, intent extra, fallback URL을 허용하지 않는다.
- iOS: 공식 근거가 VERIFIED인 Universal Link 또는 custom scheme만 사용한다. 연관 도메인과
  앱 entitlement를 실제 기기에서 확인한다.
- 모바일 웹/PWA: HTTPS fallback만 사용하며 새 창은 noopener,noreferrer로 연다. 서비스
  워커는 실행 URL·목적지·좌표를 캐시하지 않는다.
- 모든 플랫폼: 전경의 실제 사용자 동작, 2분 TTL, 일회용 launch token, 지사·라이더·주문·
  배차·세션 범위, 멱등키를 요구한다. 자동 반복 실행은 금지한다.
- 앱 설치 여부와 probe 결과는 기기 내 선택에만 사용하고 분석·서버 로그에 보내지 않는다.
- 기본 앱 선택에는 공급자 ID만 로컬 보관할 수 있고 목적지·경로·좌표는 저장하지 않는다.

## 물리기기 인증 체크리스트

각 공급자와 지원 OS 조합별로 아래 증거가 모두 있어야 한다.

- [ ] 공급자 최신 공식 문서 URL, 조회일, 해시와 약관 검토
- [ ] 공식 scheme/domain/path/query와 귀속표시 검증
- [ ] Android 최소/현재/최신 OS 실제기기: 설치·미설치·업데이트·OS 제한
- [ ] iOS 최소/현재/최신 OS 실제기기: 설치·미설치·Universal Link/custom scheme
- [ ] 모바일 브라우저 및 PWA: popup 차단, offline, noopener,noreferrer
- [ ] 한글 화면읽기, 키보드/스위치, 200% 확대, 오류 focus와 live region
- [ ] URL percent-encoding, Unicode/IDNA host, open redirect, 악성 handler
- [ ] intent extra 주입, replay, background launch, 교차 라이더/주문/지사
- [ ] 취소·timeout·앱 복귀·deep-link state 단일사용·세션 만료
- [ ] fallback 시스템 지도/복사와 배송업무 복귀
- [ ] Canary·중지·rollback 기록
- [ ] 에테르니언 독립 심사와 운영자 승격

DeviceCertificationArtifact는 모든 항목이 확인될 때만 VERIFIED가 된다. 현재 물리기기와 공식
scheme 인증이 없으므로 모든 실제 공급자 조합은 BLOCKED다.

## ARKAON 권한

ARKAON은 공식 자료 후보, 어댑터 뼈대, 합성 기기행렬과 회귀시험을 제안할 수 있다. 그러나
VERIFIED 표시, 앱 설치, 자동/백그라운드 실행, 운영 설정 승격은 할 수 없다. 지도 앱 사용
실패나 경로 이탈은 배차·보수·제재·보험·평가 신호가 아니다.

## 합성 검증 행렬

| 축 | 값 |
|---|---|
| 플랫폼 | Android / iOS / mobile web |
| 상태 | available / unavailable / update required / OS restricted / offline |
| 공격 | malicious handler / redirect / intent extra / Unicode host / replay |
| 범위 | rider / order / assignment / branch / session |
| 수명 | foreground gesture / 2분 TTL / cancel / return state / single use |

이 행렬은 실제 기기 또는 공급자 인증을 대체하지 않는다.
