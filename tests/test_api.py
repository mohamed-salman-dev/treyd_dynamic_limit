"""HTTP-surface tests via FastAPI TestClient."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _daily(start: str, n_months: int, monthly: float, currency: str = "GBP", routed: bool = False) -> list[dict]:
    """Build ~daily payout dicts for the JSON payload."""
    y, m = int(start[:4]), int(start[5:])
    out: list[dict] = []
    for i in range(n_months):
        mm = m - 1 + i
        cy, cm = y + mm // 12, mm % 12 + 1
        nxt = date(cy + cm // 12, cm % 12 + 1, 1)
        dim = (nxt - date(cy, cm, 1)).days
        for day in range(1, dim + 1):
            out.append({
                "date": date(cy, cm, day).isoformat(),
                "amount": monthly / dim,
                "currency": currency,
                "routed_to_treyd": routed,
            })
    return out


def _payload(**overrides) -> dict:
    payload = {
        "merchant_id": "m1",
        "as_of_date": "2026-01-31",
        "revenue_currency": "GBP",
        "country": "gb",
        "channels": [
            {"channel_id": "shopify", "channel_type": "shopify_payments", "payouts": _daily("2025-01", 13, 50_000)}
        ],
    }
    payload.update(overrides)
    return payload


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_limit_happy_path():
    r = client.post("/v1/limit", json=_payload())
    assert r.status_code == 200
    body = r.json()
    gbp = next(limit for limit in body["limits"] if limit["currency"] == "GBP")
    assert gbp["dynamic_limit"] > 0
    assert body["merchant_trace"]["base_months"] == 3.0
    assert body["as_of_date"] == "2026-01-31"


def test_unknown_channel_type_is_422():
    bad = _payload()
    bad["channels"][0]["channel_type"] = "carrier_pigeon"
    assert client.post("/v1/limit", json=bad).status_code == 422


def test_missing_fx_rate_is_422():
    p = _payload(revenue_currency="GBP", total_revenue_ltm=1_000_000)
    # routed SEK flow but no fx_rates for SEK -> GBP
    p["channels"][0]["payouts"] = _daily("2025-11", 2, 100_000, currency="SEK", routed=True)
    r = client.post("/v1/limit", json=p)
    assert r.status_code == 422
    assert "fx rate" in r.json()["detail"].lower()


def test_as_of_defaults_to_today_when_omitted():
    p = _payload()
    del p["as_of_date"]
    r = client.post("/v1/limit", json=p)
    assert r.status_code == 200
    assert r.json()["as_of_date"] == date.today().isoformat()
