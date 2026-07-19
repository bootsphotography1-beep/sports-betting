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
from ud_edge.dashboard import _mins_until, collect_mispriced
from ud_edge.deliver import _fmt_side
from ud_edge.notify import configured_channels, notify_opportunity, should_alert
from ud_edge.propline_client import PropLineClient, propline_configured


def _nearest_tip_minutes(legs_or_ranked, now: datetime) -> Optional[float]:
    best: Optional[float] = None
    for item in legs_or_ranked:
        leg = getattr(item, "leg", item)
        mins = _mins_until(getattr(leg, "scheduled_at", None), now)
        if mins is None:
            continue
        # Prefer upcoming / just-started tips for urgency
        if -30 <= mins <= 24 * 60:
            if best is None or abs(mins) < abs(best) or (mins >= 0 and (best < 0 or mins < best)):
                # Prefer soonest upcoming; if all started, nearest
                if best is None:
                    best = mins
                elif mins >= 0 and (best < 0 or mins < best):
                    best = mins
                elif best < 0 and mins < 0 and mins > best:
                    best = mins
    return best


def _scan_once(
    *,
    budget: CallBudget,
    min_mispricing_pp: float,
    cache_path: Path,
    use_reserve: bool = False,
) -> tuple[list, Optional[float], dict]:
    """One poll cycle. Records 1 PropLine call if the fetch runs."""
    if not budget.can_spend(1, use_reserve=use_reserve):
        snap = budget.snapshot()
        print(f"[poll] budget exhausted ({snap.used}/{snap.limit}) — sleeping until UTC day rollover")
        return [], None, {"budget": snap, "skipped": True}

    # Force fresh PropLine: short TTL cache path unique per minute is overkill;
    # use ttl=0 by clearing — PropLineClient with ttl_seconds=0 still writes.
    # Bypass: temporarily use ttl_seconds=0 so every poll hits network.
    from ud_edge import dashboard as dash

    # Monkey-patch lean builder to count budget + no-cache
    original = dash._build_sharp_index_mlb

    def _counted_build(cp: Path) -> dict:
        if not propline_configured():
            return {}
        from ud_edge.propline_client import (
            BOOK_PRIORITY, SPORT_MAP, parse_prop_outcomes_to_index_rows,
        )
        from ud_edge.injury_client import normalize_name
        pl = PropLineClient(cache_path=None, ttl_seconds=0, timeout=90)  # no disk cache
        events = pl.fetch_bulk_odds(
            SPORT_MAP["MLB"],
            markets="pitcher_strikeouts,batter_hits,batter_total_bases,batter_home_runs,batter_rbis",
            bookmakers="pinnacle,draftkings,fanduel,betmgm,sleeper",
        )
        budget.record(1, use_reserve=use_reserve)
        index: dict = {}
        for ev in events:
            for p in parse_prop_outcomes_to_index_rows(ev, for_true_prob=True, sport_id="MLB"):
                key = f"{normalize_name(p['player'])}|{p['stat']}"
                new_pri = BOOK_PRIORITY.get(p.get("book_key", ""), 0)
                old = index.get(key)
                if old is not None:
                    old_book = (old.get("source") or "").replace("propline-", "")
                    if new_pri < BOOK_PRIORITY.get(old_book, 0):
                        continue
                index[key] = {
                    "over_decimal": p["over_decimal"],
                    "under_decimal": p["under_decimal"],
                    "bookmaker": p["bookmaker"],
                    "line_value": p["line"],
                    "source": p.get("source", "propline"),
                }
        return index

    dash._build_sharp_index_mlb = _counted_build  # type: ignore
    try:
        mispriced, urgent, meta = collect_mispriced(
            min_mispricing_pp=min_mispricing_pp,
            cache_path=cache_path,
        )
    finally:
        dash._build_sharp_index_mlb = original  # type: ignore

    now = meta["now"]
    nearest = _nearest_tip_minutes([u[1] for u in urgent] + mispriced, now)
    if nearest is None:
        # Tip density from full UD MLB slate (cached fetch inside collect)
        try:
            from ud_edge.ud_client import UDClient
            ud_legs = UDClient(cache_path=cache_path / "ud_lines_cache.json").parse_legs(
                UDClient(cache_path=cache_path / "ud_lines_cache.json").fetch(force=False),
                sport_filter={"MLB"},
            )
            nearest = _nearest_tip_minutes(ud_legs, now)
        except Exception:
            pass
    return mispriced, nearest, meta


def _alert_mispriced(mispriced, now: datetime, min_alert_pp: float) -> int:
    """Push alerts for qualifying legs. Returns number of alerts sent."""
    from ud_edge.notify import mark_alerted

    sent = 0
    for r in mispriced:
        if r.sharp_true_prob is None or r.mispricing_edge_pp is None:
            continue
        if r.mispricing_edge_pp < min_alert_pp:
            continue
        if r.sharp_true_prob < 0.52:
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
        )
        # Avoid re-spam even if only logging (no push channel configured)
        if not fired:
            mark_alerted(alert_key, delta_pp=r.mispricing_edge_pp, line_value=leg.line_value)
        sent += 1
    return sent


def run_poll_loop(
    *,
    daily_limit: int = 5000,
    min_mispricing_pp: float = 1.5,
    min_alert_pp: float = 1.5,
    cache_path: Path = Path("data"),
    once: bool = False,
) -> int:
    """Run the adaptive poller. Returns 0 on clean exit."""
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
            # Auto-enable a default ntfy topic so messages can flow immediately
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
            mispriced, nearest, meta = _scan_once(
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
            # Sleep until next UTC day
            time.sleep(min(300.0, seconds_left_in_utc_day(now)))
            if once:
                return 0
            continue

        # If nearest tip unknown, derive from UD legs in meta via urgent list empty
        if nearest is None and mispriced:
            nearest = _nearest_tip_minutes(mispriced, meta["now"])

        n_alert = _alert_mispriced(mispriced, meta["now"], min_alert_pp)
        print(f"[poll] mispriced={len(mispriced)}  nearest_tip={nearest if nearest is None else f'{nearest:.0f}m'}  "
              f"alerts_sent={n_alert}")

        # Confirm-burst: if we alerted, spend one reserve call ~20s later
        if n_alert > 0 and budget.can_spend(1, use_reserve=True):
            print("[poll] confirm-burst in 20s (reserve)…")
            time.sleep(20)
            try:
                _scan_once(
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
