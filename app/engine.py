"""Core Dynamic Limit computation — pure functions, no I/O, no wall-clock.

The pipeline mirrors DYNAMIC_LIMIT_SPEC.md §6. Every function is deterministic given its
inputs; `as_of` truncates all history so a past anchor is an honest, no-lookahead backtest.

Monthly series are dicts keyed by ``datetime.date`` (first of month). ``_add_months`` is the
only month-arithmetic helper needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import config as C
from .models import (
    ChannelTrace,
    CurrencyLimit,
    FlowPath,
    LegalSecurityTrace,
    MerchantLimitRequest,
    MerchantLimitResponse,
    MerchantTrace,
    MonthlyAmount,
)


class FxRateMissing(ValueError):
    """Raised when a routed currency has no FX rate to the revenue currency."""


def _add_months(d: date, n: int) -> date:
    """Shift a first-of-month date by ``n`` months (n may be negative)."""
    total = d.month - 1 + n
    return date(d.year + total // 12, total % 12 + 1, 1)


# ────────────────────────────────────────────────────────────────────────────────────────
# Series helpers
# ────────────────────────────────────────────────────────────────────────────────────────
def _free_by_month(entries: list[MonthlyAmount], as_of: date) -> dict[date, float]:
    """Sparse {month: free amount} for months ≤ as_of. Encumbered amounts excluded."""
    out: dict[date, float] = {}
    for e in entries:
        if e.month > as_of:  # no lookahead
            continue
        if e.encumbered:
            continue
        out[e.month] = out.get(e.month, 0.0) + e.amount
    return out


def _dense_series(sparse: dict[date, float], as_of: date) -> dict[date, float]:
    """Contiguous [min..as_of] series with in-range gaps filled as 0.0. Empty if no data."""
    if not sparse:
        return {}
    out: dict[date, float] = {}
    cur = min(sparse)
    while cur <= as_of:
        out[cur] = sparse.get(cur, 0.0)
        cur = _add_months(cur, 1)
    return out


def _weighted_trailing(series: dict[date, float], t: date) -> float:
    """0.5·f(t-1) + 0.3·f(t-2) + 0.2·f(t-3); absent months contribute 0."""
    return sum(w * series.get(_add_months(t, -k), 0.0) for k, w in enumerate(C.TRAILING_WEIGHTS, start=1))


def _seasonal_index(series: dict[date, float]) -> dict[int, float] | None:
    """Normalized seasonal shape (mean 1.0) by calendar month, or None if < 12 months.

    month_avg[c] / overall, 3-month centered (circular) smoothed, then re-normalized to mean 1.0
    so uneven calendar coverage in partial-year windows cannot inflate every expectation.
    """
    if len(series) < C.MIN_MONTHS_SEASONAL:
        return None
    buckets: dict[int, list[float]] = {c: [] for c in range(1, 13)}
    for d, val in series.items():
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


def _ltm_avg(series: dict[date, float], t: date) -> float | None:
    """Mean over the trailing 12 months [t-11 .. t]; None if the window predates the data."""
    if _add_months(t, -11) < min(series):
        return None
    return sum(series.get(_add_months(t, -k), 0.0) for k in range(0, 12)) / 12.0


# ────────────────────────────────────────────────────────────────────────────────────────
# Currency streams — one channel fans out into one stream per settlement currency
# ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Stream:
    channel_id: str
    channel_type: str
    currency: str
    payouts: list[MonthlyAmount]
    routed: list[MonthlyAmount]
    routing_confirmed: bool | None


def _streams(req: MerchantLimitRequest) -> list[_Stream]:
    """Split each channel's multi-currency payouts into one stream per currency."""
    out: list[_Stream] = []
    for ch in req.channels:
        currencies = {e.currency for e in ch.payouts_history} | {e.currency for e in ch.treyd_routed_payouts}
        for ccy in sorted(currencies):
            out.append(
                _Stream(
                    channel_id=ch.channel_id,
                    channel_type=ch.channel_type,
                    currency=ccy,
                    payouts=[e for e in ch.payouts_history if e.currency == ccy],
                    routed=[e for e in ch.treyd_routed_payouts if e.currency == ccy],
                    routing_confirmed=ch.routing_confirmed,
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


def _base_months(routing_months: int, verified_api_months: int, override: float | None) -> tuple[float, float, bool]:
    """Returns (base_months, effective_tenure, override_used)."""
    credit = min(C.HISTORY_CREDIT_FACTOR * verified_api_months, C.HISTORY_CREDIT_CAP)
    effective_tenure = routing_months + credit
    if override is not None:
        return float(override), effective_tenure, True
    base = C.BASE_MONTHS_ENTRY if effective_tenure < C.TENURE_MID_THRESHOLD else C.BASE_MONTHS_MID
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

    Numerator is actual routed flow only (not projected), annualized per currency stream over the
    trailing 12-month window and FX-normalized into the revenue currency.
    """
    window = {_add_months(as_of, -k) for k in range(0, 12)}
    routed_annual = 0.0
    for s in streams:
        routed = _free_by_month(s.routed, as_of)
        vals = [v for m, v in routed.items() if m in window]
        if not vals:
            continue
        annualized = sum(vals) * (12.0 / len(vals))
        routed_annual += annualized * _fx_rate(s.currency, req.revenue_currency, req.fx_rates)

    if not req.total_revenue_ltm or routed_annual <= 0:
        return 0.0, C.CAPTURE_FLOOR, routed_annual
    capture = routed_annual / req.total_revenue_ltm
    score = min(1.0, max(C.CAPTURE_FLOOR, capture / C.CAPTURE_ANCHOR))
    return capture, score, routed_annual


# ────────────────────────────────────────────────────────────────────────────────────────
# Per-channel computation
# ────────────────────────────────────────────────────────────────────────────────────────
def _routing_confirmation(confirmed: bool | None, routed: dict[date, float], projection: dict[date, float]) -> float:
    if confirmed is True:
        return 1.0
    if confirmed is False:
        return C.ROUTING_CONFIRMATION_UNCONFIRMED
    if len(routed) >= C.ROUTING_CONFIRM_MIN_MONTHS:
        return 1.0
    for m, amount in routed.items():
        proj = projection.get(m, 0.0)
        if proj > 0 and amount >= C.ROUTING_CONFIRM_PROJECTION_RATIO * proj:
            return 1.0
    return C.ROUTING_CONFIRMATION_UNCONFIRMED


def _flow_score(routed: dict[date, float], t: date, expected_oos) -> float:
    """1.0 until ≥2 routed months (pilot inert); then clamp(actual/out-of-sample expected)."""
    if len([m for m in routed if m <= t]) < C.FLOW_SCORE_MIN_ROUTED_MONTHS:
        return 1.0
    exp = expected_oos(_add_months(t, -1))
    if exp is None or exp <= 0:
        return 1.0
    ratio = routed.get(_add_months(t, -1), 0.0) / exp
    return min(1.0, max(C.FLOW_SCORE_FLOOR, ratio))


def _compute_stream(
    s: _Stream, req: MerchantLimitRequest, as_of: date, ls: LegalSecurityTrace, jurisdiction: float
) -> ChannelTrace:
    routed = _free_by_month(s.routed, as_of)
    history = _free_by_month(s.payouts, as_of)

    # Flow source: actual routed settlements if present, else verified settlement history.
    if routed:
        source = _dense_series(routed, as_of)
        flow_path = FlowPath.ROUTED
    elif history:
        source = _dense_series(history, as_of)
        flow_path = FlowPath.API_HISTORY
    else:
        source = {}
        flow_path = FlowPath.NONE

    trailing_flow = _weighted_trailing(source, as_of)

    # Seasonal expectation: both shape AND level come from the long record (history if present,
    # else the source). Trailing above uses recent actual; the floor uses the long-record level,
    # so the seasonal floor stays available during the first year of routing.
    seasonal_basis = _dense_series(history, as_of) or source
    sindex = _seasonal_index(seasonal_basis)
    ltm = _ltm_avg(seasonal_basis, as_of) if seasonal_basis else None

    def expected(m: date) -> float | None:
        if sindex is None or ltm is None:
            return None
        return ltm * sindex[m.month]

    seasonal_eligible = sindex is not None and ltm is not None
    expected_tm1 = expected(_add_months(as_of, -1))
    forward_vals = [e for k in range(1, req.tenor_months + 1) if (e := expected(_add_months(as_of, k))) is not None]
    forward_expected = sum(forward_vals) / len(forward_vals) if forward_vals else None

    floor_guard_ok = True
    floor_active = False
    flow_base = trailing_flow
    if seasonal_eligible and forward_expected is not None:
        actual_tm1 = source.get(_add_months(as_of, -1), 0.0)
        floor_guard_ok = expected_tm1 is None or actual_tm1 >= C.FLOOR_GUARD_THRESHOLD * expected_tm1
        if floor_guard_ok:
            floor_candidate = C.SEASONAL_FLOOR_GAMMA * forward_expected
            if floor_candidate > trailing_flow:
                flow_base = floor_candidate
                floor_active = True

    # Out-of-sample expectation for Flow_Score: rebuild from history ≤ t-2.
    def expected_oos(m: date) -> float | None:
        cutoff = _add_months(as_of, -2)
        basis = {k: v for k, v in seasonal_basis.items() if k <= cutoff}
        si = _seasonal_index(basis)
        level_series = _dense_series(routed, cutoff) or basis
        lv = _ltm_avg(level_series, cutoff) if level_series else None
        if si is None or lv is None:
            return None
        return lv * si[m.month]

    flow_score = _flow_score(routed, as_of, expected_oos)

    reg = C.CHANNEL_REGISTRY[s.channel_type]
    v_norm = C.VERIFICATION_NORMS[reg["verification_tier"]]
    ls_norm = ls.norm_platform if reg["flow_type"] == "platform" else ls.norm_b2b
    quality_q = v_norm * ls_norm * jurisdiction * flow_score
    routing_conf = _routing_confirmation(s.routing_confirmed, routed, history)
    contribution = flow_base * quality_q * routing_conf

    return ChannelTrace(
        channel_id=s.channel_id,
        channel_type=s.channel_type,
        currency=s.currency,
        flow_path=flow_path,
        trailing_flow=round(trailing_flow, 2),
        seasonal_eligible=seasonal_eligible,
        ltm_avg=round(ltm, 2) if ltm is not None else None,
        expected_flow_last_month=round(expected_tm1, 2) if expected_tm1 is not None else None,
        forward_expected_flow=round(forward_expected, 2) if forward_expected is not None else None,
        seasonal_floor_active=floor_active,
        floor_guard_ok=floor_guard_ok,
        flow_base=round(flow_base, 2),
        verification_norm=round(v_norm, 6),
        legal_security_norm=round(ls_norm, 6),
        jurisdiction=round(jurisdiction, 6),
        flow_score=round(flow_score, 6),
        quality_q=round(quality_q, 6),
        routing_confirmation=round(routing_conf, 6),
        channel_contribution=round(contribution, 2),
    )


# ────────────────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────────────────
def compute_limit(req: MerchantLimitRequest, as_of: date, computed_at: str) -> MerchantLimitResponse:
    """Compute per-currency limits for a merchant. Pure: caller supplies as_of and timestamp."""
    as_of = as_of.replace(day=1)
    ls = _legal_security(req.instruments)
    jurisdiction = _jurisdiction(req.country)
    streams = _streams(req)

    verified_api_months = max(
        (len(_free_by_month(s.payouts, as_of)) for s in streams),
        default=0,
    )
    base_months, effective_tenure, override_used = _base_months(
        req.routing_months, verified_api_months, req.base_months_override
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

    limits: dict[str, CurrencyLimit] = {}
    for ccy, traces in by_currency.items():
        channel_sum = sum(t.channel_contribution for t in traces)
        limits[ccy] = CurrencyLimit(
            dynamic_limit=round(merchant_multiplier * channel_sum, 2),
            channel_sum=round(channel_sum, 2),
            channels=traces,
        )

    merchant_trace = MerchantTrace(
        effective_tenure=round(effective_tenure, 4),
        routing_months=req.routing_months,
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
            "seasonal_floor_gamma": C.SEASONAL_FLOOR_GAMMA,
            "floor_guard_threshold": C.FLOOR_GUARD_THRESHOLD,
            "min_months_seasonal": C.MIN_MONTHS_SEASONAL,
            "capture_anchor": C.CAPTURE_ANCHOR,
            "capture_floor": C.CAPTURE_FLOOR,
            "tenor_months": req.tenor_months,
            "trailing_weights": list(C.TRAILING_WEIGHTS),
        },
    )

    return MerchantLimitResponse(
        merchant_id=req.merchant_id,
        computed_at=computed_at,
        as_of_month=as_of.strftime("%Y-%m"),
        revenue_currency=req.revenue_currency,
        limits=limits,
        merchant_trace=merchant_trace,
    )
