# 0.1.0-rc.9 지도지능 검토 전용 롤업

이 롤업은 초기 main에서 PR #1~#63의 연속 계보와 지도 기능 PR #72, #73, #75, #76을
포함한다. PR #71은 rc.7, PR #74는 rc.8 검토 Snapshot이며 기능승격이나 승인으로 세지 않는다.
번호 #64~#70은 외부 차단조건 GitHub Issue이고 PR이 아니다.

## 지도 증거

경로 선택·비용·복귀보상·묶음 우회상한과 ARKAON 지도역량 Benchmark의 소스·시험·Profile
SHA-256을 release/rollup-manifest.json에 고정했다. Benchmark는 precision 0.98, recall 0.95,
오래된 출처 거부·계약품질·파괴변경 탐지 1.0, 근거없는 주장·위험제안 0.0을 요구한다.

이 수치는 합성 계약검증 결과일 뿐 실제 지도 공급자의 오토바이 경로 합법성·정확성·안전성
승인이 아니다. 공식 scheme, parameter, Sandbox, 이용조건, 실제 Android/iOS 단말과 현장
시험은 EXT-02 GitHub Issue #65의 PENDING 차단조건이다.

## 판정

REVIEW ONLY / BLOCKED. main 병합, 자동병합, 배포, 실제 지도호출, 위치정보, 운영 키, 모델
학습을 허용하지 않는다.
