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
    #
    # Audit P1 #5: pass return_ranked=True so the live RankedLeg list is
    # returned (no JSON serialization round-trip, no field loss). The poller
    # used to rebuild RankedLeg from the flat dict list which silently dropped
    # sharp_book, match_id, fantasy_source, etc.
    #
    # Audit P1 #6 (remediation v3): forward line_tolerance (env-driven so
    # operators can opt up to 1.0+ for soft lines without touching code).
    # Default None means compare_fantasy_vs_sharp will use its module constant.
    import os as _os
    _line_tolerance_env = _os.environ.get("UD_LINE_TOLERANCE", "").strip()
    _line_tolerance = float(_line_tolerance_env) if _line_tolerance_env else None
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
            return_ranked=True,
            line_tolerance=_line_tolerance,
        )
    except Exception as e:
        print(f"[poll] compare_fantasy_vs_sharp error: {e}")
        return [], None, {"budget": snap, "skipped": False, "error": str(e)}

    # Unpack (payload, ranked) tuple. Fallback to payload-only if the
    # pipeline was called without return_ranked for some reason.
    if isinstance(result, tuple) and len(result) == 2:
        result_payload, ranked = result
    else:
        result_payload = result
        ranked = []

    # Record the actual PropLine HTTP calls this cycle made.
    # Audit P1 #4: was budget.record(1) which under-counted every cycle by
    # ~13-80x (one events call + N odds calls per sport per cycle). The
    # compare pipeline reports the real count in sharp_meta.propline_calls;
    # when it's missing (e.g. PROPLINE_API_KEY not set), record 0 so we don't
    # claim a call we didn't make.
    sharp_meta = result_payload.get("sharp_meta", {}) if isinstance(result_payload, dict) else {}
    propline_calls = int(sharp_meta.get("propline_calls", 0) or 0)
    if propline_calls > 0:
        budget.record(propline_calls, use_reserve=use_reserve)
    elif propline_calls == 0 and isinstance(sharp_meta, dict) and "propline_calls" not in sharp_meta:
        # No PropLine key configured — don't claim a call we didn't make.
        pass

    # Audit P1 #5: 'ranked' is now the LIVE RankedLeg list returned from
    # compare_fantasy_vs_sharp(return_ranked=True). No JSON round-trip, no
    # field loss. The old flat-dict reconstruction path is gone.

    # Mispriced = sharp_edge_pp >= 2.0
    mispriced = [r for r in ranked if r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0]

    # Nearest tip from ranked legs
    nearest = _nearest_tip_minutes(ranked, now)

    meta = {
        "now": now,
        "budget": budget.snapshot(),
        "skipped": False,
        "sports_count": len(result_payload.get("sports", [])),
        "opportunities_count": result_payload.get("totals", {}).get("opportunities", 0),
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
