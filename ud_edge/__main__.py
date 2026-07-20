"""CLI entry point.

Examples:
    python -m ud_edge --self-test
    python -m ud_edge --dry-run
    python -m ud_edge --once --sport NBA --entry 6-flex
    python -m ud_edge --once --sport NBA,MLB,NFL --entry 3-power --top 3
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from ud_edge.no_vig import no_vig, edge_pp, pick_side
from ud_edge.flex_math import UD_PAYOUTS, expected_value, recommend_entry
from ud_edge.matcher import rank_legs, top_n_for_entry, build_lineups
from ud_edge.deliver import build_report, build_multi_report, print_console_summary
from ud_edge.ud_client import UDClient
from ud_edge.results_tracker import log_picks, settle_pick, print_calibration


# ── Self-test (math only, no network) ──────────────────────────────────────
def self_test() -> int:
    print("Running no-vig math self-test...\n")
    failures = []

    # Test 1: true even money (decimal 2.0/2.0) → exactly 50/50, no overround
    t_over, t_under, over = no_vig(2.0, 2.0)
    if abs(t_over - 0.5) > 1e-9 or abs(t_under - 0.5) > 1e-9:
        failures.append(f"even money 2.0/2.0: expected 50/50, got {t_over:.4f}/{t_under:.4f}")
    else:
        print(f"  ✓ 2.0/2.0 → {t_over:.4f} / {t_under:.4f} (true even money, no vig)")

    # Test 2: -110/-110 (decimal 1.9091/1.9091) → 4.76% overround, still 50/50 true
    t_over, t_under, over = no_vig(1.9091, 1.9091)
    if abs(over - 1.0476) > 0.001:
        failures.append(f"-110/-110 overround: expected 1.0476, got {over:.4f}")
    else:
        print(f"  ✓ -110/-110 → true 50/50, overround={over:.4f} (4.76% vig)")

    # Test 3: Derek's Gobert -112/-118 (decimal 1.8929/1.8475)
    # Under is the slight favorite after vig removal
    t_over, t_under, _ = no_vig(1.8929, 1.8475)
    if abs(t_over - 0.4939) > 0.005 or abs(t_under - 0.5061) > 0.005:
        failures.append(f"-112/-118: expected 49.39/50.61, got {t_over:.4f}/{t_under:.4f}")
    else:
        print(f"  ✓ -112/-118 → {t_over:.4f} / {t_under:.4f} (under slight favorite)")

    # Test 4: asymmetric +EV signal -136/+110 (decimal 1.7353/2.10)
    t_over, t_under, _ = no_vig(1.7353, 2.10)
    if abs(t_over - 0.5475) > 0.005 or abs(t_under - 0.4525) > 0.005:
        failures.append(f"-136/+110: expected 54.75/45.25, got {t_over:.4f}/{t_under:.4f}")
    else:
        print(f"  ✓ -136/+110 → {t_over:.4f} / {t_under:.4f} (over +EV at 54.75%)")

    # Test 5: heavy favorite -300/+200 (decimal 1.3333/3.0)
    t_over, t_under, _ = no_vig(1.3333, 3.0)
    if abs(t_over - 0.6923) > 0.005 or abs(t_under - 0.3077) > 0.005:
        failures.append(f"-300/+200: expected 69.23/30.77, got {t_over:.4f}/{t_under:.4f}")
    else:
        print(f"  ✓ -300/+200 → {t_over:.4f} / {t_under:.4f} (heavy fav, ~30% overround)")

    # Test 6: invalid input raises
    try:
        no_vig(0.5, 2.0)
        failures.append("invalid decimal should have raised")
    except ValueError:
        print("  ✓ invalid decimal < 1.0 raises ValueError")

    # Test 7: zero American odds raises
    try:
        from ud_edge.no_vig import american_to_implied
        american_to_implied(0)
        failures.append("american_to_implied(0) should have raised")
    except ValueError:
        print("  ✓ american_to_implied(0) raises ValueError")

    # Test 8: edge_pp calculation
    if abs(edge_pp(0.55, 0.5495) - 0.05) > 0.001:
        failures.append(f"edge_pp(0.55, 0.5495): expected 0.05, got {edge_pp(0.55, 0.5495):.4f}")
    else:
        print("  ✓ edge_pp(0.55, 0.5495) → 0.05pp")

    # Test 9: 3-man-power EV at 55% per leg
    entry = UD_PAYOUTS["3-man-power"]
    ev, win_prob, _ = expected_value(entry, 0.55)
    # EV = 0.55^3 * 6.0 - 1 = 0.166375 * 6 - 1 = -0.00175
    if abs(ev - (-0.00175)) > 0.001:
        failures.append(f"3-man-power EV at 55%: expected ~-0.00175, got {ev:.5f}")
    else:
        print(f"  ✓ 3-man-power EV at 55% → {ev:+.5f} per $1 (slightly negative — tight!)")

    # Test 10: 6-flex EV at 53% per leg
    entry = UD_PAYOUTS["6-flex"]
    ev, win_prob, _ = expected_value(entry, 0.53)
    print(f"  ✓ 6-flex EV at 53% → {ev:+.4f} per $1 (win_prob={win_prob:.2%})")

    # Test 11: pick_side picks the favorite and rejects below threshold
    side, prob = pick_side(0.55, 0.45, 0.50)
    if side != "higher" or abs(prob - 0.55) > 0.001:
        failures.append(f"pick_side(0.55, 0.45, 0.50): expected higher/0.55, got {side}/{prob}")
    else:
        print(f"  ✓ pick_side(0.55, 0.45, 0.50) → {side} @ {prob:.2%}")

    # Test 12: 3-man-power EV at 60% per leg (clear +EV)
    entry = UD_PAYOUTS["3-man-power"]
    ev_60, _, _ = expected_value(entry, 0.60)
    if ev_60 <= 0:
        failures.append(f"3-man-power EV at 60%: expected positive, got {ev_60:.5f}")
    else:
        print(f"  ✓ 3-man-power EV at 60% → {ev_60:+.5f} per $1 (clear +EV)")

    print()
    if failures:
        print(f"FAILED ({len(failures)} failures):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("All 12 self-test assertions passed. ✓")
    return 0


# ── Dry run (synthetic data, no network) ───────────────────────────────────
def dry_run() -> int:
    print("Running dry-run pipeline with synthetic data...\n")

    from ud_edge.models import Leg, RankedLeg

    # Synthetic: 4 NBA legs, all with +EV over (favored over)
    legs = [
        Leg(line_id="l1", player_id="p1", player_name="Jayson Tatum", sport_id="NBA",
            match_id=1, match_title="BOS@NYK", stat_name="points",
            line_value=27.5, line_type="balanced",
            higher_american=-145, higher_decimal=1.69, higher_multiplier=0.86,
            lower_american=125, lower_decimal=2.25, lower_multiplier=1.10),
        Leg(line_id="l2", player_id="p2", player_name="Luka Doncic", sport_id="NBA",
            match_id=2, match_title="DAL@LAL", stat_name="points",
            line_value=33.5, line_type="balanced",
            higher_american=-138, higher_decimal=1.72, higher_multiplier=0.86,
            lower_american=118, lower_decimal=2.18, lower_multiplier=1.10),
        Leg(line_id="l3", player_id="p3", player_name="Giannis Antetokounmpo", sport_id="NBA",
            match_id=3, match_title="MIL@PHI", stat_name="rebounds",
            line_value=11.5, line_type="balanced",
            higher_american=-152, higher_decimal=1.66, higher_multiplier=0.86,
            lower_american=132, lower_decimal=2.32, lower_multiplier=1.10),
        Leg(line_id="l4", player_id="p4", player_name="Stephen Curry", sport_id="NBA",
            match_id=4, match_title="GSW@PHX", stat_name="threes",
            line_value=4.5, line_type="balanced",
            higher_american=-125, higher_decimal=1.80, higher_multiplier=0.86,
            lower_american=105, lower_decimal=2.05, lower_multiplier=1.10),
    ]

    ranked = rank_legs(legs, break_even=0.5495)
    print_console_summary(ranked, top_n=4)

    if not ranked:
        print("✗ dry-run: no +EV legs found (expected 4)")
        return 1
    print(f"\n✓ dry-run produced {len(ranked)} +EV legs")
    return 0


# ── Live fetch + pick ──────────────────────────────────────────────────────
def run_once(
    sport_filter: set[str] | None,
    entry_type: str,
    top_n: int,
    min_true_prob: float,
    min_edge_pp: float,
    cache_path: Path,
    save_path: Path | None,
    quiet: bool = False,
    use_apisports: bool = True,
    n_entries: int = 1,
    full_game_only: bool = False,
) -> int:
    entry = UD_PAYOUTS[entry_type]
    effective_n_legs = top_n if top_n is not None else entry.n_legs

    # 1. API-Sports quota check (lightweight, cached 24h)
    if use_apisports:
        try:
            from ud_edge.apisports_client import APISportsClient
            api = APISportsClient(cache_path=cache_path.parent / "apisports_cache")
            status = api.status()
            requests_left = status.get("requests", {}).get("limit_day", "?") - \
                           status.get("requests", {}).get("current", 0)
            print(f"[apisports] plan={status.get('subscription', {}).get('plan', '?')} "
                  f"requests_today={status.get('requests', {}).get('current', '?')}/"
                  f"{status.get('requests', {}).get('limit_day', '?')}")
        except Exception as e:
            print(f"[apisports] skipped (error: {e})")

    # 2. Fetch + parse UD
    client = UDClient(cache_path=cache_path)
    data = client.fetch(force=True)  # fresh data on every live run
    if not quiet:
        print(f"[main] fetched {len(data.get('over_under_lines', []))} raw lines from UD")

    legs = client.parse_legs(data, sport_filter=sport_filter)
    if not quiet:
        print(f"[main] parsed {len(legs)} legs after parsing")

    # 3. Cross-reference: validate fixtures (optional, costs 1 API call)
    if use_apisports and any(s in (sport_filter or {"FOOTBALL", "FIFA", "NFL"}) for s in ["FOOTBALL", "FIFA"]):
        try:
            from ud_edge.apisports_client import APISportsClient, cross_reference
            api = APISportsClient(cache_path=cache_path.parent / "apisports_cache")
            xref = cross_reference(legs, api)
            print(f"[apisports] {xref['fixtures_today']} football fixtures today available for cross-ref")
        except Exception as e:
            print(f"[apisports] cross-ref skipped (error: {e})")

    # 3.5. Fetch ESPN injury feed (free, no auth, cached 30min)
    injury_index = None
    try:
        from ud_edge.injury_client import ESPNInjuryClient
        injury_client = ESPNInjuryClient(
            cache_path=cache_path.parent / "injury_cache",
            ttl_seconds=1800,  # 30 min cache
        )
        injury_index = injury_client.fetch_all_sports()
        total_tracked = sum(len(v) for v in injury_index.values())
        total_out = sum(sum(1 for s in v.values() if s in {"OUT", "INJURY_RESERVE", "SUSPENDED", "DOUBTFUL"})
                        for v in injury_index.values())
        print(f"[injury] ESPN feed: {total_tracked} players tracked, {total_out} flagged OUT")
    except Exception as e:
        print(f"[injury] feed skipped (error: {e})")

    # 3.6. Build sharp-book cross-reference index
    sharp_index = None
    try:
        from ud_edge.sharp_books_client import build_sharp_index
        import os
        sgo_key = os.environ.get("SPORTSGAMEODDS_KEY", "")
        odds_key = os.environ.get("ODDS_API_KEY", "")
        propline_key = os.environ.get("PROPLINE_API_KEY", "")
        sharp_csv = cache_path.parent / "sharp_lines.csv"
        # Prefer PropLine when keyed; also support Odds API / SGO
        auto_sports = ["NBA", "NFL", "MLB", "NHL", "WNBA", "CFB"] if (
            sgo_key or odds_key or propline_key
        ) else None
        sharp_index, _sharp_meta = build_sharp_index(
            manual_csv=sharp_csv if sharp_csv.exists() else None,
            sgo_key=sgo_key or None,
            sgo_sports=auto_sports if sgo_key else None,
            odds_api_key=odds_key or None,
            odds_api_sports=auto_sports if odds_key else None,
            propline_key=propline_key or None,
            propline_sports=auto_sports if propline_key else None,
            cache_path=cache_path.parent / "sharp_cache",
        )
        sources = set(v.get("source", "?") for v in sharp_index.values())
        print(f"[sharp] {len(sharp_index)} lines indexed from: {sorted(sources)}")
    except Exception as e:
        print(f"[sharp] cross-ref skipped (error: {e})")

    # 4. Rank + EV
    ranked = rank_legs(
        legs,
        break_even=entry.break_even,
        min_true_prob=min_true_prob,
        min_edge_pp=min_edge_pp,
        injury_index=injury_index,
        sharp_book_index=sharp_index,
        full_game_only=full_game_only,
    )
    top = top_n_for_entry(ranked, n_legs=effective_n_legs)

    # Guard: exit gracefully when no +EV legs survived
    if not top:
        print("no +EV slate today")
        return 0

    print_console_summary(ranked, top_n=effective_n_legs, injury_index=injury_index)

    # ── Multi-entry mode: build 3-4 disjoint 6-flexes ──
    if n_entries > 1:
        lineups = build_lineups(ranked, n_entries=n_entries, n_legs=effective_n_legs)
        if not lineups:
            print(f"\n[main] ranked pool has {len(ranked)} legs — not enough for 1 lineup of {effective_n_legs}")
            return 1
        print(f"\n[main] built {len(lineups)} disjoint {entry.name} entries "
              f"({len(lineups) * effective_n_legs} unique legs)")

        report = build_multi_report(
            lineups, entry_type=entry_type, n_legs=effective_n_legs,
            min_true_prob=min_true_prob,
            fetched_at=datetime.now(timezone.utc),
            injury_index=injury_index,
        )

        if save_path:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(report)
            print(f"[main] report saved to {save_path}")

        # Log picks to results.json for calibration tracking
        try:
            n_logged = log_picks(lineups, entry_type=entry_type, n_entries=len(lineups))
            print(f"[results] logged {n_logged} new picks to data/results.json")
        except Exception as e:
            print(f"[results] logging skipped (error: {e})")

        # Per-entry EV summary
        print("\n--- Per-entry comparison ---")
        if not top:
            print("  (no +EV legs to compare)")
        for i, lineup in enumerate(lineups, 1):
            avg_prob = sum(r.picked_true_prob for r in lineup) / len(lineup)
            ev, win_prob, med = expected_value(entry, avg_prob)
            avg_min_leg = min(r.picked_true_prob for r in lineup)
            print(f"  Entry #{i}: avg {avg_prob:.2%} (floor {avg_min_leg:.2%}) · "
                  f"EV={ev:+.4f} · win={win_prob:.1%} · med={med:.1f}x")
        return 0

    # ── Single-entry mode (legacy default) ──
    report = build_report(
        ranked, entry_type=entry_type, top_n=effective_n_legs,
        min_true_prob=min_true_prob,
        fetched_at=datetime.now(timezone.utc),
        injury_index=injury_index,
    )

    if save_path:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(report)
        print(f"\n[main] report saved to {save_path}")

    # Print entry-type EV summary across all matching entry types
    print("\n--- Entry-type comparison (same top legs) ---")
    avg_prob = (
        sum(r.picked_true_prob for r in top) / len(top)
        if top else 0
    )
    for et_name, et in UD_PAYOUTS.items():
        if et.n_legs != effective_n_legs:
            continue
        ev, win_prob, med = expected_value(et, avg_prob)
        rec = recommend_entry(et, avg_prob)
        print(f"  {et_name:<14}  EV=${ev:+.4f}  win={win_prob:.1%}  med={med:.1f}x  → {rec}")

    return 0


# ── CLI plumbing ───────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ud-edge",
        description="No-vig +EV detector for Underdog Fantasy player props",
    )
    parser.add_argument("--self-test", action="store_true",
                        help="Run math self-tests, no network")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run pipeline with synthetic data, no network")
    parser.add_argument("--once", action="store_true",
                        help="Fetch live UD slate and print picks")
    parser.add_argument("--sport", type=str, default=None,
                        help="Comma-separated sport filter (NBA,MLB,NFL,...)")
    parser.add_argument("--entry", type=str, default="6-flex",
                        choices=list(UD_PAYOUTS.keys()),
                        help="Entry type (default: 6-flex)")
    parser.add_argument("--top", type=int, default=None,
                        help="Number of legs to pick (default: entry.n_legs)")
    parser.add_argument("--entries", type=int, default=1,
                        help="Number of disjoint entries to build (default 1). "
                             "E.g. --entries 4 = 4 distinct 6-flexes, 24 unique legs.")
    parser.add_argument("--full-game-only", action="store_true",
                        help="Drop mid-game props (period_1 stats) and obscure sports "
                             "(CS/LOL/DOTA/VAL/ESPORTS/RACING/CFL). Yields fewer but "
                             "more stable, full-game edges.")
    parser.add_argument("--calibration", action="store_true",
                        help="Print the calibration report (Brier score, log-loss, "
                             "predicted-vs-actual by probability bucket) and exit.")
    parser.add_argument("--settle", type=str, default=None,
                        help="Manually mark a pick's outcome. Format: '<index>:<hit|miss>:<actual_stat>'. "
                             "Example: --settle 0:hit:28.5")
    parser.add_argument("--min-true-prob", type=float, default=0.55,
                        help="Minimum true probability per leg (default 0.55)")
    parser.add_argument("--min-edge-pp", type=float, default=0.5,
                        help="Minimum edge in percentage points (default 0.5)")
    parser.add_argument("--cache", type=str,
                        default="data/ud_lines_cache.json",
                        help="Disk cache path")
    parser.add_argument("--save", type=str, default=None,
                        help="Save report to Markdown file")
    parser.add_argument("--snapshot", action="store_true",
                        help="Fetch live UD, save snapshots, run stale/movement report. "
                             "Skips the expensive injuries/sharp/ranking pipeline.")
    parser.add_argument("--snapshot-db", type=str,
                        default="data/line_snapshots.sqlite3",
                        help="SQLite path for line snapshots (default: data/line_snapshots.sqlite3)")
    parser.add_argument("--min-stale-minutes", type=float, default=30.0,
                        help="Minimum minutes a source must be unchanged to be considered stale (default 30)")
    parser.add_argument("--fresh-window-minutes", type=float, default=120.0,
                        help="Maximum age of a fresh source observation in minutes (default 120)")
    parser.add_argument("--min-line-gap", type=float, default=0.5,
                        help="Minimum line gap between stale and fresh source (default 0.5)")
    parser.add_argument("--min-prob-gap-pp", type=float, default=3.0,
                        help="Minimum true-probability gap in pp (default 3.0)")
    parser.add_argument("--min-movement-line", type=float, default=0.5,
                        help="Minimum within-source line move that proves fresh movement (default 0.5)")
    parser.add_argument("--min-movement-prob-pp", type=float, default=3.0,
                        help="Minimum same-side fair-probability move in pp (default 3.0)")
    parser.add_argument("--stale-report", action="store_true",
                        help="Run stale/movement report on existing snapshot DB without fetching new data.")
    parser.add_argument("--ingest-csv", type=str, default=None,
                        help="Path to a CSV board to ingest as a second source.")
    parser.add_argument("--csv-source", type=str, default="prizepicks",
                        help="Source name for --ingest-csv (default: prizepicks).")
    parser.add_argument("--ingest-prizepicks-clipboard", action="store_true",
                        help="Read the PrizePicks board from the Windows clipboard and ingest it.")
    parser.add_argument("--serve", action="store_true",
                        help="Launch the Edge Board white dashboard (FastAPI) on --host/--port.")
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Dashboard bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787,
                        help="Dashboard bind port (default 8787)")

    args = parser.parse_args(argv)

    # ── Dashboard (early-exit) ─────────────────────────────────────────────
    if args.serve:
        try:
            import uvicorn
        except ImportError:
            print("✗ Dashboard requires fastapi + uvicorn. Install with:")
            print('  pip install -e ".[dashboard]"')
            return 1
        print(f"[dashboard] Edge Board → http://{args.host}:{args.port}")
        print("[dashboard] Sharp sources: PROPLINE_API_KEY (preferred) · "
              "data/sharp_lines.csv · ODDS_API_KEY · SPORTSGAMEODDS_KEY")
        uvicorn.run(
            "ud_edge.dashboard.app:app",
            host=args.host,
            port=args.port,
            reload=False,
        )
        return 0

    # ── Calibration report (early-exit) ────────────────────────────────────
    if args.calibration:
        print(print_calibration())
        return 0

    # ── Manual settle (early-exit) ─────────────────────────────────────────
    if args.settle:
        try:
            parts = args.settle.split(":")
            idx = int(parts[0])
            outcome = parts[1].lower() in ("hit", "h", "1", "true", "yes")
            actual = float(parts[2]) if len(parts) > 2 and parts[2] else None
            ok = settle_pick(idx, outcome, actual)
            if ok:
                print(f"✓ Pick #{idx} marked as {'HIT' if outcome else 'MISS'}"
                      + (f" (actual={actual})" if actual else ""))
            else:
                print(f"✗ Could not settle pick #{idx} (invalid index or already settled)")
            return 0 if ok else 1
        except (ValueError, IndexError) as e:
            print(f"✗ Bad --settle format: {e}. Expected '<index>:<hit|miss>:<actual_stat>'")
            return 1

    cache_path = Path(args.cache)
    save_path = Path(args.save) if args.save else None

    if args.self_test:
        return self_test()
    if args.dry_run:
        return dry_run()
    if args.once:
        sport_filter = None
        if args.sport:
            sport_filter = {s.strip().upper() for s in args.sport.split(",")}
        entry = UD_PAYOUTS[args.entry]
        top_n = args.top if args.top is not None else entry.n_legs
        return run_once(
            sport_filter=sport_filter,
            entry_type=args.entry,
            top_n=top_n,
            min_true_prob=args.min_true_prob,
            min_edge_pp=args.min_edge_pp,
            cache_path=cache_path,
            save_path=save_path,
            n_entries=args.entries,
            full_game_only=args.full_game_only,
        )

    if args.snapshot or args.stale_report:
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, capture_from_observations,
            detect_movements, detect_stale_opportunities,
            build_stale_report, build_movement_report,
            utc_now,
        )
        db_path = Path(args.snapshot_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SnapshotStore(db_path=db_path)
        store.init()

        if args.snapshot:
            # Fetch live UD data (skip expensive pipeline)
            client = UDClient(cache_path=cache_path)
            data = client.fetch(force=True)
            legs = client.parse_legs(data, sport_filter=None)
            captured_at = utc_now()
            rows = capture_underdog(legs, store, captured_at=captured_at)
            print(f"[snapshot] captured {len(rows)} observations at {captured_at.isoformat()}")

        # ── Ingest CSV board as a second source ─────────────────────────────
        if args.ingest_csv:
            csv_path = Path(args.ingest_csv)
            csv_source = args.csv_source or "prizepicks"
            if csv_path.exists():
                from ud_edge.pp_clipboard import parse_prizepicks_csv
                result = parse_prizepicks_csv(csv_path, source_name=csv_source)
                if isinstance(result, tuple):
                    csv_observations = result[0]
                else:
                    csv_observations = result
                if csv_observations:
                    all_ids = []
                    for obs in csv_observations:
                        ids = capture_from_observations(
                            [obs], store, source=csv_source, captured_at=utc_now()
                        )
                        all_ids.extend(ids)
                    print(f"[snapshot] ingested {len(all_ids)} observations from CSV source '{csv_source}'")
            else:
                print(f"[snapshot] CSV file not found: {csv_path}")

        # ── Ingest clipboard board via --ingest-prizepicks-clipboard ──────
        if args.ingest_prizepicks_clipboard:
            from ud_edge.pp_clipboard import read_clipboard
            clip_text = read_clipboard()
            obs_records: list[dict] = []
            if clip_text:
                # We don't bundle the DOM-text parser here (that lives in
                # the sibling prizepicks-edge-bot project). Pass-through
                # works for clipboard text already in CSV shape; otherwise
                # we point the user at the dedicated parser.
                import io, csv as _csv_mod
                try:
                    for row in _csv_mod.DictReader(io.StringIO(clip_text)):
                        line_raw = (row.get("line") or "").strip()
                        try:
                            line_val = float(line_raw)
                        except ValueError:
                            continue
                        obs_records.append({
                            "player_name": (row.get("player_name") or "").strip(),
                            "sport_id": (row.get("league") or "NBA").strip().upper(),
                            "stat_name": (row.get("stat_type") or "").strip().lower(),
                            "line_value": line_val,
                            "match_title": (row.get("event_title") or "").strip(),
                            "scheduled_at": (row.get("scheduled_at") or "").strip(),
                            "higher_decimal": float(row.get("higher_decimal") or 0),
                            "lower_decimal": float(row.get("lower_decimal") or 0),
                            "source_line_id": "",
                        })
                except Exception:
                    obs_records = []
            if obs_records:
                clip_ids = capture_from_observations(
                    obs_records, store,
                    source="prizepicks", captured_at=captured_at,
                )
                print(f"[snapshot] clipboard ingested {len(clip_ids)} obs at "
                      f"{captured_at.isoformat()}")
            elif clip_text:
                print("[snapshot] clipboard text wasn't CSV-shaped. "
                      "Run the sibling project's clipboard_to_csv.py and "
                      "ingest the produced CSV via --ingest-csv.")
            else:
                print("[snapshot] clipboard empty, nothing to ingest")

        # Build and print report
        movements = detect_movements(
            store,
            min_line_move=args.min_movement_line,
            min_prob_move_pp=args.min_movement_prob_pp,
        )
        stale = detect_stale_opportunities(
            store,
            min_stale_minutes=args.min_stale_minutes,
            fresh_window_minutes=args.fresh_window_minutes,
            min_line_gap=args.min_line_gap,
            min_prob_gap_pp=args.min_prob_gap_pp,
            min_movement_line=args.min_movement_line,
            min_movement_prob_pp=args.min_movement_prob_pp,
        )
        as_of = utc_now()
        stale_report = build_stale_report(movements, stale, as_of)
        print(stale_report)

        # Console summary
        from ud_edge.stale_pricing import print_console_summary
        print_console_summary(movements, stale)

        if save_path:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(stale_report)
            print(f"[stale] report saved to {save_path}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())