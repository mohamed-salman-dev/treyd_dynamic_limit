"""Core Dynamic Limit computation — pure functions, no I/O, no wall-clock.

Payouts are daily. Each is weighted by whether it routed to Treyd (routed = full, provisional =
discounted) and excluded if encumbered. Two views are derived from the daily series:
  • Trailing_Flow — a fixed 90-day window from as_of, recency-weighted across sub-buckets.
  • Seasonal curve — daily aggregated to calendar months (complete months only), for the
    seasonal index and LTM level.
`as_of` truncates everything, so a past date is an honest, no-lookahead backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from statsmodels.tsa.exponential_smoothing.ets import ETSModel
from statsmodels.tsa.forecasting.stl import STLForecast

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



def _is_month_end(d: date) -> bool:
    return (d + timedelta(days=1)).month != d.month


# ────────────────────────────────────────────────────────────────────────────────────────
# Daily series helpers
# ────────────────────────────────────────────────────────────────────────────────────────
def _payouts_df(payouts: list[Payout]) -> pd.DataFrame:
    """All payouts as a DataFrame. Callers apply their own filters."""
    if not payouts:
        return pd.DataFrame(columns=["date", "amount", "currency", "routed_to_treyd", "encumbered"])
    return pd.DataFrame([
        {"date": p.date, "amount": p.amount, "currency": p.currency,
         "routed_to_treyd": p.routed_to_treyd, "encumbered": p.encumbered}
        for p in payouts
    ])


def _weighted_daily(df: pd.DataFrame, as_of: date, provisional_weight: float = C.PROVISIONAL_WEIGHT) -> pd.DataFrame:
    """Daily weighted free amounts up to as_of. Returns DataFrame[date, amount].
    Routed → full weight, provisional → provisional_weight; encumbered excluded."""
    if df.empty:
        return pd.DataFrame(columns=["date", "amount"])
    df = df[(df["date"] <= as_of) & ~df["encumbered"]].copy()
    df["amount"] = df["amount"] * df["routed_to_treyd"].map({True: C.ROUTED_WEIGHT, False: provisional_weight})
    return df.groupby("date", as_index=False)["amount"].sum()


def _routed_daily(df: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Routed, non-encumbered payouts up to as_of."""
    if df.empty:
        return df[["date", "amount"]]
    return df[(df["date"] <= as_of) & df["routed_to_treyd"] & ~df["encumbered"]][["date", "amount"]].reset_index(drop=True)


def _trailing_flow(daily: pd.DataFrame, as_of: date) -> float:
    """Recency-weighted flow over a fixed window: the window is split into equal sub-buckets,
    one per weight, and each bucket's summed flow is weighted. Yields a representative month."""
    n = len(C.TRAILING_WEIGHTS)
    bucket_days = C.TRAILING_WINDOW_DAYS / n
    total = 0.0
    for i, w in enumerate(C.TRAILING_WEIGHTS):
        hi = as_of - timedelta(days=round(bucket_days * i))
        lo = as_of - timedelta(days=round(bucket_days * (i + 1)))
        total += w * daily.loc[(daily["date"] > lo) & (daily["date"] <= hi), "amount"].sum()
    return total


def _monthly_from_daily(daily: pd.DataFrame, as_of: date) -> pd.Series:
    """Aggregate daily amounts to a Series keyed by monthly Period. The as_of month
    is dropped unless as_of is its last day, so a partial month never pollutes seasonal/LTM."""
    if daily.empty:
        return pd.Series(dtype=float)
    monthly = daily.assign(period=daily["date"].apply(lambda x: pd.Period(x, "M"))).groupby("period")["amount"].sum()
    if not _is_month_end(as_of):
        monthly = monthly.drop(pd.Period(as_of, "M"), errors="ignore")
    return monthly


def _distinct_months(df: pd.DataFrame, as_of: date) -> int:
    if df.empty:
        return 0
    return pd.to_datetime(df[df["date"] <= as_of]["date"]).dt.to_period("M").nunique()


