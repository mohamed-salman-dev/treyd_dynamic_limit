"""FastAPI surface for the Dynamic Limit Service.

The HTTP layer is thin: resolve `as_of_date` and the timestamp at the edge, delegate to the
pure engine, and translate domain errors into 422s. All model logic lives in `engine.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException

from . import __version__
from .engine import FxRateMissing, compute_limit
from .models import MerchantLimitRequest, MerchantLimitResponse

app = FastAPI(
    title="Treyd ONE — Dynamic Limit Service",
    version=__version__,
    summary="Stateless per-currency dynamic credit limit with full audit trace.",
)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/v1/limit", response_model=MerchantLimitResponse, tags=["limit"])
def post_limit(req: MerchantLimitRequest) -> MerchantLimitResponse:
    """Compute the dynamic limit per payout currency for one merchant."""
    as_of = req.as_of_date or date.today()
    computed_at = datetime.now(timezone.utc).isoformat()
    try:
        return compute_limit(req, as_of, computed_at)
    except FxRateMissing as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
