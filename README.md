# Hyperliquid Multi-Ticker Alert Flow

Concurrent, multi-agent monitoring for Hyperliquid markets with per-ticker thresholds, cooldowns, and dedicated Google Sheets tabs.

The system detects price and/or volume threshold breaches, evaluates OI-based market conditions, enriches signals with news + LLM causality analysis, and writes structured alerts to Sheets.

---

## Quick Start (2 Minutes)

1. Install dependencies:
   ```bash
   python3 -m pip install -r requirements.txt
   ```
2. Create your local config from sample:
   ```bash
   cp config/Settings_sample.py config/settings.py
   ```
3. Set secrets via `.env` or environment:
   ```bash
   export OPENROUTER_API_KEY="..."
   export SERP_API_KEY="..."
   ```
4. Set Google Sheets values in `config/settings.py`:
   - `GOOGLE_CREDENTIALS_FILE`
   - `GOOGLE_SHEET_ID`
5. Adjust ticker list + thresholds in `config/tickers.py`.
6. Start monitor:
   ```bash
   python3 main.py
   ```
7. Optional dry test:
   ```bash
   python3 test_full_flow.py NVDA TSLA
   ```

---

## Features

- Multi-ticker concurrent scanning from one process
- Fully isolated state per ticker (history, cooldown, tab)
- Trigger gate based on price and/or volume movement
- Condition classification (`C1` to `C4`) using price + OI direction
- Parallel enrichment pipeline:
  - Agent 1: news fetch/summarization
  - Agent 2: OI/funding/volume report
  - Agent 3: causality verdict via LLM
- Per-ticker Google Sheets tabs with lazy auto-creation
- Backward compatibility retained in key agent interfaces

---

## Repository Layout

```text
flow/
├── main.py
├── config/
│   ├── settings.py
│   ├── Settings_sample.py
│   └── tickers.py
├── core/
│   ├── ticker_worker.py
│   ├── hl_client.py
│   ├── condition_engine.py
│   ├── oi_tracker.py          # legacy single-ticker utility
│   ├── price_monitor.py       # legacy single-ticker utility
│   └── volume_monitor.py      # legacy single-ticker utility
├── agents/
│   ├── agent1_news.py
│   ├── agent2_oi.py
│   └── agent3_causality.py
├── notifiers/
│   └── sheets.py
└── test_full_flow.py
```

---

## Architecture

## Main Loop

`main.py`:

- loads `TICKERS`
- creates one `TickerWorker` per symbol
- fetches Hyperliquid market payload once per cron tick
- fan-outs workers concurrently using shared market payload

This design avoids duplicate network calls and prevents connection-pool pressure when tracking many symbols.

## Per-Ticker Worker

`core/ticker_worker.py`:

- keeps independent per-symbol rolling histories:
  - price history
  - volume history
  - OI history
- keeps per-symbol cooldown timer
- processes one full decision pipeline in `run_tick(...)`

## Hyperliquid Client

`core/hl_client.py`:

- shared `requests.Session`
- retry policy for transient transport failures
- enlarged connection pool for concurrent workloads

## Condition Engine

`core/condition_engine.py`:

- maps trigger + OI movement to `C1`/`C2`/`C3`/`C4`
- applies `should_alert()` policy gate

## Agents

- `agent1_news.py`
  - `fetch_news(symbol, full_name)` for per-ticker queries
  - still supports default single-asset usage

- `agent2_oi.py`
  - `build_oi_report_for_ticker(oi_snapshot, volume_trigger, ctx)`
  - consumes pre-fetched ctx (no duplicate HL call per ticker)
  - `build_oi_report()` retained for backward compatibility

- `agent3_causality.py`
  - uses `price_trigger["asset"]`
  - includes volume context in prompt
  - returns structured JSON verdict

## Sheets Notifier

`notifiers/sheets.py`:

- `log_alert(..., sheets_tab=...)`
- creates missing tabs automatically
- caches client + worksheet handles in-process

---

## Configuration Reference

## 1) Global Config: `config/settings.py`

Use `config/Settings_sample.py` as template.

Key fields:

- runtime:
  - `CRON_INTERVAL_SECONDS`
  - `HL_PERP_DEX`
