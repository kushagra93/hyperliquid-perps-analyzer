# core/freshness.py
# ─────────────────────────────────────────────────────────────────
# Data-freshness gate.
#
# Problem: if Hyperliquid returns a cached/frozen payload (same prices
# across consecutive ticks for most tickers), the per-worker stale-
# repeats counter catches it eventually — but only after ≥15 ticks
# per symbol. For a fleet-wide freeze that's wasted work and risks
# alerting on stale data.
#
# This module computes a cross-ticker hash of (symbol, price) tuples
# and flags when the hash repeats across consecutive ticks. Main
# loop can consult `feed_freshness_ok()` to skip a tick entirely.
#
# Env flags:
#   FRESHNESS_ENABLED        default "true" — master switch
#   FRESHNESS_REPEAT_LIMIT   default 3 — tolerate up to N identical ticks
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class FreshnessState:
    last_hash: str = ""
    repeats: int = 0
    total_ticks: int = 0
    skipped_ticks: int = 0


_STATE = FreshnessState()


def _hash_snapshot(data: list) -> str:
    """Hash (symbol, markPx) pairs across all universe entries."""
    if not data or len(data) < 2:
        return ""
    universe = data[0].get("universe", []) or []
    ctxs = data[1] or []
    pairs = []
    for i, asset in enumerate(universe):
        name = asset.get("name", "")
        if i < len(ctxs):
            px = ctxs[i].get("markPx") or ctxs[i].get("midPx") or ""
            pairs.append(f"{name}={px}")
    return hashlib.md5("\n".join(sorted(pairs)).encode()).hexdigest()


def feed_freshness_ok(data: list) -> bool:
    """
    Returns True if the feed looks fresh (prices have moved since last tick
    OR repeats are still within tolerance).

    Call once per tick in the main loop before fanning out to workers.
    """
    if not _env_bool("FRESHNESS_ENABLED", True):
        return True

    limit = _env_int("FRESHNESS_REPEAT_LIMIT", 3)
    h = _hash_snapshot(data)
    _STATE.total_ticks += 1

    if not h:
        # Empty or malformed — let caller decide; don't block
        return True

    if h == _STATE.last_hash:
        _STATE.repeats += 1
        if _STATE.repeats >= limit:
            _STATE.skipped_ticks += 1
            logger.warning(
                f"[Freshness] Feed appears FROZEN — identical price snapshot for "
                f"{_STATE.repeats + 1} consecutive ticks. Skipping this tick. "
                f"(total={_STATE.total_ticks}, skipped={_STATE.skipped_ticks})"
            )
            return False
        logger.info(
            f"[Freshness] Price snapshot unchanged "
            f"({_STATE.repeats + 1}/{limit + 1} allowed before skip)."
        )
        return True

    # Fresh
    _STATE.last_hash = h
    _STATE.repeats = 0
    return True


def get_stats() -> dict:
    """Expose counters for logging / healthchecks."""
    return {
        "total_ticks": _STATE.total_ticks,
        "skipped_ticks": _STATE.skipped_ticks,
        "current_repeat_streak": _STATE.repeats,
    }
