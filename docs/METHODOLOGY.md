# Methodology

## The math: no-vig true probability

When a bookmaker prices both sides of a market (Over and Under), the sum
of the implied probabilities exceeds 1.0 — the difference is the book's
overround (vig). To recover the **true** probability (the real-world
frequency the side hits), we normalize:

```
Given decimal odds d_over, d_under:

  implied_over  = 1 / d_over
  implied_under = 1 / d_under
  overround     = implied_over + implied_under     # ≈ 1.05 for sharp pricing
  true_over     = implied_over  / overround
  true_under    = implied_under / overround
  (true_over + true_under) = 1.0 exactly
```

**Worked example** (from real UD data, 2026-07-18):

| Side | Decimal | Implied | True (after vig) |
|---|---|---|---|
| Higher (over) | 1.74 | 0.5747 | **54.69%** |
| Lower (under) | 2.10 | 0.4762 | 45.31% |
| Overround | — | 1.0509 | 5.09% vig |

The favorite side after vig-stripping (Higher, 54.69%) is the **+EV side**
for any entry whose break-even per leg is ≤ 54.69%.

## Why the favorite side wins

Intuitively: when a book prices a prop at -136 / +110, the public is
betting in proportions that imply the under is more likely than the over.
The book, taking the other side, ends up with a portfolio that wins more
often than it loses — but the **line** they offer reflects that. The
book's pricing is *their* probability estimate, *plus* vig.

After stripping vig, you get the book's best estimate of the true
probability. **The favorite side (≥50%) is more likely to hit than the
underdog side**, even after you account for vig. Taking the favorite at
real probability 54.69% in a 3-man-power (54.95% break-even) is roughly
EV-neutral. Taking it at 60% in the same entry is +EV.

## Why the bot uses Underdog's own odds

The skill loaded for this project describes the typical workflow as
**cross-reference UD against sharp-book odds** (Pinnacle, DK, FD). That's
the academically correct approach. But Underdog's own two-sided pricing
is **a reasonable proxy** for fair probability for several reasons:

1. **UD prices both sides** with explicit decimal odds. Many retail books
   don't even post two-sided player props before login.
2. **UD's `payout_multiplier` field** (0.86 to 1.10 in the data we tested)
   reflects their payout structure directly — this is similar info to what
   we'd cross-reference against a sharp book.
3. **UD's overround is ~5%**, comparable to retail books like BetMGM/Caesars.
   Not as sharp as Pinnacle (~2-3%) but better than nothing.

If you upgrade later to the-odds-api's paid tier ($79/mo) or add DK/FD
cross-reference, the matcher architecture supports it: replace the
`no_vig()` inputs with the sharper book's decimal odds and the rest of
the pipeline is unchanged.

## Entry-type math

| Entry | n | Payouts | Per-leg break-even | EV at 55% / 60% |
|---|---|---|---|---|
| 3-man-power | 3 | 3/3 = 6× | 54.95% | -0.002 / +0.296 |
| 4-flex | 4 | 4/4=6×, 3/4=1.5× | 57.81% | -0.082 / +0.190 |
| 5-flex | 5 | 5/5=10×, 4/5=4×, 3/5=2× | 57.81% | -0.143 / +0.151 |
| 6-flex | 6 | 6/6=25×, 5/6=2×, 4/6=0.4× | 52.40% | +0.066 / +0.585 |

(EV is net per $1 staked, accounting for the full binomial distribution.)

**6-flex is positive EV even at 55% per leg** — that's why it's tempting —
but it requires 6 simultaneous correct picks. A 5/6 still pays 2× (good),
but a 4/6 only pays 0.4× (you lose 60% of your stake). And a 3/6 or worse
pays nothing.

**3-man-power is the safest high-EV play**: at 60% per leg, EV is +$0.30 per
dollar staked. At 55%, it's basically break-even — only worth it if you have
a calibrated model.

## Calibration

After 50 settled picks, compute realized hit rate vs predicted and
recalibrate the threshold:

| Predicted bucket | Realized < Predicted - 2σ | Action |
|---|---|---|
| 50-55% | Y | Raise threshold to 55-60% bucket |
| 55-60% | N | Hold |
| 60-65% | Y | Raise to 65%+ bucket |

σ for p̂ at n=50: sqrt(p(1-p)/n) ≈ sqrt(0.55*0.45/50) ≈ 0.070 = 7pp.
So 2σ ≈ 14pp. The realized hit rate has to be **>14pp below predicted**
to flag a recalibration.

## Injury filter (live)

The live pipeline fetches ESPN's public injury feeds (NBA/NFL/MLB/NHL/WNBA/
CFB/FIFA) and **excludes** legs where the player is `OUT`, `INJURY_RESERVE`,
`SUSPENDED`, or `DOUBTFUL`. Players listed `DAY_TO_DAY` / `QUESTIONABLE` /
`PROBABLE` remain in the report with a ⚠️ flag — still verify before
submitting. Rest, back-to-backs, and weather are **not** modeled.

## Sharp-book same-side rule

When a sharp line is matched, the bot uses the sharp book's **true
probability for the same side UD picked** — never the sharp favorite if that
favorite is the opposite side. Legs where the sharp book disagrees
(same-side true prob < 50%) are demoted and usually filtered out. Positive
mispricing (sharp same-side > UD) boosts rank.

## Calibration prerequisite

Advertised EV assumes predicted true probs are calibrated. Settle picks with
`--settle` and check `--calibration`. **Do not trust EV estimates until
≥50 settled legs** show hit rate ≈ predicted within ~2σ.

## Limitations

- **Partial game-context.** Injuries OUT/IR/Suspended/Doubtful are filtered;
  rest, back-to-backs, and weather are not. Always sanity-check the slip.
- **Single-book source by default.** UD's prices are a reasonable proxy, but
  they include retail vig (~5%). Manual CSV / SportsGameOdds cross-ref
  tightens estimates only when same-side sharp data is present and fresh.
- **Independence assumed in EV.** Flex EV uses a binomial model; same-game
  correlation can erase modeled edge. Lineup builder prefers ≤1 leg per
  game/player when alternatives exist.
- **Multi-entry dilution.** Later entries use weaker legs; a floor gate
  (break-even + 1pp) stops emitting cards that fail the quality bar.
- **No closing-line value tracking.** A +EV leg at 10am that moves against
  you by tip-off may not still be +EV.
- **No line-movement timing.** If UD moves the line at 11am, our 10am
  snapshot is stale.