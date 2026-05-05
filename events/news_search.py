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


_QUESTION_RE = re.compile(
    r"^(why|is|are|should|can|will|does|do|how)\b", re.IGNORECASE)
_PROMO_RE = re.compile(r"by investing\.com|sponsored|paid partner", re.IGNORECASE)
_GENERIC_RE = re.compile(r"^(stock(s)?|the (top|best))\s", re.IGNORECASE)


def _score_headline(h: "Headline", age_sec: float | None) -> float:
    """Higher = better. Used to pick the most informative recent headline."""
    s = 100.0
    title = h.title or ""
    if _QUESTION_RE.match(title):
        s -= 60   # question / clickbait
    if _PROMO_RE.search(title) or _PROMO_RE.search(h.source or ""):
        s -= 40   # generic Investing.com filler
    if _GENERIC_RE.match(title):
        s -= 20
    # Age penalty
    if age_sec is not None:
        if age_sec > 7 * 86400: s -= 50
        elif age_sec > 3 * 86400: s -= 30
        elif age_sec > 86400:    s -= 10
        elif age_sec < 3600:     s += 15  # very fresh bonus
    # Specificity bonus: contains numbers
    if re.search(r"\d", title):
        s += 8
    # Source quality bonus for known-credible outlets
    src = (h.source or "").lower()
    if any(s2 in src for s2 in ("reuters", "bloomberg", "cnbc", "wsj",
                                   "ft.com", "barron", "marketwatch",
                                   "the economic times", "yahoo finance")):
        s += 12
    return s


def _parse_age(date_str: str) -> tuple[float, str]:
    """Returns (seconds_old, human_label)."""
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone
    try:
        pd = parsedate_to_datetime(date_str.strip())
        secs = (datetime.now(timezone.utc) - pd).total_seconds()
    except Exception:
        return (1e9, "recent")
    if secs < 3600:
        return secs, f"{int(secs/60)}m ago"
    if secs < 86400:
        return secs, f"{int(secs/3600)}h ago"
    return secs, f"{int(secs/86400)}d ago"


def fetch_top_headlines(query: str, *, n: int = 5,
                          hl: str = "en-IN", gl: str = "IN") -> list[Headline]:
    """Hit Google News RSS, return up to n parsed headlines."""
    cache_key = f"list::{query}::{n}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return [Headline(**h) for h in cached.get("items", [])]

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
        return []

    items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    out: list[Headline] = []
    for block in items[: n * 3]:  # oversample then filter
        title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        src_m   = re.search(r"<source[^>]*>(.*?)</source>", block, re.DOTALL)
        date_m  = re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
        if not title_m: continue
        title = _strip_html(title_m.group(1)).strip()
        source = _strip_html(src_m.group(1)).strip() if src_m else ""
        if " - " in title and source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)]
        when = "recent"; age_sec = None
        if date_m:
            age_sec, when = _parse_age(date_m.group(1))
        out.append(Headline(title=title, source=source, when=when))
        # store age for scoring later
        out[-1].__dict__["_age_sec"] = age_sec
        if len(out) >= n:
            break

    _cache_put(cache_key, {"items": [
        {"title": h.title, "source": h.source, "when": h.when} for h in out
    ]})
    return out


# ── Reason synthesizer (keyword extraction → answer phrase) ─────

REASON_KEYWORDS = [
    # (regex, label)
    (re.compile(r"\btariff", re.I),                 "tariff news"),
    (re.compile(r"\bbeats?\b|\bbeat estimates", re.I), "earnings beat"),
    (re.compile(r"\bmiss(es|ed)?\b", re.I),         "earnings miss"),
    (re.compile(r"\bearnings\b|\breports?\b", re.I), "earnings reaction"),
    (re.compile(r"\bguidance\b", re.I),             "guidance update"),
    (re.compile(r"\bdowngrad", re.I),               "analyst downgrade"),
    (re.compile(r"\bupgrad", re.I),                 "analyst upgrade"),
    (re.compile(r"\bprice target\b", re.I),         "price target change"),
    (re.compile(r"\bbuyback\b|\brepurchase", re.I), "buyback news"),
    (re.compile(r"\bdividend\b", re.I),             "dividend update"),
    (re.compile(r"\bmerger\b|\bacquisition\b|\bacquire", re.I), "M&A news"),
    (re.compile(r"\btakeover\b", re.I),             "takeover bid"),
    (re.compile(r"\blawsuit\b|\bsued?\b|\bsec\b", re.I), "regulatory/legal"),
    (re.compile(r"\binvestigat", re.I),             "investigation"),
    (re.compile(r"\bbann?ed\b|\bban\b", re.I),      "ban / restriction"),
    (re.compile(r"\brecall\b", re.I),               "product recall"),
    (re.compile(r"\bfomc\b|\bfederal reserve\b|\bfed\b", re.I), "Fed decision"),
    (re.compile(r"\binterest rate", re.I),          "rate news"),
    (re.compile(r"\bcpi\b|\binflation\b", re.I),    "CPI / inflation print"),
    (re.compile(r"\bunemploy", re.I),               "jobs print"),
    (re.compile(r"\bnfp\b|\bnon-?farm", re.I),      "NFP print"),
    (re.compile(r"\bgdp\b", re.I),                  "GDP print"),
    (re.compile(r"\bchina\b|\btrade war", re.I),    "China / trade tension"),
    (re.compile(r"\biran\b|\bmiddle east\b", re.I), "Middle-East tension"),
    (re.compile(r"\bopec\b", re.I),                 "OPEC supply news"),
    (re.compile(r"\bsupply chain\b", re.I),         "supply-chain disruption"),
    (re.compile(r"\bai\b|artificial intelli", re.I), "AI narrative"),
    (re.compile(r"\bchip(s)?\b|semiconductor", re.I), "chip / semi news"),
    (re.compile(r"\bbitcoin\b|\bbtc\b|\bcrypto", re.I), "crypto move"),
    (re.compile(r"\bgold\b", re.I),                 "gold flow"),
    (re.compile(r"\boil price|\bcrude\b", re.I),    "oil price move"),
    (re.compile(r"\bdollar\b|\bdxy\b|\busd\b", re.I), "USD move"),
    (re.compile(r"\bcourt\b|\blegal\b", re.I),      "court ruling"),
    (re.compile(r"\bipo\b", re.I),                  "IPO news"),
    (re.compile(r"\blayoff\b|\bcut(ting)? jobs", re.I), "layoffs"),
    (re.compile(r"\bpartner(ship)?\b|\bdeal\b", re.I), "partnership / deal"),
    (re.compile(r"\bfda\b|\bdrug\b|\bclinical", re.I), "FDA / clinical news"),
    (re.compile(r"\bwins?\b|\baward", re.I),         "contract win"),
    (re.compile(r"\bsurge|\bjump|\brall(y|ied|ies)|\bspike", re.I),
                                                     "rally"),
    (re.compile(r"\bplunge|\bcrash|\btumble|\bsink|\bslump", re.I), "selloff"),
]


