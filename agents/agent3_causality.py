# agents/agent3_causality.py
# ─────────────────────────────────────────────────────────────────
# Updated for multi-ticker:
# - Asset name comes from price_trigger["asset"] not global ASSET
# - Volume section added to prompt so LLM reasons about it explicitly
# - primary_driver now includes "volume" as possible value
# - Backward compatible with existing single-ticker main.py
# ─────────────────────────────────────────────────────────────────

import os
import json
import logging
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from config.settings import LLM_PROVIDER, OPENROUTER_API_KEY, OPENROUTER_MODEL, ASSET

logger = logging.getLogger(__name__)


def _build_volume_section(price_trigger: dict) -> str:
    """Build volume context section for the LLM prompt."""
    volume_trigger = price_trigger.get("volume_trigger")
    if not volume_trigger:
        return "## Volume\nNo volume threshold breach this tick."

    volume_change_pct = volume_trigger.get("volume_change_pct", 0.0)
    volume_threshold_pct = volume_trigger.get("volume_threshold_pct")
    window_minutes = volume_trigger.get("volume_window_minutes")
    threshold_line = ""
    if isinstance(volume_threshold_pct, (int, float)):
        threshold_line = f"- Config threshold: {volume_threshold_pct:+.2f}%"
        if isinstance(window_minutes, (int, float)):
            threshold_line += f" over {int(window_minutes)}m"
        threshold_line += "\n"
    above_threshold_line = ""
    if isinstance(volume_threshold_pct, (int, float)):
        above_threshold_line = (
            f"- Breach magnitude above threshold: "
            f"{(volume_change_pct - volume_threshold_pct):+.2f}%\n"
        )

    return (
        f"## Volume\n"
        f"- 24h notional volume: ${volume_trigger.get('current_volume', 0):,.0f}\n"
        f"- Window start volume: ${volume_trigger.get('window_start_volume', 0):,.0f}\n"
        f"- Volume added in window: ${volume_trigger.get('window_delta', 0):,.0f}\n"
        f"- Volume change: {volume_change_pct:+.2f}%\n"
        f"{threshold_line}"
        f"{above_threshold_line}"
        f"- Volume threshold breached: yes"
    )


def _build_prompt(price_trigger: dict, news_report: dict, oi_report: dict, condition: dict) -> str:
    # Use asset from trigger (multi-ticker aware), fall back to global ASSET
    asset = price_trigger.get("asset", ASSET)
    trigger_source = price_trigger.get("trigger_source", "price")
    volume_section = _build_volume_section(price_trigger)

    price_pct = price_trigger.get("price_change_pct", 0.0)
    if price_pct != 0.0:
        price_section = (
            f"## Price move\n"
            f"- Current price: {price_trigger['current_price']:.4f}\n"
            f"- Price change: {price_pct:+.2f}% in the last 5 minutes\n"
            f"- Window start price: {price_trigger['window_start_price']:.4f}\n"
            f"- Trigger source: {trigger_source}"
        )
    else:
        price_section = (
            f"## Price move\n"
            f"- No price threshold breach (triggered by volume only)\n"
            f"- Trigger source: {trigger_source}"
        )

    vol_change_line = ""
    if condition.get("volume_change_pct") is not None:
        vol_change_line = f"\n- Volume change: {condition['volume_change_pct']:+.2f}%"

    has_news = bool(news_report.get("has_news"))
    return f"""You are a quantitative trading analyst for {asset} perpetual futures on Hyperliquid.

STRICT GROUNDING RULES — violating any of these is a failure:
- Your verdict may ONLY reference these numeric inputs: price_change_pct, oi_change_pct, funding_rate, volume_change_pct, has_news={has_news}.
- Do NOT invent price levels, dates, analyst names, or events. Do NOT extrapolate beyond the inputs.
- Do NOT mention any ticker symbol other than {asset}.
- If has_news is false, primary_driver MUST NOT be "news".
- If no news summary is provided, do not claim a news-driven catalyst.
- Be explicit: every claim in `reasoning` must be derivable from the numbers above OR the news summary below.

{price_section}

## Condition
- {condition['condition_id']} — {condition['label']}: {condition['description']}
- OI change: {condition['oi_change_pct']:+.2f}%{vol_change_line}

## Open Interest
{oi_report['interpretation']}

{volume_section}

## Recent News (has_news={has_news})
{news_report['summary']}

Return ONLY a JSON object, no markdown, no preamble:
{{
  "verdict": "one-sentence causal explanation (no prices, no tickers other than {asset})",
  "confidence": "high" or "medium" or "low",
  "primary_driver": "news" or "oi_flow" or "volume" or "technical" or "unknown",
  "flags": ["short flag strings"],
  "reasoning": "2-3 sentence explanation grounded strictly in the inputs above",
  "cited_inputs": ["price_change_pct" | "oi_change_pct" | "funding_rate" | "volume_change_pct" | "news" — list the inputs you actually used]
}}"""


