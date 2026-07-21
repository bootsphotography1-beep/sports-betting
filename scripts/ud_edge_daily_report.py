"""Multi-fire daily report — runs as a cron target, not interactively.

Schedule (Central Time, Fin's spec 2026-07-21):
  10:00  CT  =  16:00  UTC  (pre-noon slate, 1pm NBA tips + early MLB)
  12:30  CT  =  18:30  UTC  (afternoon slate, 1pm NFL Sunday if applicable)
  16:00  CT  =  22:00  UTC  (late-afternoon line movement before 7pm tip-offs)
  20:00  CT  =  02:00  UTC  (post-7pm live monitoring, NFL SNF / MNF)

Each fire:
  1. Loads the broker from .env (PROPLINE_ACCOUNTS + PROPLINE_KEY_*)
  2. Calls compare_fantasy_vs_sharp across all supported sports
  3. Builds 4 disjoint 6-flexes + 3 disjoint 4-flexes
  4. Writes a timestamped Markdown report
  5. Charges the routed account with the actual PropLine call count
  6. Notifies via the configured channel (ntfy / Slack / Discord / file)

This is the cron target. --once is blocked for this exact reason;
--poll runs forever (wrong shape for a 4x/day schedule).

CLI:
  python -m ud_edge_daily_report           # fire all 4 in sequence (test mode)
  python -m ud_edge_daily_report --fire 2  # fire only the 12:30pm CT slot
  python -m ud_edge_daily_report --fire 0 --live  # actually call the API
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Make the repo importable when this module is run as `python scripts/ud_edge_daily_report.py`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# E402: the import block below must follow the sys.path hook so that
# running this file directly (`python scripts/ud_edge_daily_report.py`)
# resolves the ud_edge package from the repo root.
from ud_edge.notify import configured_channels, notify_opportunity  # noqa: E402
from ud_edge.results_tracker import log_picks  # noqa: E402
from ud_edge.safety_gate import safety_status  # noqa: E402


# ── Constants ───────────────────────────────────────────────────────────

# All supported sports — covers PropLine's coverage + UD's live board.
# PropLine free tier (verified 2026-07-18) covers these 8 leagues with
# sharp books; UD's v5 unauth endpoint also has these + a few more
# (Tennis / Soccer / MMA / WNBA). We hit all in one fire and the
# ranker filters by full_game_only + min_true_prob + min_edge_pp.
ALL_SPORTS: list[str] = [
    "NBA", "NFL", "MLB", "NHL", "WNBA",
    "CFB", "EPL", "MLS",
    "TENNIS", "SOCCER", "MMA",  # UD-only; PropLine has no sharp for these
]

# Central-time fire slots (hour, minute, friendly label).
# UTC offsets for the cron schedule (Central is UTC-5 CST / UTC-6 CDT;
# we use the conservative UTC-6 mapping which is correct Nov-Mar; in
# CDT (Mar-Nov) the cron fires 1 hour earlier than the user expects —
# operators can adjust the cron itself if they want CD-time semantics).
CRON_FIRES_CT: list[tuple[int, int, str]] = [
    (10,  0,  "10am CT (pre-noon slate)"),
    (12, 30, "12:30pm CT (afternoon slate)"),
    (16,  0, "4pm CT (late-afternoon lines)"),
    (20,  0, "8pm CT (post-tip live monitoring)"),
]

REPORTS_DIR = ROOT / "reports"
BROKER_STATE_DIR = ROOT / "data" / "broker_state"


# ── Helpers ──────────────────────────────────────────────────────────────


def _ct_to_utc(hour: int, minute: int) -> tuple[int, int]:
    """Convert a (hour, minute) Central Time to UTC.

    We use UTC-6 (CST) for the cron. The user can adjust the cron
    itself for DST (CDT = UTC-5).
    """
    return hour + 6, minute


def _next_fire_utc(now: datetime) -> tuple[int, datetime]:
    """Return (fire_index, next_fire_utc_datetime) for the next fire
    after `now`. Used by the live-mode runner to know which fire
    we're currently in."""
    # Use a fixed offset (UTC-6) for testability — no DST in the test env
    now_ct_hour = (now.hour - 6) % 24
    now_ct_minute = now.minute
    candidates = []
    for i, (h, m, _) in enumerate(CRON_FIRES_CT):
        # Minutes-of-day in CT
        target = h * 60 + m
        current = now_ct_hour * 60 + now_ct_minute
        if target > current:
            # Today
            delta_minutes = target - current
            fire_utc = now + timedelta(minutes=delta_minutes)
        else:
            # Tomorrow
            delta_minutes = (24 * 60 - current) + target
            fire_utc = now + timedelta(minutes=delta_minutes)
        candidates.append((i, fire_utc))
    # Pick the soonest
    candidates.sort(key=lambda x: x[1])
    return candidates[0]


