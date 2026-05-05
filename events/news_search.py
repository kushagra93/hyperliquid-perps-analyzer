# events/news_search.py
# ─────────────────────────────────────────────────────────────────
# Free Google News RSS fetcher. Returns the freshest headline for
# a query. No API key. ~5 min cache to avoid hammering.
#
# Used by top_movers.py to attach a 1-line catalyst per ticker:
#
#   📊 BRENTOIL · 24h +2.8% · 30m -0.3%
#      "OPEC+ extends production cut into Q1" — Reuters · 2h ago
#      Day trend extending. heavy volume.
#
# Query is built per ticker:
#   commodity / fx → "gold price today" / "USD JPY news"
#   index          → "S&P 500 today"
#   stock          → "AAPL stock today"
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import quote

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300   # 5 min
_cache: dict[str, tuple[float, dict]] = {}

# Per-cluster query template
QUERIES = {
    "commodity":  "{full} price today",
    "index":      "{full} index today",
    "fx":         "{full} forex news",
    "uranium":    "{full} uranium news",
    "crypto-proxy": "{full} stock news",
    "semi":       "{full} stock today",
    "mega-tech":  "{full} stock today",
    "high-beta":  "{full} stock today",
    "healthcare": "{full} stock today",
    "consumer":   "{full} stock today",
    "china":      "{full} stock today",
    "asia":       "{full} stock today",
}

# Friendly names for queries (better hit rate vs raw ticker)
TICKER_QUERY_NAME = {
    "GOLD": "gold", "SILVER": "silver", "PLATINUM": "platinum",
    "PALLADIUM": "palladium", "COPPER": "copper",
    "BRENTOIL": "brent oil", "CL": "crude oil WTI", "NATGAS": "natural gas",
    "XLE": "energy stocks ETF",
    "SP500": "S&P 500", "XYZ100": "Nasdaq 100", "JP225": "Nikkei 225",
    "EWY": "Korea ETF", "EWJ": "Japan ETF", "KR200": "KOSPI 200",
    "EUR": "euro dollar", "JPY": "USD yen",
    "URNM": "uranium ETF", "USAR": "USA Rare Earth uranium",
    "CBRS": "Cibus uranium", "BABA": "Alibaba",
    "HYUNDAI": "Hyundai motor", "SMSN": "Samsung Electronics",
    "SKHX": "SK Hynix", "LLY": "Eli Lilly", "HIMS": "Hims Health",
    "COST": "Costco", "TSM": "TSMC", "MSTR": "MicroStrategy",
    "COIN": "Coinbase", "HOOD": "Robinhood", "CRCL": "Circle Internet",
    "PLTR": "Palantir", "RIVN": "Rivian", "DKNG": "DraftKings",
    "GME": "GameStop", "BIRD": "Allbirds", "CRWV": "CoreWeave",
    "NVDA": "Nvidia", "AMD": "AMD", "INTC": "Intel", "MU": "Micron",
    "SNDK": "SanDisk", "MRVL": "Marvell", "DRAM": "memory chip",
    "LITE": "Lumentum",
    "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet Google",
    "AMZN": "Amazon", "META": "Meta Platforms", "NFLX": "Netflix",
    "ORCL": "Oracle",
    "TSLA": "Tesla",
}


@dataclass
class Headline:
    title: str
    source: str
    when: str   # human-readable age e.g. "2h ago"


def _cache_get(key: str) -> dict | None:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL_SEC:
            return data
    return None


def _cache_put(key: str, data: dict) -> None:
    _cache[key] = (time.time(), data)


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")


def fetch_top_headline(query: str, *, hl: str = "en-IN",
                        gl: str = "IN") -> Headline | None:
    """
    Hit Google News RSS, return freshest headline as Headline dict.
    Free, no API key. ~250ms typical latency.
    """
    cached = _cache_get(query)
    if cached:
        return Headline(**cached) if cached else None

    url = (f"https://news.google.com/rss/search?q={quote(query)}"
           f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")
    try:
        r = subprocess.run(
            ["curl", "-s", "-A", "Mozilla/5.0", "--max-time", "8", url],
            capture_output=True, text=True, timeout=10,
        )
        xml = r.stdout
    except Exception as e:
        logger.warning(f"[news] curl failed for {query}: {e}")
        return None

    # Parse just the first <item>: title, source, pubDate
    item_m = re.search(r"<item>(.*?)</item>", xml, re.DOTALL)
    if not item_m:
        _cache_put(query, {})
        return None
    block = item_m.group(1)

    title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
    src_m   = re.search(r"<source[^>]*>(.*?)</source>", block, re.DOTALL)
    date_m  = re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)

    if not title_m:
        _cache_put(query, {})
        return None

    title_raw = _strip_html(title_m.group(1)).strip()
    source = _strip_html(src_m.group(1)).strip() if src_m else ""

    # Google News titles often look "Title - Source"; strip trailing source
    if " - " in title_raw and source and title_raw.endswith(" - " + source):
        title_raw = title_raw[: -(len(source) + 3)]

    # Age string (rough)
    when = "recent"
    if date_m:
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            pd = parsedate_to_datetime(date_m.group(1).strip())
            secs = (datetime.now(timezone.utc) - pd).total_seconds()
            if secs < 3600:
                when = f"{int(secs/60)}m ago"
            elif secs < 86400:
                when = f"{int(secs/3600)}h ago"
            else:
                when = f"{int(secs/86400)}d ago"
        except Exception:
            pass

    h = Headline(title=title_raw, source=source, when=when)
    _cache_put(query, {"title": h.title, "source": h.source, "when": h.when})
    return h


def reason_for_ticker(symbol: str, cluster: str, full_name: str | None = None) -> str | None:
    """
    Build a query for the ticker's cluster + symbol, fetch headline,
    return a 1-line catalyst string ≤ 100 chars or None.
    """
    name = TICKER_QUERY_NAME.get(symbol, full_name or symbol)
    template = QUERIES.get(cluster, "{full} stock today")
    query = template.format(full=name)
    h = fetch_top_headline(query)
    if not h or not h.title:
        return None
    title = h.title.strip()
    if len(title) > 90:
        title = title[:89] + "…"
    src = f" — {h.source}" if h.source else ""
    age = f" · {h.when}" if h.when else ""
    return f"\"{title}\"{src}{age}"


if __name__ == "__main__":
    # Smoke test
    for sym, cluster in [("GOLD", "commodity"), ("SP500", "index"),
                          ("JPY", "fx"), ("AAPL", "mega-tech"),
                          ("URNM", "uranium")]:
        print(f"\n{sym} ({cluster}):")
        print(f"  → {reason_for_ticker(sym, cluster)}")
