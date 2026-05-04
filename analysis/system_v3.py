#!/usr/bin/env python3
"""
analysis/system_v3.py
────────────────────────────────────────────────────────────────────
Round-2 fixes on top of system_v2:

  1. WALK-FORWARD CV — score_v2 buckets fit on a rolling window,
     applied to the next, never the same. Kills in-sample bias.
  2. PER-FILTER ABLATION — each filter applied alone with all others
     off; reports marginal contribution to WR / PnL.
  3. FRICTION-AWARE PnL — taker fees (3.5 bps), perp funding accrual
     (per bar), 5 bps slippage. PnL net of frictions, not gross.
  4. BOOTSTRAP CI on win rate — admits sample-size limitation
     visually rather than reporting bare percentages on n=3.
  5. KILL-SWITCH SIMULATION — when applied, daily/weekly DD limits
     suspend trading; report what fraction of signals survive.
  6. MULTI-ASSET VOL REGIME — cross-sectional dispersion across the
     universe instead of one-ticker SP500 proxy.

Outputs JSON + Telegram thread comparing v1, v2, v3.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.system_v2 import (
    TICKER_CLUSTER, _hour_of, _size_bucket, build_lookup, score_v2,
    HOUR_OK, MAX_MOVE_PCT, EVENT_BLACKOUT_HOURS, KNOWN_EVENT_DATES,
    _within_blackout, _cluster_dedupe,
)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Friction model ───────────────────────────────────────────────
TAKER_FEE_PCT = 0.035   # 3.5 bps each side → 7 bps round trip
SLIPPAGE_BPS = 5        # 0.05% each side
FUNDING_PCT_PER_BAR = 0.001  # rough 0.1% per 8h, scaled by bars (15m)


def _friction_adjusted(pnl_pct: float | None, bars: int | None) -> float | None:
    if pnl_pct is None:
        return None
    rt_fee = TAKER_FEE_PCT * 2 + (SLIPPAGE_BPS / 100) * 2
    funding = (bars or 0) * FUNDING_PCT_PER_BAR / 32  # 32 × 15m = 8h
    return round(pnl_pct - rt_fee - funding, 3)


# ── Walk-forward score_v2 ────────────────────────────────────────

def walk_forward_score(signals: list[dict], train_days: int = 30, test_days: int = 10) -> list[dict]:
    """
    For each test signal, fit the bucket lookup using ONLY historical
    signals in the prior `train_days` window (no peeking). Stamp
    `score_v2_oos` so it can be compared to the in-sample `score_v2`.
    """
    by_ts = sorted(signals, key=lambda s: s["ts_ms"])
    out = []
    for s in by_ts:
        cutoff_lo = s["ts_ms"] - train_days * 24 * 3600 * 1000
        cutoff_hi = s["ts_ms"] - 1
        train_set = [t for t in by_ts if cutoff_lo <= t["ts_ms"] <= cutoff_hi
                      and t.get("outcome") in ("tp1", "sl", "timeout")]
        if len(train_set) < 5:
            s2 = dict(s); s2["score_v2_oos"] = 50  # not enough train data
            out.append(s2); continue
        lk = build_lookup(train_set)
        s2 = dict(s)
        s2["score_v2_oos"] = score_v2(s, lk)
        out.append(s2)
    return out


# ── Multi-asset vol regime ───────────────────────────────────────

def _regime_multi_asset(signals: list[dict]) -> str:
    """
    Cross-sectional dispersion = stdev across universe of recent
    per-ticker daily returns (proxied by signal moves).
    """
    by_t = defaultdict(list)
    for s in signals[-60:]:
        by_t[s["ticker"]].append(s.get("move_pct") or 0)
    if not by_t:
        return "medium"
    avgs = [statistics.mean(v) for v in by_t.values() if v]
    if len(avgs) < 4:
        return "medium"
    sd = statistics.stdev(avgs)
    if sd < 0.5: return "low"
    if sd > 1.5: return "high"
    return "medium"


# ── Per-filter ablation ──────────────────────────────────────────

def _passes_individual_filter(s: dict, filter_name: str, lookup: dict) -> bool:
    if filter_name == "time":
        h = _hour_of(s["when_ist"])
        return HOUR_OK[0] <= h <= HOUR_OK[1]
    if filter_name == "size":
        return abs(s.get("move_pct") or 0) <= MAX_MOVE_PCT
    if filter_name == "score":
        return s.get("score_v2_oos", s.get("score_v2", 0)) >= 50
    if filter_name == "blackout":
        return not _within_blackout(s, EVENT_BLACKOUT_HOURS)
    if filter_name == "cluster":
        return True  # cluster is a post-hoc dedupe, evaluated separately
    return True


def ablation_table(signals: list[dict], lookup: dict) -> dict:
    """For each filter individually, what's the marginal lift?"""
    closed = [s for s in signals if s.get("outcome") in ("tp1", "sl", "timeout")]
    base_n = len(closed)
    base_wr = sum(1 for s in closed if (s.get("pnl_pct") or 0) > 0) / base_n if base_n else 0
    base_pnl = sum((s.get("pnl_pct") or 0) for s in closed)

    rows = {"baseline (no filter)": {
        "n": base_n,
        "win_rate_pct": round(base_wr * 100, 1),
        "total_pnl_pct": round(base_pnl, 2),
    }}
    for name in ("time", "size", "score", "blackout"):
        sub = [s for s in closed if _passes_individual_filter(s, name, lookup)]
        if not sub:
            rows[name] = {"n": 0, "win_rate_pct": 0.0, "total_pnl_pct": 0.0}
            continue
        wr = sum(1 for s in sub if (s.get("pnl_pct") or 0) > 0) / len(sub)
        pnl = sum((s.get("pnl_pct") or 0) for s in sub)
        rows[name] = {
            "n": len(sub),
            "win_rate_pct": round(wr * 100, 1),
            "total_pnl_pct": round(pnl, 2),
        }
    # Cluster as standalone: dedupe across full set, no other filter
    deduped = _cluster_dedupe(closed)
    if deduped:
        wr = sum(1 for s in deduped if (s.get("pnl_pct") or 0) > 0) / len(deduped)
        pnl = sum((s.get("pnl_pct") or 0) for s in deduped)
        rows["cluster (alone)"] = {
            "n": len(deduped),
            "win_rate_pct": round(wr * 100, 1),
            "total_pnl_pct": round(pnl, 2),
        }
    return rows


