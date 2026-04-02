# NVDA Futures Flow — HL Multi-Agent Monitor

Monitors NVDA/USDC perpetual on Hyperliquid, detects threshold price moves,
fires agents to find causality, and logs structured alerts to Google Sheets.

---

## File Structure

```
nvda_flow/
├── main.py                  # Entry point — runs the cron loop
├── config/
│   └── settings.py          # All configurable parameters (thresholds, rules, keys)
├── core/
│   ├── price_monitor.py     # Polls HL price every 60s, detects threshold breach
│   ├── oi_tracker.py        # Tracks OI over rolling 3hr window
│   └── condition_engine.py  # Evaluates C1–C4 and causality rules
├── agents/
│   ├── agent1_news.py       # Fetches Yahoo Finance news (last 1hr)
│   ├── agent2_oi.py         # Fetches HL open interest delta
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
Edit `config/settings.py` — set your thresholds, Google Sheets ID, LLM key.

### 3. Google Sheets credentials
- Create a Google Cloud project
- Enable the Google Sheets API
- Create a Service Account, download the JSON key
- Save it as `credentials.json` in the project root
- Share your target Google Sheet with the service account email

### 4. LLM API key
Set in `config/settings.py` — supports Anthropic Claude or OpenAI GPT-4.
Choose by setting `LLM_PROVIDER = "anthropic"` or `"openai"`.

### 5. Run
```bash
python main.py
```

---

## Condition Logic

| Condition | Signal | Trigger |
|-----------|--------|---------|
| C1 | Price ↑ + OI ↑ | Strong bull — always alert |
| C2 | Price ↓ + OI ↑ | Strong bear — always alert |
| C3 | Price ↓ + OI ↓ | Weak fall / longs exiting — alert only if news confirms |
| C4 | Price ↑ + OI ↓ | Weak rally / shorts exiting — alert only if news confirms |

---

## Google Sheets Output Columns

`Timestamp | Price | Price Δ% | OI Δ% | Condition | News Summary | Causality Verdict | Confidence`
