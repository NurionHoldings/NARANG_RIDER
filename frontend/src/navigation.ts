export type NavigationProviderChoice = {
  providerId: string;
  displayName: string;
  installed: boolean;
  verifiedUntil: string;
  motorcycleRouteVerified: boolean;
};

export type NavigationLaunch = {
  launchId: string;
  externalUrl: string;
  expiresAt: string;
  disclosure: string;
  fallbackLabel: string;
};

export const NAVIGATION_DISCLOSURE =
  "선택한 외부 지도 앱으로 이동합니다. 경로는 참고용이며 교통법규와 현장 안전을 우선하세요. " +
  "나랑라이더는 외부 앱 사용 중 백그라운드 위치추적을 시작하지 않습니다.";

export function renderNavigationSettings(
  providers: readonly NavigationProviderChoice[],
  selectedProviderId: string | null,
): string {
  const options = providers
    .filter((provider) => provider.installed)
    .map((provider) => {
      const checked = provider.providerId === selectedProviderId ? " checked" : "";
      const motorcycle = provider.motorcycleRouteVerified
        ? "오토바이 경로 공식지원 확인"
        : "오토바이 전용경로 미확인";
      return `<label class="choice"><input type="radio" name="navigation-provider" ` +
        `value="${escapeAttribute(provider.providerId)}"${checked}>` +
        `${escapeText(provider.displayName)} — ${motorcycle}</label>`;
    })
    .join("");

  return `<section aria-labelledby="navigation-heading">
    <h2 id="navigation-heading">지도 앱 선택</h2>
    <p>${NAVIGATION_DISCLOSURE}</p>
    <fieldset><legend>이번 배송에 사용할 앱</legend>
      ${options || "<p>사용 가능한 앱이 없습니다. 시스템 지도 또는 목적지 복사를 이용하세요.</p>"}
    </fieldset>
    <button type="button" data-navigation-launch disabled>선택한 지도 앱 열기</button>
    <button type="button" data-navigation-fallback>시스템 지도에서 열기</button>
    <p role="status" aria-live="polite" data-navigation-status></p>
  </section>`;
}

export function validateLaunchResponse(value: NavigationLaunch): URL {
  const target = new URL(value.externalUrl);
  if (!["https:", "geo:"].includes(target.protocol)) {
    throw new Error("NAVIGATION_TARGET_REJECTED");
  }
  if (!value.disclosure.includes("백그라운드 위치추적을 시작하지 않습니다")) {
    throw new Error("NAVIGATION_DISCLOSURE_REQUIRED");
  }
  if (Date.parse(value.expiresAt) <= Date.now()) {
    throw new Error("NAVIGATION_SESSION_EXPIRED");
  }
  return target;
}

function escapeText(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function escapeAttribute(value: string): string {
  return escapeText(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}