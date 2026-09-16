import { ApiClient, assertRole, type RouteName, type SessionAdapter } from "./api.js";
import { ActiveFormMutation, journeys, type PortalRole } from "./workflows.js";
import { REQUIRED_REPORT_NOTICE, rights } from "./korean-ux.js";
import { adminArkaonConsole } from "./admin-arkaon-console.js";

class ServerSession implements SessionAdapter {
  private async value(): Promise<{ role: PortalRole; branch_id: string | null; csrf_token: string }> {
    const response = await fetch("/api/v1/session", { credentials: "include", cache: "no-store" });
    if (!response.ok) throw new Error("SESSION_REQUIRED");
    return response.json();
  }
  async csrfToken() { return (await this.value()).csrf_token; }
  async role() { return (await this.value()).role; }
  async branchId() { return (await this.value()).branch_id; }
}

const titles: Record<PortalRole, string> = { merchant: "점주 주문관리", rider: "라이더 업무", customer: "배송 확인", "branch-ops": "지사 관제" };

function roleGuidance(role: PortalRole): string {
  if (role === "rider") return `<h2>사고·보험 보호</h2><p>거절 불이익은 없습니다. 미분쟁 수익은 계속 지급 대상입니다. 사진만으로 과실·보상 거절을 확정하지 않습니다.</p>`;
  if (role === "customer") return `<h2>알림센터</h2><p>잠금화면에는 주소·정밀위치·라이더 정보·주문내용을 표시하지 않습니다. 정밀위치나 라이더 정보 없이 대략적인 진행상태만 확인합니다. 알림 확인 여부는 환불·이의제기 등 권리를 제한하지 않습니다.</p>`;
  if (role === "branch-ops") return `<h2>파일럿 체크리스트</h2><p>합성 → 내부 shadow → 폐쇄 sandbox → 제한 지사 pilot. 체크리스트 통과는 법률·정부·보험 승인이 아닙니다. 위험은 자동 격리하며 독립 2인 승인 전에는 재개하지 않습니다.</p><p>합성 검증 보고서는 실데이터가 아닌 부하·경제성·악용 가설만 보여줍니다.</p>`;
  return `<h2>거리·예상시간 안내</h2><p>주소와 정밀위치는 표시하거나 저장하지 않습니다. 공급자 장애 시 보수에 불리하지 않은 보수적 공개견적을 사용합니다.</p>`;
}

export async function mount(root: HTMLElement, session: SessionAdapter = new ServerSession()): Promise<void> {
  const role = (root.dataset.role ?? "customer") as PortalRole;
  assertRole(await session.role(), role, await session.branchId());
  root.innerHTML = `<a class="skip-link" href="#main">본문으로 바로가기</a><header><a class="brand" href="#main">나랑라이더 <small lang="en">NARANG RIDER</small></a><span class="connection" role="status" aria-live="polite">합성 서버 연결</span></header>
  <main id="main" tabindex="-1"><h1>${titles[role]}</h1><p>서버 권한 검증이 최종 기준입니다.</p>
  <p id="form-help">각 작업은 한 번만 전송됩니다. 연결이 끊기면 내용을 확인한 뒤 직접 다시 시도하세요.</p>
  <div id="form-error" class="message error" role="alert" tabindex="-1"></div><div id="form-status" class="message status" role="status" aria-live="polite" tabindex="-1"></div>
  ${role === "customer" ? `<section aria-labelledby="delivery-notice"><h2 id="delivery-notice">배송 외관 확인</h2><p>${REQUIRED_REPORT_NOTICE}</p></section>` : ""}
  <section class="cards" aria-labelledby="tasks-heading"><h2 id="tasks-heading" class="section-heading">주요 업무</h2>${journeys[role].map(action => `<form data-action="${action.id}" data-route="${action.route}" aria-describedby="form-help form-error"><h3>${action.label}</h3><label for="note-${action.id}">처리 메모</label><input id="note-${action.id}" name="note" autocomplete="off">${action.id === "notifications" ? `<label class="choice"><input name="optional-consent" type="checkbox"> 선택 알림 수신에 동의합니다</label>` : ""}${action.destructive ? `<label class="choice"><input name="confirmed" type="checkbox" required> 결과를 확인했고 이 작업을 요청합니다</label>` : ""}<button type="submit"${action.destructive ? ` class="danger"` : ""}>${action.label}</button></form>`).join("")}</section>
  <button id="retry" type="button" hidden>다시 시도</button>${roleGuidance(role)}
  ${role === "branch-ops" ? adminArkaonConsole() : ""}
  <p>아르카온 예상시간은 참고 정보이며 배차 배제·보수 삭감·제재의 근거가 아닙니다. 우회 주행만으로 과실을 판단하지 않습니다.</p>
  <nav class="rights" aria-labelledby="rights-heading"><h2 id="rights-heading">내 권리와 선택</h2><ul>${rights.map(right => `<li><a href="/rights#${encodeURIComponent(right)}">${right}</a></li>`).join("")}</ul><p>안내 확인 여부나 선택 동의 거부만으로 환불·이의제기·배차·보수 권리를 제한하지 않습니다.</p></nav>
  <aside aria-live="polite">오류·중복 요청은 자동 송금이나 책임 확정 없이 다시 확인합니다.</aside></main>
  <footer>토큰·정밀위치·원본 사진은 이 기기에 저장하거나 캐시하지 않습니다.</footer>`;
  const client = new ApiClient(session);
  const mutation = new ActiveFormMutation(client, async () => { await client.request("merchantOrders"); });
  const error = root.querySelector<HTMLElement>("#form-error")!;
  const status = root.querySelector<HTMLElement>("#form-status")!;
  const retry = root.querySelector<HTMLButtonElement>("#retry")!;
  root.querySelectorAll<HTMLFormElement>("form[data-route]").forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector<HTMLButtonElement>("button")!;
    button.setAttribute("aria-busy", "true");
    const originalLabel = button.textContent ?? "처리";
    button.textContent = "처리 중…";
    button.disabled = true;
    const result = await mutation.submit(form.dataset.route as RouteName, { synthetic_ref: "config://active-form" });
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = originalLabel;
    error.textContent = result.state === "success" ? "" : result.message;
    status.textContent = result.state === "success" ? result.message : "";
    retry.hidden = result.state !== "offline";
    (result.focusTarget === "error" ? error : status).focus();
  }));
  retry.addEventListener("click", async () => { const result = await mutation.retryExplicitly(); status.textContent = result.message; status.focus(); });
}

const root = document.querySelector<HTMLElement>("#app");
if (root) void mount(root);
