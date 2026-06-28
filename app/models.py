"""Request and response (trace) models for the Dynamic Limit Service.

Months are represented as `datetime.date` pinned to the first of the month. The `Month` type
accepts either a full ISO date or a `"YYYY-MM"` string and normalizes to the first of the month,
so callers can send `"2026-01"` and Pydantic handles validation.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from .config import CHANNEL_REGISTRY, INSTRUMENT_SCORES


def _coerce_year_month(v: object) -> object:
    """Turn a bare ``"YYYY-MM"`` into a parseable ISO date; pass anything else through."""
    if isinstance(v, str) and len(v) == 7 and v[4] == "-":
        return f"{v}-01"
    return v


def _first_of_month(v: date) -> date:
    return v.replace(day=1)


Month = Annotated[date, BeforeValidator(_coerce_year_month), AfterValidator(_first_of_month)]


# ────────────────────────────────────────────────────────────────────────────────────────
# Request
# ────────────────────────────────────────────────────────────────────────────────────────
class MonthlyAmount(BaseModel):
    """One month of flow for a channel, in that channel's declared currency."""

    model_config = ConfigDict(extra="forbid")

    month: Month = Field(description="Calendar month: 'YYYY-MM' or an ISO date.")
    amount: float = Field(ge=0.0, description="Positive amount in this entry's currency.")
    currency: str = Field(min_length=3, max_length=3, description="ISO-4217 of this settlement, e.g. 'GBP'.")
    encumbered: bool = Field(
        default=False,
        description="On routed entries: True if this settlement repays financed receivables "
        "(excluded from free flow). Defaults False → all free until flagged.",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()


class ChannelInput(BaseModel):
    """One channel/integration (e.g. Shopify Payments). Multi-currency settlements are carried on
    the payout entries; the service splits them into per-currency streams internally."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    channel_type: str = Field(description="Registry key, e.g. 'shopify_payments'.")

    payouts_history: list[MonthlyAmount] = Field(
        default_factory=list,
        description="All platform settlements (pre-Treyd and Treyd), any currency. Feeds flow curve + tenure.",
    )
    treyd_routed_payouts: list[MonthlyAmount] = Field(
        default_factory=list,
        description="Settlements that landed in the Treyd account, any currency. Feeds Trailing, capture, RC.",
    )
    payouts_history_is_accounting: bool = Field(
        default=False,
        description="True if payouts_history is accounting/sales estimate rather than verified "
        "settlements; applies the provisional 0.7 haircut when no routed flow exists yet.",
    )
    routing_confirmed: bool | None = Field(
        default=None,
        description="True/False to assert (channel-wide); None → derive per currency (fallback 0.75).",
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
    as_of_month: Month | None = Field(
        default=None,
        description="Anchor month. Defaults to current month; a past month is a backtest.",
    )
    tenor_months: int = Field(default=3, ge=1, le=12, description="Forward window for the seasonal floor.")

    # merchant-level factors (computed once, applied to every currency limit)
    routing_months: int = Field(default=0, ge=0, description="Months since routing onboarding. 0 pre-launch.")
    base_months_override: float | None = Field(
        default=None, ge=0.0, description="Credit override; the only path to 4.0+."
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
class FlowPath(str, Enum):
    ROUTED = "routed"
    API_HISTORY = "api_history"
    PROVISIONAL = "provisional"
    NONE = "none"  # no usable history


class ChannelTrace(BaseModel):
    channel_id: str
    channel_type: str
    currency: str
    flow_path: FlowPath

    # flow
    trailing_flow: float
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
    routing_months: int
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
    as_of_month: str
    revenue_currency: str
    limits: dict[str, CurrencyLimit]
    merchant_trace: MerchantTrace