def _call_openrouter(prompt: str, model: str | None = None) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model or OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.2,
        },
        timeout=30,
        verify=False,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _is_high_priority(condition: dict, news_report: dict) -> bool:
    """
    Lightweight rule mirroring the Telegram scorer's 'PN candidate' bar.
    A high-priority alert deserves the extra cost of a second model call.
    """
    cid = condition.get("condition_id", "")
    if cid not in ("C1", "C2"):
        return False
    score = abs(condition.get("price_change_pct") or 0) * abs(condition.get("oi_change_pct") or 0)
    if score < 6.0:  # ≈ 2% price × 3% OI
        return False
    return bool(news_report.get("has_news"))


def _run_cross_check(primary: dict, prompt: str, asset: str) -> dict:
    """
    Run Agent 3 against a second, independent model. If the two disagree
    on `primary_driver`, append a flag and downgrade confidence one notch.
    Controlled by env vars:
      ENABLE_CROSS_CHECK=true         master switch
      CROSS_CHECK_MODEL=<slug>        e.g. anthropic/claude-haiku-4.5
    """
    import os, json as _json
    if os.environ.get("ENABLE_CROSS_CHECK", "").lower() not in ("1", "true", "yes"):
        return primary
    alt_model = os.environ.get("CROSS_CHECK_MODEL", "").strip()
    if not alt_model:
        logger.info(f"[Agent3/{asset}] cross-check skipped: CROSS_CHECK_MODEL not set")
        return primary
    try:
        raw = _call_openrouter(prompt, model=alt_model).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        second = _json.loads(raw.strip())
        flags = primary.get("flags") or []
        alt_driver = (second.get("primary_driver") or "").strip()
        if alt_driver and alt_driver != primary.get("primary_driver"):
            flags.append(f"cross_check_disagree:{alt_model}:{alt_driver}")
            # Downgrade confidence one notch
            cur = (primary.get("confidence") or "low").lower()
            primary["confidence"] = {"high": "medium", "medium": "low", "low": "low"}[cur]
            logger.warning(
                f"[Agent3/{asset}] cross-check DISAGREES "
                f"(primary='{primary.get('primary_driver')}' vs '{alt_driver}' from {alt_model}); "
                f"confidence downgraded to {primary['confidence']}."
            )
        else:
            flags.append("cross_check_agree")
            logger.info(f"[Agent3/{asset}] cross-check agreed ({alt_model}).")
        primary["flags"] = flags
    except Exception as e:
        logger.warning(f"[Agent3/{asset}] cross-check errored: {e} — keeping primary verdict.")
        flags = primary.get("flags") or []
        flags.append("cross_check_error")
        primary["flags"] = flags
    return primary


_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_DRIVERS = {"news", "oi_flow", "volume", "technical", "unknown"}
# Common English words / tickers of the current asset itself that should not trip the "other ticker" check.
_TICKER_ALLOWLIST = {"AI", "CEO", "CFO", "US", "USA", "UK", "EU", "SEC", "FDA", "EPS",
                      "Q1", "Q2", "Q3", "Q4", "OI", "TP", "SL", "ATR", "RSI", "ETF",
                      "IPO", "GDP", "CPI", "FOMC", "FED", "PDT"}


