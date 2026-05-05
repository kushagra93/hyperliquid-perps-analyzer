# events/external_indices.py
# ─────────────────────────────────────────────────────────────────
# Free Yahoo Finance fetcher for indices not listed on Hyperliquid
# (Nifty 50 ^NSEI, etc). Also pulls S&P 500 ^GSPC as a check-source
# vs the xyz:SP500 perp.
#
# No API key. ~250ms typical latency. Cached 60s.
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import subprocess
import time
from urllib.parse import quote

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 60
_cache: dict[str, tuple[float, dict]] = {}


# Symbol → human-readable name
INDEX_NAMES = {
    "^NSEI":   "Nifty 50",
    "^BSESN":  "Sensex",
    "^GSPC":   "S&P 500",
    "^IXIC":   "Nasdaq Comp",
    "^DJI":    "Dow Jones",
    "^VIX":    "VIX",
    "^N225":   "Nikkei 225",
    "^HSI":    "Hang Seng",
    "DX-Y.NYB":"DXY (USD index)",
}

# Default panel for the digest
DEFAULT_PANEL = ["^NSEI", "^GSPC", "^IXIC", "^VIX"]


def _yahoo_chart(symbol: str, range_: str = "2d",
                  interval: str = "15m") -> dict | None:
    cache_key = f"{symbol}:{range_}:{interval}"
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if time.time() - ts < CACHE_TTL_SEC:
            return data

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
           f"?range={range_}&interval={interval}&includePrePost=false")
    try:
        r = subprocess.run(
            ["curl", "-s", "-A", "Mozilla/5.0", "--max-time", "8", url],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(r.stdout)
    except Exception as e:
        logger.warning(f"[yf] fetch failed {symbol}: {e}")
        return None

    chart = (data or {}).get("chart") or {}
    res = (chart.get("result") or [None])[0]
    if not res:
        return None

    quotes = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    timestamps = res.get("timestamp") or []
    out = {
        "symbol": symbol,
        "name": INDEX_NAMES.get(symbol, symbol),
        "timestamps": timestamps,
        "closes": [c for c in closes if c is not None],
        "raw_closes": closes,
        "currency": (res.get("meta") or {}).get("currency", ""),
    }
    _cache[cache_key] = (time.time(), out)
    return out


def index_moves(symbol: str) -> dict | None:
    """
    Returns {symbol, name, price, move_24h_pct, move_30m_pct, asof}.
    None when fetch fails.
    """
    chart = _yahoo_chart(symbol, range_="2d", interval="15m")
    if not chart or not chart["closes"]:
        return None
    cur = chart["closes"][-1]

    # 30m back: 2 bars on a 15m series
    if len(chart["closes"]) >= 3:
        ref_30 = chart["closes"][-3]
    else:
        ref_30 = chart["closes"][0]
    move_30 = (cur - ref_30) / ref_30 * 100 if ref_30 else 0

    # 24h back: 96 bars
    if len(chart["closes"]) >= 97:
        ref_24 = chart["closes"][-97]
    else:
        ref_24 = chart["closes"][0]
    move_24 = (cur - ref_24) / ref_24 * 100 if ref_24 else 0

    return {
        "symbol": symbol,
        "name": chart["name"],
        "price": round(cur, 2),
        "move_24h_pct": round(move_24, 2),
        "move_30m_pct": round(move_30, 2),
        "asof": time.time(),
    }


def macro_panel(symbols: list[str] | None = None) -> list[dict]:
    """Fetch the panel for the digest. Returns list of move dicts."""
    syms = symbols or DEFAULT_PANEL
    out = []
    for s in syms:
        m = index_moves(s)
        if m:
            out.append(m)
    return out


def render_panel_html(panel: list[dict]) -> str:
    if not panel:
        return ""
    lines = ["<b>🌐 Macro indices</b>"]
    for p in panel:
        m24 = p["move_24h_pct"]
        m30 = p["move_30m_pct"]
        emoji = "🟢" if m24 > 0.2 else "🔴" if m24 < -0.2 else "⚪"
        lines.append(
            f"  {emoji} <b>{p['name']:11s}</b> "
            f"24h <b>{m24:+.2f}%</b>  ·  30m {m30:+.2f}%  "
            f"<i>({p['price']:,})</i>"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    panel = macro_panel()
    for p in panel:
        print(p)
    print()
    print(render_panel_html(panel))
