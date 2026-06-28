"""Engine tests: factor lookups, the seasonal floor on real data, and full-pipeline math."""

from __future__ import annotations

import pytest

from app.engine import (
    _add_months,
    _base_months,
    _capture_score,
    _dense_series,
    _free_by_month,
    _jurisdiction,
    _legal_security,
    _map_pbs,
    _map_rating,
    _seasonal_index,
    _streams,
    compute_limit,
)
from app.models import ChannelInput
from tests.conftest import flat_history, make_request, month, month_range, seasonal_history


# ── Legal security ────────────────────────────────────────────────────────────────────────
def test_legal_security_default_is_floating_pg():
    ls = _legal_security(None)
    assert ls.instruments_applied == ["floating_charge", "pg_cg"]
    assert ls.norm_platform == pytest.approx(0.71, abs=0.01)
    assert ls.norm_b2b == pytest.approx(0.71, abs=0.01)


def test_legal_security_full_package():
    ls = _legal_security(
        ["fixed_charge_controlled_account", "floating_charge", "assignment_of_receivables", "pg_cg"]
    )
    assert ls.norm_b2b == pytest.approx(1.0, abs=0.01)        # 1.40 / 1.40
    assert ls.norm_platform == pytest.approx(0.89, abs=0.01)  # 1.25 / 1.40


def test_legal_security_pg_only():
    ls = _legal_security(["pg_cg"])
    assert ls.norm_b2b == pytest.approx(0.64, abs=0.01)


# ── Merchant score ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pbs,expected", [(10, 1.0), (8, 1.0), (7, 0.8), (6, 0.8), (5, 0.6), (1, 0.6), (None, 0.8)])
def test_map_pbs(pbs, expected):
    assert _map_pbs(pbs) == expected


@pytest.mark.parametrize("rating,expected", [(10, 1.0), (8, 1.0), (7, 0.8), (6, 0.8), (5, 0.6), (4, 0.6), (3, 0.0), (1, 0.0), (None, 0.8)])
def test_map_rating(rating, expected):
    assert _map_rating(rating) == expected


def test_below_appetite_zeroes_limit():
    chans = [ChannelInput(channel_id="c", channel_type="shopify_payments",
                          payouts_history=flat_history(month_range("2025-01", 13), 50_000))]
    resp = compute_limit(make_request(chans, rating_score=2), month("2026-01"), "t")
    assert resp.merchant_trace.merchant_score == 0.0
    assert resp.limits["GBP"].dynamic_limit == 0.0


# ── Jurisdiction ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("country,expected", [("gb", 1.0), ("ie", 1.0), ("se", 0.7), ("us", 0.6), (None, 0.6), ("zz", 0.6)])
def test_jurisdiction(country, expected):
    assert _jurisdiction(country) == expected


# ── Base months ──────────────────────────────────────────────────────────────────────────────
def test_base_months_pre_launch_is_3():
    # onboarding: routing 0, full history credit (capped at 6) → ET 6 → 3.0
    base, et, override = _base_months(routing_months=0, verified_api_months=24, override=None)
    assert et == 6.0            # 0 + min(0.5*24, 6)
    assert base == 3.0
    assert override is False


def test_base_months_thin_history():
    base, et, _ = _base_months(routing_months=0, verified_api_months=4, override=None)
    assert et == 2.0
    assert base == 2.0


def test_base_months_four_earned_by_tenure():
    # 7 routed months + 6 history credit → ET 13 → 4.0, no override needed
    base, et, used = _base_months(routing_months=7, verified_api_months=24, override=None)
    assert et == 13.0
    assert base == 4.0
    assert used is False


def test_base_months_override_only_above_four():
    base, _, used = _base_months(routing_months=20, verified_api_months=24, override=5.0)
    assert base == 5.0 and used is True


# ── Seasonal index (synthetic data) ─────────────────────────────────────────────────────────────
def test_seasonal_index_normalized_and_shaped():
    hist = seasonal_history("2024-01", 24, base=40_000)
    as_of = month("2025-12")
    series = _dense_series(_free_by_month(hist, as_of), as_of)
    sindex = _seasonal_index(series)
    assert sindex is not None
    assert sum(sindex.values()) / 12 == pytest.approx(1.0, abs=1e-9)   # normalized
    assert sindex[12] > 1.5 and sindex[8] < 0.7                        # Dec peak, Aug trough


def test_thin_history_has_no_seasonal():
    series = {_add_months(month("2026-01"), i): 1000.0 for i in range(5)}
    assert _seasonal_index(series) is None


# ── Flow base / seasonal floor (synthetic data) ──────────────────────────────────────────────────
def test_floor_binds_entering_peak():
    hist = seasonal_history("2024-01", 21, base=40_000)  # ends 2025-09
    chans = [ChannelInput(channel_id="shopify", channel_type="shopify_payments", payouts_history=hist)]
    resp = compute_limit(make_request(chans, country="gb"), month("2025-09"), "t")
    ch = resp.limits["GBP"].channels[0]
    assert ch.seasonal_floor_active is True
    assert ch.flow_base > ch.trailing_flow * 2          # floor lifts capacity into the peak


def test_trailing_wins_post_peak():
    hist = seasonal_history("2024-01", 24, base=40_000)  # ends 2025-12 (peak just passed)
    chans = [ChannelInput(channel_id="shopify", channel_type="shopify_payments", payouts_history=hist)]
    resp = compute_limit(make_request(chans, country="gb"), month("2025-12"), "t")
    ch = resp.limits["GBP"].channels[0]
    assert ch.seasonal_floor_active is False
    assert ch.flow_base == ch.trailing_flow


