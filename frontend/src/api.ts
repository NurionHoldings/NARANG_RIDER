export const routes = {
  merchantOrders: "/api/v1/merchant/orders",
  merchantQuote: "/api/v1/merchant/orders/{order_id}/quote-confirmations",
  merchantSubmit: "/api/v1/merchant/orders/{order_id}/submissions",
  merchantRiderCall: "/api/v1/merchant/orders/{order_id}/submissions",
  merchantPackaging: "/api/v1/merchant/orders/{order_id}/packaging",
  merchantOrderStatus: "/api/v1/merchant/orders/{order_id}",
  merchantCancel: "/api/v1/merchant/orders/{order_id}/cancellations",
  riderAvailability: "/api/v1/rider/availability",
  riderOffers: "/api/v1/rider/offers",
  riderOfferResponse: "/api/v1/rider/offers/{offer_id}/responses",
  riderProgress: "/api/v1/rider/assignments/{assignment_id}/progress",
  riderProofGrant: "/api/v1/rider/assignments/{assignment_id}/proof-grants",
  riderDelivery: "/api/v1/rider/assignments/{assignment_id}/delivery",
  riderIncident: "/api/v1/rider/availability",
  riderEarnings: "/api/v1/rider/assignments/{assignment_id}/earnings",
  customerProof: "/api/v1/customer/orders/{order_id}/delivery-proof",
  customerReportGrant: "/api/v1/customer/orders/{order_id}/exterior-report-grants",
  customerReport: "/api/v1/customer/orders/{order_id}/exterior-reports",
  customerReportStatus: "/api/v1/customer/orders/{order_id}/exterior-reports",
  customerNotificationPreferences: "/api/v1/customer/orders/{order_id}/delivery-proof",
  branchDashboard: "/api/v1/control/branches/{branch_id}/dashboard",
  branchReadiness: "/api/v1/control/branches/{branch_id}/activate",
  branchIncidents: "/api/v1/control/branches/{branch_id}/dashboard",
  branchCommand: "/api/v1/control/commands",
  branchCommandApproval: "/api/v1/control/commands/{command_id}/approvals",
  branchSettlementApproval: "/api/v1/control/commands/{command_id}/approvals",
  branchPilotChecklist: "/api/v1/control/branches/{branch_id}/dashboard",
  branchSandboxReports: "/api/v1/control/branches/{branch_id}/dashboard",
} as const;

export type RouteName = keyof typeof routes;
export type RequestState<T> = { kind: "idle" | "loading" | "offline" } | { kind: "error"; code: string; retryable: boolean } | { kind: "success"; value: T; replayed: boolean };

export interface SessionAdapter {
  csrfToken(): Promise<string>;
  role(): Promise<"merchant" | "rider" | "customer" | "branch-ops">;
  branchId(): Promise<string | null>;
}

export class ApiError extends Error {
  constructor(readonly status: number) { super(`API_${status}`); }
}

export class ApiClient {
  constructor(private readonly session: SessionAdapter, private readonly fetcher = fetch) {}
  async request<T>(name: RouteName, init: RequestInit & { idempotencyKey?: string } = {}): Promise<T> {
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    const branch = await this.session.branchId();
    if (branch) headers.set("X-Branch-Id", branch);
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
      headers.set("X-CSRF-Token", await this.session.csrfToken());
      if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);
    }
    const response = await this.fetcher(routes[name], { ...init, method, headers, credentials: "include", cache: "no-store" });
    if (!response.ok) throw new ApiError(response.status);
    return response.json() as Promise<T>;
  }
}

export function redact(value: string): string {
  return value.replace(/\b01\d[- ]?\d{3,4}[- ]?\d{4}\b/g, "[연락처 보호]").replace(/(?:위도|경도|lat|lng)\s*[:=]\s*-?\d+(?:\.\d+)?/gi, "[정밀위치 보호]");
}

export function assertRole(actual: string, expected: string, branchId?: string | null): void {
  if (actual !== expected || (expected === "branch-ops" && !branchId)) throw new Error("ROUTE_GUARD_DENIED");
}
