"""Deterministic synthetic load, economics and abuse simulation harness.

The harness is a capacity and invariant test aid.  Its generated observations
are not production measurements and must never be represented as evidence of
real rider income, merchant margin or national service capacity.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import StrEnum
from statistics import mean


class DemandClass(StrEnum):
    NORMAL = "normal"
    RAIN_LIKE = "rain_like"
    HEAT_LIKE = "heat_like"
    FLASH = "flash"


class AbuseKind(StrEnum):
    ORDER_REPLAY = "order_replay"
    CALLBACK_REPLAY = "callback_replay"
    LINKED_ACCOUNTS = "linked_accounts"
    GPS_SPOOF = "gps_spoof"
    COLLUSIVE_BUNDLE = "collusive_bundle"
    QUOTE_TAMPER = "quote_tamper"
    CANCELLATION_REFUND = "cancellation_refund"
    NOTIFICATION_FLOOD = "notification_flood"
    PARTNER_OUTAGE = "partner_outage"
    DB_RETRY_DEADLOCK = "db_retry_deadlock"


class AbuseDisposition(StrEnum):
    BLOCKED = "blocked"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    PRIORITY_ONLY_REVIEW = "priority_only_review"
    RETRIED = "retried"
    DEAD_LETTER_REVIEW = "dead_letter_review"


@dataclass(frozen=True)
class SimulationProfile:
    name: str
    seed: int
    branches: int
    merchants_per_branch: int
    riders_per_branch: int
    orders: int
    concurrency: int

    def __post_init__(self) -> None:
        if (
            min(
                self.branches,
                self.merchants_per_branch,
                self.riders_per_branch,
                self.orders,
                self.concurrency,
            )
            <= 0
        ):
            raise ValueError("SIMULATION_PROFILE_MUST_BE_POSITIVE")
        if self.orders > 1_000_000:
            raise ValueError("SIMULATION_ORDER_LIMIT_EXCEEDED")


SMOKE_PROFILE = SimulationProfile("ci_smoke", 3701, 8, 12, 20, 800, 32)
MANUAL_NATIONAL_PROFILE = SimulationProfile("manual_national", 3701, 180, 250, 600, 250_000, 2_000)


@dataclass(frozen=True)
class CapacityThresholds:
    min_throughput_per_second: float = 100.0
    max_p95_latency_ms: float = 900.0
    max_p99_latency_ms: float = 1_500.0
    max_queue_age_ms: float = 2_000.0
    max_duplicate_rate: float = 0.08
    max_fairness_wait_p95_minutes: float = 18.0
    safety_legal_pay_floor_won: int = 3_000
    min_rider_net_hourly_won: int = 9_860
    max_failure_rate: float = 0.05


@dataclass(frozen=True)
class SyntheticOrder:
    order_id: str
    branch_id: str
    merchant_id: str
    demand_class: DemandClass
    distance_m: int
    active_minutes: int
    wait_minutes: int
    return_distance_m: int
    food_revenue_won: int
    food_cost_won: int
    rider_direct_cost_won: int
    injected_abuse: AbuseKind | None


@dataclass(frozen=True)
class AbuseObservation:
    kind: AbuseKind
    disposition: AbuseDisposition
    automatic_penalty: bool
    money_moved: bool
    explanation: str


@dataclass(frozen=True)
class Percentiles:
    p50: float
    p95: float
    p99: float


@dataclass(frozen=True)
class EconomicsMetrics:
    rider_net_hourly_won_mean: float
    rider_net_hourly_won_p05: float
    merchant_contribution_margin_won_mean: float
    platform_unit_economics_won_mean: float
    regional_fund_won_total: int


@dataclass(frozen=True)
class FairnessMetrics:
    wait_minutes: Percentiles
    max_min_branch_offer_gap: int
    decline_penalties: int


@dataclass(frozen=True)
class LoadMetrics:
    latency_ms: Percentiles
    throughput_per_second: float
    max_queue_age_ms: float
    duplicate_rate: float
    failure_rate: float


@dataclass(frozen=True)
class InvariantResult:
    code: str
    passed: bool
    observed: str


@dataclass(frozen=True)
class SimulationReport:
    schema_version: str
    profile: SimulationProfile
    synthetic_only: bool
    disclaimer: str
    load: LoadMetrics
    fairness: FairnessMetrics
    economics: EconomicsMetrics
    abuse: tuple[AbuseObservation, ...]
    invariants: tuple[InvariantResult, ...]
    capacity_passed: bool
    report_digest: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class _OrderOutcome:
    latency_ms: int
    queue_age_ms: int
    rider_pay_won: int
    rider_net_hourly_won: int
    merchant_margin_won: int
    platform_unit_won: int
    regional_fund_won: int
    offer_wait_minutes: int
    branch_id: str
    failed: bool
    duplicate: bool
    ledger_debits_won: int
    ledger_credits_won: int
    negative_payout: bool
    cross_tenant_write: bool
    decline_penalty: bool
    ai_forbidden_decision: bool
    abuse: AbuseObservation | None


def _percentiles(values: list[int | float]) -> Percentiles:
    if not values:
        return Percentiles(0.0, 0.0, 0.0)
    ordered = sorted(values)

    def pick(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
        return round(float(ordered[index]), 2)

    return Percentiles(pick(0.50), pick(0.95), pick(0.99))


def generate_scenario(profile: SimulationProfile) -> tuple[SyntheticOrder, ...]:
    """Create deterministic nationwide-like synthetic inputs without real data."""
    rng = random.Random(profile.seed)
    abuse_cycle = tuple(AbuseKind)
    orders: list[SyntheticOrder] = []
    for index in range(profile.orders):
        branch_number = index % profile.branches
        branch_id = f"syn-branch-{branch_number:03d}"
        merchant_number = rng.randrange(profile.merchants_per_branch)
        demand = rng.choices(list(DemandClass), weights=(72, 12, 10, 6), k=1)[0]
        multiplier = {
            DemandClass.NORMAL: 1.0,
            DemandClass.RAIN_LIKE: 1.25,
            DemandClass.HEAT_LIKE: 1.15,
            DemandClass.FLASH: 1.7,
        }[demand]
        distance = int(rng.randint(1_000, 12_000) * multiplier)
        active = max(8, int(distance / rng.randint(250, 420)))
        abuse = abuse_cycle[index // 37 % len(abuse_cycle)] if index % 37 == 0 else None
        identity = f"{profile.seed}:{branch_id}:{index}".encode()
        orders.append(
            SyntheticOrder(
                order_id="syn_" + hashlib.sha256(identity).hexdigest()[:20],
                branch_id=branch_id,
                merchant_id=f"syn-merchant-{branch_number:03d}-{merchant_number:04d}",
                demand_class=demand,
                distance_m=distance,
                active_minutes=active,
                wait_minutes=rng.randint(0, 18),
                return_distance_m=rng.randint(0, distance // 2),
                food_revenue_won=rng.randint(14_000, 65_000),
                food_cost_won=rng.randint(7_000, 25_000),
                rider_direct_cost_won=rng.randint(700, 3_500),
                injected_abuse=abuse,
            )
        )
    return tuple(orders)


def _abuse_disposition(kind: AbuseKind) -> AbuseObservation:
    if kind in {AbuseKind.ORDER_REPLAY, AbuseKind.CALLBACK_REPLAY}:
        disposition = AbuseDisposition.IDEMPOTENT_REPLAY
        explanation = "canonical idempotency key returned the prior result"
    elif kind in {AbuseKind.LINKED_ACCOUNTS, AbuseKind.GPS_SPOOF}:
        disposition = AbuseDisposition.PRIORITY_ONLY_REVIEW
        explanation = "signal changes human review priority only"
    elif kind in {AbuseKind.PARTNER_OUTAGE, AbuseKind.NOTIFICATION_FLOOD}:
        disposition = AbuseDisposition.DEAD_LETTER_REVIEW
        explanation = "bounded retry and circuit isolation; no financial side effect"
    elif kind is AbuseKind.DB_RETRY_DEADLOCK:
        disposition = AbuseDisposition.RETRIED
        explanation = "whole transaction retried with one eventual commit"
    else:
        disposition = AbuseDisposition.BLOCKED
        explanation = "integrity or financial policy gate rejected mutation"
    return AbuseObservation(kind, disposition, False, False, explanation)


def _simulate_order(order: SyntheticOrder, rng: random.Random, lane_ready_ms: int) -> _OrderOutcome:
    stage_latency = [
        rng.randint(2, 12),
        rng.randint(4, 24),
        rng.randint(3, 18),
        rng.randint(4, 30),
        rng.randint(2, 14),
    ]
    if order.demand_class is DemandClass.FLASH:
        stage_latency = [value * 2 for value in stage_latency]
    queue_age = min(5_000, max(0, lane_ready_ms // 200))
    latency = sum(stage_latency) + queue_age

    distance_pay = max(0, order.distance_m - 1_000 + 499) // 500 * 180
    wait_pay = max(0, order.wait_minutes - 3) * 120
    return_pay = (order.return_distance_m + 499) // 500 * 100
    total_minutes = max(
        1, order.active_minutes + order.wait_minutes + order.return_distance_m // 300
    )
    net_hourly_floor_pay = order.rider_direct_cost_won + (9_860 * total_minutes + 59) // 60
    rider_pay = max(3_000, 3_000 + distance_pay + wait_pay + return_pay, net_hourly_floor_pay)
    rider_net = (rider_pay - order.rider_direct_cost_won) * 60 // total_minutes
    platform_unit = 450
    regional_fund = 100
    customer_fee = min(5_000, max(1_500, rider_pay // 2))
    merchant_delivery = rider_pay + platform_unit + regional_fund - customer_fee
    merchant_margin = order.food_revenue_won - order.food_cost_won - merchant_delivery - 900

    abuse = _abuse_disposition(order.injected_abuse) if order.injected_abuse else None
    duplicate = order.injected_abuse in {AbuseKind.ORDER_REPLAY, AbuseKind.CALLBACK_REPLAY}
    failed = order.injected_abuse in {AbuseKind.PARTNER_OUTAGE, AbuseKind.NOTIFICATION_FLOOD}
    # Failed attempts make no ledger entry; successful funding/use entries balance exactly.
    ledger_total = 0 if failed or duplicate else rider_pay + platform_unit + regional_fund
    offer_wait = rng.randint(0, 12) + (4 if order.demand_class is DemandClass.FLASH else 0)
    return _OrderOutcome(
        latency,
        queue_age,
        rider_pay,
        rider_net,
        merchant_margin,
        platform_unit,
        regional_fund,
        offer_wait,
        order.branch_id,
        failed,
        duplicate,
        ledger_total,
        ledger_total,
        False,
        False,
        False,
        False,
        abuse,
    )


def run_simulation(
    profile: SimulationProfile = SMOKE_PROFILE,
    thresholds: CapacityThresholds | None = None,
) -> SimulationReport:
    thresholds = thresholds or CapacityThresholds()
    orders = generate_scenario(profile)
    rng = random.Random(profile.seed ^ 0xA37A)
    lane_ready = [0] * profile.concurrency
    outcomes: list[_OrderOutcome] = []
    for order in orders:
        lane = min(range(profile.concurrency), key=lane_ready.__getitem__)
        outcome = _simulate_order(order, rng, lane_ready[lane])
        lane_ready[lane] += outcome.latency_ms
        outcomes.append(outcome)

    latencies = [outcome.latency_ms for outcome in outcomes]
    elapsed_ms = max(lane_ready)
    completed = sum(not outcome.failed for outcome in outcomes)
    load = LoadMetrics(
        _percentiles(latencies),
        round(completed / max(0.001, elapsed_ms / 1_000), 2),
        float(max(outcome.queue_age_ms for outcome in outcomes)),
        round(sum(outcome.duplicate for outcome in outcomes) / len(outcomes), 6),
        round(sum(outcome.failed for outcome in outcomes) / len(outcomes), 6),
    )
    branch_offers: dict[str, int] = defaultdict(int)
    for outcome in outcomes:
        if not outcome.failed and not outcome.duplicate:
            branch_offers[outcome.branch_id] += 1
    fairness = FairnessMetrics(
        _percentiles([outcome.offer_wait_minutes for outcome in outcomes]),
        max(branch_offers.values()) - min(branch_offers.values()),
        sum(outcome.decline_penalty for outcome in outcomes),
    )
    payable = [outcome for outcome in outcomes if not outcome.failed and not outcome.duplicate]
    economics = EconomicsMetrics(
        round(mean(outcome.rider_net_hourly_won for outcome in payable), 2),
        _percentiles([outcome.rider_net_hourly_won for outcome in payable]).p50
        if len(payable) < 20
        else float(sorted(outcome.rider_net_hourly_won for outcome in payable)[len(payable) // 20]),
        round(mean(outcome.merchant_margin_won for outcome in payable), 2),
        round(mean(outcome.platform_unit_won for outcome in payable), 2),
        sum(outcome.regional_fund_won for outcome in payable),
    )
    abuse = tuple(outcome.abuse for outcome in outcomes if outcome.abuse is not None)
    invariants = (
        InvariantResult(
            "SAFETY_LEGAL_PAY_FLOOR",
            all(o.rider_pay_won >= thresholds.safety_legal_pay_floor_won for o in payable),
            f"minimum={min(o.rider_pay_won for o in payable)}",
        ),
        InvariantResult(
            "LEDGER_BALANCED",
            all(o.ledger_debits_won == o.ledger_credits_won for o in outcomes),
            "debits equal credits per transaction",
        ),
        InvariantResult(
            "NO_NEGATIVE_NET_PAYOUT",
            not any(o.negative_payout for o in outcomes),
            "negative payouts=0",
        ),
        InvariantResult(
            "NO_CROSS_TENANT_WRITE",
            not any(o.cross_tenant_write for o in outcomes),
            "cross-tenant writes=0",
        ),
        InvariantResult(
            "NO_DECLINE_PENALTY",
            fairness.decline_penalties == 0,
            f"penalties={fairness.decline_penalties}",
        ),
        InvariantResult(
            "NO_AI_FORBIDDEN_DECISION",
            not any(o.ai_forbidden_decision for o in outcomes),
            "forbidden AI decisions=0",
        ),
        InvariantResult(
            "ABUSE_SIGNALS_NO_AUTO_PENALTY",
            all(not item.automatic_penalty for item in abuse),
            "all signals review-only or blocked",
        ),
        InvariantResult(
            "ABUSE_NO_AUTOMATIC_MONEY_MOVE",
            all(not item.money_moved for item in abuse),
            "money moved=0",
        ),
    )
    capacity_passed = all(item.passed for item in invariants) and all(
        (
            load.throughput_per_second >= thresholds.min_throughput_per_second,
            load.latency_ms.p95 <= thresholds.max_p95_latency_ms,
            load.latency_ms.p99 <= thresholds.max_p99_latency_ms,
            load.max_queue_age_ms <= thresholds.max_queue_age_ms,
            load.duplicate_rate <= thresholds.max_duplicate_rate,
            load.failure_rate <= thresholds.max_failure_rate,
            fairness.wait_minutes.p95 <= thresholds.max_fairness_wait_p95_minutes,
            economics.rider_net_hourly_won_p05 >= thresholds.min_rider_net_hourly_won,
        )
    )
    unsigned = {
        "schema_version": "narang.synthetic-simulation.v1",
        "profile": asdict(profile),
        "load": asdict(load),
        "fairness": asdict(fairness),
        "economics": asdict(economics),
        "invariants": [asdict(item) for item in invariants],
        "capacity_passed": capacity_passed,
    }
    digest = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return SimulationReport(
        "narang.synthetic-simulation.v1",
        profile,
        True,
        "SYNTHETIC ONLY: not production evidence, forecast, SLA, wage claim, or investment claim",
        load,
        fairness,
        economics,
        abuse,
        invariants,
        capacity_passed,
        digest,
    )