def _validate_and_ground(result: dict, price_trigger: dict, news_report: dict, condition: dict, asset: str) -> dict:
    """
    Post-hoc hallucination guardrails. Mutates `result` in place and appends flags.
    Rules:
      1. Enum fields must be valid; else coerce to 'low' / 'unknown'.
      2. If verdict mentions a price number (e.g. $123.45 or 123.4), it must match ctx current_price within 1%.
      3. If primary_driver == 'news' but has_news == False → force 'unknown' + flag 'news_driver_without_news'.
      4. If verdict mentions a ticker other than `asset` → flag 'foreign_ticker'.
      5. cited_inputs must be a list of known keys; unknown keys dropped.
    """
    import re

    flags = result.get("flags") or []
    if not isinstance(flags, list):
        flags = []

    # 1. enum validation
    for key in ("verdict", "confidence", "primary_driver", "reasoning"):
        if key not in result or not isinstance(result[key], str):
            result[key] = result.get(key) or ""
    if result.get("confidence") not in _VALID_CONFIDENCE:
        result["confidence"] = "low"
        flags.append("invalid_confidence_coerced")
    if result.get("primary_driver") not in _VALID_DRIVERS:
        result["primary_driver"] = "unknown"
        flags.append("invalid_driver_coerced")

    # 2. price-grounding check
    current_price = float(price_trigger.get("current_price") or 0.0)
    if current_price > 0:
        for m in re.finditer(r"\$?(\d{1,6}(?:\.\d+)?)", result["verdict"]):
            try:
                v = float(m.group(1))
            except Exception:
                continue
            # Skip values that are likely percentages (≤100 and adjacent to %)
            if v <= 100 and m.end() < len(result["verdict"]) and result["verdict"][m.end():m.end()+1] == "%":
                continue
            if v < 1:  # skip tiny funding-rate-ish numbers
                continue
            if abs(v - current_price) / current_price > 0.01:
                logger.warning(f"[Agent3/{asset}] Price-grounding violation: verdict mentions {v} but ctx price is {current_price}.")
                flags.append("price_grounding_violation")
                break

    # 3. news-driver consistency
    has_news = bool(news_report.get("has_news"))
    if result["primary_driver"] == "news" and not has_news:
        logger.warning(f"[Agent3/{asset}] Driver=news but has_news=False; coercing to 'unknown'.")
        result["primary_driver"] = "unknown"
        flags.append("news_driver_without_news")

    # 4. foreign-ticker check
    foreign = set(re.findall(r"\b[A-Z]{2,6}\b", result["verdict"]))
    foreign -= _TICKER_ALLOWLIST
    foreign.discard(asset)
    foreign.discard(asset.split(":")[-1])  # handle "xyz:NVDA"
    if foreign:
        logger.info(f"[Agent3/{asset}] Verdict mentions other tokens {foreign} (could be peer co.; flagging only).")
        flags.append(f"foreign_tokens:{','.join(sorted(foreign))}")

    # 5. cited_inputs sanity
    allowed = {"price_change_pct", "oi_change_pct", "funding_rate", "volume_change_pct", "news"}
    cited = result.get("cited_inputs")
    if isinstance(cited, list):
        result["cited_inputs"] = [c for c in cited if c in allowed]
    else:
        result["cited_inputs"] = []

    result["flags"] = flags
    return result


def run_causality_analysis(price_trigger, news_report, oi_report, condition) -> dict:
    asset = price_trigger.get("asset", ASSET)
    prompt = _build_prompt(price_trigger, news_report, oi_report, condition)
    try:
        if LLM_PROVIDER == "openrouter":
            raw = _call_openrouter(prompt)
        else:
            logger.error(f"[Agent3] Unknown LLM_PROVIDER: {LLM_PROVIDER}")
            return _fallback_verdict()

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw.strip())
        result = _validate_and_ground(result, price_trigger, news_report, condition, asset)
        if _is_high_priority(condition, news_report):
            result = _run_cross_check(result, prompt, asset)
        logger.info(f"[Agent3/{asset}] Verdict: {result.get('verdict')} | confidence={result.get('confidence')} | flags={result.get('flags')}")
        return result

    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status == 401:
            logger.error("[Agent3] OpenRouter auth failed (401). Check OPENROUTER_API_KEY.")
        else:
            logger.error(f"[Agent3] LLM HTTP error ({status}): {e}")
        return _fallback_verdict()
    except Exception as e:
        logger.error(f"[Agent3] LLM call failed: {e}")
        return _fallback_verdict()


def _fallback_verdict() -> dict:
    # Keep a stable machine-detectable flag so callers can surface
    # degraded-mode failures clearly in terminal logs.
    return {
        "verdict": "Unable to determine causality — LLM call failed.",
        "confidence": "low",
        "primary_driver": "unknown",
        "flags": ["agent3_error"],
        "reasoning": "The causality agent encountered an error. Manual review recommended.",
    }