def _seasonal_index_forecast(monthly: pd.Series, tenor_months: int) -> float | None:
    """Legacy spec §3.3 seasonal-index forecast. Not called by the production path (replaced by
    STLForecast). Kept as reference implementation.

    Requires ≥ 24 months (two full years) — one for the index, one for YoY growth.
    Returns Forward_Expected_Flow (average Expected_Flow over tenor_months) or None.
    """
    if len(monthly) < 24:
        return None
    start, end = monthly.index.min(), monthly.index.max()
    series = monthly.reindex(pd.period_range(start, end, freq="M"), fill_value=0.0)

    prior_year = series.iloc[-12:]
    ltm_avg = float(prior_year.mean())
    if ltm_avg <= 0:
        return None

    # Seasonal index: 3-month centered rolling mean on prior year, normalised by LTM avg
    smoothed = prior_year.rolling(3, center=True, min_periods=1).mean()
    si_by_month = {p.month: float(v) / ltm_avg for p, v in smoothed.items()}

    # YoY growth: LTM avg over the year before the prior year, clamped
    prev_year_avg = float(series.iloc[-24:-12].mean())
    yoy = min(1.3, max(0.7, ltm_avg / prev_year_avg)) if prev_year_avg > 0 else 1.0

    next_period = series.index.max() + 1
    expected = [ltm_avg * si_by_month.get((next_period + i).month, 1.0) * yoy for i in range(tenor_months)]
    return sum(expected) / len(expected)


@dataclass(frozen=True)
class _SeasonalFlow:
    seasonal_eligible: bool
    ltm_avg: float | None
    forward_expected: float | None


def _seasonal_flow(monthly: pd.Series, tenor_months: int) -> _SeasonalFlow:
    """Forward expected flow via STLForecast + ETS(A,A,N) with per-month YoY clamp.
    Assumes eligibility has already been checked by the caller."""
    _ineligible = _SeasonalFlow(seasonal_eligible=False, ltm_avg=None, forward_expected=None)

    start, end = monthly.index.min(), monthly.index.max()
    series = monthly.reindex(pd.period_range(start, end, freq="M"), fill_value=0.0)

    ltm = float(series.iloc[-12:].mean())
    if ltm <= 0:
        return _ineligible

    try:
        res = STLForecast(
            series, ETSModel,
            model_kwargs={"error": "add", "trend": "add", "seasonal": None},
            period=12,
        ).fit(fit_kwargs={"disp": False})
        raw_forecast = res.forecast(tenor_months)
    except Exception:
        return _ineligible

    clamped = []
    for period, val in raw_forecast.items():
        prior_idx = period - 12
        prior = float(series[prior_idx]) if prior_idx in series.index else None
        baseline = prior if (prior is not None and prior > 0) else ltm
        clamped.append(max(0.7 * baseline, min(1.3 * baseline, val)))
    forward_expected = sum(clamped) / len(clamped)

    return _SeasonalFlow(seasonal_eligible=True, ltm_avg=ltm, forward_expected=forward_expected)


def _oos_expected_flow(monthly: pd.Series) -> float | None:
    """Expected flow for the last month in `monthly`, estimated out-of-sample.

    Fits STLForecast on all but the last month, forecasts one step ahead.
    Returns None if there are fewer than MIN_MONTHS_SEASONAL + 1 months available.
    """
    if len(monthly) < C.MIN_MONTHS_SEASONAL + 1:
        return None
    training = monthly.iloc[:-1]
    start, end = training.index.min(), training.index.max()
    series = training.reindex(pd.period_range(start, end, freq="M"), fill_value=0.0)
    try:
        res = STLForecast(
            series, ETSModel,
            model_kwargs={"error": "add", "trend": "add", "seasonal": None},
            period=12,
        ).fit(fit_kwargs={"disp": False})
        return max(0.0, float(res.forecast(1).iloc[0]))
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────────────────────
# Currency streams — one channel fans out into one stream per settlement currency
# ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Stream:
    channel_id: str
    channel_type: str
    currency: str
    df: pd.DataFrame
    routing_confirmation: float


def _streams(req: MerchantLimitRequest) -> list[_Stream]:
    """Split each channel's payouts into one stream per currency via groupby."""
    out: list[_Stream] = []
    for ch in req.channels:
        full_df = _payouts_df(ch.payouts)
        if full_df.empty:
            continue
        for ccy, sub in full_df.groupby("currency"):
            out.append(_Stream(
                channel_id=ch.channel_id,
                channel_type=ch.channel_type,
                currency=str(ccy),
                df=sub.reset_index(drop=True),
                routing_confirmation=ch.routing_confirmation,
            ))
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


def _base_months(routing_days: int, verified_api_months: int) -> tuple[float, float]:
    """Returns (base_months, effective_tenure).

    Effective_Tenure (months) = routing_days/30 + half the verified history (capped at 6 months of
    credit). 2.0 / 3.0 / 4.0 are earned by tenure; override above 4.0 is applied by the caller.
    """
    credit = min(C.HISTORY_OBSERVED_FLOW_FACTOR * verified_api_months, C.OBSERVED_HISTORY_FLOW_CAP)
    effective_tenure = routing_days / C.DAYS_PER_MONTH + credit
    if effective_tenure < C.TENURE_MID_THRESHOLD:
        base = C.BASE_MONTHS_ENTRY
    elif effective_tenure < C.TENURE_HIGH_THRESHOLD:
        base = C.BASE_MONTHS_MID
    else:
        base = C.BASE_MONTHS_HIGH
    return base, effective_tenure


