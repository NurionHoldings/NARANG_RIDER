import { assertRole, redact, type SessionAdapter } from "./api.js";

type Role = "merchant" | "rider" | "customer" | "branch-ops";
const labels: Record<Role, { title: string; cards: [string, string][] }> = {
  merchant: { title: "점주 주문관리", cards: [["새 주문", "공개 거리·시간·통행료와 보수 산식을 확인하고 주문을 접수합니다."], ["라이더 호출", "가게 직접 호출과 대행사 호출의 중복을 차단합니다."], ["포장·봉인", "이중 포장과 봉인번호를 기록합니다."]]},
  rider: { title: "라이더 업무", cards: [["공정 배차 제안", "보수·예상 순수익·배차 사유를 먼저 확인합니다."], ["배송 진행", "지도 없이도 구간·예상시간·대기·복귀 보상을 확인합니다."], ["사고·보험 지원", "위험하면 불이익 없이 즉시 멈추고 사람 담당자에게 지원을 요청합니다."]]},
  customer: { title: "배송 확인", cards: [["배송 진행", "정밀위치나 라이더 정보 없이 대략적인 진행상태만 확인합니다."], ["외관 이상 신고", "개봉 전 외관 이상이 있으면 먼저 신고해 주세요."], ["실시간 촬영", "갤러리 사진 대신 일회용 촬영 권한을 사용합니다."]]},
  "branch-ops": { title: "지사 관제", cards: [["집계 현황", "최소 집단 기준을 충족한 주문·라이더·점주 현황만 표시합니다."], ["사고·연동 상태", "사고, 파트너 회로, 대기열과 정산 상태를 점검합니다."], ["운영 일시정지", "사유와 티켓이 필요하며 민감 명령은 독립 2인 승인을 받습니다."]]},
};

class DemoSession implements SessionAdapter {
  constructor(private readonly currentRole: Role, private readonly branch: string | null) {}
  async csrfToken() { return "session-bound-demo-token"; }
  async role() { return this.currentRole; }
  async branchId() { return this.branch; }
}

export async function mount(root: HTMLElement): Promise<void> {
  const role = (root.dataset.role ?? "customer") as Role;
  const session = new DemoSession(role, role === "branch-ops" ? "demo-branch" : null);
  assertRole(await session.role(), role, await session.branchId());
  const model = labels[role];
  root.innerHTML = `<header><a class="brand" href="#main">나랑라이더 <small>NARANG RIDER</small></a><span class="connection" role="status">서버 연결 대기</span></header>
  <main id="main"><h1>${model.title}</h1><p class="lead">${redact("나랑 달리고, 나란히 성장하다.")}</p>
  <section class="cards" aria-label="주요 업무">${model.cards.map(([title, body]) => `<article><h2>${title}</h2><p>${body}</p><button type="button">열기</button></article>`).join("")}</section>
  <section aria-labelledby="route-title"><h2 id="route-title">거리·예상시간 안내</h2>
  <p>주소와 정밀위치는 표시하거나 저장하지 않습니다. 경로 제공자가 응답하지 않으면 보수에 불리하지 않은 보수적 공개견적을 사용합니다.</p>
  <p>아르카온 예상시간은 범위가 있는 참고 정보이며 배차 배제·보수 삭감·제재의 근거가 아닙니다. 우회 주행만으로 과실을 판단하지 않습니다.</p></section>
  ${role === "rider" ? `<section aria-labelledby="incident-title"><h2 id="incident-title">사고·보험 보호</h2>
  <p><strong>먼저 안전한 곳에 정차하세요.</strong> 사고 신고 즉시 새 배차가 중지되며 거절 불이익은 없습니다.</p>
  <label>사고 유형 <select name="incident-kind"><option>긴급상황</option><option>교통사고</option><option>부상</option><option>차량 손상</option><option>재산 피해</option></select></label>
  <p>정밀위치·진단 내용은 화면이나 기기에 저장하지 않고 보호 저장소 참조로만 처리합니다.</p>
  <button type="button">안전 중지 및 지원 요청</button>
  <p>미분쟁 수익은 계속 지급 대상입니다. 사진만으로 과실·보상 거절을 확정하지 않습니다.</p></section>` : ""}
  <aside aria-live="polite"><strong>안전한 상태 처리</strong><p>오프라인·오류·중복 요청은 자동 송금이나 책임 확정 없이 다시 확인합니다.</p></aside></main>
  <section aria-labelledby="notification-title"><h2 id="notification-title">알림센터</h2>
  <p>앱 알림이 기본입니다. 잠금화면에는 주소·정밀위치·라이더 정보·주문내용을 표시하지 않습니다.</p>
  <label><input type="checkbox" checked disabled> 앱 알림</label>
  <label><input type="checkbox"> 푸시</label><label><input type="checkbox"> 문자</label><label><input type="checkbox"> 이메일</label>
  <label>방해금지 시작 <input type="time" value="22:00"></label><label>종료 <input type="time" value="07:00"></label>
  <p>긴급 안전·법적 필수 알림은 별도로 표시됩니다. 알림 확인 여부는 환불·이의제기 등 권리를 제한하지 않습니다.</p></section>
  <footer>서버 권한 검증이 최종 기준입니다. 토큰·정밀위치·원본 사진은 이 기기에 저장하지 않습니다.</footer>`;
}

const root = document.querySelector<HTMLElement>("#app");
if (root) void mount(root);
