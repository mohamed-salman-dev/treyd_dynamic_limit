"""Core Dynamic Limit computation — pure functions, no I/O, no wall-clock.

Payouts are daily. Each is weighted by whether it routed to Treyd (routed = full, provisional =
discounted) and excluded if encumbered. Two views are derived from the daily series:
  • Trailing_Flow — a fixed 90-day window from as_of, recency-weighted across sub-buckets.
  • Seasonal curve — daily aggregated to calendar months (complete months only), for the
    seasonal index and LTM level.
`as_of` truncates everything, so a past anchor is an honest, no-lookahead backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import config as C
from .models import (
    ChannelTrace,
    CurrencyLimit,
    LegalSecurityTrace,
    MerchantLimitRequest,
    MerchantLimitResponse,
    MerchantTrace,
    Payout,
)


class FxRateMissing(ValueError):
    """Raised when a routed currency has no FX rate to the revenue currency."""


def _add_months(d: date, n: int) -> date:
    total = d.month - 1 + n
    return date(d.year + total // 12, total % 12 + 1, 1)


def _is_month_end(d: date) -> bool:
    return (d + timedelta(days=1)).month != d.month


# ────────────────────────────────────────────────────────────────────────────────────────
# Daily series helpers
# ────────────────────────────────────────────────────────────────────────────────────────
def _weighted_daily(payouts: list[Payout], as_of: date) -> dict[date, float]:
    """{day: weighted free amount} for days ≤ as_of. Routed → full weight, else provisional;
    encumbered excluded."""
    out: dict[date, float] = {}
    for p in payouts:
        if p.date > as_of or p.encumbered:
            continue
        weight = C.ROUTED_WEIGHT if p.routed_to_treyd else C.PROVISIONAL_WEIGHT
        out[p.date] = out.get(p.date, 0.0) + p.amount * weight
    return out


def _routed_daily(payouts: list[Payout], as_of: date) -> dict[date, float]:
    """{day: full amount} for routed, non-encumbered days ≤ as_of. Used for capture."""
    out: dict[date, float] = {}
    for p in payouts:
        if p.date > as_of or p.encumbered or not p.routed_to_treyd:
            continue
        out[p.date] = out.get(p.date, 0.0) + p.amount
    return out


def _trailing_flow(daily: dict[date, float], as_of: date) -> float:
    """Recency-weighted flow over a fixed window: the window is split into equal sub-buckets,
    one per weight, and each bucket's summed flow is weighted. Yields a representative month."""
    n = len(C.TRAILING_WEIGHTS)
    bucket_days = C.TRAILING_WINDOW_DAYS / n
    total = 0.0
    for i, w in enumerate(C.TRAILING_WEIGHTS):
        hi = as_of - timedelta(days=round(bucket_days * i))
        lo = as_of - timedelta(days=round(bucket_days * (i + 1)))
        total += w * sum(v for d, v in daily.items() if lo < d <= hi)
    return total


def _monthly_from_daily(daily: dict[date, float], as_of: date) -> dict[date, float]:
    """Aggregate a daily series into calendar-month buckets keyed by month-start. The as_of month
    is dropped unless as_of is its last day, so a partial month never pollutes seasonal/LTM."""
    out: dict[date, float] = {}
    for d, v in daily.items():
        key = date(d.year, d.month, 1)
        out[key] = out.get(key, 0.0) + v
    if not _is_month_end(as_of):
        out.pop(date(as_of.year, as_of.month, 1), None)
    return out


def _distinct_months(payouts: list[Payout], as_of: date) -> int:
    return len({(p.date.year, p.date.month) for p in payouts if p.date <= as_of})


def _seasonal_index(monthly: dict[date, float]) -> dict[int, float] | None:
    """Normalized seasonal shape (mean 1.0) by calendar month, or None if < 12 months.

    month_avg[c] / overall, 3-month centered (circular) smoothed, then re-normalized to mean 1.0
    so uneven calendar coverage in partial-year windows cannot inflate every expectation.
    """
    if len(monthly) < C.MIN_MONTHS_SEASONAL:
        return None
    buckets: dict[int, list[float]] = {c: [] for c in range(1, 13)}
    for d, val in monthly.items():
        buckets[d.month].append(val)
    if any(not buckets[c] for c in range(1, 13)):
        return None  # not all 12 calendar months represented
    month_avg = {c: sum(v) / len(v) for c, v in buckets.items()}
    overall = sum(month_avg.values()) / 12.0  # equal weight per calendar month
    if overall <= 0:
        return None
    raw = {c: month_avg[c] / overall for c in range(1, 13)}
    smoothed = {c: (raw[(c - 2) % 12 + 1] + raw[c] + raw[c % 12 + 1]) / 3.0 for c in range(1, 13)}
    norm = sum(smoothed.values()) / 12.0
    if norm <= 0:
        return None
    return {c: smoothed[c] / norm for c in range(1, 13)}


