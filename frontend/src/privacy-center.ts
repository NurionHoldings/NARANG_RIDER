export type PrivacyView = "subject" | "privacy-ops";

export const privacyRoutes = {
  policies: "/api/v1/privacy/policies",
  requests: "/api/v1/privacy/requests",
  request: "/api/v1/privacy/requests/{request_id}",
  export: "/api/v1/privacy/requests/{request_id}/export",
  consentWithdrawal: "/api/v1/privacy/consents/{consent_id}/withdraw",
  holds: "/api/v1/privacy-ops/holds",
  sweeps: "/api/v1/privacy-ops/sweeps",
} as const;

export function privacyCenter(view: PrivacyView): string {
  if (view === "subject") return `<section aria-labelledby="privacy-title">
    <h1 id="privacy-title">개인정보·내 권리</h1>
    <p>열람·정정·삭제·처리정지·내보내기·이의를 요청할 수 있습니다.</p>
    <p>본인확인 자료는 보호 저장소 참조로만 처리합니다. 요청은 사람 담당자가 심사하며 아르카온이 거절하지 않습니다.</p>
    <form aria-describedby="privacy-help"><label>요청 유형 <select name="right"><option>열람</option><option>정정</option><option>삭제</option><option>처리정지</option><option>내보내기</option><option>이의</option></select></label><button type="submit">권리 요청</button></form>
    <p id="privacy-help">삭제가 제한되는 법정 보존자료는 사유와 범위를 별도로 안내합니다.</p><div role="status" aria-live="polite"></div></section>`;
  return `<section aria-labelledby="ops-privacy-title"><h1 id="ops-privacy-title">개인정보 운영</h1>
    <p>지사 범위 요청·응답기한·보존 만료·Vault 처리 대기 상태만 표시합니다.</p>
    <h2>보존 만료 처리</h2><p>미리보기 → 독립 검토 → 실행 순서이며 재실행은 중복 처리되지 않습니다.</p>
    <h2>법적 보존</h2><p>대상 기록·사유·만료일·서로 다른 승인자 두 명이 필요합니다. 무기한 일괄 보존은 허용하지 않습니다.</p>
    <h2>침해 대응</h2><p>탐지시각과 검토기한을 추적하되 법적 결론은 자동 확정하지 않습니다.</p></section>`;
}

export function safePrivacyMessage(status: number): string {
  return ({401: "로그인이 필요합니다.", 403: "본인 또는 담당 지사의 권한을 확인해 주세요.",
    404: "요청을 찾을 수 없습니다.", 409: "이미 처리된 요청입니다.",
    422: "본인확인 자료와 요청 내용을 확인해 주세요.", 429: "잠시 후 다시 시도해 주세요."} as Record<number, string>)[status]
    ?? "요청을 처리하지 못했습니다. 담당자가 확인합니다.";
}
