"""Shared fixtures: synthetic daily-payout builders for self-contained tests."""

from __future__ import annotations

from datetime import date

from app.models import ChannelInput, MerchantLimitRequest, Payout


def d(value: str) -> date:
    """'YYYY-MM-DD' → date."""
    return date.fromisoformat(value)


def m1(value: str) -> date:
    """'YYYY-MM' → first-of-month date (for seasonal-index unit tests)."""
    return date(int(value[:4]), int(value[5:]), 1)


def _days_in_month(year: int, month: int) -> int:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return (nxt - date(year, month, 1)).days


# A clear seasonal shape: deep summer trough, sharp Q4 peak (calendar month → multiplier).
SEASONAL_PATTERN: dict[int, float] = {
    1: 0.7, 2: 0.5, 3: 0.6, 4: 0.7, 5: 0.8, 6: 0.7,
    7: 0.6, 8: 0.5, 9: 0.9, 10: 1.5, 11: 2.5, 12: 3.0,
}


def _emit(year: int, month: int, monthly_total: float, currency: str, routed: bool) -> list[Payout]:
    dim = _days_in_month(year, month)
    per_day = monthly_total / dim
    return [
        Payout(date=date(year, month, day), amount=per_day, currency=currency, routed_to_treyd=routed)
        for day in range(1, dim + 1)
    ]


def flat_daily(start_month: str, n_months: int, monthly_total: float, currency: str = "GBP", routed: bool = False) -> list[Payout]:
    """Daily payouts with the same total every month."""
    y, m = int(start_month[:4]), int(start_month[5:])
    out: list[Payout] = []
    for i in range(n_months):
        mm = m - 1 + i
        out += _emit(y + mm // 12, mm % 12 + 1, monthly_total, currency, routed)
    return out


def seasonal_daily(start_month: str, n_months: int, monthly_base: float, currency: str = "GBP", routed: bool = False) -> list[Payout]:
    """Daily payouts following SEASONAL_PATTERN — a stand-in for a real seasonal merchant."""
    y, m = int(start_month[:4]), int(start_month[5:])
    out: list[Payout] = []
    for i in range(n_months):
        mm = m - 1 + i
        cy, cm = y + mm // 12, mm % 12 + 1
        out += _emit(cy, cm, monthly_base * SEASONAL_PATTERN[cm], currency, routed)
    return out


def make_request(channels: list[ChannelInput], **overrides) -> MerchantLimitRequest:
    defaults = dict(merchant_id="test", revenue_currency="GBP", channels=channels)
    defaults.update(overrides)
    return MerchantLimitRequest(**defaults)
