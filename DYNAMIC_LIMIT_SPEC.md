# Dynamic Limit Service — API Specification

**Status:** v1.0 (MVP) · derived from *Treyd ONE — Dynamic Limit Model: Production Spec v2.1*
**Endpoint:** `POST /v1/limit`
**Output:** one `Dynamic_Limit` per payout currency, with a full audit trace.

---

## 1. Service Contract

The service is a **stateless pure function**. It holds no state, makes no external
calls, and enforces no policy decisions. Every call is independent and reproducible.

- The caller owns all state: prior limits, the displayed-limit glide, drawn balances,
  freeze decisions, FX rate fetching, and revenue/instrument/rating lookups.
- The service computes the **model limit** and returns every intermediate value used
  to reach it. It takes **no policy action** — no freezes, no triggers. Consumers build
  triggers on top of the numbers the trace exposes (e.g. expected-vs-actual flow).

### Per-currency, with shared merchant factors

The unit of a limit is `(merchant, payout currency)`. A merchant settling GBP + USD + EUR
receives three independent limits, each in its own currency.

- **Per-currency** quantities: `Trailing_Flow`, `Expected_Flow`, `Flow_Base`,
  `Flow_Score`, `Verification_norm`, `Routing_Confirmation` → combined into the channel sum `Q`.
- **Merchant-level** factors, computed once and applied to every currency limit:
  `Base_Months`, `Capture_Score`, `Merchant_Score`, `Jurisdiction`, `Legal_Security_norm`.

FX is used in exactly one place — inside `Capture_Score` — never on the limit itself.

---

## 2. The Formula

```text
Dynamic_Limit[ccy] =
      Base_Months × Capture_Score × Merchant_Score                       ← merchant-level
    × Σ over channels in ccy (
          Flow_Base × Verification_norm × Legal_Security_norm
                    × Jurisdiction × Flow_Score × Routing_Confirmation
      )                                                                  ← per-channel

Q          = Verification_norm × Legal_Security_norm × Jurisdiction × Flow_Score
Flow_Base  = max( Trailing_Flow , γ × Forward_Expected_Flow )            γ = 0.8
```

Every factor is ≤ 1.0, so `Dynamic_Limit ≤ Base_Months × Σ Flow_Base` holds by
construction — the coverage ceiling is structural, with no separate hard cap.

---

## 3. Responsibility Boundary

| This service owns | The calling service owns |
|---|---|
| Trailing_Flow (weighted 3-month) | Filtering payouts to Treyd-routed only |
| Expected_Flow curve + seasonal index | Tagging each payout with its settlement currency |
| Splitting channels into per-currency streams | — |
| Seasonal floor + floor guard | Aggregating raw payouts to monthly totals |
| Legal-security norm from instruments | Fetching FX rates (e.g. Google Sheet) → `fx_rates` |
| Quality `Q` per channel | Prior displayed limit + glide computation |
| Capture_Score (with FX normalization) | Drawn-balance maintenance |
| Effective_Tenure → Base_Months | Enforcing any freeze/trigger from trace values |
| Routing_Confirmation (derive or accept) | Pulling instruments record from CRM |
| Dynamic_Limit + full trace | Pulling PBS + rating; ERP revenue; encumbered flow |
| All lookup tables | Deciding whether to call (e.g. below-appetite) |

---

## 4. Lookup Tables

### 4.1 Channel registry (extensibility seam)

Adding a channel is one row here — no pipeline change. `flow_type` selects the
legal-security column (platform vs b2b).

| channel_type | verification_tier | flow_type | settlement_cadence_days |
|---|---|---|---|
| `shopify_payments` | 1 | platform | 3 |
| `stripe` | 1 | platform | 3 |
| `adyen` | 1 | platform | 3 |
| `amazon` | 2 | platform | 14 |
| `zalando` | 2 | platform | 14 |
| `b2b` | 3 | b2b | 30 |

### 4.2 Verification norms (`V_norm = raw / 1.5`)

| Tier | V_norm | Description |
|---|---|---|
| 1 | 1.00 | Platform/PSP settlement into Treyd + full API |
| 2 | 1.00 | Marketplace settlement into Treyd + API; longer detection lag |
| 3 | 0.67 | Bank inflow into Treyd, no platform API |
| 4 | 0.00 | No routing — accounting data only |

Pilot is constant at 1.0 (Shopify only). Tier > 0 requires funds landing in the Treyd
account; read-only API where cash settles elsewhere is tier 4.

