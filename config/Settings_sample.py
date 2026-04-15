import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # Optional dependency; environment variables may already be set.
    pass

# config/Settings_sample.py
# ─────────────────────────────────────────────
# Sample configuration for documentation/GitHub.
# Copy this to config/settings.py and update values.
# ─────────────────────────────────────────────

# ── Asset (single-ticker legacy compatibility) ───────────────────
ASSET = "NVDA"
SPOT_ASSET = "NVDA"
# Perp dex to query in Hyperliquid `metaAndAssetCtxs` requests.
# Use "xyz" for deployer-listed markets.
HL_PERP_DEX = os.environ.get("HL_PERP_DEX", "xyz").strip()

# ── Thresholds (used by legacy single-ticker path) ───────────────
PRICE_CHANGE_THRESHOLD_PCT = 2.0     # % move that triggers agents
PRICE_WINDOW_MINUTES = 5             # Rolling window for price delta calculation
VOLUME_CHANGE_THRESHOLD_PCT = 15.0   # % change in 24h notional volume to trigger agents
VOLUME_WINDOW_MINUTES = 180          # Rolling window for volume baseline comparison
ENABLE_VOLUME_TRIGGER = True         # Feature toggle for volume-based trigger
CRON_INTERVAL_SECONDS = 60           # How often workers run

# ── OI Tracking ───────────────────────────────────────────────────
OI_WINDOW_HOURS = 3                  # Rolling window for OI baseline comparison

# ── News / APIs ───────────────────────────────────────────────────
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "your_news_api_key_here").strip()
SERP_API_KEY = os.environ.get("SERP_API_KEY", "your_serp_api_key_here").strip()

# ── LLM ───────────────────────────────────────────────────────────
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openrouter").strip().lower()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "your_openrouter_api_key_here").strip()
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat").strip()

# ── Google Sheets ────────────────────────────────────────────────
GOOGLE_CREDENTIALS_FILE = "credentials.json"  # Path to service account JSON
GOOGLE_SHEET_ID = "your_google_sheet_id_here"
GOOGLE_SHEET_TAB = "Alerts"                   # Fallback/default worksheet name

# ── Notes ─────────────────────────────────────────────────────────
# Multi-ticker production config lives in config/tickers.py.
# This sample file is intentionally safe for public repos.
