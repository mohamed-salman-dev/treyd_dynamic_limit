"""Shared fixtures: synthetic builders for self-contained tests (no external data files)."""

from __future__ import annotations

from datetime import date

from app.models import ChannelInput, MerchantLimitRequest, MonthlyAmount


def month(value: str) -> date:
    """'YYYY-MM' → first-of-month date, for passing as_of to the engine in tests."""
    return date(int(value[:4]), int(value[5:]), 1)


def month_range(start: str, count: int) -> list[str]:
    year, mon = int(start[:4]), int(start[5:])
    out: list[str] = []
    for i in range(count):
        total = mon - 1 + i
        out.append(f"{year + total // 12:04d}-{total % 12 + 1:02d}")
    return out


def flat_history(months: list[str], amount: float) -> list[MonthlyAmount]:
    return [MonthlyAmount(month=m, amount=amount) for m in months]


# A clear seasonal shape: deep summer trough, sharp Q4 peak (calendar month → multiplier).
SEASONAL_PATTERN: dict[int, float] = {
    1: 0.7, 2: 0.5, 3: 0.6, 4: 0.7, 5: 0.8, 6: 0.7,
    7: 0.6, 8: 0.5, 9: 0.9, 10: 1.5, 11: 2.5, 12: 3.0,
}


def seasonal_history(start: str, count: int, base: float) -> list[MonthlyAmount]:
    """Synthetic monthly flow following SEASONAL_PATTERN — a stand-in for a real seasonal merchant."""
    out: list[MonthlyAmount] = []
    for ym in month_range(start, count):
        cal = int(ym[5:])
        out.append(MonthlyAmount(month=ym, amount=base * SEASONAL_PATTERN[cal]))
    return out


def make_request(channels: list[ChannelInput], **overrides) -> MerchantLimitRequest:
    defaults = dict(
        merchant_id="test",
        revenue_currency="GBP",
        channels=channels,
    )
    defaults.update(overrides)
    return MerchantLimitRequest(**defaults)