def _ltm_avg(monthly: dict[date, float], anchor: date) -> float | None:
    """Mean over the trailing 12 months [anchor-11 .. anchor]; None if the window predates data."""
    if _add_months(anchor, -11) < min(monthly):
        return None
    return sum(monthly.get(_add_months(anchor, -k), 0.0) for k in range(0, 12)) / 12.0


# ────────────────────────────────────────────────────────────────────────────────────────
# Currency streams — one channel fans out into one stream per settlement currency
# ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Stream:
    channel_id: str
    channel_type: str
    currency: str
    payouts: list[Payout]
    routing_confirmation: float


def _streams(req: MerchantLimitRequest) -> list[_Stream]:
    """Split each channel's multi-currency daily payouts into one stream per currency."""
    out: list[_Stream] = []
    for ch in req.channels:
        for ccy in sorted({p.currency for p in ch.payouts}):
            out.append(
                _Stream(
                    channel_id=ch.channel_id,
                    channel_type=ch.channel_type,
                    currency=ccy,
                    payouts=[p for p in ch.payouts if p.currency == ccy],
                    routing_confirmation=ch.routing_confirmation,
                )
            )
    return out


# ────────────────────────────────────────────────────────────────────────────────────────
# Merchant-level factors
# ────────────────────────────────────────────────────────────────────────────────────────
def _legal_security(instruments: list[str] | None) -> LegalSecurityTrace:
    applied = C.LS_DEFAULT_INSTRUMENTS if instruments is None else instruments
    raw_platform = C.LS_BASE + sum(C.INSTRUMENT_SCORES[i]["platform"] for i in applied)
    raw_b2b = C.LS_BASE + sum(C.INSTRUMENT_SCORES[i]["b2b"] for i in applied)
    return LegalSecurityTrace(
        instruments_applied=list(applied),
        raw_platform=round(raw_platform, 6),
        raw_b2b=round(raw_b2b, 6),
        norm_platform=round(min(raw_platform / C.LS_DENOMINATOR, 1.0), 6),
        norm_b2b=round(min(raw_b2b / C.LS_DENOMINATOR, 1.0), 6),
    )


def _map_pbs(pbs: int | None) -> float:
    if pbs is None:
        return C.MERCHANT_SCORE_DEFAULT
    if pbs >= 8:
        return 1.0
    if pbs >= 6:
        return 0.8
    return 0.6


def _map_rating(rating: int | None) -> float:
    if rating is None:
        return C.MERCHANT_SCORE_DEFAULT
    if rating >= 8:
        return 1.0
    if rating >= 6:
        return 0.8
    if rating >= 4:
        return 0.6
    return 0.0  # below appetite


def _jurisdiction(country: str | None) -> float:
    return C.JURISDICTION_FACTORS.get(country, C.JURISDICTION_DEFAULT) if country else C.JURISDICTION_DEFAULT


def _base_months(routing_days: int, verified_api_months: int, override: float | None) -> tuple[float, float, bool]:
    """Returns (base_months, effective_tenure, override_used).

    Effective_Tenure (months) = routing_days/30 + half the verified history (capped at 6 months of
    credit). 2.0 / 3.0 / 4.0 are all earned by tenure; the override is reserved for above 4.0.
    """
    credit = min(C.HISTORY_CREDIT_FACTOR * verified_api_months, C.HISTORY_CREDIT_CAP)
    effective_tenure = routing_days / C.DAYS_PER_MONTH + credit
    if override is not None:
        return float(override), effective_tenure, True
    if effective_tenure < C.TENURE_MID_THRESHOLD:
        base = C.BASE_MONTHS_ENTRY
    elif effective_tenure < C.TENURE_HIGH_THRESHOLD:
        base = C.BASE_MONTHS_MID
    else:
        base = C.BASE_MONTHS_HIGH
    return base, effective_tenure, False


def _fx_rate(currency: str, revenue_currency: str, fx_rates: dict[str, float]) -> float:
    if currency == revenue_currency:
        return 1.0
    rate = fx_rates.get(currency)
    if rate is None:
        raise FxRateMissing(f"no fx rate for {currency}->{revenue_currency}")
    return rate


