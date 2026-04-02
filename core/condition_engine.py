# core/condition_engine.py
# ─────────────────────────────────────────────────────────────────
# Evaluates the four conditions (C1–C4) from price and OI direction,
# then applies causality rules to decide whether to fire an alert.
# ─────────────────────────────────────────────────────────────────

import logging

logger = logging.getLogger(__name__)

# Condition definitions
CONDITIONS = {
    "C1": {
        "label": "Strong bull",
        "description": "Price up + OI up — new longs entering, conviction move",
        "price_dir": "up",
        "oi_dir": "up",
        "always_alert": True,
    },
    "C2": {
        "label": "Strong bear",
        "description": "Price down + OI up — new shorts entering, conviction sell",
        "price_dir": "down",
        "oi_dir": "up",
        "always_alert": True,
    },
    "C3": {
        "label": "Weak fall",
        "description": "Price down + OI down — longs exiting, no fresh conviction",
        "price_dir": "down",
        "oi_dir": "down",
        "always_alert": False,   # only alert if news found
    },
    "C4": {
        "label": "Weak rally",
        "description": "Price up + OI down — shorts exiting, no fresh conviction",
        "price_dir": "up",
        "oi_dir": "down",
        "always_alert": False,   # only alert if news found
    },
}


def classify_price_direction(price_change_pct: float) -> str:
    if price_change_pct > 0:
        return "up"
    elif price_change_pct < 0:
        return "down"
    return "flat"


def evaluate_condition(price_trigger: dict, oi_snapshot: dict) -> dict | None:
    """
    Matches price and OI direction to one of C1–C4.

    Returns:
    {
        "condition_id": "C1" | "C2" | "C3" | "C4",
        "label": str,
        "description": str,
        "always_alert": bool,
        "price_change_pct": float,
        "oi_change_pct": float,
    }
    or None if no condition matched (e.g. price up + OI flat).
    """
    price_dir = classify_price_direction(price_trigger["price_change_pct"])
    oi_dir = oi_snapshot["direction"]

    if price_dir == "flat" or oi_dir == "flat":
        logger.info("[ConditionEngine] No clear condition — price or OI direction is flat.")
        return None

    for cid, cdef in CONDITIONS.items():
        if cdef["price_dir"] == price_dir and cdef["oi_dir"] == oi_dir:
            result = {
                "condition_id": cid,
                "label": cdef["label"],
                "description": cdef["description"],
                "always_alert": cdef["always_alert"],
                "price_change_pct": price_trigger["price_change_pct"],
                "oi_change_pct": oi_snapshot["oi_change_pct"],
            }
            logger.info(f"[ConditionEngine] Matched {cid}: {cdef['label']}")
            return result

    logger.info(f"[ConditionEngine] No matching condition for price_dir={price_dir}, oi_dir={oi_dir}")
    return None


def should_alert(condition: dict, news_report: dict) -> bool:
    """
    Applies causality alert rules:
    - C1 or C2: always alert
    - C3: alert only if news found
    - C4: alert only if news found

    news_report["has_news"] must be True for news-gated conditions.
    """
    if condition["always_alert"]:
        logger.info(f"[ConditionEngine] {condition['condition_id']} — always alert. Firing.")
        return True

    has_news = news_report.get("has_news", False)
    if has_news:
        logger.info(
            f"[ConditionEngine] {condition['condition_id']} — news found. Firing."
        )
        return True

    logger.info(
        f"[ConditionEngine] {condition['condition_id']} — no news. Suppressing alert."
    )
    return False