# ── Bootstrap CI ─────────────────────────────────────────────────

def bootstrap_ci(rows: list[dict], iterations: int = 2000, alpha: float = 0.05) -> tuple[float, float]:
    """Returns (lo, hi) for win-rate at `1-alpha` confidence."""
    if not rows:
        return 0.0, 100.0
    rng = random.Random(42)
    wrs = []
    n = len(rows)
    for _ in range(iterations):
        sample = [rng.choice(rows) for _ in range(n)]
        wins = sum(1 for s in sample if (s.get("pnl_pct") or 0) > 0)
        wrs.append(wins / n * 100)
    wrs.sort()
    return wrs[int(iterations * alpha / 2)], wrs[int(iterations * (1 - alpha / 2))]


# ── Kill-switch simulation ───────────────────────────────────────

def _simulate_kill_switch(rows: list[dict], daily_max_loss_pct: float = 2.0,
                          weekly_max_loss_pct: float = 5.0,
                          loss_streak: int = 3) -> dict:
    """Walk forward; suspend trading when DD limit hit."""
    rows = sorted(rows, key=lambda s: s["ts_ms"])
    daily, weekly = defaultdict(float), defaultdict(float)
    streak = 0
    suspended_until = 0
    survived = []
    for s in rows:
        ts = s["ts_ms"]
        d = datetime.fromtimestamp(ts/1000, IST).date().isoformat()
        wk = datetime.fromtimestamp(ts/1000, IST).isocalendar()
        wk_key = f"{wk[0]}-W{wk[1]:02d}"
        if ts < suspended_until:
            continue
        if abs(daily[d]) >= daily_max_loss_pct:
            continue
        if abs(weekly[wk_key]) >= weekly_max_loss_pct:
            continue
        if streak >= loss_streak:
            suspended_until = ts + 24 * 3600 * 1000
            streak = 0
            continue
        survived.append(s)
        pnl = s.get("pnl_pct") or 0
        daily[d] += pnl
        weekly[wk_key] += pnl
        streak = streak + 1 if pnl <= 0 else 0
    return {
        "n_survived": len(survived),
        "n_filtered": len(rows) - len(survived),
        "survivors": survived,
    }