def _has_missing_fx(streams: list[_Stream], req: MerchantLimitRequest) -> bool:
    """True if any foreign stream currency lacks an FX rate to revenue_currency."""
    foreign = {s.currency for s in streams if s.currency != req.revenue_currency}
    return any(c not in req.fx_rates for c in foreign)


def _capture_score(streams: list[_Stream], req: MerchantLimitRequest, as_of: date) -> tuple[float, float, float]:
    """Returns (capture, capture_score, total_routed_annualized) in revenue_currency.

    Precondition: FX rates are available for all routed currencies (caller checks via _has_missing_fx).
    """
    capture_window_start = as_of - timedelta(days=365)
    routed_annual = 0.0
    for s in streams:
        window = _routed_daily(s.df, as_of)
        window = window[window["date"] > capture_window_start]
        if window.empty:
            continue
        rate = 1.0 if s.currency == req.revenue_currency else req.fx_rates[s.currency]
        span_days = min((as_of - window["date"].min()).days + 1, 365)
        routed_annual += window["amount"].sum() * rate * 365.0 / max(span_days, 1)

    if not req.total_revenue_ltm or routed_annual <= 0:
        return 0.0, C.CAPTURE_FLOOR, routed_annual
    capture = routed_annual / req.total_revenue_ltm
    score = min(1.0, max(C.CAPTURE_FLOOR, capture / C.CAPTURE_ANCHOR))
    return capture, score, routed_annual


# ────────────────────────────────────────────────────────────────────────────────────────
# Per-stream computation
# ────────────────────────────────────────────────────────────────────────────────────────
def _compute_stream(
    s: _Stream, req: MerchantLimitRequest, as_of: date, ls: LegalSecurityTrace,
    jurisdiction: float, merchant_multiplier: float,
    prev_channel_display: float | None, glide_delta: float,
) -> ChannelTrace:
    routed = _routed_daily(s.df, as_of)
    routed_months = pd.to_datetime(routed["date"]).dt.to_period("M").nunique() if not routed.empty else 0

    # Non-routed payouts are excluded once routing fills the full trailing window.
    prov_weight = (
        C.PROVISIONAL_WEIGHT_POST_OBSERVATION
        if routed_months >= len(C.TRAILING_WEIGHTS)
        else C.PROVISIONAL_WEIGHT
    )
    weighted = _weighted_daily(s.df, as_of, prov_weight)
    trailing_flow = _trailing_flow(weighted, as_of)

    # routed share over the trailing window (full value, unweighted) — transparency only
    win_lo = as_of - timedelta(days=C.TRAILING_WINDOW_DAYS)
    win = s.df[(s.df["date"] > win_lo) & (s.df["date"] <= as_of) & ~s.df["encumbered"]]
    win_total = win["amount"].sum()
    win_routed = win[win["routed_to_treyd"]]["amount"].sum()
    routed_share = win_routed / win_total if win_total > 0 else 0.0

    monthly = _monthly_from_daily(weighted, as_of)

    # OOS expected for last month — used by both the floor guard and flow_score.
    oos_expected: float | None = _oos_expected_flow(monthly) if not monthly.empty else None

    sf = (
        _seasonal_flow(monthly, req.tenor_months)        if len(monthly) >= C.MIN_MONTHS_SEASONAL and routed_months > 1
        else _SeasonalFlow(seasonal_eligible=False, ltm_avg=None, forward_expected=None)
    )

    # Floor guard: actual last month must be ≥ 70% of OOS expected (or LTM avg if OOS unavailable).
    # Uses the per-month expectation so a peak-month shortfall is caught correctly.
    guard_denominator = oos_expected if oos_expected is not None else sf.ltm_avg
    floor_guard_ok = (
        float(monthly.iloc[-1]) >= C.FLOOR_GUARD_THRESHOLD * guard_denominator
        if (guard_denominator and not monthly.empty) else True
    )

    floor_active = False
    flow_base = trailing_flow
    if sf.seasonal_eligible and floor_guard_ok and sf.forward_expected is not None:
        floor_candidate = C.SEASONAL_FLOOR_GAMMA * sf.forward_expected
        if floor_candidate > trailing_flow:
            flow_base = floor_candidate
            floor_active = True

    # Flow_Score — inert until ≥2 routed months; then actual vs OOS expected (§4.4).
    # Fallback when model can't run: compare month-1 against month-2 (same scale, no lookahead).
    flow_score = 1.0
    if routed_months >= C.FLOW_SCORE_MIN_ROUTED_MONTHS and not monthly.empty:
        last_month_actual = float(monthly.iloc[-1])
        denominator = oos_expected if oos_expected is not None else (float(monthly.iloc[-2]) if len(monthly) >= 2 else None)
        if denominator is not None and denominator > 0:
            flow_score = min(1.0, max(C.FLOW_SCORE_FLOOR, last_month_actual / denominator))

    reg = C.CHANNEL_REGISTRY[s.channel_type]
    v_norm = C.VERIFICATION_NORMS[reg["verification_tier"]]
    ls_norm = ls.norm_platform if reg["flow_type"] == "platform" else ls.norm_b2b
    quality_q = v_norm * ls_norm * jurisdiction * flow_score
    contribution = flow_base * quality_q * s.routing_confirmation
    contribution_overall = contribution * merchant_multiplier

    # per-channel asymmetric glide vs this channel's own previous display limit
    glide_floor = prev_channel_display * (1 - glide_delta) if prev_channel_display is not None else 0.0
    display_limit = max(contribution_overall, glide_floor)

    return ChannelTrace(
        channel_id=s.channel_id,
        channel_type=s.channel_type,
        currency=s.currency,
        trailing_flow=round(trailing_flow, 2),
        routed_share=round(routed_share, 6),
        seasonal_eligible=sf.seasonal_eligible,
        ltm_avg=round(sf.ltm_avg, 2) if sf.ltm_avg is not None else None,
        expected_flow_last_month=round(oos_expected, 2) if oos_expected is not None else None,
        forward_expected_flow=round(sf.forward_expected, 2) if sf.forward_expected is not None else None,
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
        contribution_to_overall_limit=round(contribution_overall, 2),
        display_limit=round(display_limit, 2),
    )


