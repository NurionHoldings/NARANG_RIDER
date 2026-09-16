export type CertificationVerdict = "PASS" | "FAIL" | "BLOCKED";

export interface PartnerCertificationSummary {
  profile: string;
  adapterVersion: string;
  verdict: CertificationVerdict;
  evidenceDigest: string;
  checkCounts: Readonly<Record<CertificationVerdict, number>>;
  liveCertified: false;
}

const labels: Readonly<Record<CertificationVerdict, string>> = {
  PASS: "계약 검증 통과",
  FAIL: "계약 검증 실패",
  BLOCKED: "공식 명세 대기",
};

export function renderPartnerCertificationReport(report: PartnerCertificationSummary): string {
  const digest = report.evidenceDigest.replace(/[^a-f0-9]/g, "").slice(0, 12);
  return `<section aria-labelledby="partner-certification-title">
    <h2 id="partner-certification-title">제휴사 Sandbox 적합성</h2>
    <p><strong>${report.profile}</strong> · ${labels[report.verdict]}</p>
    <dl><dt>어댑터 버전</dt><dd>${report.adapterVersion}</dd>
    <dt>증거 식별자</dt><dd>${digest}</dd></dl>
    <p role="note">로컬 합성 계약 증거이며 실제 제휴사 인증·운영 연결을 의미하지 않습니다.</p>
    <p>공식 API 명세와 승인 자격증명 확인 전 live certified 표시는 항상 거짓입니다.</p>
  </section>`;
}
