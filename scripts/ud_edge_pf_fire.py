"""PrizePicks-specific mispricing fire.

Unlike `ud_edge_fire.py` (which routes to the book closest to sharp), this
script emits legs where **PrizePicks specifically** has a stale line vs
sharp. The destination book is forced to `prizepicks` regardless of how
close other books (Sleeper, UD) are to sharp.

Use case: when Fin wants to bet specifically on PF — not "best book per
leg" — this shows what PF's mispricings look like right now.

Output: same Telegram format as the main fire, but every pick says
`BET ON PRIZEPICKS` and the edge is calculated against PF's no-vig prob
specifically. Includes the `VERIFY ON APP` footer (PF drops Less sides
mid-day as players lock — verify each leg on the actual app).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import ud_edge_fire  # noqa: E402  reuse helpers
from scripts.ud_edge_fire import (  # noqa: E402
    DEFAULT_BUDGET_PER_FIRE,
    TIERS,
    alert_both_keys_exhausted,
    build_fantasy_lookup,
    load_dotenv,
    log,
    refresh_dashboard,
    run_compare,
    send_telegram,
)


def reroute_to_pf(legs: list[dict], include_fantasy_only: bool = False) -> list[dict]:
    """Override `fantasy_book` to 'prizepicks' for any leg that has a PF price.

    For each leg, if PF has a non-None price in `all_fantasy_books`, force the
    destination book to PF and recompute `fantasy_prob` and `ev` against PF
    specifically.

    Two cases:
    1. vs_sharp legs: edge = (sharp - PF) - 1pp friction. Drops legs where
       PF is overpriced vs sharp (negative edge).
    2. fantasy-only legs (no sharp): only included if `include_fantasy_only=True`.
       Edge = (PF prob - 54.2% break-even) - 1pp friction. Drops legs where PF
       is overpriced vs break-even.

    Why drop legs without PF coverage: a leg with no PF price means PF doesn't
    have that player/stat/line at all — those can't be PF mispricings.

    Synthetic-default flag: if PF's no-vig prob is exactly 50.0% (the default
    even-money value used when a side is absent on the app), add a flag to the
    leg so the formatter can tag it as 'PF-SYNTHETIC' — operator should verify
    on the PF app that the side is actually offered before placing.
    """
    BREAK_EVEN_6FLEX = 54.21  # 6-flex break-even (from ud_edge.payouts)
    pf_only = []
    for leg in legs:
        all_fb = leg.get("all_fantasy_books") or {}
        pf_prob = all_fb.get("prizepicks")
        if pf_prob is None:
            continue
        pf_prob_pct = round(float(pf_prob) * 100, 1)
        sharp_prob = leg.get("sharp_prob")
        try:
            sharp_prob_f = float(sharp_prob) if sharp_prob not in (None, "?") else None
        except (TypeError, ValueError):
            sharp_prob_f = None

        if sharp_prob_f is not None:
            # vs_sharp: edge = sharp - PF - friction
            raw_edge = sharp_prob_f - pf_prob_pct
            ev = round(raw_edge - 1.0, 2)
            new_edge_kind = "vs_sharp_pf"
        elif include_fantasy_only:
            # fantasy-only: edge = PF - break_even - friction
            raw_edge = pf_prob_pct - BREAK_EVEN_6FLEX
            ev = round(raw_edge - 1.0, 2)
            new_edge_kind = "vs_breakeven_pf"
        else:
            continue

        if ev <= 0:
            # PF overpriced vs the benchmark — not actionable
            continue
        new_leg = dict(leg)
        new_leg["fantasy_book"] = "prizepicks"
        new_leg["fantasy_prob"] = pf_prob_pct
        new_leg["ev"] = ev
        new_leg["edge_kind"] = new_edge_kind
        # Flag synthetic 50% defaults so operator can verify on the PF app
        # before placing. Exact match for 50.0%; we treat this as the canonical
        # "default even money" value the parser uses when one side is absent.
        new_leg["pf_synthetic_default"] = (pf_prob_pct == 50.0)
        pf_only.append(new_leg)
    return pf_only


def format_pf_message(tier: str, legs: list[dict], min_edge_pp: float) -> str:
    """PF-specific Telegram message.

    Reuses the main format_message() but:
    - Lowers the tier threshold to `min_edge_pp` (typically 3-4pp) because
      PF-specific mispricings are tighter than standard 5-12pp thresholds.
    - Tags synthetic 50% defaults with a 'PF-SYNTHETIC' marker so the
      operator knows the price may not exist on the actual app.
    """
    # Monkey-patch: temporarily lower the tier threshold for PF-specific fire
    orig_threshold = TIERS[tier][1]
    orig_max_legs = TIERS[tier][2]
    TIERS[tier] = (TIERS[tier][0], min_edge_pp, orig_max_legs, TIERS[tier][3])
    try:
        msg = ud_edge_fire.format_message(tier, legs)
    finally:
        # Restore so we don't pollute subsequent calls in the same process
        TIERS[tier] = (
            TIERS[tier][0],
            orig_threshold,
            orig_max_legs,
            TIERS[tier][3],
        )
    # Append a synthetic-default section if any legs have the flag set
    synth_legs = [lg for lg in legs if lg.get("pf_synthetic_default")]
    if synth_legs:
        synth_lines = [
            "",
            "⚠️ *PF-SYNTHETIC WARNING* ({} legs):".format(len(synth_legs)),
            "  These picks show PF=50.0% Under/Over — that's the parser's "
            "default even-money value when a side is absent on the PF app. "
            "Verify each on the actual app before placing. If PF doesn't "
            "offer the Less/More side, the pick is unplaceable.".format(),
            *(f"  - {lg['player']} {lg['stat']} {lg['line']} {lg.get('side_label','')} → "
              f"BET ON PRIZEPICKS ({lg['fantasy_prob']}% vs {lg.get('sharp_book','?')} "
              f"{lg.get('sharp_prob','?')}%) — edge +{lg['ev']}pp"
              for lg in synth_legs[:10]),
        ]
        msg = msg + "\n".join(synth_lines)
    return msg


def main():
    ap = argparse.ArgumentParser(
        description="PrizePicks-specific mispricing fire. "
                    "Forces destination book to PF regardless of proximity-to-sharp.",
    )
    ap.add_argument("--tier", required=True, choices=list(TIERS.keys()))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget-per-fire", type=int, default=DEFAULT_BUDGET_PER_FIRE)
    ap.add_argument(
        "--pf-min-edge",
        type=float,
        default=3.0,
        help=(
            "Min edge (pp) for the PF-specific fire. Default 3.0pp is lower than "
            "the standard tier thresholds because PF-specific mispricings are "
            "typically tighter (PF is the sharpest fantasy book). Standard "
            "tiers use 5-12pp, which would drop most PF picks."
        ),
    )
    ap.add_argument(
        "--show-pf-synthetic",
        action="store_true",
        help=(
            "Show PF legs with the synthetic 50% default flag, even when they "
            "are technically unplaceable (PF doesn't offer the side on the app). "
            "Useful for the operator to see what mispricings PF WOULD have if "
            "they actually offered the side. Off by default."
        ),
    )
    ap.add_argument(
        "--include-fantasy-only",
        action="store_true",
        help=(
            "Include fantasy-only PF legs (no sharp match) where PF's no-vig prob "
            "exceeds the 54.2% 6-flex break-even by >= --pf-min-edge. Off by "
            "default — the bot's sharp-only filter applies."
        ),
    )
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")

    desc, threshold, _, _ = TIERS[args.tier]
    log(f"=== PF-SPECIFIC {args.tier.upper()} fire "
        f"(tier threshold {threshold}% EV, PF min edge {args.pf_min_edge}pp, "
        f"budget={args.budget_per_fire} calls) ===")

    # 0. Broker pre-check
    alert_both_keys_exhausted(budget_per_fire=args.budget_per_fire)

    # 1. Force-refresh dashboard (live PropLine pull)
    refresh_ok, refresh_msg = refresh_dashboard(threshold)
    log(f"dashboard refresh={refresh_ok} {refresh_msg}")

    # 2. Run compare + load raw fantasy legs for per-book coverage
    if args.show_pf_synthetic:
        # Bypass tier threshold: pull a wider set so we can show PF legs that
        # the standard fire would filter out (because they're synthetic 50%
        # defaults with low raw edge).
        from ud_edge.compare import compare_fantasy_vs_sharp
        payload, ranked = compare_fantasy_vs_sharp(
            entry_type="6-flex",
            min_true_prob=0.50,
            min_edge_pp=-10,
            mispriced_only=False,
            force_fetch=True,
            return_ranked=True,
            line_tolerance=0.5,
        )
        log(f"show-pf-synthetic: bypassed tier filter, {len(ranked)} raw ranked")
        # Skip fantasy_legs loading (not needed for synthetic view)
        fantasy_legs = []
    else:
        payload, ranked, fantasy_legs = run_compare(args.tier)
        log(f"compare returned {len(ranked)} ranked, {len(fantasy_legs)} raw fantasy legs")

    # 3. Build per-book lookup and parse
    fantasy_lookup = build_fantasy_lookup(fantasy_legs)
    all_legs = ud_edge_fire.parse_legs(payload, fantasy_lookup)
    log(f"parsed {len(all_legs)} legs with per-book coverage")

    # 4. PF-specific reroute (force destination to PF; include fantasy-only
    # legs only if --include-fantasy-only is set)
    pf_legs = reroute_to_pf(all_legs, include_fantasy_only=args.include_fantasy_only)
    log(f"PF-specific reroute: {len(pf_legs)} legs with placeable PF coverage "
        f"(include_fantasy_only={args.include_fantasy_only})")
    pf_legs.sort(key=lambda lg: -lg["ev"])
    if pf_legs:
        top = pf_legs[0]
        log(f"top PF pick: {top['player']} {top['ev']}pp")
    else:
        log("no PF picks")

    # 6. Format
    title_suffix = f" | PF-specific (≥{args.pf_min_edge}pp)"
    body = format_pf_message(args.tier, pf_legs, args.pf_min_edge)
    title = f"UD Edge | {args.tier.replace('_', ' ').title()}{title_suffix}"

    if args.dry_run:
        print("=" * 60)
        print(f"TITLE: {title}")
        print("=" * 60)
        print(body)
        print("=" * 60)
        return

    ok = send_telegram(title, body)
    log(f"telegram send: {ok}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()