### 4.3 Legal-security instruments (`LS_norm = raw / 1.4`)

```text
Legal_Security_raw = 0.7 + Σ instrument_scores[instrument][flow_type]
```

| Instrument | b2b | platform | What it buys |
|---|---|---|---|
| base (T&Cs + routed intercept) | 0.70 | 0.70 | Always present; intercept is primary security |
| `pg_cg` | +0.20 | +0.20 | Deterrence at the redirect decision; demand in days |
| `floating_charge` | +0.10 | +0.10 | Administrator appointment without court |
| `assignment_of_receivables` | +0.15 | +0.10 | Redirects in-flight invoices in days |
| `fixed_charge_controlled_account` | +0.25 | +0.15 | Perfects the intercept; UK/Ireland only |

Reference combinations:

| Combination | b2b → norm | platform → norm |
|---|---|---|
| Full package (fixed+floating+assignment+PG) | 1.40 → 1.00 | 1.25 → 0.89 |
| Floating+assignment+PG (Sweden max) | 1.15 → 0.82 | 1.10 → 0.79 |
| **Floating+PG (current UK debenture) — DEFAULT** | 1.00 → **0.71** | 1.00 → **0.71** |
| PG only | 0.90 → 0.64 | 0.90 → 0.64 |
| No security (T&Cs only) | 0.70 → 0.50 | 0.70 → 0.50 |

**Default when `instruments` is missing: 0.71** (floating + PG). This matches the
current UK debenture baseline that the pilot cohort already holds. The consumer passes
the explicit instrument list per merchant; the default applies only when data is absent.

### 4.4 Jurisdiction

| country | factor |
|---|---|
| `gb`, `ie` | 1.0 |
| `se`, `fi`, `dk`, `no` | 0.7 |
| `us` | 0.6 |
| None / unknown | **0.6** (default — weakest known enforceability) |

### 4.5 Base_Months schedule

```text
Effective_Tenure = routing_months + min(0.5 × verified_api_history_months, 6)
```

| Effective_Tenure | Base_Months | Condition |
|---|---|---|
| 0 – 5 | 2.0 | rating within appetite |
| 6 – 12 | 3.0 | no hard-freeze triggers |
| ≥ 13 | 4.0 | requires `base_months_override` (behaviour gate / committee) |

`verified_api_history_months` is **derived** from `payouts_history` (count of distinct
months, longest currency stream). The credit caps at 6, so any merchant with ≥12 months of
history lands at Effective_Tenure 6 → **Base_Months 3.0** at onboarding (`routing_months = 0`).
**4.0 is unreachable by computation alone** — it requires `base_months_override`.

### 4.6 Merchant_Score components (1–10 raw → factor)

```text
Merchant_Score = min( map_pbs(PBS) , map_rating(rating) )
```

| PBS | factor | | rating | tier | factor |
|---|---|---|---|---|---|
| 8–10 | 1.0 | | 8–10 | top | 1.0 |
| 6–7 | 0.8 | | 6–7 | mid | 0.8 |
| ≤5 | 0.6 | | 4–5 | bottom | 0.6 |
| null | 0.8 | | 1–3 | below appetite | 0.0 |
| &nbsp; | &nbsp; | | null | mid | 0.8 |

Boundaries are lower-inclusive, upper-exclusive (top inclusive). The default applies
**per component before the min**, so a missing input caps at 0.8 rather than being ignored.
A null rating defaults to mid (0.8) — never to below-appetite; only an actual sub-4 rating
zeroes the limit.

---

## 5. Input Contract

