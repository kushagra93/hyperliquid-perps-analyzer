#!/usr/bin/env python3
"""
analysis/historical_report.py
────────────────────────────────────────────────────────────────────
Reconstructs every threshold-breach signal that *would* have fired
since DAYS days ago, scores each with the same AI rubric used in
alerts, and resolves the outcome (win/loss vs ATR-based TP1/SL).

Why "reconstruct" rather than read the Telegram channel:
  - Bot API does not return the bot's own messages.
  - User-account scraping (telethon) requires phone/2FA login.
  - Reconstruction is reproducible, deterministic, and captures
    every signal that ever should have been sent — including the
    ones the deployed service missed because the Telegram notifier
    isn't on the running branch yet.

For each detected breach we compute:
  • condition_id        (C1/C2 from price direction; OI history isn't
                         in candleSnapshot, so we tag oi=unknown)
  • AI score 0..100     (matching the Telegram alert scoring rubric)
  • stars 1..5
  • outcome             (TP1 hit, SL hit, timeout)
  • realized_pnl_pct    over the resolution window

Outputs:
  analysis/report.json     — full structured data, fed to the dashboard
  analysis/report.md       — human-readable summary
  + Telegram summary       — when --telegram is passed
  + per-condition + per-ticker + overall win rates
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.tickers import TICKERS

logger = logging.getLogger(__name__)

HL = "https://api.hyperliquid.xyz/info"
IST = timezone(timedelta(hours=5, minutes=30))

# Mirrors the rubric in the Telegram alert scorer
MEGA = {"NVDA","TSLA","AAPL","MSFT","META","GOOGL","AMZN"}
MEME = {"MSTR","COIN","HOOD","PLTR","AMD"}


def _candles(coin: str, interval: str, lb_ms: int) -> list[dict]:
    now = int(time.time() * 1000)
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": now - lb_ms, "endTime": now,
    }}
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", HL,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=20,
    )
    try:
        return json.loads(r.stdout) or []
    except Exception:
        return []


def _atr(candles: list, n: int = 14) -> float:
    if len(candles) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["h"]); l = float(candles[i]["l"])
        pc = float(candles[i - 1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


@dataclass
class Signal:
    ts_ms: int
    when_ist: str
    ticker: str
    full_name: str
    direction: str          # "up" / "down"
    condition_id: str       # C1 (up) / C2 (down)  (OI unknown in this scan)
    move_pct: float
    threshold_pct: float
    price: float
    atr: float
    score: int              # 0..100
    stars: int              # 1..5
    tier: str               # mega / meme / other
    # outcome (filled in by _resolve)
    outcome: str = "open"   # tp1 / sl / timeout / open
    pnl_pct: float | None = None
    bars_to_resolution: int | None = None
    exit_price: float | None = None


def _score(direction: str, ticker: str, move_pct: float) -> int:
    s = 30
    # Direction strength → confidence proxy
    s += min(int(abs(move_pct) * 8), 25)
    # Tier
    if ticker in MEGA: s += 10
    elif ticker in MEME: s += 8
    # Heuristic news-presence boost: mega + larger move likely has catalyst
    if ticker in MEGA and abs(move_pct) > 3.0: s += 12
    elif abs(move_pct) > 5.0: s += 8
    # Light technical confluence proxy: trend-aligned moves get bonus
    s += 5 if abs(move_pct) >= 3.0 else 0
    return min(100, s)


def _stars(score: int) -> int:
    if score >= 88: return 5
    if score >= 72: return 4
    if score >= 58: return 3
    if score >= 45: return 2
    return 1


def _detect_breaches(sym: str, cfg: dict, candles: list) -> list[Signal]:
    th = cfg["price_change_threshold_pct"]
    out: list[Signal] = []
    if len(candles) < 20:
        return out
    full = cfg.get("full_name", sym)
    tier = "mega" if sym in MEGA else ("meme" if sym in MEME else "other")
    a14 = _atr(candles, 14) or 0.0
    for i in range(1, len(candles)):
        prev_close = float(candles[i - 1]["c"])
        cur_close = float(candles[i]["c"])
        if prev_close <= 0: continue
        m = (cur_close - prev_close) / prev_close * 100
        if abs(m) < th:
            continue
        direction = "up" if m > 0 else "down"
        cid = "C1" if direction == "up" else "C2"
        score = _score(direction, sym, m)
        ts = int(candles[i]["t"])
        out.append(Signal(
            ts_ms=ts,
            when_ist=datetime.fromtimestamp(ts / 1000, tz=IST).isoformat(timespec="minutes"),
            ticker=sym, full_name=full,
            direction=direction, condition_id=cid,
            move_pct=round(m, 2), threshold_pct=th,
            price=cur_close, atr=round(a14, 4),
            score=score, stars=_stars(score), tier=tier,
        ))
    return out


def _resolve(sig: Signal, candles: list, max_bars: int = 24) -> None:
    """Walk forward up to max_bars and resolve TP1 / SL / timeout."""
    if sig.atr <= 0:
        sig.outcome = "no_atr"
        return
    # Find the bar at sig.ts_ms
    start_idx = None
    for i, c in enumerate(candles):
        if int(c["t"]) == sig.ts_ms:
            start_idx = i
            break
    if start_idx is None or start_idx + 1 >= len(candles):
        return
    entry = sig.price
    if sig.direction == "up":
        sl = entry - 1.5 * sig.atr
        tp = entry + 2.0 * sig.atr
    else:
        sl = entry + 1.5 * sig.atr
        tp = entry - 2.0 * sig.atr
    for j in range(start_idx + 1, min(start_idx + 1 + max_bars, len(candles))):
        c = candles[j]
        hi = float(c["h"]); lo = float(c["l"])
        bars = j - start_idx
        if sig.direction == "up":
            if lo <= sl:
                sig.outcome = "sl"; sig.exit_price = sl
                sig.pnl_pct = round((sl - entry) / entry * 100, 2)
                sig.bars_to_resolution = bars; return
            if hi >= tp:
                sig.outcome = "tp1"; sig.exit_price = tp
                sig.pnl_pct = round((tp - entry) / entry * 100, 2)
                sig.bars_to_resolution = bars; return
        else:
            if hi >= sl:
                sig.outcome = "sl"; sig.exit_price = sl
                sig.pnl_pct = round((entry - sl) / entry * 100, 2)
                sig.bars_to_resolution = bars; return
            if lo <= tp:
                sig.outcome = "tp1"; sig.exit_price = tp
                sig.pnl_pct = round((entry - tp) / entry * 100, 2)
                sig.bars_to_resolution = bars; return
    # Timeout — mark to last close
    last = candles[min(start_idx + max_bars, len(candles) - 1)]
    exit_p = float(last["c"])
    pnl = (exit_p - entry) / entry * 100 if sig.direction == "up" else (entry - exit_p) / entry * 100
    sig.outcome = "timeout"; sig.exit_price = exit_p
    sig.pnl_pct = round(pnl, 2)
    sig.bars_to_resolution = max_bars


def _load_pn_filter() -> tuple[list[str], dict]:
    """Read config/pn_filter.json if present; return (filter_names, raw_cfg)."""
    p = ROOT / "config" / "pn_filter.json"
    if not p.exists():
        return [], {}
    try:
        cfg = json.loads(p.read_text())
        return list(cfg.get("filter") or []), cfg
    except Exception:
        return [], {}


def _apply_pn_filter_to_dicts(signal_dicts: list[dict]) -> dict:
    """
    Stamps pn_eligible / pn_today on each signal dict using the
    persisted filter. Returns a metadata dict describing the filter.
    """
    filter_names, cfg = _load_pn_filter()
    if not filter_names:
        return {"applied": False}
    from analysis.pn_optimizer import FILTERS, _daily_cap

    eligible = [s for s in signal_dicts if all(FILTERS[f](s, signal_dicts) for f in filter_names)]
    pn = _daily_cap(eligible)
    pn_keys = {(s["ticker"], s["ts_ms"]) for s in pn}
    elig_keys = {(s["ticker"], s["ts_ms"]) for s in eligible}
    for s in signal_dicts:
        s["pn_eligible"] = (s["ticker"], s["ts_ms"]) in elig_keys
        s["pn_today"] = (s["ticker"], s["ts_ms"]) in pn_keys
    return {
        "applied": True,
        "filter": filter_names,
        "config": cfg,
        "eligible": len(eligible),
        "pn_count": len(pn),
    }


def aggregate(signals: list[Signal]) -> dict:
    closed = [s for s in signals if s.outcome in ("tp1", "sl", "timeout")]
    wins = [s for s in closed if (s.pnl_pct or 0) > 0]
    by_cond: dict = {}
    by_ticker: dict = {}
    for s in closed:
        for key, bucket in (("condition", s.condition_id), ("ticker", s.ticker)):
            target = by_cond if key == "condition" else by_ticker
            target.setdefault(bucket, {"n": 0, "wins": 0, "pnl_sum": 0.0})
            target[bucket]["n"] += 1
            if (s.pnl_pct or 0) > 0:
                target[bucket]["wins"] += 1
            target[bucket]["pnl_sum"] += (s.pnl_pct or 0.0)
    def _wr(d):
        return {k: {"n": v["n"], "win_rate_pct": round(v["wins"] / v["n"] * 100, 1) if v["n"] else 0,
                     "avg_pnl_pct": round(v["pnl_sum"] / v["n"], 2) if v["n"] else 0}
                for k, v in d.items()}
    return {
        "total_signals": len(signals),
        "closed": len(closed),
        "open": len(signals) - len(closed),
        "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "avg_pnl_pct": round(sum((s.pnl_pct or 0) for s in closed) / len(closed), 2) if closed else 0.0,
        "by_condition": _wr(by_cond),
        "by_ticker": _wr(by_ticker),
    }


def run(days: int) -> dict:
    print(f"Scanning {days}d of 15m candles for {len(TICKERS)} tickers...", flush=True)
    all_sigs: list[Signal] = []
    for sym, cfg in TICKERS.items():
        coin = cfg["hl_asset"]
        cs = _candles(coin, "15m", days * 24 * 3600 * 1000)
        sigs = _detect_breaches(sym, cfg, cs)
        for s in sigs:
            _resolve(s, cs)
        all_sigs.extend(sigs)
        print(f"  {sym}: {len(sigs)} signals", flush=True)

    all_sigs.sort(key=lambda s: -s.ts_ms)
    agg = aggregate(all_sigs)

    sig_dicts = [asdict(s) for s in all_sigs]
    pn_meta = _apply_pn_filter_to_dicts(sig_dicts)

    # Aggregate of PN-only subset
    pn_only = [s for s in sig_dicts if s.get("pn_today")]
    pn_agg = None
    if pn_only:
        closed = [s for s in pn_only if s.get("outcome") in ("tp1", "sl", "timeout")]
        wins = [s for s in closed if (s.get("pnl_pct") or 0) > 0]
        pn_agg = {
            "n": len(pn_only),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "avg_pnl_pct": round(
                sum((s.get("pnl_pct") or 0) for s in closed) / len(closed), 2
            ) if closed else 0.0,
        }

    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scan_days": days,
        "tickers_scanned": list(TICKERS.keys()),
        "aggregate": agg,
        "pn_filter": pn_meta,
        "pn_aggregate": pn_agg,
        "signals": sig_dicts,
    }


def render_markdown(report: dict) -> str:
    a = report["aggregate"]
    lines = [
        f"# Historical signal report — {report['scan_days']}d window",
        f"_Generated {report['as_of']}_",
        "",
        f"**Scanned tickers:** {len(report['tickers_scanned'])}  ·  "
        f"**Signals detected:** {a['total_signals']}  ·  "
        f"**Closed:** {a['closed']}  ·  "
        f"**Open/timeout:** {a['open']}",
        "",
        f"## Headline win rate",
        f"- Wins (TP1 hit OR positive timeout PnL): **{a['wins']} / {a['closed']}** = **{a['win_rate_pct']}%**",
        f"- Average realised PnL per signal: **{a['avg_pnl_pct']:+}%** (resolution window 24×15m bars / 6h)",
        "",
        f"## By condition",
        "| Cond | n | win % | avg PnL % |",
        "|---|---:|---:|---:|",
    ]
    for k in sorted(a["by_condition"]):
        v = a["by_condition"][k]
        lines.append(f"| {k} | {v['n']} | {v['win_rate_pct']} | {v['avg_pnl_pct']:+} |")
    lines += ["", "## By ticker", "| Ticker | n | win % | avg PnL % |", "|---|---:|---:|---:|"]
    for k in sorted(a["by_ticker"], key=lambda x: -a["by_ticker"][x]["n"]):
        v = a["by_ticker"][k]
        lines.append(f"| {k} | {v['n']} | {v['win_rate_pct']} | {v['avg_pnl_pct']:+} |")
    lines += ["", "## All signals (most recent first)",
              "| When (IST) | Ticker | Cond | Move% | Score | ⭐ | Outcome | PnL % | Bars |",
              "|---|---|---|---:|---:|---:|---|---:|---:|"]
    for s in report["signals"][:200]:
        pnl = s.get("pnl_pct"); bars = s.get("bars_to_resolution")
        lines.append(
            f"| {s['when_ist'].replace('T',' ')[:16]} | {s['ticker']} | {s['condition_id']} | "
            f"{s['move_pct']:+.2f} | {s['score']} | {s['stars']} | {s['outcome']} | "
            f"{(f'{pnl:+.2f}' if pnl is not None else '—')} | {bars or '—'} |"
        )
    return "\n".join(lines)


def render_telegram(report: dict) -> list[str]:
    a = report["aggregate"]
    head = (
        f"<b>📊 HISTORICAL SIGNAL REPORT — {report['scan_days']}d</b>\n"
        f"<i>{report['as_of']}</i>\n\n"
        f"<b>Total signals:</b> {a['total_signals']}  ·  "
        f"<b>closed:</b> {a['closed']}  ·  <b>open:</b> {a['open']}\n"
        f"<b>Win rate:</b> {a['wins']}/{a['closed']} = <b>{a['win_rate_pct']}%</b>  ·  "
        f"avg PnL <b>{a['avg_pnl_pct']:+}%</b>  (TP=+2×ATR, SL=−1.5×ATR, 6h timeout)\n\n"
        f"<b>By condition:</b>\n"
    )
    for k in sorted(a["by_condition"]):
        v = a["by_condition"][k]
        head += f"  • <b>{k}</b> — n={v['n']}  win {v['win_rate_pct']}%  avg {v['avg_pnl_pct']:+}%\n"
    head += "\n<b>By ticker (most active first):</b>\n"
    for k in sorted(a["by_ticker"], key=lambda x: -a["by_ticker"][x]["n"])[:10]:
        v = a["by_ticker"][k]
        head += f"  • <b>{k}</b> — n={v['n']}  win {v['win_rate_pct']}%  avg {v['avg_pnl_pct']:+}%\n"

    # Per-signal cards (top 12 by score, then anything else above 4 stars)
    cards: list[str] = []
    top = sorted(report["signals"], key=lambda s: -s["score"])[:12]
    for s in top:
        outcome_emoji = {"tp1": "🟢 TP1", "sl": "🔴 SL", "timeout": "⚪ timeout", "no_atr": "—", "open": "⏳"}[s["outcome"]]
        pnl = s.get("pnl_pct")
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "n/a"
        when = s["when_ist"].replace("T", " ")[:16]
        cards.append(
            f"<b>{s['ticker']}</b> {when} IST · "
            f"{s['condition_id']} {s['move_pct']:+.2f}% · "
            f"{'⭐'*s['stars']} <code>{s['score']}/100</code> → {outcome_emoji} ({pnl_str})"
        )

    second = "<b>🏆 Top 12 by AI score</b>\n" + "\n".join(cards)
    return [head, second]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--out-dir", default="analysis")
    p.add_argument("--telegram", action="store_true")
    args = p.parse_args()

    report = run(args.days)
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    (out_dir / "report.md").write_text(render_markdown(report))
    print(f"\nWrote {out_dir / 'report.json'} and {out_dir / 'report.md'}")
    print(f"Total signals: {report['aggregate']['total_signals']}  "
          f"win rate: {report['aggregate']['win_rate_pct']}%")

    # Also publish a copy under dashboard/public/ for the static frontend
    pub = ROOT / "dashboard" / "public" / "report.json"
    pub.parent.mkdir(parents=True, exist_ok=True)
    pub.write_text(json.dumps(report))

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN",
            "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        for msg in render_telegram(report):
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "--data-urlencode", f"chat_id={chat}",
                "--data-urlencode", f"text={msg}",
                "--data-urlencode", "parse_mode=HTML",
                "--data-urlencode", "disable_web_page_preview=true",
            ], capture_output=True, text=True, timeout=15)
            time.sleep(0.4)


if __name__ == "__main__":
    main()
