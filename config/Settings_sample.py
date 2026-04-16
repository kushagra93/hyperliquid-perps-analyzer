"""
Reference: environment variables read by `config/settings.py`.

`config/settings.py` is committed and contains no secrets. Copy variable
names into `.env` or your host's secret store — do not duplicate keys here.

Required for production (`validate_runtime_settings`):
  SERP_API_KEY
  OPENROUTER_API_KEY
  GOOGLE_SHEET_ID
  Plus either a readable GOOGLE_CREDENTIALS_FILE path or GOOGLE_CREDENTIALS_JSON

Optional / tuning (see settings.py for defaults):
  ASSET, SPOT_ASSET, HL_PERP_DEX
  PRICE_CHANGE_THRESHOLD_PCT, PRICE_WINDOW_MINUTES
  VOLUME_CHANGE_THRESHOLD_PCT, VOLUME_WINDOW_MINUTES, ENABLE_VOLUME_TRIGGER
  CRON_INTERVAL_SECONDS, OI_WINDOW_HOURS
  NEWS_API_KEY, LLM_PROVIDER, OPENROUTER_MODEL
  GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_TAB

Multi-ticker thresholds live in `config/tickers.py`.
"""
