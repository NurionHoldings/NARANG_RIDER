# NARA-ARKAON 정확도 보증과 전국지사 운영

기준일: **2026-09-16**

## 원칙

나랑라이더는 특정 지역 서비스가 아니라 전국지사 체제로 운영한다. 전국 평균이 좋아도 한
지사의 오차가 크면 전국 승격을 허용하지 않는다. 신규 지사는 전국 공통모델과 공개 규칙
fallback으로 시작하고, 해당 지사의 실제 표본이 기준을 충족한 뒤에만 지역 보정모델을
사용한다.

금액 계산은 예측 문제가 아니다. 라이더 순수익과 점주 공헌이익은 정수 원화와 공개 산식으로
정확히 계산하며 오차를 허용하지 않는다. AI는 누락 비용 탐지와 설명만 보조한다.

## 능력별 검증

| 능력 | 핵심 지표 | 승격 조건 | 실패 시 동작 |
|---|---|---|---|
| 주문수요 | MAE, MASE, 편향, 구간 coverage | 시간순 holdout과 지사별 최소표본 통과 | 계절 naive/공개 규칙 |
| 조리시간 | MAE, P90 절대오차, 과소예측률 | 음식군·점포규모·지사별 기준 통과 | 보수적 범위와 점주 입력 |
| 주행시간 | MAE, P90, interval coverage | 차종·시간대·기상·지사별 기준 통과 | 지도 규칙 ETA 또는 사람 판단 |
| 라이더 순수익 | 정확일치율 100% | 원장·공개단가와 1원까지 일치 | AI 결과 폐기, 결정식 재계산 |
| 점주 공헌이익 | 정확일치율 100% | 매출·원가·포장·결제·환불 원장 일치 | AI 결과 폐기, 결정식 재계산 |
| 정산 이상탐지 | 재현율·정밀도·오탐률 | 금액 자동변경 없이 감사대상 선별만 | 사람 감사 |

모든 예측은 점추정만 내놓지 않고 예측구간을 함께 제공한다. 구간이 지나치게 넓거나 실제
coverage가 기준보다 낮으면 모델을 사용하지 않는다. 시계열은 교환가능성 가정이 깨질 수
있으므로 고전적 conformal coverage를 맹신하지 않고 rolling-origin 검증과 온라인 coverage
감시를 병행한다.

## 전국지사 승격 규칙

1. 본사 공통 schema와 지표 정의를 고정한다.
2. 각 지사는 동일한 방법으로 out-of-sample 결과를 제출한다.
3. 필수 지사 누락, 지사별 최소표본 미달, 어느 한 지사의 실패가 있으면 전국 승격을 막는다.
4. 최상·최하 지사 MAE 비율이 허용범위를 넘으면 지역 격차로 판정한다.
5. 신규 지사는 cold-start 동안 전국 공통모델과 공개 규칙 fallback만 사용한다.
6. drift가 기준을 넘으면 해당 지사 모델만 격리하고 전국 시스템은 안전한 fallback을 유지한다.
7. 에테르니언 심사와 운영자 승인 없이는 새 모델을 활성화하지 않는다.

## 근거

- NIST AI RMF Playbook은 운영 중 drift, 맥락 이탈, 부정적 결과를 지속적으로 감시하고
  대응·비활성화 절차를 유지하도록 제안한다. 조회 2026-09-16.
  https://airc.nist.gov/airmf-resources/playbook/measure/
- NIST의 배포 AI 모니터링 보고서는 실제 환경에서 신뢰성, 예상치 못한 출력과 drift를
  확인하기 위해 배포 후 측정이 필요하다고 설명한다. 조회 2026-09-16.
  https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.800-4.pdf
- EU AI Act Article 15는 수명주기 전체에서 적절한 정확도·견고성·사이버보안을 유지하는
  방향을 제시한다. 조회 2026-09-16.
  https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX%3A02024R1689-20260727
- Forecasting: Principles and Practice는 학습 잔차가 아니라 새로운 데이터에 대한 genuine
  forecast로 정확도를 평가하고, rolling forecasting origin을 사용한 시계열 교차검증을
  설명한다. 조회 2026-09-16.
  https://otexts.com/fpp3/accuracy.html
  https://otexts.com/fpp3/tscv.html
- 같은 자료는 서로 규모가 다른 계열·그룹 비교에 MASE 같은 scaled metric과 분포예측
  skill score를 사용할 수 있음을 설명한다. 조회 2026-09-16.
  https://otexts.com/fpp3/distaccuracy.html
- 최근 시계열 conformal 연구는 시간 의존성과 분포 변화가 고전적인 exchangeability를
  위반해 명목 coverage가 깨질 수 있음을 지적한다. 따라서 나랑라이더는 경험적 coverage와
  drift를 별도로 감시한다. 조회 2026-09-16.
  https://arxiv.org/abs/2511.13608
