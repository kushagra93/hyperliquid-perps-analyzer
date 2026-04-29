# notifiers/telegram.py
# ─────────────────────────────────────────────────────────────────
# Telegram notifier. Posts formatted alert messages to a channel or
# chat using the Bot API. Designed to be called from TickerWorker
# after log_alert() succeeds.
#
# Env vars:
#   TELEGRAM_BOT_TOKEN          Bot API token (from @BotFather)
#   TELEGRAM_CHAT_ID            Channel id (-100...) or DM id
#   TELEGRAM_STRONG_ONLY        "true" → only C1/C2 + confidence>=medium
#   TELEGRAM_MIN_SCORE          integer 0-100; skip alerts below this
#
# Usage (wired in ticker_worker after log_alert):
#   from notifiers.telegram import send_alert_if_enabled
#   send_alert_if_enabled(alert_payload)
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"

_CONF_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🟠"}
_DRIVER_PLAIN = {
    "news": "news catalyst",
    "oi_flow": "fresh money flowing in",
    "volume": "volume spike",
    "technical": "technical move",
    "unknown": "unclear",
}


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _is_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


_PN_FILTER_CACHE: dict | None = None
_PN_DAILY_STATE: dict = {"date": None, "best_score": -1}


def _load_pn_filter_cfg() -> dict | None:
    """Load config/pn_filter.json once; cache."""
    global _PN_FILTER_CACHE
    if _PN_FILTER_CACHE is not None:
        return _PN_FILTER_CACHE
    try:
        import os, json
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "config" / "pn_filter.json"
        if not path.exists():
            _PN_FILTER_CACHE = {}
            return None
        _PN_FILTER_CACHE = json.loads(path.read_text())
        return _PN_FILTER_CACHE
    except Exception:
        _PN_FILTER_CACHE = {}
        return None


def _alert_passes_pn_filter(alert: dict) -> tuple[bool, str]:
    """
    Apply the persisted PN filter (from analysis/pn_optimizer.py --apply)
    plus the once-per-UTC-day cap. Returns (eligible, reason_if_not).
    """
    from datetime import datetime, timezone
    cfg = _load_pn_filter_cfg()
    if not cfg or not cfg.get("filter"):
        return True, ""  # no filter configured → don't gate

    filter_names = cfg["filter"]
    pt = alert.get("price_trigger") or {}
    price = float(pt.get("current_price") or 0)
    tech = alert.get("technical_outlook") or {}
    atr = float(tech.get("atr") or 0)
    score = int(alert.get("score") or 0)

    # Re-derive the filter signals at alert time
    when_iso = (pt.get("triggered_at") or
                datetime.now(timezone.utc).astimezone().isoformat())
    try:
        hour = datetime.fromisoformat(str(when_iso)).hour
        minute = datetime.fromisoformat(str(when_iso)).minute
    except Exception:
        hour, minute = 12, 0

    cash_hours = (hour >= 19) or (hour < 1) or (hour == 1 and minute <= 30)
    atr_pct = (atr / price * 100) if price > 0 else 0.0

    for f in filter_names:
        if f == "cash_hours" and not cash_hours:
            return False, "outside US cash hours"
        if f.startswith("atr_min_"):
            min_p = float(f.split("_")[-1])
            if atr_pct < min_p:
                return False, f"ATR {atr_pct:.2f}% < {min_p}%"
        if f.startswith("score_"):
            min_s = int(f.split("_")[-1])
            if score < min_s:
                return False, f"score {score} < {min_s}"
        # 'sustained' is post-hoc; can't evaluate at alert time → permissive
        if f == "sustained":
            continue

    # Daily cap: keep only highest-scoring alert per UTC day
    today = datetime.now(timezone.utc).date().isoformat()
    if _PN_DAILY_STATE["date"] != today:
        _PN_DAILY_STATE["date"] = today
        _PN_DAILY_STATE["best_score"] = -1
    if score <= _PN_DAILY_STATE["best_score"]:
        return False, (f"already PNed score {_PN_DAILY_STATE['best_score']} today "
                        f"(this is {score})")
    _PN_DAILY_STATE["best_score"] = score
    return True, ""


def _should_send(alert: dict) -> tuple[bool, str]:
    """Filter logic. Returns (should_send, reason_if_not)."""
    condition = alert.get("condition") or {}
    causality = alert.get("causality") or {}
    score = int(alert.get("score") or 0)

    min_score = _env_int("TELEGRAM_MIN_SCORE", 0)
    if score < min_score:
        return False, f"score {score} < min {min_score}"

    if _env_bool("TELEGRAM_STRONG_ONLY", False):
        cid = condition.get("condition_id", "")
        conf = (causality.get("confidence") or "").lower()
        if cid not in ("C1", "C2"):
            return False, f"condition {cid} not C1/C2"
        if conf not in ("high", "medium"):
            return False, f"confidence {conf} too low"

    # PN-tier filter (only gates when TELEGRAM_PN_TIER_ONLY=true)
    if _env_bool("TELEGRAM_PN_TIER_ONLY", False):
        ok, reason = _alert_passes_pn_filter(alert)
        if not ok:
            return False, f"PN filter: {reason}"

    return True, ""


