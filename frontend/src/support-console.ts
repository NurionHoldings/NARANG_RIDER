export type CaseRole = "customer" | "merchant" | "rider" | "support";

export const supportRoutes = {
  inbox: "/api/v1/support/cases",
  caseDetail: "/api/v1/support/cases/{case_id}",
  messages: "/api/v1/support/cases/{case_id}/messages",
  evidence: "/api/v1/support/cases/{case_id}/evidence",
  appeal: "/api/v1/support/cases/{case_id}/appeals",
  assign: "/api/v1/support-ops/cases/{case_id}/assign",
  transition: "/api/v1/support-ops/cases/{case_id}/transition",
  action: "/api/v1/support-ops/cases/{case_id}/actions",
} as const;

export function caseInbox(role: CaseRole): string {
  if (role === "support") return `<section aria-labelledby="support-title"><h1 id="support-title">지원·분쟁 처리</h1>
    <p>지사 범위, 심각도, 응답기한과 담당 업무량으로 배정합니다. 처리속도를 상담원 제재나 성과 압박에 사용하지 않습니다.</p>
    <h2>사건 검토</h2><p>내부 메모는 당사자 화면과 분리됩니다. 사진·AI·반복 신호만으로 책임, 환불거절 또는 제재를 결정하지 않습니다.</p>
    <h2>해결과 이의신청</h2><p>사람의 사유와 권리안내가 있어야 종결할 수 있으며 이의신청은 독립 담당자가 검토합니다.</p>
    <h2>금융 조치</h2><p>임시 크레딧·환불·조정은 기존 2인 통제에 전달하는 실행 전 지시입니다.</p><div role="status" aria-live="polite"></div></section>`;
  return `<section aria-labelledby="case-inbox-title"><h1 id="case-inbox-title">내 문의·분쟁</h1>
    <p>배송·파손·환불·결제·정산·배차·안전·개인정보·보험·제휴 문제를 요청할 수 있습니다.</p>
    <p>다른 당사자의 연락처와 내부 메모는 표시하지 않습니다.</p>
    <button type="button">새 문의</button><h2>진행 중 사건</h2><div role="status" aria-live="polite"></div>
    <p>해결 사유와 권리안내를 확인하고 이의를 신청할 수 있습니다.</p></section>`;
}

export function safeCaseError(status: number): string {
  return ({401: "로그인이 필요합니다.", 403: "이 사건을 볼 권한이 없습니다.",
    404: "사건을 찾을 수 없습니다.", 409: "다른 변경이 먼저 저장되었습니다.",
    422: "증거와 입력 내용을 확인해 주세요.", 429: "잠시 후 다시 시도해 주세요."} as Record<number, string>)[status]
    ?? "요청을 처리하지 못했습니다. 자동으로 책임이나 환불 여부를 결정하지 않습니다.";
}
