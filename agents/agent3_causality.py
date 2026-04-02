import os
import json
import logging
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from config.settings import LLM_PROVIDER, OPENROUTER_API_KEY, OPENROUTER_MODEL, ASSET

logger = logging.getLogger(__name__)

# OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

def _build_prompt(price_trigger, news_report, oi_report, condition):
    return f"""You are a quantitative trading analyst. Analyze the following market data for {ASSET} perpetual futures on Hyperliquid and identify the most likely causal explanation.

## Price Move
- Current price: {price_trigger['current_price']:.4f}
- Price change: {price_trigger['price_change_pct']:+.2f}% in the last 5 minutes
- Window start price: {price_trigger['window_start_price']:.4f}

## Condition
- {condition['condition_id']} — {condition['label']}: {condition['description']}
- OI change: {condition['oi_change_pct']:+.2f}%

## Open Interest
{oi_report['interpretation']}

## Recent News (last 60 minutes)
{news_report['summary']}

Respond ONLY with a JSON object, no markdown, no preamble:
{{
  "verdict": "one sentence causal explanation",
  "confidence": "high" or "medium" or "low",
  "primary_driver": "news" or "oi_flow" or "technical" or "unknown",
  "flags": ["short flag strings"],
  "reasoning": "2-3 sentence explanation"
}}"""

def _call_openrouter(prompt: str) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.2,
        },
        timeout=30,
        verify=False,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

def _call_openai(prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY", ""))
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        temperature=0.2,
    )
    return response.choices[0].message.content

def run_causality_analysis(price_trigger, news_report, oi_report, condition) -> dict:
    prompt = _build_prompt(price_trigger, news_report, oi_report, condition)
    try:
        if LLM_PROVIDER == "openrouter":
            raw = _call_openrouter(prompt)
        elif LLM_PROVIDER == "openai":
            raw = _call_openai(prompt)
        else:
            logger.error(f"[Agent3] Unknown LLM_PROVIDER: {LLM_PROVIDER}")
            return _fallback_verdict()

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw.strip())
        logger.info(f"[Agent3] Verdict: {result.get('verdict')} | confidence={result.get('confidence')}")
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
    return {
        "verdict": "Unable to determine causality — LLM call failed.",
        "confidence": "low",
        "primary_driver": "unknown",
        "flags": ["agent3_error"],
        "reasoning": "The causality agent encountered an error. Manual review recommended.",
    }