def test_no_lookahead_truncation():
    """A past as_of must ignore later months: the limit is unchanged by adding later data."""
    hist = seasonal_history("2024-01", 24, base=40_000)
    early = [a for a in hist if a.month <= month("2025-09")]
    chans_full = [ChannelInput(channel_id="s", channel_type="shopify_payments", payouts_history=hist)]
    chans_early = [ChannelInput(channel_id="s", channel_type="shopify_payments", payouts_history=early)]
    a = compute_limit(make_request(chans_full, country="gb"), month("2025-09"), "t")
    b = compute_limit(make_request(chans_early, country="gb"), month("2025-09"), "t")
    assert a.limits["GBP"].dynamic_limit == b.limits["GBP"].dynamic_limit


# ── Capture ───────────────────────────────────────────────────────────────────────────────────
def test_capture_floors_at_half_with_no_routed_flow():
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          payouts_history=flat_history(month_range("2025-01", 13), 70_000))]
    req = make_request(chans, total_revenue_ltm=1_200_000)
    _, score, routed = _capture_score(_streams(req), req, month("2026-01"))
    assert routed == 0.0 and score == 0.5


def test_capture_with_routed_flow():
    months = month_range("2025-02", 12)
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          treyd_routed_payouts=flat_history(months, 70_000))]
    req = make_request(chans, total_revenue_ltm=1_200_000)  # 70k*12 = 840k routed / 1.2m = 0.7
    capture, score, _ = _capture_score(_streams(req), req, month("2026-01"))
    assert capture == pytest.approx(0.7, abs=0.01)
    assert score == pytest.approx(0.82, abs=0.01)           # 0.7 / 0.85


def test_capture_fx_normalizes_to_revenue_currency():
    months = month_range("2025-02", 12)
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          treyd_routed_payouts=flat_history(months, 700_000, currency="SEK"))]
    req = make_request(chans, revenue_currency="GBP", total_revenue_ltm=1_000_000, fx_rates={"SEK": 0.075})
    _, _, routed = _capture_score(_streams(req), req, month("2026-01"))
    assert routed == pytest.approx(700_000 * 12 * 0.075, rel=1e-6)  # annualized then FX'd


# ── Full pipeline, controlled inputs ───────────────────────────────────────────────────────────
def test_full_pipeline_known_factors():
    """Flat 70k history, onboarding (no routed): every factor is known, assert the limit exactly."""
    months = month_range("2025-01", 15)  # 15 months → verified_api 15 → ET 6 → base 3.0
    chans = [ChannelInput(channel_id="s", channel_type="shopify_payments",
                          payouts_history=flat_history(months, 70_000))]
    req = make_request(
        chans,
        country="gb",
        payment_behaviour_score=9,                  # 1.0
        instruments=["fixed_charge_controlled_account", "floating_charge", "assignment_of_receivables", "pg_cg"],
        total_revenue_ltm=1_400_000,                # no routed → capture floor 0.5
    )
    resp = compute_limit(req, month("2026-03"), "t")
    mt = resp.merchant_trace
    assert mt.base_months == 3.0
    assert mt.merchant_score == 0.8                 # min(PBS 1.0, rating null 0.8)
    assert mt.capture_score == 0.5
    ch = resp.limits["GBP"].channels[0]
    assert ch.flow_path.value == "api_history"
    assert ch.flow_base == pytest.approx(70_000, abs=1)   # flat → trailing, no floor lift
    assert ch.routing_confirmation == 0.75
    assert ch.flow_score == 1.0                            # inert (no routed months)
    assert ch.legal_security_norm == pytest.approx(0.89, abs=0.01)  # platform full package
    # 3.0 × 0.5 × 0.8 × (70000 × V 1.0 × LS (1.25/1.40) × juris 1.0 × FS 1.0 × RC 0.75)
    ls_norm = 1.25 / 1.40
    expected = 3.0 * 0.5 * 0.8 * (70_000 * 1.0 * ls_norm * 1.0 * 1.0 * 0.75)
    assert resp.limits["GBP"].dynamic_limit == pytest.approx(expected, abs=1)


def test_per_currency_independent_limits():
    """One Shopify channel settling in two currencies → two independent limits (split inside)."""
    months = month_range("2025-01", 13)
    mixed = flat_history(months, 50_000, currency="GBP") + flat_history(months, 20_000, currency="USD")
    ch = ChannelInput(channel_id="shopify", channel_type="shopify_payments", payouts_history=mixed)
    resp = compute_limit(make_request([ch], country="gb"), month("2026-01"), "t")
    assert set(resp.limits) == {"GBP", "USD"}
    assert resp.limits["GBP"].dynamic_limit > resp.limits["USD"].dynamic_limit


def test_routing_confirmation_derived_from_three_months():
    months = month_range("2025-11", 3)
    ch = ChannelInput(channel_id="s", channel_type="shopify_payments",
                      payouts_history=flat_history(month_range("2024-01", 24), 50_000),
                      treyd_routed_payouts=flat_history(months, 50_000))
    resp = compute_limit(make_request([ch], country="gb"), month("2026-01"), "t")
    assert resp.limits["GBP"].channels[0].routing_confirmation == 1.0
