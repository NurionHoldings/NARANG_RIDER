# 시장 조사 및 baseline capability matrix

기준일: **2026-09-16**
방법: 정부·공공기관·법령·기업 공식 문서와 연구기관 자료를 우선 확인했다. 아래에서
`사실`은 출처로 확인한 내용, `설계 가설`은 나랑라이더가 실증해야 할 제안이다. 타사의
코드·문구·화면을 사용하지 않고 요구와 위험만 추출한 clean-room 분석이다.

## 확인된 구조

- 음식서비스 온라인 거래는 2025년 5월 전년동월 대비 14.2%, 6월 12.9%, 2026년 2월
  9.7% 증가했다. 배달은 여전히 성장하는 생활 인프라다.[S1][S2][S3]
- 배민 공식 안내의 상생요금제는 중개이용료 2.0~7.8%(부가세 별도), 점주 부담 배달비
  1,900~3,400원이다.[S4] 명목 수수료만으로 점주 비용을 판단하면 안 된다.
- 뉴욕시는 앱 배달노동자의 최저 지급률을 2025년 시간당 21.44달러, 2026년 4월부터
  22.13달러로 정했다. Standard 방식은 전체 노동자 집단의 on-call 시간을 포함하고,
  Alternative 방식은 active time 요율을 60%로 나눠 높인다.[S5][S6] 유휴시간 부담의
  귀속이 핵심이라는 공식 비교사례다.
- EU Directive 2024/2831은 고용지위 판정, 자동 모니터링·의사결정, 개인정보,
  투명성 및 집단교섭을 규율한다.[S7]
- CoopCycle은 주문·경로·라이더·청구·상점 기능을 노동자 소유 협동조합에 제공한다.[S8]
  OECD는 플랫폼 협동조합의 노동조건 가능성과 함께 자금·규모·경영 역량 과제를
  검토한다.[S9]

## 추적 매트릭스

| 기존 필수 기능 | 현장 불편·착취 위험 | 기존 보완 사례 | 나랑라이더 재설계(가설) | 검증 지표·악용 테스트 |
|---|---|---|---|---|
| 주문 접수·상태 추적 | 채널별 재입력, 누락, 특정 앱 락인 | CoopCycle 주문·상점 관리[S8] | 채널 중립 주문계약과 상태 이벤트, CSV/API 반출 | 중복·순서역전·replay; 주문 누락률, 이관시간 |
| 가맹점 메뉴·영업관리 | 다중채널 가격·품절 불일치 | 협동조합 공통 상점도구[S8] | 메뉴 원본 소유권은 점주, 채널별 projection | stale update·권한탈취; 품절 전파 P95 |
| 거리 기반 단가 | 실제 대기·복귀비용 외부화 | NYC 비용·on-call 반영[S5][S6] | 기본+거리+대기+복귀+안전 항목 공개 quote | 500m 경계, GPS spoof; 실제비용 차감 순시급 P20 |
| 묶음배달 | 두 번째 주문의 노동이 저가 처리, 지연 책임 불명 | 공식 단일 표준은 확인 못함 | 추가주문별 공개 증분보수, 주문당 순익, 우회 상한 | 묶음 분리·순서변경; 단건 대조군 대비 순익/지연 |
| 배차·재배차 | 숨은 점수, 블랙박스, 거절 보복 | EU 알고리즘 관리 투명성[S7] | 적격/안전 필터 뒤 longest-available FIFO, 배차 영수증 | 거절 전후 오퍼가·노출률 동일; 금지 feature 주입 실패 |
| ETA·조리시간 예측 | 예측오차 비용을 라이더/점주가 부담 | EU 인간 감독 원칙[S7] | AI는 시간예측만, 낮은 신뢰도는 공개 fallback | 모델 장애·drift; 배차 rank/pay 연결 시 빌드 실패 |
| 배송 증명·고객 연락 | 고객주소·전화번호 장기노출 | EU 개인정보 최소화[S7] | 중계번호, 배송 중 최소 위치, 종료 후 격자화 | IDOR·주소 export; 종료 후 정밀좌표 잔존 0 |
| 파손·누수 민원 증거 | 정상 배송 후 수취인의 사후 훼손, 반대로 사진만으로 정당 민원 기각 | NYC는 취소 이동시간 미지급을 제재한 사례로 증거·사람심사의 필요성을 보임[S12] | 서버 nonce 인앱 2~3프레임, 포장·봉인·지정위치만 촬영, 서명 영수증, 고객도 실시간 민원촬영; 사진은 정황증거만 | 과거사진·갤러리·nonce 재사용·봉인훼손·내부누수; 자동환불/자동기각/라이더 선환수 0 |
| 정산 | 정산지연, 불명확 공제, 지급보류 | CoopCycle 청구관리[S8] | 공개 quote에서 불변 복식분개, D+0/D+1 상태 | webhook 재전송·계좌변조·이중지급; 미대사율 |
| 취소·환불·분쟁 | 비용이 점주/라이더에 일방 전가 | EU 이의제기·인간감독 방향[S7] | 책임코드·증거고지·사람심사·재심 | 자동정지 금지; 집단별 인용률/처리시간 편차 |
| 사고·보험·안전 | 복수앱·대기 중 공백, 위험 인센티브 | 한국 산재 전속성 요건 폐지 방향[S10] | 보험 최소정보, 안전중단 무벌점, 기상 시 감속/서비스중단 | 허위사고·과다공유; 안전중단 뒤 오퍼 불이익 0 |
| 지역 공동물류 | 보조금 종료 후 수요 붕괴, 수익 역외유출 | 영주시 공공앱 건당 2천원 지원[S11] | 주문별 지역기금 원장, 배당은 법률검토 후 | Sybil 조합원·대형점주 포획; 보조금 제외 공헌이익 |
| 데이터·평판 | 플랫폼에 작업기록 고립, 탈퇴 시 상실 | EU 데이터 권리[S7] | 라이더 수익/비용·점주 손익 개인 데이터 지갑 및 export | 타인 export·파생정보 은폐; 권리행사 후 보복 0 |

