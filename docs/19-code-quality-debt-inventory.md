# 코드 품질·기술부채 목록 (#045)

기준일: 2026-09-16  
범위: `feat/044-release-candidate-readiness`의 Python 소스·테스트·CI  
원칙: 기능 및 공개 API를 바꾸지 않고, 동작 보존 회귀 테스트를 먼저 추가한다.

## 해결됨

| 부채 | 조치 | 검증 |
|---|---|---|
| `connector.py`, `status_sync.py`의 파일 전체 `ruff: noqa` | 광역 억제 제거, 명시적 타입과 일반 형식으로 재작성 | 기존 계약 테스트 및 충돌 재사용 회귀 테스트 |
| 관련 테스트 3개의 광역 `ruff: noqa`와 wildcard import | 명시적 import와 일반 테스트 형식으로 변경 | Ruff 및 전체 pytest |
| 한 줄 다중 문장과 세미콜론 | Python 품질 게이트에서 토큰 단위로 금지 | `check_python_quality.py` |
| 암묵적 가변 컨테이너 타입 | 서비스 내부 상태에 구체적인 `dict` 타입 부여 | compile·Ruff·회귀 테스트 |
| 콜백 message ID 재결합 가능성 | 다른 상태로 같은 ID를 쓰면 fail-closed | 신규 회귀 테스트 |
| 재전송 fingerprint 일반 비교 | `hmac.compare_digest`로 일관된 비교 | 신규 상충 재전송 테스트 |
| 새 광역 `noqa` 유입 가능성 | 소스·테스트·스크립트 전체 CI 차단 | quality job |
| 숨은 문법 오류와 로컬 import cycle | 전 파일 compile 및 AST import graph 검사 | quality job |

## 보류됨

| 항목 | 사유 | 후속 조건 |
|---|---|---|
| 전 저장소 strict 정적 타입 검사 | 기존 모듈은 단계적 타입 경계를 사용하며, 한 번에 strict 적용하면 기능 변경 없는 이번 범위를 초과함 | 모듈별 typed migration PR에서 오류 예산 0으로 확대 |
| DB/네트워크 예외의 세부 타입 분할 | 현재 어댑터는 rollback·재시도 경계에서 의도적으로 드라이버 예외 전체를 변환함 | 실제 공급자 SDK 확정 후 예외 taxonomy 고정 |
| 테스트의 dataclass 내부 필드 검사 | 직렬화·불변 계약을 확인하는 제한된 테스트 용도이며 운영 소스의 private 접근이 아님 | 공개 serialization helper 도입 시 교체 |

광역 린트 억제, 무근거 TODO/FIXME, 운영 비밀·개인정보 로깅은 보류 항목으로 인정하지 않는다.
