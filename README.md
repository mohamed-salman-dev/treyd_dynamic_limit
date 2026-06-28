# Dynamic Limit Service — Treyd ONE

Stateless FastAPI service that turns merchant flow history plus external scoring inputs into a
**Dynamic credit limit per payout currency**, with a full audit trace.

The model is defined in [`DYNAMIC_LIMIT_SPEC.md`](DYNAMIC_LIMIT_SPEC.md). The service is a pure
function: it holds no state, makes no external calls, and takes no policy action (no freezes /
triggers) — consumers own state and build any monitoring on top of the returned numbers.

## Layout

```text
app/
  config.py    lookup tables + tunable constants (parameter sign-off values)
  models.py    Pydantic request/response (trace) models; months as date
  engine.py    pure computation pipeline (spec §6)
  main.py      FastAPI surface — resolves as_of/timestamp, delegates to engine
tests/         engine + API tests, incl. validation against the real pilot CSV
Dockerfile     non-root, uv-based, with /health healthcheck
```

## Run

```bash
uv sync
uv run uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
uv run pytest -q                          # 45 tests
```

### Docker

```bash
docker build -t dynamic-limit .
docker run -p 8000:8000 dynamic-limit     # GET /health, POST /v1/limit
```

## Request shape (minimal)

```bash
curl -X POST localhost:8000/v1/limit -H 'content-type: application/json' -d '{
  "merchant_id": "demo",
  "as_of_month": "2026-01",          # omit → current month; a past month is a backtest
  "revenue_currency": "GBP",
  "country": "gb",
  "payment_behaviour_score": 9,
  "rating_score": 8,
  "channels": [{
    "channel_id": "shopify",
    "channel_type": "shopify_payments",
    "currency": "GBP",
    "payouts_history": [{"month": "2025-01", "amount": 40000}, ...],
    "treyd_routed_payouts": []        # flow actually routed to Treyd; empty pre-launch
  }]
}'
```

Returns one limit per payout currency under `limits`, plus a `merchant_trace` and per-channel
trace carrying every intermediate factor.

## Key behaviours

- **Per currency.** One limit per payout currency; merchant-level factors (Base_Months,
  Capture_Score, Merchant_Score, Jurisdiction, Legal_Security) apply to each.
- **`as_of_month` is point-in-time.** History is truncated to the anchor — a past month yields
  exactly the limit the model would have produced then (backtesting, no lookahead).
- **Safe defaults.** Missing rating/PBS → 0.8; missing instruments → 0.71 (UK debenture);
  unknown jurisdiction → 0.6; no routed flow → capture floor 0.5.
- **Seasonal floor.** With ≥12 months of history, `Flow_Base = max(Trailing, 0.8 × forward
  seasonal expectation)`, lifting capacity into a merchant's peak.