- APIs:
  - `SERP_API_KEY`
  - `OPENROUTER_API_KEY`
  - `OPENROUTER_MODEL`
  - `LLM_PROVIDER`
- Sheets:
  - `GOOGLE_CREDENTIALS_FILE`
  - `GOOGLE_SHEET_ID`
  - `GOOGLE_SHEET_TAB` (fallback/default tab)

## 2) Per-Ticker Config: `config/tickers.py`

Each ticker entry is independent:

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

Example:

```python
"NVDA": {
    "hl_asset": "xyz:NVDA",
    "full_name": "Nvidia",
    "price_change_threshold_pct": 2.0,
    "price_window_minutes": 5,
    "volume_change_threshold_pct": 15.0,
    "volume_window_minutes": 180,
    "volume_reset_drop_pct": 30.0,
    "oi_window_hours": 3,
    "enable_volume_trigger": True,
    "alert_cooldown_seconds": 300,
    "sheets_tab": "NVDA",
}
```

---

## Setup Guide

## 1) Python and dependencies

```bash
python3 -m pip install -r requirements.txt
```

## 2) Local settings file

```bash
cp config/Settings_sample.py config/settings.py
```

Then edit `config/settings.py`.

## 3) Secrets

Set via environment or `.env`:

```bash
OPENROUTER_API_KEY=...
SERP_API_KEY=...
```

## 4) Google Sheets service account

1. Create Google Cloud project
2. Enable Google Sheets API
3. Create service account and download JSON key
4. Save key path in `GOOGLE_CREDENTIALS_FILE`
5. Share sheet with service account email

---

## Runtime Behavior (Per Tick)

1. Main loop fetches HL market payload once.
2. Worker extracts ticker context from shared payload.
3. Worker updates OI history.
4. Worker evaluates price and volume triggers.
5. If no trigger, worker returns early.
6. Cooldown check is applied.
7. Effective trigger is built (price/volume/combined).
8. Condition engine computes `C1`–`C4`.
9. Agent 1 + Agent 2 run in parallel.
10. `should_alert()` gate runs.
11. Agent 3 builds final causality verdict.
12. Alert is appended to ticker-specific Sheets tab.

---

## Condition Model

| Condition | Signal | Alert behavior |
|---|---|---|
| C1 | Price up + OI up | Always alert |
| C2 | Price down + OI up | Always alert |
| C3 | Price down + OI down | Alert only if news confirms |
| C4 | Price up + OI down | Alert only if news confirms |

---

## Running

Start live monitor:

```bash
python3 main.py
```

---

## Testing

`test_full_flow.py` is a dry-run harness for multi-ticker APIs.

Run specific symbols:

```bash
python3 test_full_flow.py NVDA TSLA
```

Run default first three symbols:

```bash
python3 test_full_flow.py
```

The test:

- forces deterministic trigger conditions
- executes condition + agents + notifier flow
- validates per-ticker sheet tab behavior

---

## Google Sheets Schema

Each alert row includes:

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

## Backward Compatibility Notes

- `agent1_news.fetch_news()` still works without params.
- `agent2_oi.build_oi_report()` is retained for legacy flow.
- Legacy single-ticker monitor modules remain in repo.

---

## Troubleshooting

- **`Connection pool is full` warnings**
  - Resolved by shared-per-tick HL fetch + larger pool in `hl_client.py`.

- **Ticker not found in HL universe**
  - Check `hl_asset` and `HL_PERP_DEX`.

- **No alerts firing**
  - Lower thresholds in `config/tickers.py`.
  - Check cooldown (`alert_cooldown_seconds`).
  - Verify condition gating in `core/condition_engine.py`.

- **News/LLM requests failing**
  - Validate API keys.
  - Check outbound network/proxy.
  - Fallback paths may still produce low-confidence output.

- **Sheets write fails**
  - Confirm service account is shared on target sheet.
  - Confirm `GOOGLE_SHEET_ID` and credentials path.

---

## Security and Repo Hygiene

- Do not commit `config/settings.py` if it contains secrets.
- Commit `config/Settings_sample.py` instead.
- Keep API keys only in `.env` or environment variables.
