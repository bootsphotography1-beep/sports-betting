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

from ud_edge.broker import (
    Account,
    Broker,
    BrokerExhausted,
)
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
        # Guard malformed alerts (audit 2026-07-20): never push player="Unknown"
        # or line=0.0 from parser gaps / empty PropLine fantasy rows.
        player = (leg.player_name or "").strip()
        if not player or player.lower() == "unknown":
            continue
        if leg.line_value is None or float(leg.line_value) == 0.0:
            continue
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
            player=player,
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


def _active_account_or_budget(
    *,
    budget: Optional[CallBudget],
    broker: Optional[Broker],
) -> tuple[Optional[Broker], Optional[Account], CallBudget, str]:
    """Pick the active call counter for this cycle.

    Returns (broker, account, budget_for_snapshots, key_for_propline).
    - If broker is set: route() the account; the snapshot/budget_for_snapshots
      is that account's underlying CallBudget.
    - Else: fall back to the legacy single-Account path.
    - key_for_propline is the account's key (so compare_fantasy_vs_sharp
      uses the right PropLine key) or the legacy PROPLINE_API_KEY env.
    """
    if broker is not None:
        account = broker.route()
        return broker, account, account.snapshot().__class__(
            day=account.snapshot().day,
            used=account.snapshot().used,
            limit=account.snapshot().limit,
            reserve=account.snapshot().reserve,
            remaining_scheduled=account.snapshot().remaining_scheduled,
            remaining_total=account.snapshot().remaining_total,
        ), account.key
    # Legacy single-key path
    return None, None, budget, os.environ.get("PROPLINE_API_KEY", "") or ""


def _run_poll_cycle(
    *,
    budget: CallBudget,
    min_mispricing_pp: float,
    cache_path: Path,
    use_reserve: bool = False,
    broker: Optional[Broker] = None,
) -> tuple[list[RankedLeg], Optional[float], dict]:
    """One poll cycle: fetch data, rank legs, record budget.

    When `broker` is provided, route the cycle through the lowest-indexed
    account that still has spend room and charge that account (no
    mid-cycle rebalance). When `broker` is None, fall back to the legacy
    single-Account CallBudget path so existing tests/operators keep
    working without config changes.

    Returns (mispriced_ranked, nearest_tip_minutes, meta).
    meta includes 'budget' snapshot and 'skipped' flag.
    """
    active_broker, active_account, snap, propline_key = _active_account_or_budget(
        budget=budget, broker=broker
    )

    if not (active_account and active_account.can_spend(1, use_reserve=use_reserve)
             if active_broker else
             budget.can_spend(1, use_reserve=use_reserve)):
        print(f"[poll] budget exhausted ({snap.used}/{snap.limit}) — sleeping until UTC day rollover")
        return [], None, {"budget": snap, "skipped": True}

    if not propline_key:
        return [], None, {"budget": snap, "skipped": True, "error": "PROPLINE_API_KEY missing"}

    # PropLine auth: when running through the broker, inject the account's
    # key into os.environ so compare_fantasy_vs_sharp (and any
    # PropLineClient it constructs) reads the right value. We restore
    # on exit so other call sites aren't affected.
    prev_key = os.environ.get("PROPLINE_API_KEY")
    if propline_key:
        os.environ["PROPLINE_API_KEY"] = propline_key
    try:
        return _run_poll_cycle_inner(
            budget=budget,
            snap=snap,
            propline_key=propline_key,
            min_mispricing_pp=min_mispricing_pp,
            cache_path=cache_path,
            use_reserve=use_reserve,
            broker=active_broker,
            active_account=active_account,
        )
    finally:
        if prev_key is None:
            os.environ.pop("PROPLINE_API_KEY", None)
        else:
            os.environ["PROPLINE_API_KEY"] = prev_key


