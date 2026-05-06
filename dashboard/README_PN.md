# PN Dashboard — Vercel deploy

Static dashboard reading two JSON files:

- `pn_feed.json` — every PN ever fired, ordered most-recent-first, with resolved outcome where the engine has caught up.
- `performance.json` — overall win-rate + per-PN-type + per-ticker tables.

Pages:
- `index.html` — the existing 60-day historical signal report (unchanged).
- `pn.html` — **new** live PN feed + performance dashboard. Default home for the PN service.

## Deploy

```bash
# CLI (one-time):
npm i -g vercel

# From repo root:
cd dashboard
vercel               # first run, links to your account
vercel --prod        # deploy
```

Or via GitHub integration:

1. Push the `feat/compact-pn-channel` branch to `kushagra93/hyperliquid-perps-analyzer`.
2. https://vercel.com/new → Import the repo.
3. **Root Directory:** `dashboard`.
4. Framework: Other.
5. Deploy.

## Updating the data

The publisher walks every PN in the JSONL forward through HL candles and resolves TP/SL/timeout. Run it as a cron on the host that runs the daemons:

```cron
*/15 * * * * cd /home/ubuntu/hyperliquid-perps-analyzer && \
  /home/ubuntu/hyperliquid-perps-analyzer/.venv/bin/python3 tools/publish_dashboard.py && \
  cd /home/ubuntu/hyperliquid-perps-analyzer && \
  git add dashboard/pn_feed.json dashboard/performance.json && \
  git -c user.email=oracle@bot -c user.name=oracle-bot commit -m "data: refresh PN feed" 2>/dev/null && \
  git push origin main 2>/dev/null
```

Vercel auto-deploys on the push.

## What you see

- **Top KPI strip** — total resolved · wins · win rate · avg PnL · total PnL
- **By PN type** table — sentiment / volume_spike / cluster_shift / breakout / recap, each with n / win % / avg PnL / total
- **Top tickers** table — most-active tickers with their win rate
- **Live + history feed** — every PN as a card with the same compact body, plus its resolved outcome badge (`tp1` / `sl` / `timeout` / `unresolved`)

Filter by:
- Type (sentiment / volume / cluster / breakout / recap)
- Outcome (tp1 / sl / timeout / unresolved)
- Ticker (substring match)

## Resolution methodology

- TP1: +1 × ATR(14) past entry
- SL: −1.5 × ATR(14) from entry
- Timeout: 16 × 15-min bars (4 hours)
- Direction inferred from the PN payload (move_pct sign / breakout_dir / title emoji)

Same engine that powers the live PN service, so the dashboard's win rate is a faithful audit of what subscribers got pushed to their phones.
