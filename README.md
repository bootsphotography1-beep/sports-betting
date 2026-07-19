# ud-edge-bot

**Sharp-book vs fantasy mispricing detector** for Underdog, PrizePicks, and Sleeper.

Fetches Underdog's live player-prop board (plus optional PrizePicks/Sleeper CSV
boards), pulls sharp/reputable sportsbook prices, strips vig, ranks soft fantasy
lines, and serves a white **Edge Board** dashboard so you can browse by sport and
copy-paste picks into fantasy apps.

## Why this works

Underdog's `/beta/v5/over_under_lines` endpoint returns **thousands of player-prop
lines** across many sports — with **both sides priced** (American + decimal
odds + payout multiplier). When a book prices both sides of a prop, we can
strip the vig and recover the true probability of each side:

```
implied_over  = 1 / decimal_over
implied_under = 1 / decimal_under
overround     = implied_over + implied_under  (>1.0 = book vig)
true_over     = implied_over  / overround
true_under    = implied_under / overround
```

The **favorite side after vig removal** is the +EV side. When a sharp book
(DraftKings / FanDuel / BetMGM / manual Pinnacle CSV) disagrees with the fantasy
line on the **same side**, that mispricing is boosted to the top of the board.

## Quick start

```bash
cd ~/projects/ud-edge-bot
uv venv && uv pip install -e ".[dev,dashboard]"

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
python -m ud_edge --once --entry 6-flex --entries 4 --save reports/$(date +%F)_multi.md

# 8. Same, but with --full-game-only to drop mid-game / obscure-sport props
python -m ud_edge --once --entry 6-flex --entries 4 --full-game-only

# 9. Launch the white Edge Board dashboard on your computer
python -m ud_edge --serve
# → http://127.0.0.1:8787
```

## Edge Board dashboard

```bash
pip install -e ".[dashboard]"
python -m ud_edge --serve --host 127.0.0.1 --port 8787
```

Open **http://127.0.0.1:8787** in your browser. The board:

- Divides opportunities by **sport** (tabs + per-sport sections)
- Highlights legs where sharp books disagree with soft fantasy prices (≥2pp)
- Builds disjoint lineups you can paste into apps
- One-click **Copy** buttons for PrizePicks / Sleeper / Underdog formats
  (per pick, per sport, or entire slate)

Optional env keys for live sharp / fantasy pulls:

| Env var | Source |
|---|---|
| `PROPLINE_API_KEY` | **Preferred** — [PropLine](https://prop-line.com) (Pinnacle + DK/FD + PrizePicks/Underdog/Sleeper) |
| `ODDS_API_KEY` | [The Odds API](https://the-odds-api.com) player props |
| `SPORTSGAMEODDS_KEY` | [SportsGameOdds](https://sportsgameodds.com) free tier |

```bash
export PROPLINE_API_KEY=your_key_here
python -m ud_edge --serve
```

Without keys, `data/sharp_lines.csv` is still used as the sharp ground truth.

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

### Source limitations (verified 2026-07-18)

The following sources have been investigated and are **NOT** currently accessible:

| Source | Status | Reason |
|---|---|---|
| **PrizePicks** | ❌ 403 X-DataDome from this host | The Stack Overflow `/projections?...` method is currently challenged; keep a manual/partner-feed fallback |
| **Sleeper** | ❌ No Picks API | Public API has no player-prop or Picks endpoint |
| **Pinnacle** | ❌ Geo-blocked + closed | Public access closed 2025-07-23; geo-restricted |
| **Southpaw** | ❌ Wrong type | FanDuel DFS contest wrapper, not a sportsbook props feed |

**Currently wired into snapshot history:** Underdog Fantasy (live) and any
manually-supplied CSV (`--ingest-csv`); `--ingest-prizepicks-clipboard` reads
the Windows clipboard with the parser copied from the sibling
`prizepicks-edge-bot` project. `data/sharp_lines.csv` still powers one-run
mispricing ranking, but is not yet automatically snapshotted as a second
source. See the *Second source: CSV (and clipboard) ingestion* section
above for a working two-source demo.

## Mispricing workflow (sharp-book cross-reference)

The bot supports these sharp / fantasy data sources:

1. **PropLine** (`PROPLINE_API_KEY`) — **preferred**. Pinnacle + DK/FD/BetMGM
   plus PrizePicks / Underdog / Sleeper boards in one feed. Hobby ($9) / Pro ($19).

2. **Manual CSV** (`data/sharp_lines.csv`) — works with no signup.
   Copy lines from any sportsbook into this file as a fallback.

3. **The Odds API** (`ODDS_API_KEY`) — US-book player props (paid plans start $30).

4. **SportsGameOdds** (`SPORTSGAMEODDS_KEY`) — free Amateur tier available;
   paid plans are much higher.

Comparison is **side-aligned**: sharp over ↔ fantasy Higher/More,
sharp under ↔ Lower/Less. Soft fantasy prices rise to the top of Edge Board.

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
├── __main__.py              # CLI: --self-test | --once | --snapshot | --serve
├── models.py                # Pydantic: Leg, RankedLeg, …
├── no_vig.py                # Decimal/American odds → true_prob
├── flex_math.py             # Payout tables + per-entry EV
├── ud_client.py             # Underdog live board
├── sharp_books_client.py    # Manual CSV + Odds API + SportsGameOdds
├── compare.py               # Fantasy-vs-sharp pipeline (dashboard feed)
├── copy_format.py           # PrizePicks / Sleeper / Underdog paste lines
├── matcher.py               # rank_legs() + side-aligned sharp compare
├── stale_pricing.py         # Snapshot DB + stale opportunity detector
├── pp_clipboard.py          # CSV / clipboard second-source adapter
├── deliver.py               # Markdown reports
└── dashboard/               # White Edge Board frontend
    ├── app.py               # FastAPI: /api/opportunities, /api/export/{platform}
    └── static/              # index.html + styles.css + app.js
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
- **The Odds API** (`ODDS_API_KEY`) — US books player props (DK/FD/BetMGM, …).
- **SportsGameOdds free tier** (`SPORTSGAMEODDS_KEY`) — validate adapter vs live key.
- Comparison is **side-aligned** (sharp over ↔ fantasy Higher; sharp under ↔ Lower).
- Soft fantasy lines (sharp same-side true prob higher) are boosted on Edge Board.

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