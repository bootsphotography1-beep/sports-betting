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

If you upgrade later with PropLine / the-odds-api keys, or rely on the
**owned scrapers** (`ud_edge/book_scrapers.py` — DraftKings + FanDuel public
JSON, no third-party key), the matcher architecture already supports it:
`build_sharp_index()` feeds sharp decimals into `rank_legs()`, which gates
on same-side sharp probability when present. Scrapers are on by default;
`--scrapers-only` ignores paid keys; `--no-scrapers` disables them.
Note: many DK/FD batter props are milestones (`1+`, `2+`), mapped to
Over `(N−0.5)` with a synthetic Under so overround = 1.0 — not true
two-sided sharp prices like Pinnacle.

### Multi-DFS misprice scan (`--misprices`)

Compares **Underdog / Sleeper / PrizePicks / Dabble** against sharp consensus
(Pinnacle → DK → FD → BetMGM) via PropLine. Surfaces line gaps and same-line
probability edges. Flipped/outlier sharp feeds are dropped against a
Pinnacle/DK anchor.

## Entry-type math

| Entry | n | Payouts | Per-leg break-even | EV at 55% / 60% |
|---|---|---|---|---|
| 3-man-power | 3 | 3/3 = 6× | 55.03% | -0.002 / +0.296 |
| 4-flex | 4 | 4/4=6×, 3/4=1.5× | 55.03% | -0.001 / +0.296 |
| 5-flex | 5 | 5/5=10×, 4/5=4×, 3/5=2× | 42.16% | +0.76 / +1.41 |
| 6-flex | 6 | 6/6=25×, 5/6=2×, 4/6=0.4× | 54.21% | +0.075 / +0.664 |

(EV is net per $1 staked, accounting for the full binomial distribution.
Break-evens are the exact EV=0 roots of each payout table — not padded
"safety" thresholds. The old 52.40% 6-flex figure was wrong: it treated
4/6 as a 1.0× return instead of Underdog's actual 0.4×.)

**6-flex is positive EV at 55% per leg** (about +$0.075 per dollar) —
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

## Limitations

- **No game-context filter.** The bot doesn't know about injuries, rest,
  back-to-backs, or weather. A 60% probability at "starting QB confirmed"
  is worth more than 60% at "starting QB uncertain." **Always do a 5-min
  injury scan before submitting the slip.**
- **Single-book source.** UD's prices are a reasonable proxy, but they
  include retail vig (~5%). A sharp-book cross-reference would tighten
  true-prob estimates by ~1-2pp.
- **No closing-line value tracking.** A +EV leg at 10am that moves against
  you by tip-off may not still be +EV.
- **No line-movement timing.** If UD moves the line at 11am, our 10am
  snapshot is stale.