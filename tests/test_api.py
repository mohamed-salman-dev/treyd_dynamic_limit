"""HTTP-surface tests via FastAPI TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _payload(**overrides) -> dict:
    payload = {
        "merchant_id": "m1",
        "as_of_month": "2026-01",
        "revenue_currency": "GBP",
        "country": "gb",
        "channels": [
            {
                "channel_id": "shopify",
                "channel_type": "shopify_payments",
                "currency": "GBP",
                "payouts_history": [
                    {"month": f"2025-{m:02d}", "amount": 50_000.0} for m in range(1, 13)
                ]
                + [{"month": "2026-01", "amount": 50_000.0}],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_limit_happy_path():
    r = client.post("/v1/limit", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert "GBP" in body["limits"]
    assert body["limits"]["GBP"]["dynamic_limit"] > 0
    assert body["merchant_trace"]["base_months"] == 3.0
    assert body["as_of_month"] == "2026-01"


def test_unknown_channel_type_is_422():
    bad = _payload()
    bad["channels"][0]["channel_type"] = "carrier_pigeon"
    r = client.post("/v1/limit", json=bad)
    assert r.status_code == 422


def test_missing_fx_rate_is_422():
    p = _payload(revenue_currency="GBP", total_revenue_ltm=1_000_000)
    p["channels"][0]["currency"] = "SEK"
    p["channels"][0]["treyd_routed_payouts"] = [{"month": "2025-12", "amount": 100_000.0}]
    # no fx_rates provided for SEK -> revenue GBP
    r = client.post("/v1/limit", json=p)
    assert r.status_code == 422
    assert "fx rate" in r.json()["detail"].lower()


def test_as_of_defaults_to_current_month_when_omitted():
    p = _payload()
    del p["as_of_month"]
    r = client.post("/v1/limit", json=p)
    assert r.status_code == 200
    assert len(r.json()["as_of_month"]) == 7  # "YYYY-MM"
