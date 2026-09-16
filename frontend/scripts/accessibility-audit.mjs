import { readFileSync } from "node:fs";

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const app = read("src/app.ts");
const css = read("src/styles.css");
const flows = read("src/workflows.ts");
const ux = read("src/korean-ux.ts");
const pages = ["merchant", "rider", "customer", "branch-ops"].map(name => read(`${name}.html`));
const failures = [];
const requireMatch = (source, pattern, reason) => { if (!pattern.test(source)) failures.push(reason); };

for (const [index, page] of pages.entries()) {
  requireMatch(page, /<html lang="ko">/, `portal ${index + 1}: Korean language missing`);
  requireMatch(page, /name="viewport" content="width=device-width,initial-scale=1"/, `portal ${index + 1}: responsive viewport missing`);
  requireMatch(page, /<title>[^<]+<\/title>/, `portal ${index + 1}: title missing`);
}

for (const [pattern, reason] of [
  [/<main id="main" tabindex="-1">/, "focusable main landmark missing"],
  [/class="skip-link"/, "skip link missing"],
  [/role="alert"/, "error summary is not assertive"],
  [/role="status" aria-live="polite"/, "status live region missing"],
  [/for="note-\$\{action\.id\}"/, "explicit input labels missing"],
  [/aria-busy/, "loading state missing"],
  [/type="checkbox" required/, "destructive confirmation missing"],
  [/선택 알림 수신에 동의합니다/, "optional consent control missing"],
  [/내 권리와 선택/, "equal-prominence rights navigation missing"],
]) requireMatch(app, pattern, reason);

requireMatch(ux, /개봉 전 외관 이상이 있으면 먼저 신고해 주세요\./, "required delivery notice changed");
requireMatch(flows, /label: "외관 이상 신고"/, "required report button changed");
requireMatch(app + flows, /거절 불이익은 없습니다/, "rider no-retaliation notice missing");
for (const forbidden of [/checked(?:=|\s)/, /countdown/i, /남은 시간/, /자동 동의/, /확인하지 않으면/]) {
  if (forbidden.test(app + flows)) failures.push(`coercive pattern found: ${forbidden}`);
}

for (const [pattern, reason] of [
  [/min-height:44px/, "44px touch target contract missing"],
  [/@media\(max-width:20rem\)/, "320px reflow contract missing"],
  [/@media\(prefers-reduced-motion:reduce\)/, "reduced-motion contract missing"],
  [/overflow-wrap:anywhere/, "200% text wrapping contract missing"],
  [/\.error:not\(:empty\)::before\{content:"오류:/, "error depends on color alone"],
  [/\.status:not\(:empty\)::before\{content:"상태:/, "status depends on color alone"],
]) requireMatch(css, pattern, reason);

function luminance(hex) {
  const rgb = hex.match(/[0-9a-f]{2}/gi).map(value => parseInt(value, 16) / 255)
    .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
}
function contrast(a, b) {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}
for (const [foreground, background, minimum, label] of [
  ["#ffffff", "#10243f", 4.5, "header"],
  ["#ffffff", "#185fa5", 4.5, "primary button"],
  ["#17202a", "#f6f8fb", 4.5, "body"],
  ["#9b1c1c", "#ffffff", 4.5, "danger action"],
]) if (contrast(foreground, background) < minimum) failures.push(`${label}: contrast below ${minimum}:1`);

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("Korean UX/accessibility deterministic audit: PASS (static contract only)");