```python
class MonthlyAmount(BaseModel):
    month:      str            # "YYYY-MM"
    amount:     float          # positive, in this entry's currency
    currency:   str            # ISO-4217 of this settlement — the service splits streams by it
    encumbered: bool = False   # on routed entries: True = repays financed receivables (excluded
                               #   from free flow). Defaults False → all free until flagged.

class ChannelInput(BaseModel):
    channel_id:           str
    channel_type:         str                       # registry key (integration, e.g. shopify_payments)
    payouts_history:      list[MonthlyAmount]        # all settlements, any currency — flow curve + tenure
    treyd_routed_payouts: list[MonthlyAmount] = []   # Treyd-routed, any currency — Trailing, capture, RC
    routing_confirmed:    bool | None         = None # None → derive per currency; fallback 0.75
    # Currency lives on each MonthlyAmount. A channel = one integration; the service splits its
    # payouts into one stream per settlement currency (so a Shopify Markets store with 5 currencies
    # is ONE ChannelInput → 5 streams → 5 limits).

class MerchantLimitRequest(BaseModel):
    merchant_id:   str
    as_of_month:   str | None = None     # default current month; a past month = backtest
    tenor_months:  int        = 3

    # merchant-level factors (computed once, applied to every currency limit)
    routing_months:          int                       # onboarding state; 0 pre-launch
    base_months_override:    int | None       = None    # credit sets 4.0 / individual review
    payment_behaviour_score: int | None       = None    # PBS 1–10;    None → 0.8
    rating_score:            int | None       = None    # rating 1–10; None → 0.8 (mid)
    country:                 str | None       = None    # None → jurisdiction 0.6
    instruments:             list[str] | None  = None    # None → LS_norm 0.71
    total_revenue_ltm:       float | None      = None    # ERP whole-business, in revenue_currency
    revenue_currency:        str
    fx_rates:                dict[str, float]  = {}      # ccy → revenue_currency

    channels:                list[ChannelInput]

# internal constants — echoed into trace, not part of the contract
SEASONAL_FLOOR_GAMMA  = 0.8
YOY_CLAMP             = (0.7, 1.3)
FLOOR_GUARD_THRESHOLD = 0.70
MIN_MONTHS_SEASONAL   = 12
CAPTURE_ANCHOR        = 0.85
CAPTURE_FLOOR         = 0.50
```

---

## 6. Computation Pipeline

All steps run per `(merchant, currency)` except the merchant-level factors, which are
computed once. The service first truncates all history to `≤ as_of_month` so that any
past anchor is an honest, no-lookahead backtest.

### Step 1 — Legal_Security_norm (merchant)

```text
raw(flow_type)  = 0.7 + Σ instrument_scores[instrument][flow_type]   # default → floating+PG
LS_norm(flow_type) = raw / 1.4
```

### Step 2 — Effective_Tenure → Base_Months (merchant)

```text
verified_api_history_months = count of months in payouts_history (longest stream)
Effective_Tenure = routing_months + min(0.5 × verified_api_history_months, 6)
Base_Months = base_months_override if provided else schedule(Effective_Tenure)  # caps at 3.0
```

### Step 3 — Per-channel flow (each currency stream)

**3a. Flow source (3-tier priority).** A single `effective_flow_series` feeds both
Trailing_Flow and the seasonal curve:

| Priority | Condition | Path | Trailing source | Routing_Confirmation |
|---|---|---|---|---|
| 1 | `treyd_routed_payouts` present | ROUTED | routed months | 1.0 |
| 2 | empty; history is api-verified SP payouts | API_HISTORY | payouts at face value | 0.75 |
| 3 | empty; only accounting/sales totals | PROVISIONAL | × 0.7 haircut | 0.75 |

> **Sales insights is not the flow source.** Total Sales is all payment methods; only
> Shopify Payments cash routes to Treyd. In pilot data payouts are a median 0.72 of total
> sales (range 0.34–0.89), so total sales would overstate routable flow by ~39%. The
> `× 0.7` PROVISIONAL haircut is empirically near that 0.72 SP-share.

**3b. Encumbered deduction.** Free flow per month = sum of routed amounts where
`encumbered` is false. The flag lives per-entry on `MonthlyAmount` and defaults to `False`,
so every routed payout is free until the consumer starts flagging settlements that repay
financed receivables — no separate input or migration needed.

**3c. Trailing_Flow**

```text
Trailing_Flow = 0.5×flow(t-1) + 0.3×flow(t-2) + 0.2×flow(t-3)
```

Provisional months fill missing slots and decay to zero by month 3. The weights also give
a natural 3-month glide from projected (history) to actual (routed) flow.

**3d. Expected_Flow (≥12 months in this currency stream)**

A normalized **seasonal index** is derived from history (the shape: December ≈ 2.3×, August
≈ 0.4×) and applied to the **recent LTM average** (the level). This is the spec's
`LTM_avg × Seasonal_Index` structure — the index says *how much busier* a month is than
average; the LTM average says *how big the business is now*.

```text
# Seasonal index — the shape, from all history ≤ as_of
month_avg[c]      = mean of flows in calendar month c (across all years present)
overall           = mean( month_avg[1..12] )            # equal weight per calendar month
raw[c]            = month_avg[c] / overall
smoothed[c]       = mean( raw[c-1], raw[c], raw[c+1] )   # spec's 3-month centered smoothing (circular)
Seasonal_Index[c] = smoothed[c] / mean(smoothed[1..12]) # re-normalize so the 12 indices average 1.0

# Expectation — apply the shape to the current level
LTM_avg           = mean( flow over the trailing 12 months ending at t )
Expected_Flow(m)  = LTM_avg × Seasonal_Index[ calendar_month(m) ]
Forward_Expected_Flow = mean( Expected_Flow[t+1 … t+tenor_months] )
```