# ────────────────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────────────────
def compute_limit(req: MerchantLimitRequest, as_of: date) -> MerchantLimitResponse:
    """Compute per-currency limits for a merchant. Pure: caller supplies as_of and timestamp."""
    ls = _legal_security(req.instruments)
    jurisdiction = _jurisdiction(req.country)
    streams = _streams(req)

    verified_api_months = max((_distinct_months(s.df, as_of) for s in streams), default=0)
    base_months, effective_tenure = _base_months(req.routing_days, verified_api_months)
    override_used = req.base_months_override is not None
    if override_used:
        base_months = float(req.base_months_override)

    pb_factor = _map_pbs(req.payment_behaviour_score)
    rating_factor = _map_rating(req.rating_score)
    merchant_score = min(pb_factor, rating_factor)

    if _has_missing_fx(streams, req):
        capture, capture_score, routed_annual = 0.0, C.CAPTURE_FALLBACK_NO_FX, 0.0
    else:
        capture, capture_score, routed_annual = _capture_score(streams, req, as_of)
    merchant_multiplier = base_months * capture_score * merchant_score

    pbs = req.payment_behaviour_score
    glide_delta = C.GLIDE_DELTA_GOOD_PBS if (pbs is not None and pbs >= C.GLIDE_PBS_RATING_THRESHOLD) else C.GLIDE_DELTA_DEFAULT
    # previous display limit per (currency, channel_id), from last cycle's response fed back
    prev_display = {
        (p.currency, ch.channel_id): ch.display_limit for p in req.previous_limits for ch in p.channels
    }

    # one stream per (channel, currency); each channel glides vs its own previous display limit
    by_currency: dict[str, list[ChannelTrace]] = {}
    for s in streams:
        prev = prev_display.get((s.currency, s.channel_id))
        trace = _compute_stream(s, req, as_of, ls, jurisdiction, merchant_multiplier, prev, glide_delta)
        by_currency.setdefault(s.currency, []).append(trace)

    limits: list[CurrencyLimit] = []
    for ccy in sorted(by_currency):
        traces = by_currency[ccy]
        limits.append(
            CurrencyLimit(
                currency=ccy,
                dynamic_limit=round(sum(t.contribution_to_overall_limit for t in traces), 2),
                display_limit=round(sum(t.display_limit for t in traces), 2),
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
        glide_delta=glide_delta,
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
            "glide_delta_good_pbs": C.GLIDE_DELTA_GOOD_PBS,
            "glide_delta_default": C.GLIDE_DELTA_DEFAULT,
        },
    )

    return MerchantLimitResponse(
        merchant_id=req.merchant_id,
        as_of_date=as_of.isoformat(),
        revenue_currency=req.revenue_currency,
        limits=limits,
        merchant_trace=merchant_trace,
    )
