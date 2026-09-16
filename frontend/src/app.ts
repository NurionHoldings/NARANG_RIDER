import { assertRole, redact, type SessionAdapter } from "./api.js";

type Role = "merchant" | "rider" | "customer" | "branch-ops";
const labels: Record<Role, { title: string; cards: [string, string][] }> = {
  merchant: { title: "점주 주문관리", cards: [["새 주문", "공개 견적을 확인하고 주문을 접수합니다."], ["라이더 호출", "가게 직접 호출과 대행사 호출의 중복을 차단합니다."], ["포장·봉인", "이중 포장과 봉인번호를 기록합니다."]]},
  rider: { title: "라이더 업무", cards: [["공정 배차 제안", "보수·예상 순수익·배차 사유를 먼저 확인합니다."], ["배송 진행", "도착·픽업·전달 상태를 순서대로 기록합니다."], ["안전 중지", "위험하면 불이익 없이 운행을 멈춥니다."]]},
  customer: { title: "배송 확인", cards: [["정상 외관 사진", "개인정보가 가려진 배송 사진만 확인합니다."], ["외관 이상 신고", "개봉 전 외관 이상이 있으면 먼저 신고해 주세요."], ["실시간 촬영", "갤러리 사진 대신 일회용 촬영 권한을 사용합니다."]]},
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
  <aside aria-live="polite"><strong>안전한 상태 처리</strong><p>오프라인·오류·중복 요청은 자동 송금이나 책임 확정 없이 다시 확인합니다.</p></aside></main>
  <footer>서버 권한 검증이 최종 기준입니다. 토큰·정밀위치·원본 사진은 이 기기에 저장하지 않습니다.</footer>`;
}

const root = document.querySelector<HTMLElement>("#app");
if (root) void mount(root);
