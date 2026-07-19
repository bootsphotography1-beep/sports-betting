# ud-edge-bot

**No-vig +EV detector for Underdog Fantasy player props.**

Fetches Underdog's live player-prop board, strips the bookmaker's vig from
both-sided odds, ranks legs by statistical edge, and outputs a daily Markdown
report for use as a 3-man-power / 5-flex / 6-flex / etc. entry.

## Why this works

Underdog's `/beta/v5/over_under_lines` endpoint returns **6,718 player-prop
lines** across 17 sports — with **both sides priced** (American + decimal
odds + payout multiplier). When a book prices both sides of a prop, we can
strip the vig and recover the true probability of each side:

```
implied_over  = 1 / decimal_over
implied_under = 1 / decimal_under
overround     = implied_over + implied_under  (>1.0 = book vig)
true_over     = implied_over  / overround
true_under    = implied_under / overround
```

The **favorite side after vig removal** is the +EV side. Compare against
the entry-type break-even and rank.

## Quick start

```bash
cd ~/projects/ud-edge-bot
uv venv && uv pip install -e ".[dev]"

# 1. Math self-test (no network, ~2 sec)
python -m ud_edge --self-test

# 2. Dry run with synthetic data (no network, ~2 sec)
python -m ud_edge --dry-run

# 3. Live run — all sports, 6-flex entry
python -m ud_edge --once --entry 6-flex

# 4. NBA only, 3-man-power
python -m ud_edge --once --sport NBA --entry 3-man-power

# 5. Lower threshold (more picks, smaller edge)
python -m ud_edge --once --min-true-prob 0.52 --min-edge-pp 0.1

# 6. Save Markdown report
python -m ud_edge --once --entry 6-flex --save reports/$(date +%F).md

# 7. Build 3-4 disjoint 6-flex entries from the same edge pool
#    (Entry #1 = top-6, Entry #2 = next 6, ... — no shared legs)
python -m ud_edge --once --entry 6-flex --entries 4 --save reports/$(date +%F)_multi.md

# 8. Same, but with --full-game-only to drop mid-game / obscure-sport props
python -m ud_edge --once --entry 6-flex --entries 4 --full-game-only

# 9. Check calibration after settling some picks
python -m ud_edge --settle 0:hit:29.5   # mark pick #0 as a HIT with actual stat 29.5
python -m ud_edge --calibration         # see Brier score + log-loss + bucket accuracy
```

## Daily cron

The bot runs daily at **17:00 UTC** (10am PT / 1pm ET) via Hermes cron,
delivering the Markdown pick report to Slack automatically:

```bash
# Job: ud-edge-daily-picks (id: 1c72b160e604)
# Schedule: 0 17 * * *
# Script:   ~/.hermes/scripts/ud_edge_daily.sh
# Default mode: --entries 4 → 4 disjoint 6-flex entries (24 unique legs)
```

**Multi-entry mode** (`--entries N`): partitions the ranked legs into N
disjoint 6-leg lineups so you can place multiple cards from the same edge
pool. Entry #1 has the highest-edge legs; #4 has the floor. Auto-fallbacks
to fewer entries when the slate is thin (e.g. only 12 ranked legs → 2 entries).

**Full-game-only mode** (`--full-game-only`): drops mid-game props (period 1
strikeouts, first-half goals, mid-match tennis games) and obscure sports
(CS/LOL/DOTA/VAL/ESPORTS/RACING/CFL). Yields fewer but more time-stable
edges — recommended for the daily cron job.

**Result tracking** (`--calibration`, `--settle`): every run logs all picks
to `data/results.json`. Settle picks manually with `--settle <index>:hit:stat`
or `:miss:stat` after games complete, then check calibration (Brier score,
log-loss, predicted-vs-actual by prob bucket). Target: 50+ settled legs
before trusting EV estimates.

To add sharp-book cross-reference data, edit `data/sharp_lines.csv` with
today's Pinnacle / DraftKings / Bet365 lines for the players you want
to verify. ~5 min/day for ~10-30 lines. Bot re-ranks picks where UD
disagrees with the sharp book by ≥2pp.