No separate YoY term: `LTM_avg` is the *current* trailing level, so it already carries growth.
Re-applying the spec's `YoY_growth` on top would double-count, so it is dropped.

**Bug-prevention rules (this section is numerically fragile):**

- Every referenced month must (a) exist in the series and (b) be `≤ as_of_month` — no
  lookahead. A real in-range gap reads as 0; an out-of-range month is excluded, never 0.
- Eligibility requires all 12 calendar months represented in history *and* a full trailing-12m
  window; otherwise `Flow_Base = Trailing_Flow`.
- The index is **normalized to mean 1.0** at the end. Without this, a history window with uneven
  calendar coverage (e.g. summer months sampled twice, winter once) over-weights the frequent
  months and inflates every expectation (~14% in testing). Re-normalization removes that bias.

Degradation by history depth: `<12m` → no seasonal (Flow_Base = Trailing_Flow);
`≥12m` → seasonal index × LTM. Validated bug-free across all 23 pilot streams (Appendix A).

> **Future:** the index/level split here is a deliberately simple stand-in. Once there is more
> routed history, replace it with an **STL decomposition** (seasonal-trend-Loess) for a smoother,
> less single-year-sensitive seasonal estimate. The `Flow_Base = max(Trailing, γ × Forward)`
> consumer of it stays unchanged.

**3e. Flow_Base + floor guard**

```text
guard_ok  = flow(t-1) ≥ FLOOR_GUARD_THRESHOLD × Expected_Flow(t-1)   # 0.70
Flow_Base = max(Trailing_Flow, γ × Forward_Expected_Flow)  if seasonal-eligible AND guard_ok
          = Trailing_Flow                                   otherwise
```

The guard determines the *number* (it stays in the model); the associated soft-freeze
signal is the consumer's to build from the exposed Expected vs actual.

**3f. Routing_Confirmation**

```text
routing_confirmed == True  → 1.0
routing_confirmed == False → 0.75
routing_confirmed is None  → derive: (≥3 routed months) OR (a month ≥80% of projection) → 1.0
                                       else → 0.75
```

### Step 4 — Q (per channel)

```text
V_norm       = registry[channel_type].verification_tier → norm
LS_norm      = Step 1 result for channel's flow_type
Jurisdiction = country lookup (default 0.6)
Flow_Score   = 1.0                                              if routed_months < 2  (pilot)
             = clamp( routed(t-1) / Expected_Flow(t-1 | ≤ t-2) , 0.6 , 1.0 )  otherwise
Q = V_norm × LS_norm × Jurisdiction × Flow_Score
```

> **Flow_Score uses an out-of-sample expectation.** Grading `actual(t-1)` against an
> Expected_Flow that *includes* t-1 is circular leakage. The service recomputes the
> expectation for t-1 from history `≤ t-2` (an internal point-in-time recalc reusing the
> truncation logic) — a true one-step-ahead forecast error. Not consumer state. Inert at
> 1.0 for the pilot until ≥2 routed months exist.

### Step 5 — Capture_Score (merchant)

Capture uses **actual routed flow only** (not projected). The daily recompute makes it an
organic ramp: as routing accumulates, capture rises and the limit grows. FX-normalizes the
multi-currency numerator into the single revenue currency.

```text
rate(ccy)     = fx_rates[ccy → revenue_currency]   (1.0 if same)
routed_annual = Σ streams: Σ treyd_routed_payouts[trailing 12m] × rate(ccy)
                (if <12 routed months: scale available window × 12/n)
Capture       = routed_annual / total_revenue_ltm
Capture_Score = min(1.0, max(0.50, Capture / 0.85))
```

Numerator and denominator must share a time basis: trailing-12m routed ÷ LTM revenue
(window-matched, seasonality cancels). Pre-launch (no routed flow) → Capture 0 → floor 0.5.

### Step 6 — Dynamic_Limit (per currency)

```text
channel_contribution(ch) = Flow_Base × V_norm × LS_norm × Jurisdiction
                                     × Flow_Score × Routing_Confirmation
Dynamic_Limit[ccy] = Base_Months × Capture_Score × Merchant_Score
                   × Σ channel_contribution(ch in ccy)
```

