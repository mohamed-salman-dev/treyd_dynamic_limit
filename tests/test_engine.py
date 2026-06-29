"""Engine tests: factor lookups, the seasonal floor on synthetic daily data, and pipeline wiring."""

from __future__ import annotations

import pytest

from app.engine import (
    _add_months,
    _base_months,
    _capture_score,
    _jurisdiction,
    _legal_security,
    _map_pbs,
    _map_rating,
    _monthly_from_daily,
    _seasonal_index,
    _streams,
    _weighted_daily,
    compute_limit,
)
from app.models import ChannelInput
from tests.conftest import d, flat_daily, limit_for, m1, make_request, seasonal_daily


# ── Legal security ────────────────────────────────────────────────────────────────────────
def test_legal_security_default_is_floating_pg():
    ls = _legal_security(None)
    assert ls.instruments_applied == ["floating_charge", "pg_cg"]
    assert ls.norm_platform == pytest.approx(0.71, abs=0.01)


def test_legal_security_full_package():
    ls = _legal_security(
        ["fixed_charge_controlled_account", "floating_charge", "assignment_of_receivables", "pg_cg"]
    )
    assert ls.norm_b2b == pytest.approx(1.0, abs=0.01)
    assert ls.norm_platform == pytest.approx(0.89, abs=0.01)


# ── Merchant score ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pbs,expected", [(10, 1.0), (8, 1.0), (7, 0.8), (6, 0.8), (5, 0.6), (None, 0.8)])
def test_map_pbs(pbs, expected):
    assert _map_pbs(pbs) == expected


@pytest.mark.parametrize("rating,expected", [(10, 1.0), (8, 1.0), (6, 0.8), (4, 0.6), (3, 0.0), (None, 0.8)])
def test_map_rating(rating, expected):
    assert _map_rating(rating) == expected


def test_below_appetite_zeroes_limit():
    chans = [ChannelInput(channel_id="c", channel_type="shopify_payments",
                          payouts=flat_daily("2025-01", 13, 50_000))]
    resp = compute_limit(make_request(chans, rating_score=2), d("2026-01-31"), "t")
    assert resp.merchant_trace.merchant_score == 0.0
    assert limit_for(resp, "GBP").dynamic_limit == 0.0


# ── Jurisdiction ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("country,expected", [("gb", 1.0), ("se", 0.7), ("us", 0.6), (None, 0.6), ("zz", 0.6)])
def test_jurisdiction(country, expected):
    assert _jurisdiction(country) == expected


# ── Base months (days now) ─────────────────────────────────────────────────────────────────────
def test_base_months_pre_launch_is_3():
    base, et, override = _base_months(routing_days=0, verified_api_months=24, override=None)
    assert et == 6.0 and base == 3.0 and override is False


def test_base_months_thin_history():
    base, et, _ = _base_months(routing_days=0, verified_api_months=4, override=None)
    assert et == 2.0 and base == 2.0


def test_base_months_four_earned_by_tenure():
    base, et, used = _base_months(routing_days=210, verified_api_months=24, override=None)  # 7m + 6 credit
    assert et == 13.0 and base == 4.0 and used is False


def test_base_months_override_only_above_four():
    base, _, used = _base_months(routing_days=600, verified_api_months=24, override=5.0)
    assert base == 5.0 and used is True


# ── Seasonal index (synthetic daily → monthly) ──────────────────────────────────────────────────
def test_seasonal_index_normalized_and_shaped():
    payouts = seasonal_daily("2024-01", 24, monthly_base=40_000)
    monthly = _monthly_from_daily(_weighted_daily(payouts, d("2025-12-31")), d("2025-12-31"))
    sindex = _seasonal_index(monthly)
    assert sindex is not None
    assert sum(sindex.values()) / 12 == pytest.approx(1.0, abs=1e-9)
    assert sindex[12] > 1.5 and sindex[8] < 0.7


def test_thin_history_has_no_seasonal():
    monthly = {_add_months(m1("2026-01"), i): 1000.0 for i in range(5)}
    assert _seasonal_index(monthly) is None


# ── Flow base / seasonal floor (synthetic daily) ─────────────────────────────────────────────────
def test_floor_binds_entering_peak():
    payouts = seasonal_daily("2024-01", 21, monthly_base=40_000)  # ends 2025-09
    chans = [ChannelInput(channel_id="shopify", channel_type="shopify_payments", payouts=payouts)]
    resp = compute_limit(make_request(chans, country="gb"), d("2025-09-30"), "t")
    ch = limit_for(resp, "GBP").channels[0]
    assert ch.seasonal_floor_active is True
    assert ch.flow_base > ch.trailing_flow * 2


def test_trailing_wins_post_peak():
    payouts = seasonal_daily("2024-01", 24, monthly_base=40_000)  # ends 2025-12
    chans = [ChannelInput(channel_id="shopify", channel_type="shopify_payments", payouts=payouts)]
    resp = compute_limit(make_request(chans, country="gb"), d("2025-12-31"), "t")
    ch = limit_for(resp, "GBP").channels[0]
    assert ch.seasonal_floor_active is False
    assert ch.flow_base == ch.trailing_flow