def synthesize_reason(headlines: list[Headline]) -> str | None:
    """
    Extract concrete reason tags from the top N headlines and merge
    into one short answer (not a question). Returns ≤ 60 chars or
    None if nothing concrete found.
    """
    if not headlines:
        return None
    blob = " ".join(h.title for h in headlines if h.title)
    found: list[str] = []
    for rx, label in REASON_KEYWORDS:
        if rx.search(blob) and label not in found:
            found.append(label)
    if not found:
        return None
    # Top 2 reasons, deduped
    if len(found) == 1:
        return found[0]
    return f"{found[0]} + {found[1]}"


def _question_to_statement(title: str) -> str:
    """Convert clickbait questions into best-effort statements."""
    t = title.strip()
    # Drop trailing "?"
    t = t.rstrip("?")
    # "Why is X surging today" → "X surging today"
    t = re.sub(r"^why is\s+", "", t, flags=re.I)
    t = re.sub(r"^why are\s+", "", t, flags=re.I)
    t = re.sub(r"^why\s+", "", t, flags=re.I)
    t = re.sub(r"^is\s+(.{1,40}?)\s+about to\s+", r"\1 may ", t, flags=re.I)
    t = re.sub(r"^should you\s+", "Consider ", t, flags=re.I)
    t = re.sub(r"^can\s+", "", t, flags=re.I)
    return t.strip().capitalize()


def reason_for_ticker(symbol: str, cluster: str, full_name: str | None = None) -> dict | None:
    """
    Returns a dict with:
      reason   — synthesized 'answer' phrase (e.g. 'tariff news + analyst downgrade')
      headline — best supporting headline (questions converted to statements)
      source   — source attribution
      age      — human-readable age
    """
    name = TICKER_QUERY_NAME.get(symbol, full_name or symbol)
    template = QUERIES.get(cluster, "{full} stock today")
    query = template.format(full=name)
    headlines = fetch_top_headlines(query, n=5)
    if not headlines:
        return None

    # Prefer non-question, non-promo headlines: filter then score
    filtered = [h for h in headlines
                 if not _QUESTION_RE.match(h.title or "")
                 and not _PROMO_RE.search(h.title or "")
                 and not _PROMO_RE.search(h.source or "")]
    pool = filtered if filtered else headlines
    scored = []
    for h in pool:
        age_sec = h.__dict__.get("_age_sec")
        scored.append((_score_headline(h, age_sec), h))
    scored.sort(key=lambda x: -x[0])
    best = scored[0][1]

    reason = synthesize_reason(headlines)

    title = best.title.strip()
    # Strip "By Investing.com" / similar promo tail
    title = re.sub(r"\s*By\s+Investing\.com\s*$", "", title, flags=re.I)
    # Convert any residual question into statement
    if title.endswith("?") or _QUESTION_RE.match(title):
        title = _question_to_statement(title)
    if len(title) > 80:
        title = title[:79] + "…"

    return {
        "reason": reason,
        "headline": title,
        "source": best.source,
        "age": best.when,
    }


if __name__ == "__main__":
    # Smoke test
    for sym, cluster in [("GOLD", "commodity"), ("SP500", "index"),
                          ("JPY", "fx"), ("AAPL", "mega-tech"),
                          ("URNM", "uranium")]:
        print(f"\n{sym} ({cluster}):")
        print(f"  → {reason_for_ticker(sym, cluster)}")