def _run_poll_cycle_inner(
    *,
    budget: CallBudget,
    snap,
    propline_key: str,
    min_mispricing_pp: float,
    cache_path: Path,
    use_reserve: bool,
    broker: Optional[Broker],
    active_account: Optional[Account],
) -> tuple[list[RankedLeg], Optional[float], dict]:
    """Inner cycle (split out so the outer can wrap the env-var dance)."""
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
        if broker is not None and active_account is not None:
            broker.record(n=propline_calls, account=active_account, use_reserve=use_reserve)
        else:
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

    # Build the "active snapshot" reported in meta: prefer the active
    # account's underlying CallBudget (so /api/budget reflects whichever
    # account the cycle just used) when running through the broker.
    if broker is not None and active_account is not None:
        report_snap = active_account.snapshot()
    else:
        report_snap = budget.snapshot()

    meta = {
        "now": now,
        "budget": report_snap,
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
    broker: Optional[Broker] = None,
) -> int:
    """Run the adaptive poller. Returns 0 on clean exit, 1 on config error.

    When `broker` is provided, route each cycle through the lowest-indexed
    account that still has spend room. When None, fall back to the legacy
    single-Account CallBudget path so existing operators/tests keep working.
    """
    if broker is None and not propline_configured():
        print("[poll] PROPLINE_API_KEY missing — abort")
        return 1

    if broker is not None:
        # Broker-managed path. Don't require the legacy PROPLINE_API_KEY;
        # the broker's account[0].key is what compare_fantasy_vs_sharp sees.
        # We still respect propline_configured() in case the operator wants
        # both: legacy key in env AND broker in code (we ignore the legacy).
        budget = None  # marker for "broker-managed"
        pool = broker.pool_snapshot()
        primary = pool[0] if pool else None
        print(f"[poll] broker mode · {len(pool)} account(s): " +
              ", ".join(f"{a['name']}={a['used']}/{a['limit']}" for a in pool))
        if primary is not None:
            daily_limit = primary["limit"]
    else:
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
        if broker is not None:
            active = broker.pool_snapshot()
            primary_used = active[0]["used"] if active else 0
            primary_limit = active[0]["limit"] if active else 0
            active_name = active[0]["name"] if active else "(empty)"
            print(f"\n[poll] {now.strftime('%H:%M:%S')}Z  active={active_name} "
                  f"{primary_used}/{primary_limit}  pool: " +
                  ", ".join(f"{a['name']}={a['used']}/{a['limit']}{'X' if a['exhausted'] else ''}" for a in active))
        else:
            snap = budget.snapshot()
            print(f"\n[poll] {now.strftime('%H:%M:%S')}Z  budget {snap.used}/{snap.limit} "
                  f"(sched left {snap.remaining_scheduled}, reserve {snap.reserve})")

        # Build a stub CallBudget for the inner function — only used for
        # legacy single-key path. When broker is set, _run_poll_cycle ignores
        # it and uses the broker instead.
        legacy_budget = budget if budget is not None else CallBudget(
            path=cache_path / "propline_budget.json",
            daily_limit=daily_limit,
        )
        try:
            mispriced, nearest, meta = _run_poll_cycle(
                budget=legacy_budget,
                min_mispricing_pp=min_mispricing_pp,
                cache_path=cache_path,
                broker=broker,
            )
        except BrokerExhausted as e:
            # All accounts spent for this UTC day. Sleep until rollover.
            print(f"[poll] {e} — sleeping until UTC day rollover")
            time.sleep(min(3600.0, seconds_left_in_utc_day(now)))
            if once:
                return 0
            continue
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

        # Confirm-burst: spend one reserve call ~20s after an alert.
        # In broker mode, "can_spend reserve" is checked against the
        # currently-routed account (same as scheduled pool pattern).
        if n_alert > 0:
            if broker is not None:
                try:
                    can_confirm = broker.can_spend_any(1)
                except Exception:
                    can_confirm = False
            else:
                can_confirm = budget.can_spend(1, use_reserve=True)
            if can_confirm:
                print("[poll] confirm-burst in 20s (reserve)…")
                time.sleep(20)
                try:
                    _run_poll_cycle(
                        budget=legacy_budget,
                        min_mispricing_pp=min_mispricing_pp,
                        cache_path=cache_path,
                        use_reserve=True,
                        broker=broker,
                    )
                except Exception as e:
                    print(f"[poll] confirm-burst error: {e}")

        if once:
            return 0

        if broker is not None:
            # Use the active account's snapshot for the budget_floor math.
            active = broker.pool_snapshot()
            primary = active[0] if active else None
            remaining_scheduled = primary["remaining_scheduled"] if primary else 0
        else:
            snap = budget.snapshot()
            remaining_scheduled = max(snap.remaining_scheduled, 0)
        interval = compute_poll_interval_seconds(
            nearest_tip_minutes=nearest,
            remaining_scheduled=remaining_scheduled,
            seconds_left_in_utc_day=seconds_left_in_utc_day(),
        )
        print(f"[poll] next scan in {interval:.0f}s")
        time.sleep(interval)
