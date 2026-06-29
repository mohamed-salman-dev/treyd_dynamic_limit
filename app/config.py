"""Lookup tables and model constants.

These are the parameter-sign-off values from the spec (§9). They live here, not in the
request contract, so they can be tuned centrally after the backtest. Every value used in a
computation is echoed into the response trace, so a stored limit remains auditable even as
these change.
"""

from __future__ import annotations

# ── Channel registry (extensibility seam) ────────────────────────────────────────────────
# Adding a channel is one row here — no pipeline change. `flow_type` selects the
# legal-security column (platform vs b2b).
CHANNEL_REGISTRY: dict[str, dict] = {
    "shopify_payments": {"verification_tier": 1, "flow_type": "platform", "settlement_cadence_days": 3},
    "stripe": {"verification_tier": 1, "flow_type": "platform", "settlement_cadence_days": 3},
    "adyen": {"verification_tier": 1, "flow_type": "platform", "settlement_cadence_days": 3},
    "amazon": {"verification_tier": 2, "flow_type": "platform", "settlement_cadence_days": 14},
    "zalando": {"verification_tier": 2, "flow_type": "platform", "settlement_cadence_days": 14},
    "b2b": {"verification_tier": 3, "flow_type": "b2b", "settlement_cadence_days": 30},
}

# ── Verification norms (V_norm = raw / 1.5) ───────────────────────────────────────────────
VERIFICATION_NORMS: dict[int, float] = {1: 1.0, 2: 1.0, 3: 0.67, 4: 0.0}

# ── Legal-security instruments (LS_norm = raw / LS_DENOMINATOR) ───────────────────────────
LS_BASE = 0.70          # T&Cs + routed intercept — always present
LS_DENOMINATOR = 1.40   # full package raw → norm 1.0 on b2b
INSTRUMENT_SCORES: dict[str, dict[str, float]] = {
    "pg_cg": {"b2b": 0.20, "platform": 0.20},
    "floating_charge": {"b2b": 0.10, "platform": 0.10},
    "assignment_of_receivables": {"b2b": 0.15, "platform": 0.10},
    "fixed_charge_controlled_account": {"b2b": 0.25, "platform": 0.15},
}
# Default when `instruments` is missing: the current UK debenture baseline (floating + PG) → 0.71.
LS_DEFAULT_INSTRUMENTS: list[str] = ["floating_charge", "pg_cg"]

# ── Jurisdiction ──────────────────────────────────────────────────────────────────────────
JURISDICTION_FACTORS: dict[str, float] = {
    "gb": 1.0, "ie": 1.0,
    "se": 0.7, "fi": 0.7, "dk": 0.7, "no": 0.7,
    "us": 0.6,
}
JURISDICTION_DEFAULT = 0.6  # unknown → weakest known enforceability

# ── Base_Months / tenure ──────────────────────────────────────────────────────────────────
DAYS_PER_MONTH = 30.0         # routing_days → months for the tenure calc
HISTORY_CREDIT_FACTOR = 0.5   # verified API history credited at half
HISTORY_CREDIT_CAP = 6.0      # max months of credit from history
BASE_MONTHS_ENTRY = 2.0       # Effective_Tenure < 6
BASE_MONTHS_MID = 3.0         # 6 ≤ Effective_Tenure < 13
BASE_MONTHS_HIGH = 4.0        # Effective_Tenure ≥ 13 (earned by routing tenure)
TENURE_MID_THRESHOLD = 6.0
TENURE_HIGH_THRESHOLD = 13.0  # above 4.0 requires base_months_override (committee review)

# ── Merchant_Score (PBS / rating, 1–10 → factor) ──────────────────────────────────────────
MERCHANT_SCORE_DEFAULT = 0.8  # null component defaults to mid tier

# ── Capture ────────────────────────────────────────────────────────────────────────────────
CAPTURE_ANCHOR = 0.85
CAPTURE_FLOOR = 0.50

# ── Flow / seasonal ────────────────────────────────────────────────────────────────────────
TRAILING_WEIGHTS = (0.5, 0.3, 0.2)     # weights for months t-1, t-2, t-3
SEASONAL_FLOOR_GAMMA = 0.8            # Flow_Base = max(Trailing, γ × Forward_Expected)
FLOOR_GUARD_THRESHOLD = 0.70          # floor holds only if actual(t-1) ≥ this × Expected(t-1)
MIN_MONTHS_SEASONAL = 12              # months of history required for a seasonal curve
DEFAULT_TENOR_MONTHS = 3             # forward window for Forward_Expected_Flow

# ── Flow_Score ──────────────────────────────────────────────────────────────────────────────
FLOW_SCORE_FLOOR = 0.6
FLOW_SCORE_MIN_ROUTED_MONTHS = 2      # inert at 1.0 until this many routed months (pilot)

# ── Routing_Confirmation ──────────────────────────────────────────────────────────────────
ROUTING_CONFIRMATION_UNCONFIRMED = 0.75
ROUTING_CONFIRM_MIN_MONTHS = 3        # ≥ this many routed months → confirmed by definition
ROUTING_CONFIRM_PROJECTION_RATIO = 0.80  # a routed month ≥ this × projected → confirmed
