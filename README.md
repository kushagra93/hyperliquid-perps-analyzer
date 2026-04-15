# Hyperliquid Multi-Ticker Alert Flow

Concurrent multi-ticker market monitoring for Hyperliquid with per-ticker thresholds, OI + news + causality analysis, and Google Sheets alert logging.

The current codebase is fully centered around `TickerWorker` and `config/tickers.py`. Legacy single-ticker monitor files have been removed.

---

## Quick Start

1. Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

2. Create a local settings file:

```bash
cp config/Settings_sample.py config/settings.py
```

3. Set secrets via environment or `.env`:

```bash
export OPENROUTER_API_KEY="..."
export SERP_API_KEY="..."
```

4. Update Google Sheets values in `config/settings.py`:
   - `GOOGLE_CREDENTIALS_FILE`
   - `GOOGLE_SHEET_ID`

5. Tune ticker configs in `config/tickers.py`.

6. Run:

```bash
python3 main.py
```

Optional dry-run test:

```bash
python3 test_full_flow.py NVDA TSLA
```

---

## Current Repository Layout

```text
flow/
├── main.py
├── test_full_flow.py
├── config/
│   ├── settings.py
│   ├── Settings_sample.py
│   └── tickers.py
├── core/
│   ├── hl_client.py
│   ├── ticker_worker.py
│   └── condition_engine.py
├── agents/
│   ├── agent1_news.py
│   ├── agent2_oi.py
│   └── agent3_causality.py
└── notifiers/
    └── sheets.py
```

---

## Architecture Overview

## Main Loop (`main.py`)

- Loads all ticker configs from `config/tickers.py`.
- Builds one `TickerWorker` per ticker.
- Fetches Hyperliquid `metaAndAssetCtxs` once per cron tick.
- Fan-outs workers concurrently using `ThreadPoolExecutor`.
- Passes shared payload to each worker to avoid duplicate HL requests.

## Shared HL Client (`core/hl_client.py`)

- Uses a shared `requests.Session`.
- Retries transient failures (`Retry`).
- Uses enlarged connection pool:
  - `pool_connections=32`
  - `pool_maxsize=32`
- Exposes:
  - `fetch_meta_and_asset_ctxs()`

## Per-Ticker Worker (`core/ticker_worker.py`)

Each worker owns isolated state:

- price history deque
- volume history deque
- OI history deque
- volume reset tracking
- cooldown timer

`run_tick(shared_data)` flow:

1. Extract ticker ctx from shared HL payload.
2. Read price, volume, OI from ctx.
3. Prune per-ticker history windows.
4. Update OI snapshot.
5. Evaluate price trigger.
6. Evaluate volume trigger.
7. If no trigger, exit.
8. Apply per-ticker cooldown.
9. Build effective trigger context.
10. Evaluate condition (`C1`–`C4`).
11. Run Agent 1 + Agent 2 in parallel.
12. Apply alert gate (`should_alert`).
13. Run Agent 3 causality analysis.
14. Log to per-ticker sheet tab.

## Condition Engine (`core/condition_engine.py`)

- `evaluate_condition(price_trigger, oi_snapshot)`:
  - maps trigger + OI direction to `C1`/`C2`/`C3`/`C4`.
  - supports volume-direction fallback if price is flat.
- `should_alert(condition, news_report)`:
  - C1/C2 always alert.
  - C3/C4 alert when news exists or trigger source includes volume.

## Agents

### Agent 1 (`agents/agent1_news.py`)

- `fetch_news(symbol, full_name)` fetches ticker-targeted news via SerpAPI.
- Summarizes results via OpenRouter LLM.
- Keeps default args for backward compatibility.

### Agent 2 (`agents/agent2_oi.py`)

- `build_oi_report_for_ticker(oi_snapshot, volume_trigger, ctx)` is the active multi-ticker path.
- Uses already-fetched ticker ctx to avoid extra HL API calls.
- `build_oi_report()` and `fetch_asset_context()` are retained compatibility helpers.

### Agent 3 (`agents/agent3_causality.py`)

- Builds causality prompt with price, OI, volume, news, and condition context.
- Uses `price_trigger["asset"]` for multi-ticker awareness.
- Calls OpenRouter and expects JSON output.
- Returns fallback verdict on errors.

## Sheets Notifier (`notifiers/sheets.py`)

- `log_alert(..., sheets_tab=...)` writes one row per alert.
- Auto-creates missing tabs.
- Caches:
  - gspread client (`_sheet_client`)
  - worksheet handles (`_ws_cache`)
- Handles concurrent tab-create races safely.

---

## Configuration

## 1) Global settings (`config/settings.py`)

Template file: `config/Settings_sample.py`.

Most relevant keys:

- runtime:
  - `CRON_INTERVAL_SECONDS`
  - `HL_PERP_DEX`
- news/LLM:
  - `SERP_API_KEY`
  - `OPENROUTER_API_KEY`
  - `OPENROUTER_MODEL`
  - `LLM_PROVIDER`
- sheets:
  - `GOOGLE_CREDENTIALS_FILE`
  - `GOOGLE_SHEET_ID`
  - `GOOGLE_SHEET_TAB` (fallback/default tab)

## 2) Per-ticker settings (`config/tickers.py`)

Every ticker has independent configuration:

- `hl_asset`
- `full_name`
- `price_change_threshold_pct`
- `price_window_minutes`
- `volume_change_threshold_pct`
- `volume_window_minutes`
- `volume_reset_drop_pct`
- `oi_window_hours`
- `enable_volume_trigger`
- `alert_cooldown_seconds`
- `sheets_tab`

Current default ticker set includes 15 symbols (NVDA, TSLA, AAPL, MSFT, GOOGL, META, AMZN, GOLD, SP500, PLTR, AMD, MSTR, COIN, HOOD, INTC).

---

## Running

Start continuous monitoring:

```bash
python3 main.py
```

---

## Testing

`test_full_flow.py` is the current dry-run validation harness.

Run specific tickers:

```bash
python3 test_full_flow.py NVDA TSLA
```

Run default first 3 tickers:

```bash
python3 test_full_flow.py
```

What it validates:

- condition classification
- Agent 1/2/3 pipeline integration
- alert gating
- per-ticker Sheets tab logging

---

## Alert Output Schema (Google Sheets)

Columns written by `notifiers/sheets.py`:

- Timestamp (IST)
- Ticker
- Trigger Source
- Current Price
- Price Δ%
- OI Δ%
- Volume 24h
- Volume Δ%
- Condition
- Condition Label
- Primary Driver
- Confidence
- Verdict
- Flags
- News Summary
- Reasoning

---

## Troubleshooting

### HL fetch failures

- Check network/proxy access to `https://api.hyperliquid.xyz/info`.
- Verify `HL_PERP_DEX` and ticker `hl_asset` values.

### No alerts

- Lower thresholds in `config/tickers.py`.
- Check `alert_cooldown_seconds`.
- Verify `condition_engine` rules.

### News/LLM failures

- Confirm `SERP_API_KEY`, `OPENROUTER_API_KEY`.
- Check outbound network rules.
- Fallback responses may still allow pipeline completion.

### Sheets failures

- Ensure sheet is shared with service account email.
- Validate `GOOGLE_SHEET_ID` and credentials path.
- Check service account JSON permissions/scopes.

---

## Security and Repo Hygiene

- Keep secrets in `.env` or environment variables.
- Do not commit real keys in `config/settings.py`.
- Use `config/Settings_sample.py` as the public-safe template.
