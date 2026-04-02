# NVDA Futures Flow — HL Multi-Agent Monitor

Monitors NVDA on Hyperliquid, detects threshold movement in price and/or volume,
then runs an OI + news + causality pipeline and logs structured alerts to Google Sheets.

---

## File Structure

```
flow/
├── main.py                  # Entry point — runs the cron loop
├── config/
│   └── settings.py          # All configurable parameters (thresholds, rules, keys)
├── core/
│   ├── price_monitor.py     # Polls HL price every 60s, detects threshold breach
│   ├── volume_monitor.py    # Tracks 24h notional volume delta over rolling window
│   ├── oi_tracker.py        # Tracks OI over rolling 3hr window
│   └── condition_engine.py  # Evaluates C1–C4 and causality rules
├── agents/
│   ├── agent1_news.py       # Fetches Yahoo Finance news (last 1hr)
│   ├── agent2_oi.py         # Builds OI report + funding + 24h volume context
│   └── agent3_causality.py  # LLM call — correlates news + OI into verdict
└── notifiers/
    └── sheets.py            # Appends alert row to Google Sheets
```

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure settings
Edit `config/settings.py` and configure:
- `PRICE_CHANGE_THRESHOLD_PCT`
- `PRICE_WINDOW_MINUTES`
- `VOLUME_CHANGE_THRESHOLD_PCT`
- `VOLUME_WINDOW_MINUTES`
- `ENABLE_VOLUME_TRIGGER`
- `OI_WINDOW_HOURS`
- `CRON_INTERVAL_SECONDS`
- Google Sheets settings and API keys

### 3. Google Sheets credentials
- Create a Google Cloud project
- Enable the Google Sheets API
- Create a Service Account, download the JSON key
- Save it as `credentials.json` in the project root
- Share your target Google Sheet with the service account email

### 4. LLM API key
Set in `config/settings.py` and/or `.env`.
Current default provider is OpenRouter (`LLM_PROVIDER = "openrouter"`).

### 5. Run
```bash
python3 main.py
```

---

## Trigger and Flow Logic

1. On each tick, system updates:
   - OI snapshot (`OITracker`)
   - price trigger (`PriceMonitor`)
   - volume trigger (`VolumeMonitor`, if enabled)
2. Trigger gate is **OR**:
   - Continue only if `price_trigger` or `volume_trigger` exists.
3. Cooldown is applied (`ALERT_COOLDOWN_SECONDS` in `main.py`):
   - If last alert was too recent, skip this tick.
4. Condition engine classifies C1–C4 using price direction + OI direction.
5. Volume-only trigger fallback:
   - If price is flat/missing but volume triggered, volume direction is used as fallback price direction.
6. Agent 1 (news) and Agent 2 (OI report) run in parallel.
7. `should_alert()` applies rules (C1/C2 always, C3/C4 news-gated).
8. If approved, Agent 3 runs and result is logged to Google Sheets.

---

## Condition Logic (C1–C4)

| Condition | Signal | Trigger |
|-----------|--------|---------|
| C1 | Price ↑ + OI ↑ | Strong bull — always alert |
| C2 | Price ↓ + OI ↑ | Strong bear — always alert |
| C3 | Price ↓ + OI ↓ | Weak fall / longs exiting — alert only if news confirms |
| C4 | Price ↑ + OI ↓ | Weak rally / shorts exiting — alert only if news confirms |

---

## Google Sheets Output Columns

`Timestamp | Price | Price Δ% | OI Δ% | Condition | News Summary | Causality Verdict | Confidence`

---

## Local test run

```bash
python3 test_full_flow.py
```

This script runs dry scenarios for:
- price-only trigger
- volume-only trigger
- price + volume trigger
- no-trigger path