def _report_path(fire_utc: datetime, suffix: str = "") -> Path:
    """Filename: reports/YYYY-MM-DD-HH-MM[-suffix].md (Windows-friendly)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    name = fire_utc.strftime("%Y-%m-%d-%H-%M")
    if suffix:
        name = f"{name}-{suffix}"
    return REPORTS_DIR / f"{name}.md"


# ── report writer ──────────────────────────────────────────────────────


def write_report(
    payload: dict,
    *,
    fire_index: int,
    fire_utc: datetime,
    suffix: str = "",
) -> Path:
    """Write the multi-entry report to reports/<date>-<hour>-<min>[-suffix].md.

    Picks a non-clobbering filename if a file at the same path already
    exists (appends -1, -2, ...).
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = _report_path(fire_utc, suffix=suffix)
    path = base
    counter = 1
    while path.exists():
        path = REPORTS_DIR / f"{fire_utc.strftime('%Y-%m-%d-%H-%M')}-{counter}.md"
        counter += 1

    _, _, label = CRON_FIRES_CT[fire_index]
    body = _render_multi_report(
        payload, fire_label=label, fire_utc=fire_utc,
    )
    path.write_text(body, encoding="utf-8")
    return path


def _render_multi_report(
    payload: dict,
    *,
    fire_label: str,
    fire_utc: datetime,
) -> str:
    """Render a multi-entry report from the compare_fantasy_vs_sharp
    payload. We can't reuse `deliver.build_multi_report` directly
    because it takes a list[list[RankedLeg]] not a serialized payload;
    rendering from the payload is straightforward enough that
    duplicating ~80 lines of template here is cheaper than the round-trip
    of re-hydrating Pydantic objects from the dict.
    """
    entry_type = payload.get("entry_type", "6-flex")
    lineups = payload.get("lineups", [])
    totals = payload.get("totals", {})
    safety = payload.get("safety_status", {})
    sharp_meta = payload.get("sharp_meta", {})
    fantasy_meta = payload.get("fantasy_meta", {})
    methodology = payload.get("methodology", {})

    md: list[str] = []
    md.append(f"# Edge Board — {fire_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    md.append("")
    md.append(f"_Fire: **{fire_label}** | "
              f"Entry: **{entry_type}** | "
              f"{len(lineups)} lineups × "
              f"{lineups[0].get('n_legs', 6) if lineups else 6} legs | "
              f"Broker: 2 keys (5000 + 1000/day)_")
    md.append("")
    if safety.get("is_research_mode"):
        md.append(
            "⚠️ **UNVERIFIED RESEARCH ESTIMATES** — EV/$ and win% below are "
            "unverified model output. Do not treat as actionable +EV until "
            "≥50 settled legs and payout model verified (Wave 1)."
        )
        md.append("")

    # At-a-glance: per-entry summary
    md.append("## At-a-glance")
    md.append("")
    md.append("| Entry | Avg True% | EV/$1 | Win% | Rec | Mispricings |")
    md.append("|-------|-----------|-------|------|-----|-------------|")
    for lu in lineups:
        avg = lu.get("avg_true_prob", 0.0) or 0.0
        ev = lu.get("ev", 0.0) or 0.0
        win = lu.get("win_prob", 0.0) or 0.0
        rec = "🟢 play-strong" if ev > 0.10 else ("🟡 play" if ev > 0.03 else ("🟠 small" if ev > 0 else "🔴 skip"))
        # Count mispricings in this entry
        mis = sum(
            1 for o in lu.get("opportunities", [])
            if o.get("is_mispriced")
        )
        md.append(
            f"| **#{lu.get('entry', '?')}** | {avg*100:.2f}% | "
            f"**{ev:+.4f}** | {win*100:.1f}% | {rec} | {mis} |"
        )
    md.append("")
    md.append(
        f"_Totals: **{totals.get('opportunities', 0)}** opportunities | "
        f"**{totals.get('mispriced', 0)}** mispriced | "
        f"**{totals.get('sports', 0)}** sports | "
        f"**{totals.get('lineups', 0)}** lineups_"
    )
    md.append("")

    # Per-entry detail
    for lu in lineups:
        entry_n = lu.get("entry", "?")
        avg = lu.get("avg_true_prob", 0.0) or 0.0
        ev = lu.get("ev", 0.0) or 0.0
        win = lu.get("win_prob", 0.0) or 0.0
        md.append(f"## Entry #{entry_n} — {entry_type}")
        md.append("")
        md.append(
            f"_Avg true prob: **{avg*100:.2f}%** · "
            f"EV: **{ev:+.4f}** per $1 · "
            f"win prob: **{win*100:.1f}%** · "
            f"median payout: **{lu.get('median_payout', 0.0):.1f}x**_"
        )
        md.append("")
        md.append("| # | Sport | Player | Match | Pick | UD% | Sharp% | Δpp | Sharp Book |")
        md.append("|---|-------|--------|-------|------|-----|---------|-----|------------|")
        for i, opp in enumerate(lu.get("opportunities", []), 1):
            sharp_pct = (
                f"{(opp.get('sharp_true_prob') or 0)*100:.1f}%"
                if opp.get("sharp_true_prob") is not None else "—"
            )
            delta = (
                f"{opp.get('mispricing_edge_pp', 0):+.1f}"
                if opp.get("mispricing_edge_pp") is not None else "—"
            )
            book = (
                f"`{opp.get('sharp_book', '—')}`"
                if opp.get("sharp_book") else "—"
            )
            md.append(
                f"| {i} | {opp.get('sport_id', '?')} | {opp.get('player_name', '?')} | "
                f"{opp.get('match_title', '') or '—'} | "
                f"{opp.get('side_label', '?')} {opp.get('line_value', '?')} {opp.get('stat_label', opp.get('stat_name', '?'))} | "
                f"{opp.get('ud_true_prob', 0)*100:.1f}% | {sharp_pct} | {delta} | {book} |"
            )
        md.append("")

    # Sharp + fantasy source summary
    md.append("## Data sources")
    md.append("")
    if sharp_meta.get("sources"):
        md.append(f"- **Sharp books**: {', '.join(sharp_meta['sources'])} ({sharp_meta.get('count', 0)} lines)")
    if fantasy_meta.get("sources"):
        fantasy_lines = [
            f"{k} ({v})" for k, v in fantasy_meta["sources"].items()
        ]
        md.append(f"- **Fantasy books**: {', '.join(fantasy_lines)}")
    if methodology.get("break_even"):
        md.append(
            f"- **Entry type break-even**: {methodology['break_even']*100:.2f}% per leg"
        )
    if safety.get("settled_legs_count") is not None:
        md.append(
            f"- **Calibration**: {safety.get('settled_legs_count', 0)} settled legs / "
            f"{safety.get('min_settled_legs_required', 50)} required for verified mode"
        )
    md.append("")

    # Methodology footer
    if methodology.get("steps"):
        md.append("## Methodology")
        md.append("")
        for step in methodology["steps"]:
            md.append(f"- {step}")
        md.append("")

    md.append(
        "_Disclaimer: decision-support tool, not financial advice. "
        "Track every pick; calibrate after 50+ settled legs._"
    )
    return "\n".join(md) + "\n"


# ── one fire ────────────────────────────────────────────────────────────


def run_one_fire(
    *,
    fire_index: int,
    fire_utc: datetime,
    live: bool = False,
    sport_filter: Optional[set[str]] = None,
) -> dict:
    """Run a single fire. Returns a dict with report_path, picks_logged,
    propline_calls, mispriced_count, and the broker snapshot.

    If `live=False`, returns a dry-run shape (no API calls). If
    `live=True`, the broker is loaded from env, the API is called,
    picks are logged, and the routed account is charged.
    """
    _, _, label = CRON_FIRES_CT[fire_index]
    sport_filter = sport_filter or set(ALL_SPORTS)

    # Default min_true_prob / min_edge_pp match the poller's pre-tip
    # urgency band (the picks need to clear 55% true + 0.5pp edge).
    min_true_prob = 0.55
    min_edge_pp = 0.5
    n_entries = 4
    entry_type = "6-flex"

    if not live:
        # Dry-run path: no broker, no compare call, no report file.
        # Useful for `python -m ud_edge_daily_report` smoke testing.
        return {
            "report_path": None,
            "picks_logged": 0,
            "propline_calls": 0,
            "mispriced_count": 0,
            "entry_type": entry_type,
            "fire_label": label,
            "dry_run": True,
        }

    # Live path.
    # Import lazily here so tests can patch the module-level function
    # via monkeypatch.setattr on ud_edge.compare — we don't bind the
    # function name at module load (that would shadow patches).
    from ud_edge import compare as _compare
    from ud_edge import broker as _broker
    broker = _broker.broker_from_env(
        **_broker.PROPLINE_BROKER,
        state_dir=BROKER_STATE_DIR,
    )

    payload = _compare.compare_fantasy_vs_sharp(
        entry_type=entry_type,
        min_true_prob=min_true_prob,
        min_edge_pp=min_edge_pp,
        sport_filter=sport_filter,
        full_game_only=True,
        mispriced_only=False,
        fantasy_csvs=None,
        n_entries=n_entries,
        force_fetch=True,
        return_ranked=True,
        line_tolerance=1.0,
    )
    if isinstance(payload, tuple) and len(payload) == 2:
        payload, _ = payload

    # Record the actual PropLine HTTP calls against the routed account.
    propline_calls = int(
        payload.get("sharp_meta", {}).get("propline_calls", 0) or 0
    )
    if propline_calls > 0:
        account = broker.route()
        broker.record(n=propline_calls, account=account)

    # Write the report.
    report_path = write_report(
        payload,
        fire_index=fire_index,
        fire_utc=fire_utc,
    )

    # Persist picks to data/results.json for the calibration loop.
    lineups = payload.get("lineups", [])
    picks_logged = 0
    for lineup in lineups:
        for opp in lineup.get("opportunities", []):
            try:
                log_picks(
                    date=fire_utc.strftime("%Y-%m-%d"),
                    entry=lineup.get("entry", 0),
                    leg=opp,
                    entry_type=entry_type,
                )
                picks_logged += 1
            except Exception:
                pass  # log_picks has its own diagnostics; don't break the fire

    # Notification (if a channel is configured and there are mispriced legs).
    mispriced_count = payload.get("totals", {}).get("mispriced", 0)
    if mispriced_count > 0 and configured_channels():
        # Fire one alert per top mispriced leg (de-duped by line_id)
        seen = set()
        for sport_block in payload.get("sports", []):
            for opp in sport_block.get("opportunities", []):
                if opp.get("is_mispriced") and opp.get("line_value"):
                    key = f"{opp.get('player_name')}|{opp.get('stat_name')}|{opp.get('picked_side')}|{opp.get('line_value')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    notify_opportunity(
                        player=opp.get("player_name", "?"),
                        pick=opp.get("side_label", "?"),
                        match=opp.get("match_title", "—"),
                        ud_pct=opp.get("ud_true_prob", 0.0),
                        sharp_pct=opp.get("sharp_true_prob") or 0.0,
                        delta_pp=opp.get("mispricing_edge_pp") or 0.0,
                        sharp_book=opp.get("sharp_book") or "sharp",
                        tips_in_min=0,
                        alert_key=key,
                        line_value=opp.get("line_value", 0.0),
                        platform=sport_block.get("sport", ""),
                    )

    return {
        "report_path": report_path,
        "picks_logged": picks_logged,
        "propline_calls": propline_calls,
        "mispriced_count": mispriced_count,
        "entry_type": entry_type,
        "fire_label": label,
        "broker_snapshot": broker.pool_snapshot(),
        "safety": safety_status(),
    }


