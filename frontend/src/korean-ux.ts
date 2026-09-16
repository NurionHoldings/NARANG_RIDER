export const REQUIRED_REPORT_NOTICE = "개봉 전 외관 이상이 있으면 먼저 신고해 주세요.";
export const REQUIRED_REPORT_BUTTON = "외관 이상 신고";

export const statusKorean = Object.freeze({
  idle: "대기",
  submitting: "처리 중",
  offline: "연결 끊김",
  conflict: "변경 충돌",
  expired: "로그인 만료",
  error: "처리 실패",
  success: "처리 완료",
});

export function formatWon(value: number): string {
  if (!Number.isSafeInteger(value)) throw new Error("INVALID_WON");
  return `${new Intl.NumberFormat("ko-KR").format(value)}원`;
}

export function formatKoreanDateTime(value: Date): string {
  if (Number.isNaN(value.getTime())) throw new Error("INVALID_DATE");
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Seoul",
  }).format(value);
}

export const rights = Object.freeze([
  "주문·배차 제안 거절",
  "동의 철회",
  "환불 요청",
  "결정에 이의제기",
  "개인정보 열람·정정·삭제·처리제한",
]);
