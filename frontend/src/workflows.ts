import { ApiError, type ApiClient, type RouteName } from "./api.js";

export type PortalRole = "merchant" | "rider" | "customer" | "branch-ops";
export type MutationState = "idle" | "submitting" | "offline" | "conflict" | "expired" | "error" | "success";

export interface JourneyResult {
  state: MutationState;
  message: string;
  focusTarget: "form" | "error" | "status";
}

const messages: Readonly<Record<number, string>> = {
  401: "로그인이 만료되었습니다. 다시 로그인한 뒤 입력 내용을 확인해 주세요.",
  403: "이 작업을 수행할 권한이 없습니다.",
  409: "다른 변경이 먼저 저장되었습니다. 최신 내용을 불러와 다시 확인해 주세요.",
  422: "입력 내용을 확인해 주세요. 잘못된 항목으로 이동합니다.",
  429: "요청이 많습니다. 잠시 후 직접 다시 시도해 주세요.",
};

export class ActiveFormMutation {
  private idempotencyKey: string | null = null;
  private pending = false;
  private retry: (() => Promise<JourneyResult>) | null = null;

  constructor(private readonly api: ApiClient, private readonly refresh: () => Promise<void>) {}

  async submit(route: RouteName, body: object): Promise<JourneyResult> {
    if (this.pending) return { state: "submitting", message: "처리 중입니다.", focusTarget: "status" };
    this.idempotencyKey ??= crypto.randomUUID();
    this.pending = true;
    const execute = async (): Promise<JourneyResult> => {
      try {
        await this.api.request(route, {
          method: "POST",
          body: JSON.stringify(body),
          headers: { "Content-Type": "application/json" },
          idempotencyKey: this.idempotencyKey ?? undefined,
        });
        this.idempotencyKey = null;
        this.retry = null;
        return { state: "success", message: "안전하게 저장되었습니다.", focusTarget: "status" };
      } catch (error) {
        if (error instanceof TypeError) {
          this.retry = execute;
          return { state: "offline", message: "연결되지 않았습니다. 자동 재전송하지 않습니다. 확인 후 다시 시도해 주세요.", focusTarget: "error" };
        }
        const status = error instanceof ApiError ? error.status : 500;
        if (status === 409) await this.refresh();
        const state: MutationState = status === 401 ? "expired" : status === 409 ? "conflict" : "error";
        return { state, message: messages[status] ?? "처리하지 못했습니다. 입력은 유지되며 자동으로 다시 보내지 않습니다.", focusTarget: "error" };
      } finally {
        this.pending = false;
      }
    };
    return execute();
  }

  async retryExplicitly(): Promise<JourneyResult> {
    if (!this.retry) return { state: "idle", message: "다시 보낼 요청이 없습니다.", focusTarget: "form" };
    this.pending = true;
    return this.retry();
  }
}

export interface JourneyAction {
  id: string;
  label: string;
  route: RouteName;
  destructive?: boolean;
}

export const journeys: Readonly<Record<PortalRole, readonly JourneyAction[]>> = {
  merchant: [
    { id: "draft", label: "주문 초안 저장", route: "merchantOrders" },
    { id: "quote", label: "공개견적 확인", route: "merchantQuote" },
    { id: "submit", label: "주문 접수", route: "merchantSubmit" },
    { id: "call", label: "라이더 호출", route: "merchantRiderCall" },
    { id: "packaging", label: "포장·봉인 기록", route: "merchantPackaging" },
    { id: "status", label: "주문상태 확인", route: "merchantOrderStatus" },
    { id: "cancel", label: "주문 취소 요청", route: "merchantCancel", destructive: true },
  ],
  rider: [
    { id: "availability", label: "운행 가능상태 변경", route: "riderAvailability" },
    { id: "offers", label: "배차 제안 새로고침", route: "riderOffers" },
    { id: "accept", label: "배차 수락", route: "riderOfferResponse" },
    { id: "arrive", label: "가게 도착", route: "riderProgress" },
    { id: "pickup", label: "픽업 완료", route: "riderProgress" },
    { id: "proof", label: "촬영권한 요청", route: "riderProofGrant" },
    { id: "deliver", label: "배송 완료", route: "riderDelivery" },
    { id: "incident", label: "안전 중지 및 지원 요청", route: "riderIncident", destructive: true },
    { id: "earnings", label: "수익 확인", route: "riderEarnings" },
  ],
  customer: [
    { id: "proof", label: "마스킹 배송사진 확인", route: "customerProof" },
    { id: "grant", label: "실시간 촬영 시작", route: "customerReportGrant" },
    { id: "capture", label: "외관 사진 촬영", route: "customerReport" },
    { id: "report", label: "외관 이상 신고", route: "customerReport" },
    { id: "status", label: "신고상태 확인", route: "customerReportStatus" },
    { id: "notifications", label: "알림 설정 저장", route: "customerNotificationPreferences" },
  ],
  "branch-ops": [
    { id: "aggregate", label: "집계 현황 새로고침", route: "branchDashboard" },
    { id: "readiness", label: "준비상태 확인", route: "branchReadiness" },
    { id: "incidents", label: "사고·장애 확인", route: "branchIncidents" },
    { id: "pause", label: "운영 일시정지 요청", route: "branchCommand", destructive: true },
    { id: "pause-approval", label: "일시정지 독립 승인", route: "branchCommandApproval" },
    { id: "settlement", label: "정산 독립 승인", route: "branchSettlementApproval" },
    { id: "pilot", label: "파일럿 체크리스트", route: "branchPilotChecklist" },
    { id: "sandbox", label: "Sandbox 보고서", route: "branchSandboxReports" },
  ],
};