# ── CLI ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ud_edge_daily_report",
        description=(
            "Multi-fire daily report for the Edge Board. Cron target. "
            "Run with --live to actually call the API; without --live "
            "the script is a dry-run smoke test (no API calls, no budget use)."
        ),
    )
    parser.add_argument(
        "--fire",
        type=int,
        choices=[0, 1, 2, 3],
        default=None,
        help=(
            "Which of the 4 cron fires to run: 0=10am, 1=12:30pm, 2=4pm, "
            "3=8pm Central. If omitted, run all 4 in sequence (test mode)."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually call the API and write the report. Without this, "
             "no API calls are made (smoke test).",
    )
    parser.add_argument(
        "--sport",
        type=str,
        default=None,
        help="Comma-separated sport filter (default: all supported sports).",
    )
    args = parser.parse_args(argv)

    sport_filter = None
    if args.sport:
        sport_filter = {s.strip().upper() for s in args.sport.split(",") if s.strip()}

    if args.fire is not None:
        # One-shot: fire a specific slot now.
        # The cron calls this with --fire N at the scheduled time.
        # For testing: --fire 0 fires the 10am slot now.
        now = datetime.now(timezone.utc)
        # Compute the matching UTC datetime for the requested slot
        # (we use today's CT date, since the cron runs on the same day).
        from zoneinfo import ZoneInfo
        ct = now.astimezone(ZoneInfo("America/Chicago"))
        target_h, target_m, _ = CRON_FIRES_CT[args.fire]
        # The fire UTC time is target_h+6 (CST) on today's CT date
        fire_ct = ct.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        fire_utc = fire_ct.astimezone(timezone.utc)
        result = run_one_fire(
            fire_index=args.fire, fire_utc=fire_utc,
            live=args.live, sport_filter=sport_filter,
        )
        print(f"Fire #{args.fire} ({CRON_FIRES_CT[args.fire][2]}): "
              f"report={result['report_path']}, "
              f"picks={result['picks_logged']}, "
              f"propline_calls={result['propline_calls']}, "
              f"mispriced={result['mispriced_count']}")
        return 0

    # No --fire: run all 4 in sequence (test mode).
    now = datetime.now(timezone.utc)
    print("DRY RUN — 4 fires in sequence. Use --live to actually call the API.")
    for i in range(4):
        fire_utc = now + timedelta(minutes=i * 1)
        result = run_one_fire(
            fire_index=i, fire_utc=fire_utc,
            live=False, sport_filter=sport_filter,
        )
        print(f"  Fire #{i} ({CRON_FIRES_CT[i][2]}): dry_run=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
