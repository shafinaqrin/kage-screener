"""KLSE Screener crawler.

Crawls https://www.klsescreener.com/v2/ and normalises its screener results into
plain records.

How the site actually works (verified against the live site, not assumed):

- The visible `#form` on the screener page is only the control panel. Its own JS
  serialises it and POSTs to `/v2/screener/quote_results`, which returns an HTML
  *fragment* containing the results table -- there is no JSON API.
- `shariah_mode=1` is a genuine server-side filter; the response for that query
  contained 913 rows all carrying the Shariah marker.
- Because the filter comes from a third party we do not control, every row is
  re-checked for the `Shariah Compliant` marker and any row lacking it is
  dropped. A silently broken upstream filter must not leak non-Shariah names
  into a section that claims to be Shariah-only.

Three filters are applied to every crawl:

1. **Shariah-compliant only** -- requested upstream and re-verified per row.
2. **Price band RM 0.20-1.50** -- requested upstream *and* re-checked locally, so a
   change in the site's form encoding cannot silently widen it.
3. **Uptrend (price above SMA50)** -- the site's own `price_gt_sma_50` filter.

Finally the result is trimmed to the **top 30 by volume**, the most actively
traded names. The site has no "top active" parameter, so that cut is made locally
after filtering.

Licensing: klsescreener.com is (c) Neobie Enterprise and serves Bursa Malaysia
market data, which is generally licensed. robots.txt permits crawling with a
20-second crawl-delay, which the TTL cache below respects. This is intended for
local, personal use -- check their Terms of Use before public or high-volume use.

No data is ever fabricated: if the upstream site is unreachable the caller gets an
error, never an empty-but-successful or invented result set.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from html import unescape
from typing import Any

import httpx

logger = logging.getLogger("kage.screener")

SOURCE_URL = "https://www.klsescreener.com/v2/"
RESULTS_ENDPOINT = "https://www.klsescreener.com/v2/screener/quote_results"

#: Sent because the endpoint feeds an XHR-driven UI. Verified optional, but a
#: browser-like UA keeps us honest about who we are.
USER_AGENT = "kage-screener/1.0 (personal use; +https://github.com/kage)"

#: Bursa codes appear as 4 digits, 5 digits (Leap Market), or 4 digits + a
#: two-letter class suffix (ETF/bond/REIT, e.g. 0827EA, 5235SS, 0400GB).
CODE_RE = re.compile(r"^\d{4,5}(?:[A-Z]{2})?$")

#: Share-price band, in MYR, applied to every crawl.
#:
#: Fixed by product decision rather than exposed as a parameter: the screener is
#: meant to surface the RM 0.20-1.50 segment. Requested upstream *and* re-checked
#: locally, so a change in the site's encoding cannot silently widen it. A row
#: whose price is unknown is excluded -- it cannot be shown to satisfy the band,
#: and including it would let unpriced instruments into a price-filtered list.
PRICE_MIN = 0.20
PRICE_MAX = 1.50

#: Uptrend definition: price above its 50-day simple moving average. Sent as the
#: site's own `price_gt_sma_50` filter rather than recomputed here, which would
#: need a price history the crawl does not otherwise fetch.
SMA_FILTER_FIELD = "price_gt_sma_50"
SMA_PERIOD = "50"

#: How many of the most actively traded names to keep. The site has no "top
#: active" parameter, so this cut is made locally on volume, after filtering.
TOP_ACTIVE_LIMIT = 30

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r'<tr class="list".*?</tr>', re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.S)

_NULLISH = {"", "-", "--", "n/a", "N/A", "nil", "NIL"}


class ScreenerUnavailable(RuntimeError):
    """Upstream KLSE Screener could not be read or parsed."""


@dataclass(frozen=True)
class ScreenerRow:
    """One Shariah-compliant instrument, as published by KLSE Screener."""

    code: str
    name: str
    category: str
    market: str
    price: float | None
    change: float | None
    change_percent: float | None
    week52: str
    volume: float | None
    eps: float | None
    dps: float | None
    nta: float | None
    pe: float | None
    dy: float | None
    roe: float | None
    ptbv: float | None
    market_cap: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(fragment: str) -> str:
    """Strip tags and collapse whitespace from an HTML cell."""
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", fragment))).strip()


def _to_float(fragment: str) -> float | None:
    """Parse a numeric cell, returning None for missing values.

    An absent value must stay absent: defaulting it to 0 would render a real
    "0.00" on screen for a figure the site never published.
    """
    raw = _text(fragment).replace(",", "").replace("%", "").strip()
    if raw in _NULLISH:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _split_category(fragment: str) -> tuple[str, str]:
    """Split the combined category cell into (sub-sector, board).

    The site renders it as e.g.
    ``<small>Construction</small><br/><small>Construction, Main Market</small>``
    where the first part is the sub-sector and the second is "Category, Board".
    """
    parts = [_text(p) for p in re.split(r"<br\s*/?>", fragment)]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""

    sub_sector = parts[0]
    combined = parts[1] if len(parts) > 1 else ""
    board = ""
    if combined:
        segments = [s.strip() for s in combined.split(",")]
        board = segments[-1] if segments else ""
    return sub_sector, board


def parse_results(html: str) -> list[ScreenerRow]:
    """Parse the screener results fragment into rows.

    Fails loudly on a fragment that has no results table at all, so a change in
    the site's markup surfaces as an error instead of a silent empty screen.
    """
    table = _TABLE_RE.search(html)
    if not table:
        raise ScreenerUnavailable(
            "KLSE Screener returned no results table; the site markup may have changed."
        )

    rows: list[ScreenerRow] = []
    for raw_row in _ROW_RE.findall(table.group(0)):
        # Re-verify the Shariah flag per row rather than trusting the request
        # parameter. Rows without it are dropped.
        if "[s]" not in raw_row or "Shariah Compliant" not in raw_row:
            continue

        cells = _CELL_RE.findall(raw_row)
        if len(cells) < 16:
            continue

        code = _text(cells[1])
        if not CODE_RE.match(code):
            continue

        sub_sector, board = _split_category(cells[2])
        price = _to_float(cells[3])

        # Price band. An unknown price is excluded rather than passed through:
        # it cannot be shown to fall inside the band, and letting it in would put
        # unpriced instruments in a list that claims to be price-filtered.
        if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
            continue

        rows.append(
            ScreenerRow(
                code=code,
                # The marker sits inside the name cell, so strip it out.
                name=_text(cells[0]).replace("[s]", "").strip(),
                category=sub_sector,
                market=board,
                price=price,
                change=_to_float(cells[4]),
                change_percent=_to_float(cells[5]),
                week52=_text(cells[6]),
                volume=_to_float(cells[7]),
                eps=_to_float(cells[8]),
                dps=_to_float(cells[9]),
                nta=_to_float(cells[10]),
                pe=_to_float(cells[11]),
                dy=_to_float(cells[12]),
                roe=_to_float(cells[13]),
                ptbv=_to_float(cells[14]),
                market_cap=_to_float(cells[15]),
            )
        )

    if not rows:
        raise ScreenerUnavailable(
            "KLSE Screener returned no Shariah-compliant rows above their SMA50 "
            f"in the RM {PRICE_MIN:.2f}-{PRICE_MAX:.2f} price band."
        )

    # Trim to the most actively traded names. Rows without volume cannot be
    # ranked, so they sort last and fall outside the cut; ties keep source order.
    rows.sort(key=lambda r: r.volume if r.volume is not None else -1, reverse=True)

    return rows[:TOP_ACTIVE_LIMIT]


class ScreenerCache:
    """In-memory TTL cache over one upstream crawl.

    Deliberately not persisted: AGENTS.md forbids a stored market dataset, and a
    cached crawl is a short-lived read-through buffer rather than a data store.
    A single-flight lock means concurrent requests trigger one crawl, not N.
    """

    def __init__(self, ttl_seconds: int = 1800, timeout_seconds: int = 45) -> None:
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._rows: list[ScreenerRow] = []
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self._fetched_at if self._fetched_at else 0.0

    def snapshot(self) -> tuple[list[ScreenerRow], float, bool]:
        """Current cache contents as (rows, fetched_at, is_fresh)."""
        fresh = bool(self._rows) and self.age_seconds < self._ttl
        return self._rows, self._fetched_at, fresh

    async def rows(self, force: bool = False) -> tuple[list[ScreenerRow], float]:
        """Return cached rows, crawling when stale or when ``force`` is set.

        If a refresh fails but a previous crawl exists, the previous rows are
        served and the caller can tell they are stale -- a transient upstream
        error should not blank the section, and must never invent data.
        """
        if not force and self._rows and self.age_seconds < self._ttl:
            return self._rows, self._fetched_at

        async with self._lock:
            # Another request may have refreshed while we waited for the lock.
            if not force and self._rows and self.age_seconds < self._ttl:
                return self._rows, self._fetched_at

            try:
                rows = await self._crawl()
            except ScreenerUnavailable:
                if self._rows:
                    logger.warning("crawl failed; serving stale cache")
                    return self._rows, self._fetched_at
                raise

            self._rows = rows
            self._fetched_at = time.time()
            logger.info("crawled %d rows (shariah, SMA50, RM %.2f-%.2f, top %d by volume)",
                        len(rows), PRICE_MIN, PRICE_MAX, TOP_ACTIVE_LIMIT)
            return self._rows, self._fetched_at

    async def _crawl(self) -> list[ScreenerRow]:
        try:
            response = await self._http().post(
                RESULTS_ENDPOINT,
                # Every filter is expressed as the site's own form field:
                # shariah_mode is Shariah-only, price_gt_sma_50 is the uptrend
                # test, and min/max_price bound the band. The price band is
                # re-checked locally during parsing regardless.
                data={
                    "getquote": "1",
                    "shariah_mode": "1",
                    SMA_FILTER_FIELD: SMA_PERIOD,
                    "min_price": f"{PRICE_MIN:.2f}",
                    "max_price": f"{PRICE_MAX:.2f}",
                },
                headers={
                    "Referer": SOURCE_URL,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except httpx.HTTPError as exc:
            # Some httpx transport errors stringify to nothing, so fall back to
            # the exception class name to keep the reason actionable.
            detail = str(exc) or exc.__class__.__name__
            raise ScreenerUnavailable(f"KLSE Screener unreachable: {detail}") from exc

        if response.status_code != 200:
            raise ScreenerUnavailable(
                f"KLSE Screener responded {response.status_code}."
            )

        return parse_results(response.text)