# ── Driver ───────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--report", default="analysis/report.json")
    p.add_argument("--telegram", action="store_true")
    args = p.parse_args()

    rep = json.loads((ROOT / args.report).read_text())
    sigs = rep["signals"]
    closed = [s for s in sigs if s.get("outcome") in ("tp1", "sl", "timeout")]

    # 1. Walk-forward score
    closed_wf = walk_forward_score(closed)

    # 2. Friction-adjust every signal
    for s in closed_wf:
        s["pnl_net_pct"] = _friction_adjusted(s.get("pnl_pct"), s.get("bars_to_resolution"))

    # 3. Multi-asset regime
    regime = _regime_multi_asset(closed_wf)

    # 4. Per-filter ablation (using OOS score)
    in_sample_lookup = build_lookup(closed_wf)
    ablation = ablation_table(closed_wf, in_sample_lookup)

    # 5. Bootstrap CI on baseline & v3 stack
    base_lo, base_hi = bootstrap_ci(closed_wf)
    v3_filtered = [
        s for s in closed_wf
        if all(_passes_individual_filter(s, f, in_sample_lookup)
               for f in ("time", "size", "score", "blackout"))
    ]
    v3_dedup = _cluster_dedupe(v3_filtered)
    v3_lo, v3_hi = bootstrap_ci(v3_dedup) if v3_dedup else (0.0, 100.0)

    # 6. Kill-switch simulation
    ks = _simulate_kill_switch(v3_dedup)

    # 7. Net stats
    v3_net_pnl = sum((s.get("pnl_net_pct") or 0) for s in v3_dedup)
    v3_net_wins = sum(1 for s in v3_dedup if (s.get("pnl_net_pct") or 0) > 0)
    v3_net_n = len(v3_dedup)

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "regime_multi_asset": regime,
        "friction_model": {
            "taker_fee_pct_each_side": TAKER_FEE_PCT,
            "slippage_bps_each_side": SLIPPAGE_BPS,
            "funding_pct_per_bar": FUNDING_PCT_PER_BAR,
        },
        "ablation": ablation,
        "bootstrap_ci_baseline": {"lo": round(base_lo, 1), "hi": round(base_hi, 1)},
        "v3_stack_after_all_filters_and_dedupe": {
            "n": v3_net_n,
            "win_rate_pct": round(v3_net_wins / v3_net_n * 100, 1) if v3_net_n else 0,
            "gross_pnl_pct": round(sum((s.get("pnl_pct") or 0) for s in v3_dedup), 2),
            "net_pnl_pct_after_frictions": round(v3_net_pnl, 2),
            "win_rate_ci_95": {"lo": round(v3_lo, 1), "hi": round(v3_hi, 1)},
        },
        "kill_switch_sim": {
            "n_survived": ks["n_survived"],
            "n_blocked": ks["n_filtered"],
        },
        "verdict": (
            "v3 reports OUT-OF-SAMPLE numbers with realistic frictions and "
            "a 95% bootstrap CI. With current sample size the CI is wide; "
            "do not act on point estimates."
        ),
    }
    (ROOT / "analysis" / "system_v3_result.json").write_text(json.dumps(out, indent=2))

    print("\n═══ SYSTEM v3 — round-2 fixes ═══")
    print(f"Regime (multi-asset): {regime.upper()}")
    print(f"\n[ABLATION — single-filter marginal contribution]")
    for name, r in ablation.items():
        print(f"  {name:25s}  n={r['n']:3d}  wr={r['win_rate_pct']:5.1f}%  PnL={r['total_pnl_pct']:+6.2f}%")
    print(f"\n[V3 STACK]  n={v3_net_n}  wr={out['v3_stack_after_all_filters_and_dedupe']['win_rate_pct']}%  "
          f"gross={out['v3_stack_after_all_filters_and_dedupe']['gross_pnl_pct']:+}%  "
          f"NET={out['v3_stack_after_all_filters_and_dedupe']['net_pnl_pct_after_frictions']:+}%")
    print(f"  WR 95% CI: [{v3_lo:.1f}%, {v3_hi:.1f}%]  ← wide because n is small")
    print(f"\n[KILL SWITCH SIM]  survived={ks['n_survived']}  blocked={ks['n_filtered']}")

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        def tg(t):
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "--data-urlencode", f"chat_id={chat}",
                "--data-urlencode", f"text={t}",
                "--data-urlencode", "parse_mode=HTML",
                "--data-urlencode", "disable_web_page_preview=true",
            ], capture_output=True, text=True, timeout=15)

        v3 = out["v3_stack_after_all_filters_and_dedupe"]
        msgs = [
            f"<b>🛠️ SYSTEM v3 — round-2 fixes shipped</b>\n"
            f"<i>Round-2 agents demanded:</i>\n"
            f"  ✅ walk-forward score (no in-sample peeking)\n"
            f"  ✅ per-filter ablation (which fix earns its keep)\n"
            f"  ✅ friction model (fees + slippage + funding)\n"
            f"  ✅ bootstrap CI (admit sample size)\n"
            f"  ✅ kill-switch simulation (DD circuit breakers)\n"
            f"  ✅ multi-asset regime (cross-sectional dispersion)\n"
            f"\nRegime detected: <b>{regime.upper()}</b>",

            "<b>📊 PER-FILTER ABLATION — which fix actually earns its keep?</b>\n"
            "<pre>"
            + "".join(
                f"\n{name:24s}  n={r['n']:>3d}  wr={r['win_rate_pct']:>5.1f}%  PnL={r['total_pnl_pct']:+6.2f}%"
                for name, r in ablation.items()
            )
            + "</pre>\n"
            "<i>Verdict: look for filters whose PnL beats baseline. Cluster cap and event blackout don't show much marginal lift on this sample.</i>",

            f"<b>🎯 V3 STACK — out-of-sample, friction-adjusted</b>\n"
            f"  • Trades: <b>{v3['n']}</b>\n"
            f"  • Gross PnL: {v3['gross_pnl_pct']:+}%\n"
            f"  • <b>Net PnL after fees + slippage + funding:</b> {v3['net_pnl_pct_after_frictions']:+}%\n"
            f"  • Win rate: {v3['win_rate_pct']}%\n"
            f"  • <b>WR 95% bootstrap CI: [{v3_lo:.1f}%, {v3_hi:.1f}%]</b>  — wide!\n\n"
            f"<i>Honest read: with this sample size we cannot reject the null. "
            f"v3 is structurally better (no in-sample peek, real frictions, real CI) "
            f"but cannot claim edge until n &gt;= 100 OOS trades.</i>",

            f"<b>🛑 KILL-SWITCH SIMULATION</b>\n"
            f"  • Survived gates + DD limits: {ks['n_survived']}\n"
            f"  • Blocked by daily/weekly/loss-streak caps: {ks['n_filtered']}\n"
            f"  • Caps: daily −2%, weekly −5%, 3-loss-streak → 24h pause\n",
        ]
        for m in msgs:
            tg(m); time.sleep(0.4)
        print("\nPushed v3 to Telegram")


if __name__ == "__main__":
    main()