def _capture_score(streams: list[_Stream], req: MerchantLimitRequest, as_of: date) -> tuple[float, float, float]:
    """Returns (capture, capture_score, total_routed_annualized) in revenue_currency.

    Numerator is actual routed flow only (full value), summed over the trailing 365 days and
    annualized over the observed routed span, FX-normalized into the revenue currency.
    """
    lo = as_of - timedelta(days=365)
    routed_annual = 0.0
    for s in streams:
        rd = _routed_daily(s.payouts, as_of)
        dates = [d for d in rd if lo < d <= as_of]
        if not dates:
            continue
        total = sum(rd[d] for d in dates)
        span_days = min((as_of - min(dates)).days + 1, 365)
        annualized = total * 365.0 / max(span_days, 1)
        routed_annual += annualized * _fx_rate(s.currency, req.revenue_currency, req.fx_rates)

    if not req.total_revenue_ltm or routed_annual <= 0:
        return 0.0, C.CAPTURE_FLOOR, routed_annual
    capture = routed_annual / req.total_revenue_ltm
    score = min(1.0, max(C.CAPTURE_FLOOR, capture / C.CAPTURE_ANCHOR))
    return capture, score, routed_annual


# ────────────────────────────────────────────────────────────────────────────────────────
# Per-stream computation
# ────────────────────────────────────────────────────────────────────────────────────────
def _compute_stream(
    s: _Stream, req: MerchantLimitRequest, as_of: date, ls: LegalSecurityTrace, jurisdiction: float
) -> ChannelTrace:
    weighted = _weighted_daily(s.payouts, as_of)
    routed = _routed_daily(s.payouts, as_of)

    trailing_flow = _trailing_flow(weighted, as_of)

    # routed share over the trailing window (full value, unweighted) — transparency only
    win_lo = as_of - timedelta(days=C.TRAILING_WINDOW_DAYS)
    win = [p for p in s.payouts if win_lo < p.date <= as_of and not p.encumbered]
    win_total = sum(p.amount for p in win)
    win_routed = sum(p.amount for p in win if p.routed_to_treyd)
    routed_share = win_routed / win_total if win_total > 0 else 0.0

    # Seasonal curve from monthly-aggregated weighted flow (complete months only).
    monthly = _monthly_from_daily(weighted, as_of)
    sindex = _seasonal_index(monthly)
    anchor = max(monthly) if monthly else None
    ltm = _ltm_avg(monthly, anchor) if (monthly and anchor is not None) else None
    as_of_month = date(as_of.year, as_of.month, 1)

    def expected(m: date) -> float | None:
        if sindex is None or ltm is None:
            return None
        return ltm * sindex[m.month]

    seasonal_eligible = sindex is not None and ltm is not None
    expected_anchor = expected(anchor) if anchor is not None else None
    forward_vals = [e for k in range(1, req.tenor_months + 1) if (e := expected(_add_months(as_of_month, k))) is not None]
    forward_expected = sum(forward_vals) / len(forward_vals) if forward_vals else None

    floor_guard_ok = True
    floor_active = False
    flow_base = trailing_flow
    if seasonal_eligible and forward_expected is not None and anchor is not None:
        actual_anchor = monthly.get(anchor, 0.0)
        floor_guard_ok = expected_anchor is None or actual_anchor >= C.FLOOR_GUARD_THRESHOLD * expected_anchor
        if floor_guard_ok:
            floor_candidate = C.SEASONAL_FLOOR_GAMMA * forward_expected
            if floor_candidate > trailing_flow:
                flow_base = floor_candidate
                floor_active = True

    # Flow_Score — inert until ≥2 routed months; then recent actual vs out-of-sample expectation.
    def expected_oos(m: date) -> float | None:
        if anchor is None:
            return None
        cutoff = _add_months(anchor, -1)
        basis = {k: v for k, v in monthly.items() if k <= cutoff}
        if not basis:
            return None
        si = _seasonal_index(basis)
        lv = _ltm_avg(basis, max(basis))
        if si is None or lv is None:
            return None
        return lv * si[m.month]

    routed_months = len(_monthly_from_daily(routed, as_of))
    flow_score = 1.0
    if routed_months >= C.FLOW_SCORE_MIN_ROUTED_MONTHS and anchor is not None:
        e = expected_oos(anchor)
        if e and e > 0:
            flow_score = min(1.0, max(C.FLOW_SCORE_FLOOR, monthly.get(anchor, 0.0) / e))

    reg = C.CHANNEL_REGISTRY[s.channel_type]
    v_norm = C.VERIFICATION_NORMS[reg["verification_tier"]]
    ls_norm = ls.norm_platform if reg["flow_type"] == "platform" else ls.norm_b2b
    quality_q = v_norm * ls_norm * jurisdiction * flow_score
    contribution = flow_base * quality_q * s.routing_confirmation

    return ChannelTrace(
        channel_id=s.channel_id,
        channel_type=s.channel_type,
        currency=s.currency,
        trailing_flow=round(trailing_flow, 2),
        routed_share=round(routed_share, 6),
        seasonal_eligible=seasonal_eligible,
        ltm_avg=round(ltm, 2) if ltm is not None else None,
        expected_flow_last_month=round(expected_anchor, 2) if expected_anchor is not None else None,
        forward_expected_flow=round(forward_expected, 2) if forward_expected is not None else None,
        seasonal_floor_active=floor_active,
        floor_guard_ok=floor_guard_ok,
        flow_base=round(flow_base, 2),
        verification_norm=round(v_norm, 6),
        legal_security_norm=round(ls_norm, 6),
        jurisdiction=round(jurisdiction, 6),
        flow_score=round(flow_score, 6),
        quality_q=round(quality_q, 6),
        routing_confirmation=round(s.routing_confirmation, 6),
        channel_contribution=round(contribution, 2),
    )


