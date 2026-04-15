import json
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # Optional dependency; environment variables may already be set.
    pass

# config/settings.py
# ─────────────────────────────────────────────
# Committed, secret-free module. All values come from environment
# variables (with non-secret defaults only). Set secrets in `.env`
# locally or in your host's secret store (e.g. Railway variables).
# ─────────────────────────────────────────────


def _env_int(key: str, default: str) -> int:
    return int(os.environ.get(key, default).strip())


def _env_float(key: str, default: str) -> float:
    return float(os.environ.get(key, default).strip())


def _env_bool(key: str, default: str) -> bool:
    return os.environ.get(key, default).strip().lower() in ("1", "true", "yes", "on")


# ── Asset (legacy single-ticker defaults for agents) ─────────────────
ASSET = os.environ.get("ASSET", "NVDA").strip()
SPOT_ASSET = os.environ.get("SPOT_ASSET", "").strip() or ASSET
# Perp dex to query in Hyperliquid `metaAndAssetCtxs` requests.
HL_PERP_DEX = os.environ.get("HL_PERP_DEX", "xyz").strip()

# ── Threshold (legacy globals; multi-ticker uses config/tickers.py) ─
PRICE_CHANGE_THRESHOLD_PCT = _env_float("PRICE_CHANGE_THRESHOLD_PCT", "0.1")
PRICE_WINDOW_MINUTES = _env_int("PRICE_WINDOW_MINUTES", "5")
VOLUME_CHANGE_THRESHOLD_PCT = _env_float("VOLUME_CHANGE_THRESHOLD_PCT", "1.0")
VOLUME_WINDOW_MINUTES = _env_int("VOLUME_WINDOW_MINUTES", "5")
ENABLE_VOLUME_TRIGGER = _env_bool("ENABLE_VOLUME_TRIGGER", "true")
CRON_INTERVAL_SECONDS = _env_int("CRON_INTERVAL_SECONDS", "60")

# ── OI Tracking ─────────────────────────────────────────────────────
OI_WINDOW_HOURS = _env_int("OI_WINDOW_HOURS", "3")

# ── News / APIs (secrets: empty default only) ───────────────────────
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "").strip()
SERP_API_KEY = os.environ.get("SERP_API_KEY", "").strip()

# ── LLM ─────────────────────────────────────────────────────────────
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openrouter").strip().lower()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "deepseek/deepseek-chat"
).strip()

# ── Google Sheets ───────────────────────────────────────────────────
_GOOGLE_CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_CREDENTIALS_FILE", "credentials.json"
).strip()
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Alerts").strip()


def _materialize_google_credentials_from_env() -> str:
    """
    If GOOGLE_CREDENTIALS_JSON is present, write it to /tmp and use that path.
    Returns effective credential file path.
    """
    if not GOOGLE_CREDENTIALS_JSON:
        return _GOOGLE_CREDENTIALS_PATH

    path = "/tmp/credentials.json"
    try:
        parsed = json.loads(GOOGLE_CREDENTIALS_JSON)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(parsed, f)
        os.environ["GOOGLE_CREDENTIALS_FILE"] = path
        return path
    except Exception as e:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON is set but invalid JSON or not writable."
        ) from e


GOOGLE_CREDENTIALS_FILE = _materialize_google_credentials_from_env()


def validate_runtime_settings() -> None:
    """
    Fail fast at startup with actionable configuration errors.
    """
    missing = []
    if not SERP_API_KEY:
        missing.append("SERP_API_KEY")
    if not OPENROUTER_API_KEY:
        missing.append("OPENROUTER_API_KEY")
    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")

    has_file = bool(GOOGLE_CREDENTIALS_FILE) and os.path.exists(
        GOOGLE_CREDENTIALS_FILE
    )
    has_json = bool(GOOGLE_CREDENTIALS_JSON)
    if not (has_file or has_json):
        missing.append(
            "GOOGLE_CREDENTIALS_FILE (existing path) or GOOGLE_CREDENTIALS_JSON"
        )

    if missing:
        raise RuntimeError(
            "Missing required runtime configuration: " + ", ".join(missing)
        )


# ── Condition Rules ─────────────────────────────────────────────────
# C1: Price up + OI up   → always alert (strong bull)
# C2: Price down + OI up → always alert (strong bear)
# C3: Price down + OI down → alert only if news found (weak fall)
# C4: Price up + OI down   → alert only if news found (weak rally)
# These are enforced in core/condition_engine.py — edit the logic there if needed.
