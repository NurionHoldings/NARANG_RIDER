export type DeviceLaunchState = "available" | "app_unavailable" | "update_required" | "os_restricted" | "offline";

export interface DeviceLaunchInstruction {
  launchId: string;
  targetUrl: string;
  expiresAt: string;
  state: DeviceLaunchState;
  message: string;
}

export interface UserGesture {
  isTrusted: boolean;
  timestamp: number;
}

export function openNavigationFromGesture(
  instruction: DeviceLaunchInstruction,
  gesture: UserGesture,
  opener: (url: string, target: string, features: string) => Window | null,
): "opened" | "fallback" {
  if (!gesture.isTrusted || Date.now() - gesture.timestamp > 2000) {
    throw new Error("FOREGROUND_USER_GESTURE_REQUIRED");
  }
  if (instruction.state !== "available" || !instruction.targetUrl) return "fallback";
  if (Date.parse(instruction.expiresAt) <= Date.now()) throw new Error("LAUNCH_EXPIRED");
  const target = new URL(instruction.targetUrl);
  if (!["https:", "geo:"].includes(target.protocol) || target.username || target.password) {
    throw new Error("UNCERTIFIED_NAVIGATION_TARGET");
  }
  const launched = opener(target.toString(), "_blank", "noopener,noreferrer");
  if (launched) launched.opener = null;
  return launched ? "opened" : "fallback";
}

export function navigationRecoveryMessage(state: DeviceLaunchState): string {
  const messages: Record<DeviceLaunchState, string> = {
    available: "선택한 지도 앱을 엽니다.",
    app_unavailable: "지도 앱을 사용할 수 없습니다. 시스템 지도 또는 복사를 선택해 주세요.",
    update_required: "지도 앱 업데이트가 필요합니다. 다른 방법을 선택해 주세요.",
    os_restricted: "기기 설정에서 외부 앱 열기가 제한되었습니다.",
    offline: "인터넷에 연결되지 않았습니다. 자동 재시도하지 않습니다.",
  };
  return messages[state];
}

export const navigationPwaPolicy = Object.freeze({
  cacheLaunchUrls: false,
  cacheDestinations: false,
  backgroundLaunch: false,
  analyticsInstalledApps: false,
});