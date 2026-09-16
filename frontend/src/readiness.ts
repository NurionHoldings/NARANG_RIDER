export interface ReleaseCandidateReport {
  version: string;
  verdict: "BLOCKED" | "READY_FOR_OPERATOR_REVIEW";
  blockers: string[];
  limitations: string[];
  compliance_claim: string;
}

const blockerLabels: Readonly<Record<string, string>> = {
  partner_official_api: "제휴사 공식 API 명세",
  partner_sandbox_credentials: "제휴사 Sandbox 자격증명",
  map_provider_sandbox: "지도 공급자 Sandbox",
  notification_provider_sandbox: "알림 공급자 Sandbox",
  insurance_provider_sandbox: "보험 연동 Sandbox",
  legal_counsel_approval: "법률 전문가 승인",
  privacy_officer_approval: "개인정보 책임자 승인",
  operator_approval: "운영자 최종 승인",
  main_merge: "승인된 main 병합",
  production_infrastructure: "운영 인프라",
  production_secret_provisioning: "운영 비밀정보 프로비저닝",
  field_pilot: "제한 지역 현장 파일럿",
};

export function releaseReadiness(report: ReleaseCandidateReport): string {
  const blocked = report.verdict === "BLOCKED";
  return `<section aria-labelledby="rc-title"><h1 id="rc-title">출시후보 ${report.version}</h1>
    <p role="status" aria-live="polite"><strong>${blocked ? "출시 차단" : "운영자 검토 대기"}</strong></p>
    <p>이 화면은 법률 준수, 보안 인증 또는 제휴 인증을 의미하지 않습니다.</p>
    <h2>미충족 출시 조건</h2><ul>${report.blockers.map(item => `<li>${blockerLabels[item] ?? "검토 증거 미충족"}</li>`).join("")}</ul>
    <h2>알려진 한계</h2><ul>${report.limitations.map(() => "<li>운영 전 사람 검토가 필요한 제한사항</li>").join("")}</ul>
    <p>main 병합·배포·자격증명 입력은 운영자 승인 전 실행할 수 없습니다.</p></section>`;
}
