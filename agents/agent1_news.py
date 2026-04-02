import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import logging
from datetime import datetime, timezone, timedelta
from config.settings import ASSET, NEWS_LOOKBACK_MINUTES, OPENROUTER_API_KEY, OPENROUTER_MODEL, SERP_API_KEY

logger = logging.getLogger(__name__)

def _summarize_articles(articles: list, asset: str) -> str:
    if not articles:
        return "No recent news found."

    context_parts = []
    for i, a in enumerate(articles[:8]):
        part = f"Article {i+1} [{a['source']}] [{a['published_at'][:16]}]\nTitle: {a['title']}"
        if a.get("snippet"):
            part += f"\nSnippet: {a['snippet']}"
        context_parts.append(part)

    prompt = f"""You are a financial news analyst focused on short-term trading signals for {asset}.

Below are recent news articles. Provide:
1. A 3-4 sentence summary of the key stories and catalysts
2. Overall sentiment: bullish / bearish / mixed / neutral
3. Any specific risks, numbers, or events a trader should know

Articles:
{chr(10).join(context_parts)}

Respond in plain prose. No bullet points. No headers. Be direct and specific."""

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": 0.2,
            },
            timeout=20,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"[Agent1] LLM summarization failed: {e}")
        return "\n".join(f"- {a['title']} [{a['source']}]" for a in articles[:5])


def fetch_news(asset: str = ASSET) -> dict:
    articles = []

    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={
                "engine": "google_news",
                "q": f"{asset} Nvidia stock",
                "hl": "en",
                "gl": "us",
                "num": 10,
                "api_key": SERP_API_KEY,
            },
            timeout=15,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()

        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=NEWS_LOOKBACK_MINUTES)

        for item in data.get("news_results", []):
            # SerpAPI returns relative time like "2 hours ago" — parse what we can
            pub_raw = item.get("date", "")
            try:
                # Try direct ISO parse first
                pub_time = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
            except Exception:
                # Fallback — treat as recent if we can't parse
                pub_time = datetime.now(tz=timezone.utc)

            articles.append({
                "title": item.get("title", ""),
                "source": item.get("source", {}).get("name", "") if isinstance(item.get("source"), dict) else item.get("source", ""),
                "published_at": pub_time.isoformat(),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })

        logger.info(f"[Agent1] SerpAPI: {len(articles)} articles found.")

    except Exception as e:
        logger.warning(f"[Agent1] SerpAPI failed: {e}")

    has_news = len(articles) > 0
    logger.info(f"[Agent1] Summarizing {len(articles)} articles via LLM...")
    summary = _summarize_articles(articles, asset)
    logger.info(f"[Agent1] Summary ready: {summary[:150]}...")

    return {
        "has_news": has_news,
        "articles": articles[:10],
        "summary": summary,
    }
