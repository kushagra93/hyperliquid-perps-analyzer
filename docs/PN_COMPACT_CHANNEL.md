# Compact PN Channel — separate journey

Dedicated push-notification channel optimized for mobile lock-screen consumption. Hard size limits, action-verb tone, 3-stage timeline.

## Format constraints (enforced, not advised)

| Field | Limit | Rationale |
|---|---|---|
| Title | ≤ 50 chars | 1 mobile line |
| Body | ≤ 80 chars | 2 mobile lines |
| Total | ≤ 130 chars | <1s scan time |

`tests/test_pn_compact.py` asserts these; runs as part of CI.

## Three stages per event

| Stage | Delay | What | Example |
|---|---|---|---|
| **break** | T+0 | Catalyst + first move + setup | `🔴 Trump tariff 25% · AAPL -1.2%` / `Supply hit. Short zone.` |
| **cascade** | T+15 min | Follow-through + liquidation + level | `💀 $18M liq · AAPL -1.2%` / `Support 168 testing. Dip buy or fade?` |
| **repricing** | T+2 hr | Equilibrium + next catalyst | `📊 AAPL settled · -1.8% net` / `New range live. Next: Congress vote. Swing setup ready.` |

Stages 2 and 3 re-resolve live HL data at fire time — they don't replay the snapshot from stage 1.

## Tone rules

- Hinglish-crisp. Short sentences. Action verbs first.
- ❌ "consider" / "may potentially" / "watch closely"
- ✅ "Short zone." / "Dip buy." / "Trail stops."
- Emoji follows direction: 🔴 / 🟢 / ⚪ (break) · 💀 / 🚀 / 📊 (cascade)
- No emojis in the body — title carries the visual hook.

## Channel separation

Set `TELEGRAM_PN_CHANNEL_ID` to a dedicated channel (separate from the main alert channel). The wave dispatcher routes only there. Falls back to `TELEGRAM_CHAT_ID` if not set.

```bash
# Setup
# 1. Create new Telegram channel: e.g. "@hereandnow_pn"
# 2. Add @hereandnowalertbot as Admin with "Post Messages" permission
# 3. Get the channel id (negative number for channels):
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[] | select(.my_chat_member.chat.type=="channel") | .my_chat_member.chat.id'

# 4. Set in your env
export TELEGRAM_PN_CHANNEL_ID="-100xxxxxxxxxx"
```

## Firing a wave manually

```bash
# Dry run (prints all 3 stages, doesn't send)
PYTHONPATH=. python3 notifiers/wave_dispatcher.py --demo \
  --symbol AAPL --move -1.2 --catalyst "Trump tariff 25%" \
  --level 168 --liq 18 --next-event "Congress vote"

# Live (fires stage 1 now, schedules 2 and 3)
TELEGRAM_BOT_TOKEN=... TELEGRAM_PN_CHANNEL_ID=... \
PYTHONPATH=. python3 notifiers/wave_dispatcher.py \
  --symbol AAPL --move -1.2 --catalyst "Trump tariff 25%" \
  --level 168 --liq 18 --next-event "Congress vote"
```

The process must stay alive for 2 hours for the timers to fire.

## Programmatic API

```python
from notifiers.wave_dispatcher import fire_wave

timers = fire_wave(
    symbol="AAPL",
    hl_asset="xyz:AAPL",
    ref_price=170.40,        # used to compute live move at T+15 / T+2hr
    move_pct=-1.2,
    catalyst="Trump tariff 25%",
    level=168.0,
    next_event="Congress vote",
    liquidation_usd_m=18.0,
)
# stage 1 fired immediately; stages 2 and 3 scheduled
# `timers` is a list of threading.Timer; you can `.cancel()` them if needed
```

## Wiring into the live watcher (next step)

The watcher currently uses `format_pn_simple` (long-form). To switch over for the new channel:

1. Set `TELEGRAM_PN_CHANNEL_ID` for the new channel.
2. In `tools/live_watch.py` after the existing `send_alert_if_enabled` call, also call `fire_wave()` with the alert data.
3. Both channels can coexist — the long-form channel keeps the verbose tradecards, the new channel gets the compact 3-stage waves.

## Expected impact (per spec)

| Metric | Long form (current) | Compact (new) |
|---|---|---|
| Scan time | 5-10 s | < 1 s |
| Click rate | ~20% | 40%+ |
| Conversion | Delayed | 2-3 min |
| Trust | Medium (overselling) | High (no BS) |