def test_no_lookahead_truncation():
    payouts = seasonal_daily("2024-01", 24, monthly_base=40_000)
    early = [p for p in payouts if p.date <= d("2025-09-30")]
    full_ch = [ChannelInput(channel_id="s", channel_type="shopify_payments", payouts=payouts)]
    early_ch = [ChannelInput(channel_id="s", channel_type="shopify_payments", payouts=early)]
    a = compute_limit(make_request(full_ch, country="gb"), d("2025-09-30"), "t")
    b = compute_limit(make_request(early_ch, country="gb"), d("2025-09-30"), "t")
    assert limit_for(a, "GBP").dynamic_limit == limit_for(b, "GBP").dynamic_limit


# ── Provisional vs routed weighting ─────────────────────────────────────────────────────────────
def test_routed_flow_weighted_higher_than_provisional():
    """Same flow, routed vs not: routed gets full weight, provisional gets 0.7 — bigger limit."""
    prov = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                         payouts=flat_daily("2025-01", 13, 50_000, routed=False))]
    routed = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                           payouts=flat_daily("2025-01", 13, 50_000, routed=True))]
    lim_prov = limit_for(compute_limit(make_request(prov, country="gb"), d("2026-01-31"), "t"), "GBP")
    lim_routed = limit_for(compute_limit(make_request(routed, country="gb"), d("2026-01-31"), "t"), "GBP")
    assert lim_routed.channels[0].routed_share == 1.0
    assert lim_prov.channels[0].routed_share == 0.0
    assert lim_routed.dynamic_limit == pytest.approx(lim_prov.dynamic_limit / 0.7, rel=0.02)


# ── Capture ───────────────────────────────────────────────────────────────────────────────────
def test_capture_floors_at_half_with_no_routed_flow():
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          payouts=flat_daily("2025-01", 13, 70_000, routed=False))]
    req = make_request(chans, total_revenue_ltm=1_200_000)
    _, score, routed = _capture_score(_streams(req), req, d("2026-01-31"))
    assert routed == 0.0 and score == 0.5


def test_capture_with_routed_flow():
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          payouts=flat_daily("2025-02", 12, 70_000, routed=True))]
    req = make_request(chans, total_revenue_ltm=1_200_000)  # ~840k routed / 1.2m ≈ 0.7
    capture, score, _ = _capture_score(_streams(req), req, d("2026-01-31"))
    assert capture == pytest.approx(0.7, abs=0.03)
    assert score == pytest.approx(0.82, abs=0.04)


def test_capture_fx_normalizes_to_revenue_currency():
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          payouts=flat_daily("2025-02", 12, 700_000, currency="SEK", routed=True))]
    req = make_request(chans, revenue_currency="GBP", total_revenue_ltm=1_000_000, fx_rates={"SEK": 0.075})
    _, _, routed = _capture_score(_streams(req), req, d("2026-01-31"))
    assert routed == pytest.approx(700_000 * 12 * 0.075, rel=0.03)


# ── Full pipeline wiring ────────────────────────────────────────────────────────────────────────
def test_full_pipeline_factors_and_magnitude():
    """Onboarding (provisional flow): assert each factor exactly, limit within tolerance."""
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          payouts=flat_daily("2025-01", 15, 70_000, routed=False))]
    req = make_request(
        chans, country="gb", payment_behaviour_score=9,
        instruments=["fixed_charge_controlled_account", "floating_charge", "assignment_of_receivables", "pg_cg"],
        total_revenue_ltm=1_400_000,
    )
    resp = compute_limit(req, d("2026-03-31"), "t")
    mt, ch = resp.merchant_trace, limit_for(resp, "GBP").channels[0]
    assert mt.base_months == 3.0
    assert mt.merchant_score == 0.8
    assert mt.capture_score == 0.5
    assert ch.routing_confirmation == 1.0
    assert ch.flow_score == 1.0
    assert ch.routed_share == 0.0
    assert ch.legal_security_norm == pytest.approx(0.89, abs=0.01)
    # flow_base ≈ 0.7 (provisional) × 70k monthly; limit = 3.0 × 0.5 × 0.8 × (flow_base × Q)
    ls_norm = 1.25 / 1.40
    approx = 3.0 * 0.5 * 0.8 * (0.7 * 70_000 * ls_norm)
    assert limit_for(resp, "GBP").dynamic_limit == pytest.approx(approx, rel=0.05)


def test_per_currency_independent_limits():
    """One channel settling GBP + USD → two independent limits (split inside)."""
    mixed = flat_daily("2025-01", 13, 50_000, currency="GBP") + flat_daily("2025-01", 13, 20_000, currency="USD")
    ch = ChannelInput(channel_id="shopify", channel_type="shopify_payments", payouts=mixed)
    resp = compute_limit(make_request([ch], country="gb"), d("2026-01-31"), "t")
    assert {limit.currency for limit in resp.limits} == {"GBP", "USD"}
    assert limit_for(resp, "GBP").dynamic_limit > limit_for(resp, "USD").dynamic_limit


def test_routing_confirmation_multiplier():
    """routing_confirmation is a neutral-by-default multiplier on the contribution."""
    base = ChannelInput(channel_id="s", channel_type="shopify_payments", payouts=flat_daily("2025-01", 13, 50_000))
    dialed = ChannelInput(channel_id="s", channel_type="shopify_payments",
                          payouts=flat_daily("2025-01", 13, 50_000), routing_confirmation=0.5)
    full = limit_for(compute_limit(make_request([base], country="gb"), d("2026-01-31"), "t"), "GBP").dynamic_limit
    half = limit_for(compute_limit(make_request([dialed], country="gb"), d("2026-01-31"), "t"), "GBP").dynamic_limit
    assert half == pytest.approx(full * 0.5, rel=1e-6)