---

## 7. Output (trace)

Every intermediate value contributing to the limit is returned, so a reviewer can
reconstruct it from the trace alone.

```python
class CurrencyLimit(BaseModel):
    dynamic_limit:  float
    channel_sum:    float
    channels:       list[ChannelTrace]

class ChannelTrace(BaseModel):
    channel_id: str; channel_type: str; currency: str
    flow_path: str                      # ROUTED | API_HISTORY | PROVISIONAL
    trailing_flow: float
    seasonal_eligible: bool
    expected_flow_last_month: float | None
    forward_expected_flow: float | None
    floor_active: bool; floor_guard_ok: bool
    flow_base: float
    verification_norm: float; legal_security_norm: float
    jurisdiction: float; flow_score: float; Q: float
    routing_confirmation: float
    channel_contribution: float

class MerchantTrace(BaseModel):
    effective_tenure: float; routing_months: int; verified_api_history_months: int
    base_months: float; base_months_override_used: bool
    capture: float; capture_score: float
    merchant_score: float; payment_behaviour_factor: float; rating_factor: float
    legal_security: dict      # instruments, raw + norm per flow_type
    constants_applied: dict   # γ, YoY clamp, guard threshold, capture anchor/floor

class MerchantLimitResponse(BaseModel):
    merchant_id: str
    computed_at: str          # ISO-8601 UTC
    as_of_month: str
    limits: dict[str, CurrencyLimit]   # keyed by currency
    merchant_trace: MerchantTrace
```

The model emits numbers and the factor trace only — no triggers or redirection signals.
Consumers build any monitoring (flow deviation, redirection, freezes) on top of the
exposed values.

---

## 8. MVP Scope

**In scope:** Shopify-only ingestion; per-currency limits; flow source 3-tier priority;
seasonal floor + guard; Base_Months from tenure; Capture from routed flow; Merchant_Score,
Jurisdiction, Legal_Security as inputs with defaults; `as_of_month` backtest; full trace.

**Hardcoded / deferred for the pilot:**
- `Flow_Score = 1.0` until ≥2 routed months (out-of-sample recalc enabled later).
- `Verification_norm = 1.0` (Shopify only; registry already supports more).
- Encumbered flow = 0 in practice (per-entry `encumbered` flag exists in the model, defaults False → all free until the consumer flags settlements).
- Glide / displayed limit, freezes — owned by the consumer.

**Pilot cohort:** UK first; Shopify merchants (Routing_Confirmation 0.75) plus 1–2 existing
receivables-routing merchants (Routing_Confirmation 1.0, `base_months_override` for 4.0).

---

## 9. Open Items / Parameter Sign-off

Judgment values pending the 159-default backtest: Base_Months schedule, legal-security
weights, capture anchor 0.85 / floor 0.50, history credit 0.5×, routing confirmation
0.75 / 80%, seasonal γ 0.8, YoY clamp [0.7, 1.3], floor guard 0.70.

Other open items: `total_revenue_ltm` staleness rules; residual free-flow refinement for
receivables merchants; B2B capture-confidence haircut; Swedish security review; risk-adjusted
pricing from the same factors; long-cycle merchants whose cash cycle outruns `tenor_months`.

---

## Appendix A — Data validation (AWAM VENTURES LTD, GBP, 24 months)

Seasonal floor behaviour, computed on real pilot data:

Seasonal index (normalized, mean 1.000): Aug 0.33, Sep 0.64, Oct 1.15, Nov 2.23, Dec 2.27.
Numbers below are emitted by the implementation (`app/engine.py`), not hand-computed.

| As-of month | Trailing_Flow | 0.8 × Forward | Flow_Base | Result |
|---|--:|--:|--:|---|
| Sep 2025 (entering peak) | 27,629 | 112,188 | **112,188** | floor binds — 4.1× lift |
| Oct 2025 (pre-peak) | 38,908 | 131,465 | **131,465** | floor binds |
| Jan 2026 (post-peak) | 266,761 | 43,123 | **266,761** | trailing wins |
| Jun 2026 (summer) | 67,838 | 34,728 | **67,838** | trailing wins |

The floor lifts capacity going into the peak (repaid by the Q4 flows routing through), then
trailing carries the high actuals. Validated bug-free across all 23 pilot currency streams:
thin streams (<12m) fall back to trailing, and the floor guard correctly collapses on streams
with unexplained recent decline (Karen Mabon, Oy Nykarleby). Reproduced by the test suite
(`tests/test_engine.py`).
