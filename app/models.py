"""Request and response (trace) models for the Dynamic Limit Service.

Payouts are daily: each entry is a single settlement on a calendar date. The service splits a
channel's payouts into per-currency streams, weights each by whether it routed to Treyd, and
computes flow over a fixed trailing window from `as_of_date`.
"""

from __future__ import annotations

import datetime
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import CHANNEL_REGISTRY, DEFAULT_ROUTING_CONFIRMATION, INSTRUMENT_SCORES


# ────────────────────────────────────────────────────────────────────────────────────────
# Request
# ────────────────────────────────────────────────────────────────────────────────────────
class Payout(BaseModel):
    """One daily settlement, in its own currency."""

    model_config = ConfigDict(extra="forbid")

    date: datetime.date = Field(description="Settlement date (ISO 'YYYY-MM-DD').")
    amount: float = Field(ge=0.0, description="Positive amount in this entry's currency.")
    currency: str = Field(min_length=3, max_length=3, description="ISO-4217, e.g. 'GBP'.")
    routed_to_treyd: bool = Field(
        default=False,
        description="True if this settlement landed in the Treyd account (full weight). "
        "False = provisional/pre-Treyd settlement, discounted to the provisional weight.",
    )
    encumbered: bool = Field(
        default=False,
        description="True if this settlement repays financed receivables (excluded from free "
        "flow). Independent of routed_to_treyd; defaults False → all free until flagged.",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()


class ChannelInput(BaseModel):
    """One channel/integration (e.g. Shopify Payments). Daily, multi-currency payouts are carried
    on the entries; the service splits them into per-currency streams internally."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    channel_type: str = Field(description="Registry key, e.g. 'shopify_payments'.")

    payouts: list[Payout] = Field(
        default_factory=list,
        description="Daily settlements, any currency, each tagged routed_to_treyd / encumbered.",
    )
    routing_confirmation: float = Field(
        default=DEFAULT_ROUTING_CONFIRMATION,
        ge=0.0,
        le=1.0,
        description="Optional per-channel multiplier on the channel contribution. Default 1.0 "
        "(neutral); the provisional weight already carries the routing discount.",
    )

    @field_validator("channel_type")
    @classmethod
    def _known_channel(cls, v: str) -> str:
        if v not in CHANNEL_REGISTRY:
            raise ValueError(f"unknown channel_type {v!r}; known: {sorted(CHANNEL_REGISTRY)}")
        return v


class MerchantLimitRequest(BaseModel):
    """Everything needed to compute a merchant's per-currency limits. Stateless."""

    model_config = ConfigDict(extra="forbid")

    merchant_id: str
    as_of_date: date | None = Field(
        default=None,
        description="Anchor date (ISO 'YYYY-MM-DD'). Defaults to today; a past date is a backtest.",
    )
    tenor_months: int = Field(default=3, ge=1, le=12, description="Forward window for the seasonal floor.")

    # merchant-level factors (computed once, applied to every currency limit)
    routing_days: int = Field(default=0, ge=0, description="Days since routing onboarding. 0 pre-launch.")
    base_months_override: float | None = Field(
        default=None, ge=0.0, description="Credit override; required only for Base_Months above 4.0."
    )
    payment_behaviour_score: int | None = Field(default=None, ge=1, le=10, description="PBS 1–10.")
    rating_score: int | None = Field(default=None, ge=1, le=10, description="Credit rating 1–10.")
    country: str | None = Field(default=None, description="ISO-3166 alpha-2, lowercase. None → 0.6.")

    instruments: list[str] | None = Field(
        default=None, description="Signed legal-security instruments. None → floating + PG (0.71)."
    )
    total_revenue_ltm: float | None = Field(
        default=None, ge=0.0, description="Whole-business LTM revenue, in revenue_currency."
    )
    revenue_currency: str = Field(min_length=3, max_length=3, description="Currency of total_revenue_ltm.")
    fx_rates: dict[str, float] = Field(
        default_factory=dict, description="{currency: rate to revenue_currency}. revenue_currency implicit 1.0."
    )

    channels: list[ChannelInput] = Field(min_length=1)

    @field_validator("instruments")
    @classmethod
    def _known_instruments(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            unknown = [i for i in v if i not in INSTRUMENT_SCORES]
            if unknown:
                raise ValueError(f"unknown instruments {unknown}; known: {sorted(INSTRUMENT_SCORES)}")
        return v

    @field_validator("country")
    @classmethod
    def _lower_country(cls, v: str | None) -> str | None:
        return v.lower() if v else v

    @field_validator("revenue_currency")
    @classmethod
    def _upper_rev_currency(cls, v: str) -> str:
        return v.upper()


# ────────────────────────────────────────────────────────────────────────────────────────
# Response / trace
# ────────────────────────────────────────────────────────────────────────────────────────
class ChannelTrace(BaseModel):
    channel_id: str
    channel_type: str
    currency: str

    # flow
    trailing_flow: float          # weighted 90-day window (routed + provisional)
    routed_share: float           # fraction of trailing-window flow that routed to Treyd
    seasonal_eligible: bool
    ltm_avg: float | None
    expected_flow_last_month: float | None
    forward_expected_flow: float | None
    seasonal_floor_active: bool
    floor_guard_ok: bool
    flow_base: float

    # quality
    verification_norm: float
    legal_security_norm: float
    jurisdiction: float
    flow_score: float
    quality_q: float

    routing_confirmation: float
    channel_contribution: float


class LegalSecurityTrace(BaseModel):
    instruments_applied: list[str]
    raw_platform: float
    raw_b2b: float
    norm_platform: float
    norm_b2b: float


class MerchantTrace(BaseModel):
    effective_tenure: float
    routing_days: int
    verified_api_history_months: int
    base_months: float
    base_months_override_used: bool

    capture: float
    capture_score: float
    total_routed_annualized: float
    total_revenue_ltm: float | None

    merchant_score: float
    payment_behaviour_factor: float
    rating_factor: float

    legal_security: LegalSecurityTrace
    constants_applied: dict


class CurrencyLimit(BaseModel):
    dynamic_limit: float
    channel_sum: float
    channels: list[ChannelTrace]


class MerchantLimitResponse(BaseModel):
    merchant_id: str
    computed_at: str
    as_of_date: str
    revenue_currency: str
    limits: dict[str, CurrencyLimit]
    merchant_trace: MerchantTrace
