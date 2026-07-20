"""Adaptive PropLine poller — spend ≤5k calls/day, densify near tip-off, alert on edges.

Mathematical spend model
────────────────────────
• Daily hard cap: PROPLINE_DAILY_LIMIT (default 5000)
• 10% reserve for confirm-burst after an alert
• Each poll cycle costs 1 PropLine call (lean MLB bulk /odds)
• Interval = max(urgency_band, budget_floor) where
    budget_floor = seconds_left_in_UTC_day / remaining_scheduled_calls
  so we never exhaust the day before midnight UTC.

Urgency bands (minutes to nearest tip):
  [-20, 90]  → 45s   last-minute / steam
  (90, 240]  → 3m    pre-game
  (240, 720] → 10m   slate watch
  else       → 15m   quiet

When a same-side misprice ≥ min_pp appears (and passes dedup), push a message
via ntfy / Slack / Discord and remind: PLACE ON → Underdog Fantasy.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ud_edge.budget import (
    CallBudget,
    compute_poll_interval_seconds,
    seconds_left_in_utc_day,
)
from ud_edge.compare import compare_fantasy_vs_sharp
from ud_edge.deliver import _fmt_side
from ud_edge.notify import configured_channels, notify_opportunity, should_alert
from ud_edge.models import RankedLeg


def propline_configured() -> bool:
    """Return True if PROPLINE_API_KEY is set in the environment."""
    return bool(os.environ.get("PROPLINE_API_KEY", "").strip())


def _is_finite(value: float) -> bool:
    """Return True if value is a finite number (not NaN/Inf)."""
    import math
    return math.isfinite(value)


def _nearest_tip_minutes(legs_or_ranked, now: datetime) -> Optional[float]:
    """Return signed minutes to the nearest upcoming tip (negative = already started)."""
    best: Optional[float] = None
    for item in legs_or_ranked:
        leg = getattr(item, "leg", item)
        mins = _mins_until(getattr(leg, "scheduled_at", None), now)
        if mins is None:
            continue
        # Only consider tips within a reasonable window (24h)
        if -30 <= mins <= 24 * 60:
            if best is None:
                best = mins
            elif mins >= 0 and (best < 0 or mins < best):
                best = mins
            elif best < 0 and mins < 0 and mins > best:
                best = mins
    return best


def _mins_until(iso_str: Optional[str], now: datetime) -> Optional[float]:
    """Parse ISO8601 scheduled_at and return signed minutes from now."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - now
        return delta.total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def _alert_mispriced(mispriced: list[RankedLeg], now: datetime, min_alert_pp: float) -> int:
    """Push alerts for qualifying legs. Returns number of alerts sent."""
    from ud_edge.notify import mark_alerted

    sent = 0
    for r in mispriced:
        if r.sharp_true_prob is None or r.mispricing_edge_pp is None:
            continue
        if r.mispricing_edge_pp < min_alert_pp:
            continue
        if r.sharp_true_prob < 0.52:
            # Only alert on legs with meaningful sharp true probability
            continue
        leg = r.leg
        alert_key = f"{leg.player_id}|{leg.stat_name}|{r.picked_side}|{leg.line_value}"
        if not should_alert(
            alert_key,
            delta_pp=r.mispricing_edge_pp,
            line_value=leg.line_value,
        ):
            continue
        tips = _mins_until(leg.scheduled_at, now)
        # Platform from fantasy_source; default to 'Unknown' when the leg has
        # no source attached (e.g. legacy/manual entry).
        platform = (leg.fantasy_source or "Unknown").replace("_", " ").title()
        fired = notify_opportunity(
            player=leg.player_name,
            pick=_fmt_side(leg, r.picked_side),
            match=leg.match_title or "—",
            ud_pct=r.picked_true_prob,
            sharp_pct=r.sharp_true_prob,
            delta_pp=r.mispricing_edge_pp,
            sharp_book=r.sharp_book or "sharp",
            tips_in_min=tips,
            alert_key=alert_key,
            line_value=leg.line_value,
            platform=platform,
        )
        if not fired:
            mark_alerted(alert_key, delta_pp=r.mispricing_edge_pp, line_value=leg.line_value)
        sent += 1
    return sent


