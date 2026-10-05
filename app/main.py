"""Kage screener sidecar.

Crawls KLSE Screener (klsescreener.com) and exposes the filtered Bursa Malaysia
result set over HTTP for `kage-backend`.

    GET /health              cache state; never triggers a crawl
    GET /screener/shariah    Shariah-compliant uptrend rows in RM 0.20-1.50,
                             top 30 by volume (cached)

The crawl is cached in memory with a TTL (default 30 minutes) so a page load
does not become an upstream request, and so we stay well inside the site's
20-second crawl-delay. Nothing is persisted and nothing is fabricated: when the
upstream site cannot be read the API reports an error rather than returning an
empty-but-successful or invented result set.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from .klsescreener import (
    PRICE_MAX,
    PRICE_MIN,
    TOP_ACTIVE_LIMIT,
    ScreenerCache,
    ScreenerUnavailable,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kage.screener")

SOURCE = "klsescreener.com"
SOURCE_URL = "https://www.klsescreener.com/v2/"

#: How long a crawl is reused. 30 minutes is far longer than the site's
#: 20-second crawl-delay, so normal use causes at most a couple of crawls/hour.
CACHE_TTL_SECONDS = int(os.getenv("KAGE_SCREENER_TTL", "1800"))
#: The results fragment is ~2.3 MB, so the read gets a generous timeout.
UPSTREAM_TIMEOUT_SECONDS = int(os.getenv("KAGE_SCREENER_TIMEOUT", "45"))

cache = ScreenerCache(ttl_seconds=CACHE_TTL_SECONDS, timeout_seconds=UPSTREAM_TIMEOUT_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("kage-screener starting (ttl=%ss)", CACHE_TTL_SECONDS)
    yield
    await cache.aclose()
    logger.info("kage-screener stopped")


app = FastAPI(title="Kage screener sidecar", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    """Cache state only -- deliberately does not trigger a crawl so the
    container healthcheck stays cheap and never hammers the upstream site."""
    rows, fetched_at, fresh = cache.snapshot()
    return {
        "status": "ok",
        "source": SOURCE,
        "sourceUrl": SOURCE_URL,
        "rowCount": len(rows),
        "cachedAt": int(fetched_at * 1000) if fetched_at else None,
        "cacheAgeSeconds": round(cache.age_seconds, 1) if fetched_at else None,
        "cacheTtlSeconds": CACHE_TTL_SECONDS,
        "cacheFresh": fresh,
    }


@app.get("/screener/shariah")
async def shariah(refresh: bool = Query(False, description="Force a re-crawl")) -> dict[str, Any]:
    """Shariah-compliant, uptrend Bursa instruments priced RM 0.20-1.50.

    Returns the top 30 by volume, as published by KLSE Screener.
    """
    try:
        rows, fetched_at = await cache.rows(force=refresh)
    except ScreenerUnavailable as exc:
        # 503 mirrors how the OpenD-backed routes behave when their provider is
        # down: an explicit unavailable state, never a substituted result.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "source": SOURCE,
        "sourceUrl": SOURCE_URL,
        "filter": "shariah+uptrend+sma50",
        "priceMin": PRICE_MIN,
        "priceMax": PRICE_MAX,
        "topActiveLimit": TOP_ACTIVE_LIMIT,
        "count": len(rows),
        "fetchedAt": int(fetched_at * 1000),
        "rows": [row.to_dict() for row in rows],
    }
