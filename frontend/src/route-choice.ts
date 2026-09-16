export type RouteObjective = "safest" | "balanced" | "lowest_cost" | "shortest";

export interface RouteChoiceCard {
  routeId: string;
  distanceM: number;
  durationS: number;
  tollWon: number;
  returnDistanceM: number;
  uncertaintyS: number;
  reasons: readonly string[];
  acceptedPayFloorWon: number;
  supplementalReviewWon: number;
  motorcycleVerified: boolean;
}

export const objectiveLabels: Record<RouteObjective, string> = {
  safest: "안전 우선",
  balanced: "균형",
  lowest_cost: "비용 최소",
  shortest: "시간 최소",
};

export function canRiderSelect(card: RouteChoiceCard): boolean {
  return card.motorcycleVerified && card.distanceM > 0 && card.durationS > 0;
}

export const routeChoiceRights = Object.freeze({
  title: "경로는 라이더가 선택합니다",
  advisory: "아르카온의 순서는 참고용이며 강제되지 않습니다.",
  deviation: "선택 경로 이탈만으로 불이익을 주지 않습니다.",
  pay: "수락한 보수는 경로 선택 때문에 줄어들지 않습니다.",
  offline: "오프라인에서는 자동 선택하지 않고 안전 정지를 선택할 수 있습니다.",
  persistCoordinates: false,
  continuousTracking: false,
});