def _run_poll_cycle(
    *,
    budget: CallBudget,
    min_mispricing_pp: float,
    cache_path: Path,
    use_reserve: bool = False,
) -> tuple[list[RankedLeg], Optional[float], dict]:
    """One poll cycle: fetch data, rank legs, record budget.

    Returns (mispriced_ranked, nearest_tip_minutes, meta).
    meta includes 'budget' snapshot and 'skipped' flag.
    """
    snap = budget.snapshot()
    if not budget.can_spend(1, use_reserve=use_reserve):
        print(f"[poll] budget exhausted ({snap.used}/{snap.limit}) — sleeping until UTC day rollover")
        return [], None, {"budget": snap, "skipped": True}

    propline_key = os.environ.get("PROPLINE_API_KEY", "") or None
    if not propline_key:
        return [], None, {"budget": snap, "skipped": True, "error": "PROPLINE_API_KEY missing"}

    now = datetime.now(timezone.utc)

    # One complete fetch+rank cycle via compare_fantasy_vs_sharp.
    # This already handles PropLine fetching, UD line fetching, sharp indexing,
    # and ranking with sharp_authoritative_quarantine policy.
    try:
        result = compare_fantasy_vs_sharp(
            entry_type="6-flex",
            min_true_prob=0.55,
            min_edge_pp=min_mispricing_pp,
            sport_filter=None,
            full_game_only=True,
            mispriced_only=False,
            fantasy_csvs=None,
            n_entries=4,
            force_fetch=True,
        )
    except Exception as e:
        print(f"[poll] compare_fantasy_vs_sharp error: {e}")
        return [], None, {"budget": snap, "skipped": False, "error": str(e)}

    # Record one PropLine call (the compare pipeline does one PropLine fetch)
    budget.record(1, use_reserve=use_reserve)

    # Extract ranked legs from the flat result
    flat: list[dict] = result.get("flat", [])
    ranked: list[RankedLeg] = []
    for item in flat:
        # Reconstruct RankedLeg from the flat dict (opportunities_to_dict serialization)
        from ud_edge.models import RankedLeg as _RL
        from ud_edge.models import Leg as _Leg
        leg_d = item.get("leg", {})
        leg = _Leg(
            line_id=leg_d.get("line_id", ""),
            player_id=leg_d.get("player_id", ""),
            player_name=leg_d.get("player_name", "Unknown"),
            sport_id=leg_d.get("sport_id", "UNK"),
            match_id=leg_d.get("match_id"),
            match_title=leg_d.get("match_title"),
            scheduled_at=leg_d.get("scheduled_at"),
            stat_name=leg_d.get("stat_name", ""),
            line_value=float(leg_d.get("line_value", 0) or 0),
            line_type=leg_d.get("line_type", "balanced"),
            higher_american=leg_d.get("higher_american", -110),
            higher_decimal=leg_d.get("higher_decimal", 1.91),
            higher_multiplier=leg_d.get("higher_multiplier", 0.9),
            lower_american=leg_d.get("lower_american", -110),
            lower_decimal=leg_d.get("lower_decimal", 1.91),
            lower_multiplier=leg_d.get("lower_multiplier", 0.9),
        )
        r = _RL(
            leg=leg,
            higher_true_prob=item.get("higher_true_prob", 0.5),
            higher_implied_prob=item.get("higher_implied_prob", 0.5),
            higher_edge_pp=item.get("higher_edge_pp", 0.0),
            lower_true_prob=item.get("lower_true_prob", 0.5),
            lower_implied_prob=item.get("lower_implied_prob", 0.5),
            lower_edge_pp=item.get("lower_edge_pp", 0.0),
            picked_side=item.get("picked_side", "higher"),
            picked_true_prob=item.get("picked_true_prob", 0.5),
            picked_edge_pp=item.get("picked_edge_pp", 0.0),
            overround=item.get("overround"),
            sharp_true_prob=item.get("sharp_true_prob"),
            sharp_book=item.get("sharp_book"),
            sharp_overround=item.get("sharp_overround"),
            mispricing_edge_pp=item.get("mispricing_edge_pp"),
        )
        ranked.append(r)

    # Mispriced = sharp_edge_pp >= 2.0
    mispriced = [r for r in ranked if r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0]

    # Nearest tip from ranked legs
    nearest = _nearest_tip_minutes(ranked, now)

    meta = {
        "now": now,
        "budget": budget.snapshot(),
        "skipped": False,
        "sports_count": len(result.get("sports", [])),
        "opportunities_count": result.get("totals", {}).get("opportunities", 0),
    }
    return mispriced, nearest, meta