## Stale pricing detection (Phase 1)

### Snapshot storage

The bot maintains an append-only SQLite snapshot store of every observed market:

```bash
# Fetch live UD data and save snapshots
python -m ud_edge --snapshot

# Run a stale/movement report on the existing snapshot DB (no new fetch)
python -m ud_edge --stale-report

# Custom thresholds
python -m ud_edge --snapshot \
  --snapshot-db data/line_snapshots.sqlite3 \
  --min-stale-minutes 30 \
  --fresh-window-minutes 120 \
  --min-line-gap 0.5 \
  --min-prob-gap-pp 3.0 \
  --min-movement-line 0.5 \
  --min-movement-prob-pp 3.0 \
  --save reports/stale_latest.md
```

The snapshot DB (`data/line_snapshots.sqlite3`) stores every observation with:
- Source, source line ID, player name, sport, stat, line value
- No-vig higher/lower true probabilities
- Match title and scheduled start time
- UTC capture timestamp
- Canonical market key (source-agnostic: `normalized_player|sport|normalized_stat|event_title|event_date`)

### Second source: CSV (and clipboard) ingestion

`--snapshot` accepts one or more additional source feeds so the cross-source
stale detector has something to compare against. Today the practical second
source is whatever you can capture yourself (PrizePicks / Sleeper / DK /
BetMGM) — the existing PP board parser is copied into
`ud_edge/pp_clipboard.py` and exposed via the CLI:

```bash
# One-command two-source snapshot (Underdog live + your CSV)
python -m ud_edge --snapshot \
  --ingest-csv data/draftkings_demo.csv --csv-source draftkings \
  --save reports/stale_two_source_demo.md

# Pull a fresh board from the Windows clipboard (PrizePicks / Sleeper)
python -m ud_edge --snapshot --ingest-prizepicks-clipboard
```

CSV columns (canonical order): `player_name, league, stat_type, line,
higher_decimal, lower_decimal, event_title, scheduled_at`. Extra columns are
ignored; rows missing `player_name`/`stat_type`/`line` are skipped. Add
`source` if you want per-row source labels; otherwise the CLI's
`--csv-source` label applies to every row.

Two-source end-to-end example with the included demo CSVs:

```bash
# 1) Live Underdog snapshot
python -m ud_edge --snapshot
# 2) Layer in a DraftKings board you typed up
python -m ud_edge --snapshot \
  --ingest-csv data/draftkings_demo.csv --csv-source draftkings \
  --save reports/stale_two_source_demo.md
# Result (with Jayson Tatum BOS@NYK Points moving draftkings 28.5 -> 28.0):
# 🟡 medium | prizepicks (28.5) -> draftkings (28.0) | lower | gap 1.0pts / 2.1pp
```

### Movement detection

The movement detector compares each source's latest observation with its prior
observation. A movement is recorded when EITHER:
- `|line_change| >= min_line_move` (absolute line shift)
- `|prob_shift_pp| >= min_prob_move_pp` (same-side true-probability shift in pp)

Movement direction is explicit: `up`, `down`, or `flat` (price-only move).

### Cross-source stale opportunity detection

A stale opportunity is flagged only when evidence exists for BOTH conditions:

1. **Stale source**: one source has been unchanged (same line) for ≥ `min_stale_minutes`
2. **Fresh source**: another source has meaningfully moved within `fresh_window_minutes`

Additionally, either a line gap ≥ `min_line_gap` OR a true-probability gap ≥ `min_prob_gap_pp`
must exist between the two sources. Static disagreement with no movement history is
**never** flagged as stale.

Stale-opportunity `direction` is the side to play on the stale source: `higher`
means Higher/Over; `lower` means Lower/Under. Both sources need at least two
observations so the detector can prove unchanged-vs-moved behavior.

Events with `scheduled_at` in the past are excluded by default (`reject_started=True`).

### Source limitations & PropLine (planned primary multi-book feed)

