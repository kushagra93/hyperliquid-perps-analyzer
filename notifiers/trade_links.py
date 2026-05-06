# notifiers/trade_links.py
# ─────────────────────────────────────────────────────────────────
# 1-tap trade / chart links so every PN is actionable even when
# the news quality is light.
#
# Returns a single short HTML link suitable for the body line.
# Prefers Hyperliquid for tickers we trade there (US perps),
# TradingView for stand-alone charts, and Yahoo Finance for
# indices / FX / commodities not on HL.
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

from urllib.parse import quote


# TradingView chart slugs per ticker
TV_SLUG = {
    # Stocks
    "AAPL":  "NASDAQ-AAPL", "MSFT": "NASDAQ-MSFT", "GOOGL": "NASDAQ-GOOGL",
    "AMZN":  "NASDAQ-AMZN", "META": "NASDAQ-META", "NFLX":  "NASDAQ-NFLX",
    "NVDA":  "NASDAQ-NVDA", "AMD":  "NASDAQ-AMD",  "INTC":  "NASDAQ-INTC",
    "TSLA":  "NASDAQ-TSLA", "PLTR": "NASDAQ-PLTR", "MU":    "NASDAQ-MU",
    "MRVL":  "NASDAQ-MRVL", "TSM":  "NYSE-TSM",    "BABA":  "NYSE-BABA",
    "MSTR":  "NASDAQ-MSTR", "COIN": "NASDAQ-COIN", "HOOD":  "NASDAQ-HOOD",
    "CRCL":  "NYSE-CRCL",   "RIVN": "NASDAQ-RIVN", "DKNG":  "NASDAQ-DKNG",
    "GME":   "NYSE-GME",    "BIRD": "NASDAQ-BIRD", "CRWV":  "NASDAQ-CRWV",
    "LITE":  "NASDAQ-LITE", "SNDK": "NASDAQ-SNDK", "ORCL":  "NYSE-ORCL",
    "LLY":   "NYSE-LLY",    "HIMS": "NYSE-HIMS",   "COST":  "NASDAQ-COST",
    # Indices & ETFs
    "SP500":   "SP-SPX",
    "XYZ100":  "NASDAQ-NDX",
    "JP225":   "OANDA-JP225USD",
    "KR200":   "KRX-KOSPI200",
    "EWJ":     "AMEX-EWJ",
    "EWY":     "AMEX-EWY",
    "XLE":     "AMEX-XLE",
    # Commodities (TV continuous)
    "GOLD":      "TVC-GOLD",
    "SILVER":    "TVC-SILVER",
    "PLATINUM":  "TVC-PLATINUM",
    "PALLADIUM": "TVC-PALLADIUM",
    "COPPER":    "COMEX-HG1!",
    "BRENTOIL":  "TVC-UKOIL",
    "CL":        "TVC-USOIL",
    "NATGAS":    "TVC-NATGASUSD",
    # Uranium plays
    "URNM": "AMEX-URNM",
    "USAR": "NASDAQ-USAR",
    # FX
    "EUR": "FX-EURUSD",
    "JPY": "FX-USDJPY",
}


def hl_trade_link(sym: str) -> str | None:
    """Hyperliquid trade page for our perps."""
    return f"https://app.hyperliquid.xyz/trade/xyz%3A{sym}"


def tv_chart_link(sym: str) -> str | None:
    slug = TV_SLUG.get(sym)
    if not slug:
        return f"https://www.tradingview.com/symbols/{quote(sym)}/"
    return f"https://www.tradingview.com/symbols/{slug}/"


def trade_link_html(sym: str, *, show_chart: bool = True,
                     show_trade: bool = True) -> str:
    """
    Returns a single <a>-formatted line e.g.
      🔗 <a href="...">Chart</a> · <a href="...">Trade</a>
    """
    parts = []
    if show_chart:
        ch = tv_chart_link(sym)
        if ch:
            parts.append(f'<a href="{ch}">Chart</a>')
    if show_trade:
        tr = hl_trade_link(sym)
        if tr:
            parts.append(f'<a href="{tr}">Trade</a>')
    if not parts:
        return ""
    return "🔗 " + " · ".join(parts)
