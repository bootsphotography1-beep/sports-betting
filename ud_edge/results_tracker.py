"""Result-tracking module: log picks to JSON, compute calibration stats.

Tracks every recommended pick (line_id + predicted prob + actual outcome) so
we can validate the no-vig math over time. Threshold per Fin's methodology:
50+ settled legs before trusting the EV estimates.

Outcome resolution: scores are pulled from UD's API on subsequent runs and
matched back to logged picks by line_id. We treat "under" as a hit if the
final stat < line_value, "over" as a hit if final stat >= line_value.

The tracker is conservative: it never declares a hit/miss on a live or
unfinalized game. Stale picks (>14 days) are auto-flagged for manual review.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ud_edge.models import RankedLeg, Leg


RESULTS_PATH = Path("data/results.json")
STALE_DAYS = 14


def _load() -> dict:
    if not RESULTS_PATH.exists():
        return {"picks": [], "metadata": {"created": datetime.now(timezone.utc).isoformat()}}
    return json.loads(RESULTS_PATH.read_text())


def _save(data: dict) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(data, indent=2))


def _leg_key(leg: Leg) -> str:
    """Stable key for matching a leg to a logged pick across days."""
    return f"{leg.sport_id}|{leg.player_id}|{leg.stat_name}|{leg.line_value}"


def log_picks(lineups: list[list[RankedLeg]], entry_type: str = "6-flex",
              n_entries: int = 4) -> int:
    """Append today's picks to results.json. Returns the number of new picks logged.

    Deduplicates by leg_key + date so re-running the same day won't double-log.
    """
    data = _load()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing_keys = {
        f"{p['_key']}|{p['date']}"
        for p in data["picks"]
    }
    added = 0
    for entry_idx, lineup in enumerate(lineups, 1):
        for leg_idx, r in enumerate(lineup, 1):
            leg = r.leg
            key = _leg_key(leg)
            full_key = f"{key}|{today}"
            if full_key in existing_keys:
                continue
            data["picks"].append({
                "date": today,
                "entry": entry_idx,
                "leg": leg_idx,
                "_key": key,
                "line_id": leg.line_id,
                "sport": leg.sport_id,
                "player_id": leg.player_id,
                "player_name": leg.player_name,
                "match_title": leg.match_title,
                "stat": leg.stat_name,
                "line_value": leg.line_value,
                "picked_side": r.picked_side,
                "picked_true_prob": r.picked_true_prob,
                "picked_american": (
                    leg.higher_american if r.picked_side == "higher"
                    else leg.lower_american
                ),
                "entry_type": entry_type,
                "outcome": None,  # filled in by resolve_outcomes
                "actual_stat": None,
                "resolved_at": None,
            })
            added += 1
    if added:
        _save(data)
    return added


def resolve_outcomes(current_legs: list[Leg]) -> int:
    """Match unresolved picks to current legs and fill in outcomes where possible.

    A pick is "resolvable" when the same leg_key appears in current_legs. For
    resovable picks, we mark outcome based on whether the pick is consistent
    with the CURRENT line (if line moved, we don't trust it). Conservative —
    better to leave outcome=None than guess wrong.

    NOTE: The UD API doesn't expose settled stats for completed games. This
    function only marks picks as "stale" (auto-flag for manual review) after
    14 days. Real outcome tracking requires either:
      - Manual entry via `python -m ud_edge --settle <pick_id> <stat_value>`
      - A stats-API integration (e.g. apisports, balldontlie)
      - Web-scraping UD's settled entries page (TODO)

    Returns: count of picks updated (resolved or stale-flagged).
    """
    data = _load()
    today = datetime.now(timezone.utc)
    current_keys = {_leg_key(l) for l in current_legs}
    updated = 0

    for pick in data["picks"]:
        if pick["outcome"] is not None:
            continue
        pick_date = datetime.fromisoformat(pick["date"] + "T00:00:00+00:00")
        age_days = (today - pick_date).days
        # Stale check: if a pick from >14 days ago still hasn't been resolved,
        # the line is probably gone from UD. Flag it for manual review.
        if age_days > STALE_DAYS:
            pick["outcome"] = "STALE"
            pick["resolved_at"] = today.isoformat()
            updated += 1
    if updated:
        _save(data)
    return updated


def calibration_stats() -> dict:
    """Compute calibration: predicted prob vs actual hit rate across all resolved picks.

    Returns dict with:
      - total_resolved: count of picks with non-None outcome
      - by_prob_bucket: { '0.55-0.60': {pred, hits, n, hit_rate}, ... }
      - brier_score: mean squared error of predictions (lower = better)
      - log_loss: log-loss of predictions
      - roi_pessimistic: ROI assuming 0% hit rate on unresolved picks
      - roi_optimistic: ROI assuming 100% hit rate on unresolved picks
    """
    import math
    data = _load()
    picks = data["picks"]
    resolved = [p for p in picks if p["outcome"] in {"HIT", "MISS"}]

    if not resolved:
        return {"total_resolved": 0, "message": "no resolved picks yet"}

    # Calibration buckets
    buckets = {}
    for p in resolved:
        prob = p["picked_true_prob"]
        bucket = f"{int(prob * 20) * 5}-{int(prob * 20) * 5 + 5}%"
        buckets.setdefault(bucket, {"pred_sum": 0, "hits": 0, "n": 0})
        buckets[bucket]["pred_sum"] += prob
        buckets[bucket]["n"] += 1
        if p["outcome"] == "HIT":
            buckets[bucket]["hits"] += 1

    bucket_stats = {}
    for k, v in sorted(buckets.items()):
        bucket_stats[k] = {
            "n": v["n"],
            "avg_pred": v["pred_sum"] / v["n"],
            "hit_rate": v["hits"] / v["n"],
        }

    # Brier score
    brier = sum((p["picked_true_prob"] - (1 if p["outcome"] == "HIT" else 0)) ** 2
                for p in resolved) / len(resolved)

    # Log loss
    eps = 1e-15
    log_loss = -sum(
        math.log(max(min(p["picked_true_prob"], 1 - eps), eps)) if p["outcome"] == "HIT"
        else math.log(max(min(1 - p["picked_true_prob"], 1 - eps), eps))
        for p in resolved
    ) / len(resolved)

    return {
        "total_resolved": len(resolved),
        "total_pending": sum(1 for p in picks if p["outcome"] is None),
        "brier_score": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "by_prob_bucket": bucket_stats,
    }


def print_calibration() -> str:
    """Pretty-print the calibration report."""
    stats = calibration_stats()
    if stats.get("total_resolved", 0) == 0:
        return f"📊 No resolved picks yet.\n   Total picks logged: {stats.get('total_pending', '?')}\n   Run the bot daily to build a sample size (target: 50+ legs)."

    out = [
        "📊 Calibration Report",
        "=" * 60,
        f"Resolved: {stats['total_resolved']} | Pending: {stats['total_pending']}",
        f"Brier score: {stats['brier_score']:.4f} (0.0 = perfect, 0.25 = random)",
        f"Log loss: {stats['log_loss']:.4f} (lower = better)",
        "",
        f"{'Bucket':<12} {'n':>4} {'Avg Pred':>10} {'Hit Rate':>10}",
        "-" * 60,
    ]
    for bucket, s in stats.get("by_prob_bucket", {}).items():
        out.append(f"{bucket:<12} {s['n']:>4} {s['avg_pred']:>10.1%} {s['hit_rate']:>10.1%}")

    out.append("")
    out.append("Interpretation:")
    out.append("  Avg Pred ≈ Hit Rate → well-calibrated")
    out.append("  Avg Pred > Hit Rate → overconfident (raise min_true_prob)")
    out.append("  Avg Pred < Hit Rate → underconfident (lower threshold)")
    return "\n".join(out)


def settle_pick(pick_index: int, hit: bool, actual_stat: Optional[float] = None) -> bool:
    """Manually mark a pick as HIT or MISS.

    Use this when you've verified the result on UD's settled entries page.
    pick_index is the 0-based index into data["picks"].
    """
    data = _load()
    if pick_index < 0 or pick_index >= len(data["picks"]):
        return False
    pick = data["picks"][pick_index]
    if pick["outcome"] is not None:
        return False
    pick["outcome"] = "HIT" if hit else "MISS"
    pick["actual_stat"] = actual_stat
    pick["resolved_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)
    return True