def run_poll_loop(
    *,
    daily_limit: int = 5000,
    min_mispricing_pp: float = 1.5,
    min_alert_pp: float = 1.5,
    cache_path: Path = Path("data"),
    once: bool = False,
) -> int:
    """Run the adaptive poller. Returns 0 on clean exit, 1 on config error."""
    if not propline_configured():
        print("[poll] PROPLINE_API_KEY missing — abort")
        return 1

    budget = CallBudget(
        path=cache_path / "propline_budget.json",
        daily_limit=daily_limit,
    )
    channels = configured_channels()
    print(f"[poll] starting · limit={daily_limit}/day · alert_pp≥{min_alert_pp}")
    print(f"[poll] notify channels: {channels or ['alerts.jsonl only — set NTFY_TOPIC or SLACK_WEBHOOK_URL']}")
    if not channels:
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if not topic:
            topic = "ud-edge-fin-alerts"
            os.environ["NTFY_TOPIC"] = topic
            print(f"[poll] auto-enabled ntfy topic '{topic}'")
            print(f"[poll] → install ntfy app and subscribe to: https://ntfy.sh/{topic}")
            channels = configured_channels()

    while True:
        now = datetime.now(timezone.utc)
        snap = budget.snapshot()
        print(f"\n[poll] {now.strftime('%H:%M:%S')}Z  budget {snap.used}/{snap.limit} "
              f"(sched left {snap.remaining_scheduled}, reserve {snap.reserve})")

        try:
            mispriced, nearest, meta = _run_poll_cycle(
                budget=budget,
                min_mispricing_pp=min_mispricing_pp,
                cache_path=cache_path,
            )
        except Exception as e:
            print(f"[poll] scan error: {e}")
            time.sleep(60)
            if once:
                return 1
            continue

        if meta.get("skipped"):
            time.sleep(min(300.0, seconds_left_in_utc_day(now)))
            if once:
                return 0
            continue

        if nearest is None and mispriced:
            nearest = _nearest_tip_minutes(mispriced, meta["now"])

        n_alert = _alert_mispriced(mispriced, meta["now"], min_alert_pp)
        print(f"[poll] mispriced={len(mispriced)}  nearest_tip={nearest if nearest is None else f'{nearest:.0f}m'}  "
              f"alerts_sent={n_alert}")

        # Confirm-burst: spend one reserve call ~20s after an alert
        if n_alert > 0 and budget.can_spend(1, use_reserve=True):
            print("[poll] confirm-burst in 20s (reserve)…")
            time.sleep(20)
            try:
                _run_poll_cycle(
                    budget=budget,
                    min_mispricing_pp=min_mispricing_pp,
                    cache_path=cache_path,
                    use_reserve=True,
                )
            except Exception as e:
                print(f"[poll] confirm-burst error: {e}")

        if once:
            return 0

        snap = budget.snapshot()
        interval = compute_poll_interval_seconds(
            nearest_tip_minutes=nearest,
            remaining_scheduled=max(snap.remaining_scheduled, 0),
            seconds_left_in_utc_day=seconds_left_in_utc_day(),
        )
        print(f"[poll] next scan in {interval:.0f}s")
        time.sleep(interval)
