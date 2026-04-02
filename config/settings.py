# config/settings.py
# ─────────────────────────────────────────────
# All configurable parameters for the NVDA flow
# ─────────────────────────────────────────────

# ── Asset ────────────────────────────────────
ASSET = "NVDA"
HL_MARKET_TYPE = "spot"                # Hyperliquid asset name (case-sensitive)

# ── Threshold ────────────────────────────────
PRICE_CHANGE_THRESHOLD_PCT = 0.1    # % move that triggers agents
PRICE_WINDOW_MINUTES = 5            # Rolling window for price delta calculation
CRON_INTERVAL_SECONDS = 60          # How often the price monitor polls

# ── OI Tracking ──────────────────────────────
OI_WINDOW_HOURS = 3                 # Rolling window for OI baseline comparison
OI_CHANGE_THRESHOLD_PCT = 0.01       # Minimum OI % change to be considered significant

# ── News ─────────────────────────────────────
NEWS_LOOKBACK_MINUTES = 60
NEWS_API_KEY = "da2cbb8c578e4c9ab3a3c6373d205080"
SERP_API_KEY = "bfca62f74b3f7804e8cfc8ff5fad7464189d21995b3cd4a56e34159f2040c92d"
SERP_API_KEY="bfca62f74b3f7804e8cfc8ff5fad7464189d21995b3cd4a56e34159f2040c92d"

# ── LLM ──────────────────────────────────────
LLM_PROVIDER = "openrouter"
OPENROUTER_API_KEY = "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"
OPENROUTER_MODEL = "deepseek/deepseek-chat"

# ── Google Sheets ─────────────────────────────
GOOGLE_CREDENTIALS_FILE = "credentials.json"   # Path to your service account JSON
GOOGLE_SHEET_ID = "1gX21pVqYkkCrxUpRcryv5Ok7RYH3wXMz21RRxt0-FCo"                          
GOOGLE_SHEET_TAB = "Alerts"                    # Tab/worksheet name

# ── Condition Rules ───────────────────────────
# C1: Price up + OI up   → always alert (strong bull)
# C2: Price down + OI up → always alert (strong bear)
# C3: Price down + OI down → alert only if news found (weak fall)
# C4: Price up + OI down   → alert only if news found (weak rally)
# These are enforced in core/condition_engine.py — edit the logic there if needed.