Direct scrapers for PrizePicks / Sleeper / Pinnacle from this host remain blocked
or unavailable. **[PropLine](https://prop-line.com)** (`api.prop-line.com`) is the
intended aggregator — it returns many books in one the-odds-api-compatible
response, including the DFS/exchange sources we care about:

| PropLine key | Use in ud-edge-bot |
|---|---|
| `underdog` | Two-way American prices (+ optional `payout_multiplier` on boosts) |
| `prizepicks` | **Line-only** second source (synthetic +100/+100 — not for no-vig) |
| `sleeper` | **Line-only** DFS second source when present |
| `dabble` | **Line-only** DFS second source when present |
| `pinnacle` / `draftkings` / `fanduel` | Same-side sharp true-prob / mispricing |
| `kalshi` / `polymarket` | Exchange / prediction markets (mostly game lines today) |

**To activate:** export `PROPLINE_API_KEY=...` (client scaffold lives in
[`ud_edge/propline_client.py`](ud_edge/propline_client.py)). Until the key is
set, the bot keeps using live Underdog + manual CSV / optional SportsGameOdds.

**Currently wired into snapshot history without PropLine:** Underdog Fantasy
(live) and `--ingest-csv` / `--ingest-prizepicks-clipboard`. PropLine snapshot
ingestion for PrizePicks/Sleeper/Dabble is next once the key is plugged in.

## Mispricing workflow (sharp-book cross-reference)

Priority when building the sharp index (later overrides earlier):

1. **Manual CSV** (`data/sharp_lines.csv`) — works today, no signup needed.
2. **SportsGameOdds** — optional `SPORTSGAMEODDS_KEY` (free tier; no Pinnacle).
3. **PropLine** — optional `PROPLINE_API_KEY` (preferred). Includes Pinnacle +
   Underdog two-way + DFS books. PrizePicks/Sleeper/Dabble synthetic even-money
   prices are **excluded from true-prob ranking**; Pinnacle/DK/FD/clean Underdog
   feed same-side mispricing.

The bot ranks legs where the **sharp book's same-side true prob exceeds UD's**
(positive mispricing). Opposite-side sharp disagreement demotes/filters the leg.

## Entry-type cheat sheet

| Entry | Payouts | Break-even | Verdict |
|---|---|---|---|
| 3-man-power | 3/3 = 6× | 54.95% | **Best risk/reward** for daily picks |
| 4-man-power | 4/4 = 10× | 63.00% | Skip — too high a bar |
| 4-flex | 4/4=6×, 3/4=1.5× | 57.81% | OK if you have 4 strong legs |
| 5-flex | 5/5=10×, 4/5=4×, 3/5=2× | 57.81% | Better than 4-flex for variance |
| **6-flex** | **6/6=25×, 5/6=2×, 4/6=0.4×** | **52.40%** | High variance, low break-even |

The bot's daily report includes an entry-type comparison so you can pick
the variance profile you want from the same leg pool.

## Architecture

```
ud_edge/
├── __init__.py              # loads .env
├── __main__.py              # CLI: --self-test | --dry-run | --once | --snapshot | --stale-report
├── models.py                # Pydantic: Player, Appearance, Game, Leg, RankedLeg, FlexEntryType
├── no_vig.py                # Pure math: decimal odds → true_prob (the +EV calc)
├── flex_math.py             # Payout tables + per-entry EV (3-power, 4-flex, 5-flex, 6-flex)
├── ud_client.py             # /beta/v5/over_under_lines fetcher + parser (line value from N+/N- threshold)
├── apisports_client.py      # apisports.io cross-ref (football fixtures today, predictions)
├── injury_client.py         # ESPN public injury feed (NBA/NFL/MLB/NHL/WNBA/CFB/EPL/MLS/WC)
├── sharp_books_client.py    # Sharp-book cross-ref (CSV + SGO + PropLine)
├── propline_client.py       # PropLine adapter (PROPLINE_API_KEY) — multi-book props
├── matcher.py               # rank_legs() + build_lineups() (multi-entry partitioner)
├── results_tracker.py        # log_picks + settle_pick + calibration_stats (per-pick tracking)
├── stale_pricing.py         # Phase 1: SnapshotStore, movement detector, stale opportunity detector
├── pp_clipboard.py          # PP/Sleeper clipboard + CSV adapter for the second-source feed
└── deliver.py              # build_report() + build_multi_report() (per-entry Markdown sections)
tests/
├── test_no_vig.py          # 64 math + injury + sharp-book + lineup + calibration assertions
├── test_stale_pricing.py   # snapshot + movement + stale-opportunity + migration + CLI assertions
└── test_second_source.py   # capture_from_observations + CSV + CLI second-source tests
data/
├── ud_lines_cache.json      # 10-min disk cache of live UD response
├── apisports_cache/         # per-endpoint disk cache (TTL-aware)
├── injury_cache/            # per-league ESPN injury cache (30-min TTL)
└── sharp_lines.csv          # user-maintained sharp-book lines (optional, manual cross-ref source)
reports/
└── YYYY-MM-DD_6flex.md      # saved pick reports
docs/
└── METHODOLOGY.md           # math derivation + assumptions + free-plan caveats
```

## Data sources

**Primary (no-vig calc):** Underdog Fantasy's own two-sided decimal odds
from `/beta/v5/over_under_lines`. ~6,700 lines today across 17 sports. **Free, unauthenticated.**

**Injury filter (free, no auth):** [ESPN public injury API](https://site.api.espn.com/apis/site/v2/sports/)
- 1,387 players tracked today across NBA / NFL / MLB / NHL / WNBA / CFB / EPL / MLS / World Cup
- 30-min disk cache (injury status changes hour-to-hour near game time)
- Players with status `OUT / INJURY_RESERVE / SUSPENDED / DOUBTFUL` are **filtered out** of the pick report — their legs are unplayable
- Players with `DAY_TO_DAY / QUESTIONABLE / PROBABLE` are kept in the report with a ⚠️ flag — verify on ESPN/team feed before submitting

**Sharp-book cross-reference (mispricing detection):**

- **Manual CSV source** (`data/sharp_lines.csv`) — works today, no signup.
  Format: `player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker`.
  ~5 min/day to update.
- **SportsGameOdds free tier** (auto source) — sign up at [sportsgameodds.com](https://sportsgameodds.com),
  export `SPORTSGAMEODDS_KEY=...`. Advertised free-tier coverage includes books such as
  FanDuel, DraftKings, BetMGM, Caesars, and ESPN BET; Pinnacle is not included.
  Validate the adapter against your live key before relying on its parsed props.
- Mispricings where the sharp book's true prob differs from UD's by ≥2pp
  are flagged in the report (Δ column, bolded when |Δ| ≥ 2pp).
- Legs where the sharp book gives HIGHER confidence than UD are boosted
  to the top of the ranking.

**Secondary cross-reference:** [apisports.io](https://apisports.io) (free tier)
- 100 req/day budget — cache aggressively
- Football fixtures today ✅ (723 results today)
- American-football games today ✅ (offseason — 0 games today)
- Predictions (win/draw/loss %) ✅
- Player stats ⚠️ — free plan returns mostly NULL fields
- **Odds endpoint ❌** — gated to 2022-2024 historical dates AND paid plans
- **Player props ❌** — gated to paid tier (verified via `/account` endpoint: 0/388 books have player props on free)

So apisports is currently used for **fixture validation and metadata enrichment**,
NOT as a sharp-book odds source. UD's own pricing remains the no-vig source.

## Source

Built on top of the unauthenticated `/beta/v5/over_under_lines` endpoint
discovered via [aidanhall21/underdog-fantasy-pickem-scraper](https://github.com/aidanhall21/underdog-fantasy-pickem-scraper)
(verified alive 2026-07-18).

## Disclaimer

Decision-support tool, not financial advice. Track every pick. Calibrate
the threshold after 50+ settled legs. Past performance does not predict
future results. Sports betting is gambling; bet only what you can afford
to lose.