# ────────────────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────────────────
def compute_limit(req: MerchantLimitRequest, as_of: date, computed_at: str) -> MerchantLimitResponse:
    """Compute per-currency limits for a merchant. Pure: caller supplies as_of and timestamp."""
    ls = _legal_security(req.instruments)
    jurisdiction = _jurisdiction(req.country)
    streams = _streams(req)

    verified_api_months = max((_distinct_months(s.payouts, as_of) for s in streams), default=0)
    base_months, effective_tenure, override_used = _base_months(
        req.routing_days, verified_api_months, req.base_months_override
    )

    pb_factor = _map_pbs(req.payment_behaviour_score)
    rating_factor = _map_rating(req.rating_score)
    merchant_score = min(pb_factor, rating_factor)

    capture, capture_score, routed_annual = _capture_score(streams, req, as_of)
    merchant_multiplier = base_months * capture_score * merchant_score

    # one stream per (channel, currency); group the traces by currency into limits
    by_currency: dict[str, list[ChannelTrace]] = {}
    for s in streams:
        trace = _compute_stream(s, req, as_of, ls, jurisdiction)
        by_currency.setdefault(s.currency, []).append(trace)

    limits: list[CurrencyLimit] = []
    for ccy in sorted(by_currency):
        traces = by_currency[ccy]
        channel_sum = sum(t.channel_contribution for t in traces)
        limits.append(
            CurrencyLimit(
                currency=ccy,
                dynamic_limit=round(merchant_multiplier * channel_sum, 2),
                channel_sum=round(channel_sum, 2),
                channels=traces,
            )
        )

    merchant_trace = MerchantTrace(
        effective_tenure=round(effective_tenure, 4),
        routing_days=req.routing_days,
        verified_api_history_months=verified_api_months,
        base_months=base_months,
        base_months_override_used=override_used,
        capture=round(capture, 6),
        capture_score=round(capture_score, 6),
        total_routed_annualized=round(routed_annual, 2),
        total_revenue_ltm=req.total_revenue_ltm,
        merchant_score=round(merchant_score, 6),
        payment_behaviour_factor=pb_factor,
        rating_factor=rating_factor,
        legal_security=ls,
        constants_applied={
            "routed_weight": C.ROUTED_WEIGHT,
            "provisional_weight": C.PROVISIONAL_WEIGHT,
            "trailing_window_days": C.TRAILING_WINDOW_DAYS,
            "trailing_weights": list(C.TRAILING_WEIGHTS),
            "seasonal_floor_gamma": C.SEASONAL_FLOOR_GAMMA,
            "floor_guard_threshold": C.FLOOR_GUARD_THRESHOLD,
            "min_months_seasonal": C.MIN_MONTHS_SEASONAL,
            "capture_anchor": C.CAPTURE_ANCHOR,
            "capture_floor": C.CAPTURE_FLOOR,
            "tenor_months": req.tenor_months,
        },
    )

    return MerchantLimitResponse(
        merchant_id=req.merchant_id,
        computed_at=computed_at,
        as_of_date=as_of.isoformat(),
        revenue_currency=req.revenue_currency,
        limits=limits,
        merchant_trace=merchant_trace,
    )