def format_alert(alert: dict) -> str:
    """
    Build the HTML-formatted message body.

    Expected alert dict shape (matches what ticker_worker assembles):
      {
        "symbol": "NVDA",
        "full_name": "Nvidia",
        "price_trigger": {current_price, price_change_pct, trigger_source, ...},
        "oi_report": {current_oi, oi_change_pct, funding_rate, interpretation, ...},
        "condition": {condition_id, label, description, ...},
        "causality": {verdict, confidence, primary_driver, flags, reasoning},
        "news_report": {summary, has_news, articles},
        "score": int (0-100),  # optional
        "stars": int (1-5),    # optional
      }
    """
    sym = alert.get("symbol", "?")
    full = alert.get("full_name", sym)
    pt = alert.get("price_trigger") or {}
    oi = alert.get("oi_report") or {}
    cond = alert.get("condition") or {}
    caus = alert.get("causality") or {}
    news = alert.get("news_report") or {}

    price = pt.get("current_price", 0.0)
    price_pct = pt.get("price_change_pct", 0.0)
    oi_pct = cond.get("oi_change_pct", 0.0)

    conf = (caus.get("confidence") or "").lower()
    driver = (caus.get("primary_driver") or "").lower()
    flags = caus.get("flags") or []

    score = alert.get("score")
    stars = alert.get("stars")
    header = ""
    if stars:
        header = f"<b>{'⭐' * int(stars)}{'☆' * (5 - int(stars))}</b>"
        if score is not None:
            header += f"  <i>({score}/100)</i>"
        header += "\n"

    flag_line = f"\n<i>⚠️ flags: {', '.join(flags)}</i>" if flags else ""

    event_ctx = alert.get("event_context") or {}
    events_block = ""
    if event_ctx.get("enabled") and event_ctx.get("rendered"):
        events_block = f"\n<b>🗓️ Earnings & events</b>\n{event_ctx['rendered']}\n"

    tech_ctx = alert.get("technical_outlook") or {}
    tech_block = ""
    if tech_ctx.get("enabled") and tech_ctx.get("rendered"):
        tech_block = f"\n<b>📊 Technical outlook</b>\n{tech_ctx['rendered']}\n"

    return (
        f"{header}"
        f"🟢 <b>{sym} ({full})</b> — {cond.get('label', 'Alert')}\n\n"
        f"<b>📍 Price</b> ${price:.2f} ({price_pct:+.2f}%)  •  <b>OI</b> {oi_pct:+.2f}%\n"
        f"<b>💡 Signal</b> {_CONF_EMOJI.get(conf, '⚪')} {conf.upper() or '—'}  •  "
        f"Driver: <b>{_DRIVER_PLAIN.get(driver, driver or '—')}</b>\n\n"
        f"<b>📰 News</b>\n{(news.get('summary') or '—')[:300]}\n\n"
        f"<b>🧠 Verdict</b>\n{caus.get('verdict', '—')}\n"
        f"{tech_block}"
        f"{events_block}\n"
        f"<b>🚦 Playbook</b>\n"
        f"Entry on small pullback • Stop –1.5% • TP1 +2%, trail rest • Max 2–3% capital{flag_line}\n\n"
        f"<code>#{sym} {cond.get('condition_id', '?')}</code>"
    )


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Raw send. Returns True on ok=true from Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.debug("[Telegram] not configured; skipping send")
        return False
    try:
        r = requests.post(
            _API.format(token=token, method="sendMessage"),
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": "true",
            },
            timeout=10,
            verify=False,
        )
        r.raise_for_status()
        ok = r.json().get("ok", False)
        if not ok:
            logger.warning(f"[Telegram] API returned ok=false: {r.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"[Telegram] send failed: {e}")
        return False


def send_alert_if_enabled(alert: dict) -> bool:
    """Format + send if token/chat configured and filters pass."""
    if not _is_enabled():
        return False
    should, reason = _should_send(alert)
    if not should:
        logger.info(f"[Telegram/{alert.get('symbol')}] filtered out: {reason}")
        return False
    text = format_alert(alert)
    ok = send_message(text)
    if ok:
        logger.info(f"[Telegram/{alert.get('symbol')}] alert sent")
    return ok


# ── Convenience test entrypoint ──────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    sample = {
        "symbol": "NVDA", "full_name": "Nvidia",
        "price_trigger": {"current_price": 201.0, "price_change_pct": 2.8, "trigger_source": "price"},
        "oi_report": {"oi_change_pct": 6.7, "funding_rate": 0.0006, "interpretation": "OI up +6.7%."},
        "condition": {"condition_id": "C1", "label": "Strong bull",
                       "description": "price up + OI up", "oi_change_pct": 6.7},
        "causality": {"verdict": "New longs entering with conviction.", "confidence": "high",
                       "primary_driver": "oi_flow", "flags": [], "reasoning": "—"},
        "news_report": {"summary": "UBS upgraded NVDA ahead of earnings.", "has_news": True},
        "score": 85, "stars": 5,
    }
    if "--dry-run" in sys.argv:
        print(format_alert(sample))
    else:
        ok = send_alert_if_enabled(sample)
        print("sent" if ok else "skipped/failed")
