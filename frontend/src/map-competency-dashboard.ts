export interface MapCompetencyReport {
  profile_version: string;
  platform: string;
  provider_id: string;
  branch_scope: string;
  precision: number;
  recall: number;
  unsupported_claim_rate: number;
  stale_rejection_rate: number;
  unsafe_proposal_rate: number;
  contract_quality: number;
  breaking_change_detection: number;
  passed: boolean;
  fallback: string;
  report_digest: string;
}

export function competencyStatus(report: MapCompetencyReport): {
  label: string;
  tone: "safe" | "blocked";
  actions: readonly string[];
} {
  if (!report.passed) {
    return {
      label: "차단됨 · 사람 검토 필요",
      tone: "blocked",
      actions: ["공식 출처 확인", "실패 사례 재현", "에테르니언 독립 심사"],
    };
  }
  return {
    label: "합성 Shadow 기준 통과",
    tone: "safe",
    actions: ["운영자 승격 전 서명 검토", "Rollback 증거 확인"],
  };
}

export const competencyDashboardPolicy = Object.freeze({
  operationalActivation: false,
  displaysEvidenceDateConfidence: true,
  rawProviderDocumentsRenderedAsHtml: false,
  nationalPassDoesNotOverrideBranchFailure: true,
});