## MVP 경제성 측정

라이더 순수익은 다음을 숨김없이 계산한다.

`지급액 - 유류·충전 - 리스/감가 - 정비 - 보험 - 기타 직접비`

시간당 순수익의 분모는 active time만이 아니라 관측된 픽업대기와 해당 주문에 귀속 가능한
복귀시간을 포함한다. 점주 주문당 공헌이익은 주문매출에서 상품원가·포장·결제·플랫폼·
배달분담·할인분담·환불을 차감한다. MVP의 공동 성공지표는 두 값의 **하위 20%가 동시에
개선**되는지다. GMV 또는 평균만으로 성공을 선언하지 않는다.

현재 구현은 운영 수익성을 증명하지 않는다. `PricingPolicy`와 `DeliveryFacts`로 위 산식을
재현하고 현장 관측자료를 받을 경계를 만든 것이다. 지역·차종별 실제비용과 수요밀도는
유료 pilot에서 검증한다.

## 출처

- [S1] 통계청, 「2025년 5월 온라인쇼핑동향」, 2025-07-01,
  https://kostat.go.kr/board.es?act=view&bid=241&list_no=437409&mid=a10301010000
- [S2] 통계청, 「2025년 6월 온라인쇼핑동향」, 2025-08-01,
  https://www.kostat.go.kr/board.es?act=view&bid=241&list_no=437829&mid=a10301010000
- [S3] 통계청, 「2026년 2월 온라인쇼핑동향」, 2026-04-01,
  https://www.kostat.go.kr/board.es?act=view&bid=241&list_no=444337&mid=a10301010000
- [S4] 배달의민족 사장님광장, 「배민배달의 이해」, 조회 2026-09-16,
  https://ceo.baemin.com/
- [S5] NYC DCWP, Minimum Pay Rate for Delivery Workers, 2026 갱신, 조회 2026-09-16,
  https://www.nyc.gov/site/dca/workers/Delivery-Worker-Public-Hearing-Minimum-Pay-Rate.page
- [S6] NYC DCWP, Delivery Worker Laws FAQs, 2026 갱신, 조회 2026-09-16,
  https://www.nyc.gov/site/dca/workers/workersrights/food-delivery-worker-laws-faqs.page
- [S7] EU, Directive (EU) 2024/2831, 2024-11-11,
  https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=OJ%3AL_202402831
- [S8] CoopCycle, Federation and Software, 조회 2026-09-16,
  https://coopcycle.org/en/ , https://coopcycle.org/en/logiciel/
- [S9] OECD, Platform cooperatives and employment, 2023-09-25,
  https://www.oecd.org/content/dam/oecd/en/publications/reports/2023/09/platform-cooperatives-and-employment_8e8a1d61/3eab339f-en.pdf
- [S10] 정책브리핑, 산재보험 전속성 요건 폐지 설명, 2022-07-25,
  https://www.korea.kr/news/policyNewsView.do?newsId=148903968
- [S11] 기업마당/경북·영주시, 공공배달앱 배달료 지원, 2025-10-17,
  https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000115634
- [S12] NYC DCWP, Uber Eats 등 미지급 정산·복직 합의, 2026-01-30,
  https://www.nyc.gov/mayors-office/news/2026/01/mayor-mamdani-announces--5-million-settlement--reinstatement-of-
