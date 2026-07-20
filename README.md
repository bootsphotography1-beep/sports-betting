# ud-edge-bot

> **Sharp-book vs fantasy mispricing detector for Underdog, PrizePicks, and Sleeper.**
> Pulls Underdog's live player-prop board, strips vig, ranks soft fantasy lines,
> cross-references sharp-book prices when available, and serves a white **Edge
> Board** dashboard on your LAN / Tailscale so you can browse picks by sport and
> copy-paste ready lines into the fantasy apps.

> **⚠️ RESEARCH-ONLY MODE** — payout model is mathematically verified, but the
> bot is **not externally calibrated**. Recommendation labels say
> `RESEARCH ESTIMATE (unverified model)`. ≥50 settled legs + Brier/log-loss
> bands required before lifting to verified mode. See `HONEST_STATUS.md`.

---

## Table of contents

1. [Quick start](#quick-start)
2. [The Edge Board dashboard](#the-edge-board-dashboard)
3. [Dashboard API reference](#dashboard-api-reference)
4. [CLI reference](#cli-reference)
5. [Entry-type cheat sheet](#entry-type-cheat-sheet)
6. [How the math works](#how-the-math-works)
7. [Stale pricing detection](#stale-pricing-detection)
8. [Data sources — what works, what doesn't](#data-sources--what-works-what-doesnt)
9. [Architecture](#architecture)
10. [Test & quality gates](#test--quality-gates)
11. [For other agents — operating manual](#for-other-agents--operating-manual)
12. [Roadmap to verified mode](#roadmap-to-verified-mode)
13. [Disclaimer](#disclaimer)

---

## Quick start

```bash
cd ~/projects/ud-edge-bot

# 0. Install (editable, with dev + dashboard extras)
uv venv && uv pip install -e ".[dev,dashboard]"

# 1. Math self-test (no network, ~2 sec)
python -m ud_edge --self-test

# 2. Dry run on synthetic data (no network, ~2 sec)
python -m ud_edge --dry-run

# 3. Live one-shot — all sports, 6-flex entry
python -m ud_edge --once --entry 6-flex

# 4. NBA only
python -m ud_edge --once --sport NBA --entry 3-man-power

# 5. Lower the bar (more picks, smaller edge)
python -m ud_edge --once --min-true-prob 0.52 --min-edge-pp 0.1

# 6. Save a Markdown report
python -m ud_edge --once --entry 6-flex --save reports/$(date +%F).md

# 7. Build 4 disjoint 6-flex entries from the same edge pool
python -m ud_edge --once --entry 6-flex --entries 4

# 8. Drop mid-game / obscure-sport props
python -m ud_edge --once --entry 6-flex --entries 4 --full-game-only

# 9. Launch the Edge Board on localhost
python -m ud_edge --serve
# → http://127.0.0.1:8787
```

### One-liner for a daily pick report on Slack

```bash
python -m ud_edge --once --entry 6-flex --entries 4 \
  --full-game-only --save reports/$(date +%F).md
```

---

## The Edge Board dashboard

```bash
# Local only
python -m ud_edge --serve --host 127.0.0.1 --port 8787

# LAN + Tailscale (bind 0.0.0.0; accessible on your tailnet)
python -m ud_edge --serve --host 0.0.0.0 --port 8787
```

Then open:

- **Local:**  `http://127.0.0.1:8787/`
- **Tailscale:** `http://<your-tailnet-ip>:8787/` (find it with `tailscale ip -4`)

The board:
- Tabs by sport (MLB, NBA, WNBA, NHL, EPL, MLS, NCAAF, TENNIS, …).
- Per-leg EV with `true_prob`, `edge_pp`, and `is_mispriced` flag.
- Disjoint 6-flex lineups you can paste into apps (entry #1 has the highest
  edge; #4 has the floor). Auto-fallback to fewer entries on thin slates.
- One-click **Copy** buttons for PrizePicks / Sleeper / Underdog formats —
  per pick, per sport, or whole slate.
- "Refresh slate" button re-pulls without restarting.
- **Research-only banner** always visible — model status, settled-leg count,
  recommendation label. See `HONEST_STATUS.md`.

---

## Dashboard API reference

All endpoints return JSON unless noted. Errors are JSON with `{"error": "..."}`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Edge Board HTML (white-label single-page app) |
| `GET` | `/static/*` | CSS / JS assets |
| `GET` | `/HONEST_STATUS.md` | Audit + research-mode status doc (Markdown) |
| `GET` | `/api/health` | Liveness: `{"ok": true, ...}` |
| `GET` | `/api/opportunities` | Full ranked board (per-leg opportunities + lineup summary) |
| `GET` | `/api/sports` | Sport list with per-sport leg counts |
| `GET` | `/api/props` | Per-sport prop cards with copy lines |
| `GET` | `/api/lineups` | **Disjoint parlay lineups** — the endpoint the dashboard calls when "Refresh slate" hits |
| `GET` | `/api/alerts/recent` | Stale-pricing / mispricing alerts (last 50) |
| `GET` | `/api/budget` | API-call budget snapshot (today's spend vs cap) |
| `GET` | `/api/export/{platform}` | `platform ∈ {prizepicks, sleeper, underdog}` — copy-ready text dump |

### `GET /api/lineups` — query params

| Param | Default | Meaning |
|---|---|---|
| `n_entries` | `1` | Number of disjoint lineups to build (1-10) |
| `entry_type` | `6-flex` | `3-man-power`, `4-man-power`, `4-flex`, `5-flex`, `6-flex` |
| `min_true_prob` | `0.6` | Minimum leg true probability |
| `min_edge_pp` | `0.5` | Minimum edge vs break-even, in percentage points |
| `sport` | _all_ | Filter to one sport (e.g. `MLB`, `NBA`, alias-tolerant) |
| `mispriced_only` | `false` | Restrict to legs where sharp book disagrees ≥2pp |
| `full_game_only` | `true` | Drop mid-game / obscure-sport props |
| `return_ranked` | `false` | If `true`, return the full ranked leg list alongside the lineups |

Example:

```bash
curl -s "http://127.0.0.1:8787/api/lineups?n_entries=3&entry_type=6-flex&min_true_prob=0.6" \
  | python -m json.tool | head -60
```

Response shape:

```json
{
  "entry_type": "6-flex",
  "n_entries": 3,
  "lineups": [
    {
      "entry": 1,
      "n_legs": 6,
      "avg_true_prob": 0.7123,
      "win_prob": 0.7667,
      "median_payout": 0.4,
      "ev": 3.0249,
      "opportunities": [ /* 6 RankedLeg objects */ ]
    }
  ],
  "totals": { "legs_scanned": 3886, "opportunities": 118, "sports": 4 },
  "safety_status": { "is_research_mode": true, "recommendation": "RESEARCH ESTIMATE (unverified model)" }
}
```

The dashboard's frontend calls this endpoint **once per Refresh**, with
`return_ranked=true` so the per-sport cards and copy buttons can re-use the
same ranked leg list. This replaced the old pattern of one HTTP call per
card (~2,000 calls/min) with a single batched fetch.

---

## CLI reference

```
python -m ud_edge [--once|--snapshot|--stale-report|--serve|--self-test|--dry-run|--settle ...]

One-shot pick run:
  --once                          Compute a fresh pick report
  --entry {3-man-power,4-man-power,4-flex,5-flex,6-flex}
  --entries N                     Build N disjoint lineups (default 1)
  --sport SPORT                   Filter: NBA, MLB, NHL, ... (alias-tolerant)
  --min-true-prob P               Floor on leg true probability (default 0.60)
  --min-edge-pp PP                Floor on edge vs break-even (default 0.5)
  --line-tolerance F              Max |fantasy - sharp| line gap to match (default 0.5).
                                  Audit P1 #6: was hard-coded 0.5; now configurable.
                                  Try 1.0 to capture 1-line-gap soft lines.
  --full-game-only                Drop mid-game / obscure-sport props
  --mispriced-only                Restrict to sharp-book-disagreement legs
  --save PATH                     Write Markdown report to PATH
  --results PATH                  results.json path (default data/results.json)

Snapshot / stale:
  --snapshot                      Snapshot live UD board (+ optional CSVs)
  --stale-report                  Run cross-source stale detector on snapshots
  --snapshot-db PATH              SQLite snapshot DB (default data/line_snapshots.sqlite3)
  --min-stale-minutes M           Stale threshold (default 30)
  --fresh-window-minutes M        Fresh-movement window (default 120)
  --min-line-gap G                Min line gap to flag stale (default 0.5)
  --min-prob-gap-pp PP            Min true-prob gap to flag stale (default 3.0)
  --min-movement-line L           Min line move to count as movement (default 0.5)
  --min-movement-prob-pp PP       Min prob shift to count as movement (default 3.0)
  --ingest-csv PATH               Add a CSV as a second source for stale detect
  --csv-source NAME               Label for that CSV source
  --ingest-prizepicks-clipboard   Read Windows clipboard as second source

Calibration:
  --settle INDEX:hit:STAT         Mark pick INDEX as HIT (stat is for record)
  --settle INDEX:miss:STAT        Mark pick INDEX as MISS
  --calibration                   Print Brier + log-loss + bucket accuracy

Dashboard:
  --serve                         Launch FastAPI dashboard
  --host HOST                     Bind host (use 0.0.0.0 for LAN/Tailscale)
  --port PORT                     Bind port (default 8787)

Dev:
  --self-test                     Pure-math + sanity self-test
  --dry-run                       Synthetic data end-to-end smoke test
```

---

## Entry-type cheat sheet

| Entry | Payouts | Break-even | Verdict |
|---|---|---|---|
| 2-man-power | 2/2 = 3× | 57.74% | High-variance small slip |
| 3-man-power | 3/3 = 6× | 55.03% | **Best risk/reward** for daily picks |
| 4-man-power | 4/4 = 10× | 56.23% | Skip — too high a bar |
| 3-flex | 3/3=6×, 2/3=1× | 47.53% | Generous partials, but tiny edge |
| 4-flex | 4/4=6×, 3/4=1.5× | 55.03% | OK if you have 4 strong legs |
| 5-flex | 5/5=10×, 4/5=4×, 3/5=2× | 42.16% | Better than 4-flex for variance (3/5=2× is generous) |
| **6-flex** | **6/6=25×, 5/6=2×, 4/6=0.4×** | **54.21%** | High variance, low break-even |

The 6-flex break-even is solved numerically (`flex_math.solve_break_even`)
to **0.5421** (verified Wave 1; was incorrectly 0.5240 before audit).

---

## How the math works

### No-vig from two-sided fantasy prices

```
implied_over  = 1 / decimal_over
implied_under = 1 / decimal_under
overround     = implied_over + implied_under   (>1.0 = book vig)
true_over     = implied_over  / overround
true_under    = implied_under / overround
```

When Underdog prices both sides of a prop, we strip the vig and recover the
true probability of each side. The favorite side after vig removal is +EV
relative to Underdog's own listing.

### Per-leg EV (Wave 1 fix)

EV is computed **per leg**, not as a lineup-average:

```
edge_pp = true_prob − break_even(entry_type)
```

Earlier code averaged per-leg probabilities across a lineup before comparing
to break-even — that understated edge for any lineup containing a heavy
favorite. Wave 1 closed this with `ev.leg_edge_pp` on every `RankedLeg`.

### Sharp-book cross-reference (Wave 2A)

When a sharp book (Pinnacle via PropLine, or any line in `data/sharp_lines.csv`)
disagrees with fantasy on the **same side** by ≥2pp:
- **Sharp-authoritative**: fantasy lines that disagree with sharp are de-boosted.
- **Exact tolerance**: comparison is exact (`< 0.01` rounding), no silent doubling.
- **Freshness**: sharp observations older than `SHARP_MAX_AGE_MINUTES` are dropped.
- **Event identity**: matching keys now include event title + date, so a
  stale series entry can't match today's game.

### Mispricing flag

```
is_mispriced = (sharp_same_side_true_prob > fantasy_same_side_true_prob + 2pp)
```

`is_mispriced=true` legs rise to the top of Edge Board.

---

## Stale pricing detection

### Snapshot DB

Every `--snapshot` run writes every observed market to
`data/line_snapshots.sqlite3` with:
- source, source line ID, player, sport, stat, line
- no-vig higher/lower true probabilities
- match title and scheduled start
- UTC capture timestamp
- canonical market key (source-agnostic)

### Movement detector

A movement is recorded when **either**:
- `|line_change| ≥ min_line_move`
- `|prob_shift_pp| ≥ min_prob_move_pp`

Direction: `up`, `down`, or `flat` (price-only move).

### Cross-source stale opportunity

A stale opportunity is flagged only when **both**:
1. **Stale source** — unchanged (same line) for ≥ `min_stale_minutes`
2. **Fresh source** — meaningfully moved within `fresh_window_minutes`

And **either**:
- `|line_gap| ≥ min_line_gap`, or
- `|prob_gap_pp| ≥ min_prob_gap_pp`

Static disagreement with no movement history is **never** flagged as stale.
`direction` is the side to play on the stale source: `higher` = Over,
`lower` = Under.

### Second source: CSV / clipboard

```bash
# Two-source snapshot: live Underdog + your CSV
python -m ud_edge --snapshot \
  --ingest-csv data/draftkings_demo.csv --csv-source draftkings \
  --save reports/stale_two_source_demo.md

# Pull a fresh board from the Windows clipboard (PrizePicks / Sleeper)
python -m ud_edge --snapshot --ingest-prizepicks-clipboard
```

CSV columns (canonical order):
`player_name, league, stat_type, line, higher_decimal, lower_decimal, event_title, scheduled_at`.
Extra columns ignored; rows missing required fields are skipped. `--csv-source`
labels all rows in that batch.

---

## Data sources — what works, what doesn't

| Source | Status | Notes |
|---|---|---|
| **Underdog Fantasy** `/beta/v5/over_under_lines` | ✅ Live | ~6,700 lines/day, 17 sports, no auth, both sides priced |
| **PropLine** (`PROPLINE_API_KEY`) | ✅ Preferred sharp | Pinnacle + DK/FD/BetMGM + PrizePicks/UD/Sleeper, hobby $9 / pro $19 |
| **Manual CSV** `data/sharp_lines.csv` | ✅ Free fallback | `player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker` |
| **The Odds API** (`ODDS_API_KEY`) | ✅ US books | Player props, paid plans start $30 |
| **SportsGameOdds** (`SPORTSGAMEODDS_KEY`) | ⚠️ Free tier | Validate adapter vs live key |
| **apisports.io** free | ⚠️ Fixtures only | Player props + odds gated to paid tier |
| **ESPN public injury API** | ✅ Free, no auth | Filter OUT / INJURY_RESERVE / SUSPENDED / DOUBTFUL |
| **PrizePicks** | ❌ 403 X-DataDome | This host is challenged; keep manual/partner feed fallback |
| **Sleeper** | ❌ No Picks API | Public API has no player-prop or Picks endpoint |
| **Pinnacle direct** | ❌ Geo-blocked + closed | Public access closed 2025-07-23 |
| **Southpaw** | ❌ Wrong type | FanDuel DFS contest wrapper, not a props feed |

---

## Architecture

```
ud_edge/
├── __main__.py              # CLI dispatcher (argparse → subcommands)
├── models.py                # Pydantic: Leg, RankedLeg, Opportunity, Lineup, ...
├── no_vig.py                # Decimal/American odds → true_prob
├── flex_math.py             # Payout tables + per-entry EV + break-even solver
├── ud_client.py             # Underdog live board fetcher
├── sharp_books_client.py    # Manual CSV + Odds API + SportsGameOdds
├── compare.py               # Fantasy-vs-sharp pipeline (dashboard feed)
├── copy_format.py           # PrizePicks / Sleeper / Underdog paste lines
├── matcher.py               # rank_legs() + side-aligned sharp compare
├── stale_pricing.py         # Snapshot DB + stale opportunity detector
├── pp_clipboard.py          # CSV / clipboard second-source adapter
├── safety_gate.py           # Research-mode banner + recommendation labels
├── deliver.py               # Markdown reports
├── injury_filter.py         # ESPN injury-status filter (free, no auth)
└── dashboard/
    ├── app.py               # FastAPI: all /api/* endpoints + serves static
    └── static/              # index.html + styles.css + app.js

scripts/
├── run_tests.sh             # pytest -q
├── lint.sh                  # ruff check .
├── smoke_dashboard.sh       # Hit /, /api/health, /HONEST_STATUS.md, plus 400 tests
└── ud_edge_daily.sh         # Hermes cron entry point (if scheduled)

data/
├── ud_lines_cache.json              # Last live UD snapshot
├── sharp_lines.csv                  # Manual sharp-book cross-reference
├── results.json                     # Calibration log (HIT/MISS settlement)
├── line_snapshots.sqlite3           # Append-only observation store
└── sharp_cache/propline_*.json      # PropLine per-event cache

tests/                        # 298 unit + integration tests
reports/                      # Markdown reports (one per --save run)
docs/                         # PAYOUT_RULES.md (TODO), design notes
.github/workflows/ci.yml      # Lint + test + self-test + dry-run + smoke + pip-audit
```

---

## Test & quality gates

- **383/383+ tests pass** locally on Windows + Python 3.11.9 (`pytest -q`)
- **`ruff check .` is clean** (0 findings)
- **Smoke verification** — dashboard returns:
  - `200` for `/`, `/api/health`, `/HONEST_STATUS.md`
  - `400` (JSON error) for invalid query params (`?entry=bogus`, `?min_true_prob=nan`)
- **CI** (`.github/workflows/ci.yml`): ruff, pytest, `--self-test`,
  `--dry-run`, dashboard smoke loop, `pip-audit --strict`

Run them all locally:

```bash
ruff check .
pytest -q
python -m ud_edge --self-test
python -m ud_edge --dry-run
bash scripts/smoke_dashboard.sh
```

---

## Audit remediations (2026-07-20)

An independent audit (tip `85d6722`) found **6 P0/P1 issues** in the live code path
that overstate EV, lose field identity, or under-count API spend. All six are fixed
in this branch. New tests pin the contract so each bug can't silently re-appear.

| # | Severity | Finding | Fix | Tests |
|---|---|---|---|---|
| **P0 #1** | Critical | `deliver.py` used `max(fantasy, sharp)` for per-card probability, overstating edge when sharp was slightly bearish-but-in-band | Replaced all 3 sites with `effective_true_prob()` (sharp-authoritative contract: returns sharp when present) | 8 (`test_audit_p0_probability_contract.py`) |
| **P0 #2** | Critical | `compare.py` and `dashboard/app.py` lineup EV used fantasy-only average and homogeneous `expected_value()` — both understated variance and over-stated edge | Both sites now use `effective_true_prob` averaged + `expected_value_per_card()` (heterogeneous exact EV) | 6 (`test_audit_p0_lineup_ev_contract.py`) |
| **P1 #3** | High | `docs/METHODOLOGY.md` and `README.md` quoted stale break-evens (6-flex 52.40% vs actual 54.21%; 3-man-power 54.95% vs 55.03%; etc.) | Both docs now quote `UD_PAYOUTS` directly; stale numbers guarded by tests | 18 (`test_audit_p1_documentation.py`) |
| **P1 #4** | High | Poller called `budget.record(1)` once per cycle; a real cycle makes ~13–80 HTTP calls (1 events + N odds per sport). Daily limit silently blown 60-80x | `PropLineClient.calls_made` counter; `compare_fantasy_vs_sharp` reports count in `sharp_meta.propline_calls`; poller records the actual delta | 6 (`test_audit_p1_poller_budget.py`) |
| **P1 #5** | High | Poller rebuilt `RankedLeg` from JSON-serialized `flat[]`, losing `sharp_book`, `match_id`, `fantasy_source` and silently under-alerting | Poller now passes `return_ranked=True` and consumes the live `RankedLeg` list — no JSON round-trip | 3 (`test_audit_p1_poller_ranked_rebuild.py`) |
| **P1 #6** | Medium | `LINE_TOLERANCE = 0.5` was a function-local constant; soft fantasy lines 1.0+ away from sharp silently fell through | Promoted to module constant; `rank_legs(line_tolerance=...)` parameter; `--line-tolerance` CLI flag; `UD_LINE_TOLERANCE` env var; `SharpMatch.match_distance` for fuzzy-match confidence | 7 (`test_audit_p1_line_tolerance.py`) |

**Deferred (P0 #7):** Side-flip evaluation. The pipeline always picks the fantasy
favorite, then asks whether sharp agrees. It never flips to the fantasy underdog
when sharp prefers that side. This is a real (separate) edge class; needs a
ranking pipeline rewrite. Out of scope for this remediation wave.

**Test additions:** 383 tests total = 298 (pre-audit) + 8 (P0 #1) + 6 (P0 #2) +
18 (P1 #3) + 6 (P1 #4) + 3 (P1 #5) + 7 (P1 #6) + ... + remaining adjustments.
Ruff clean, `--self-test` 12/12, `--dry-run` OK. CI green.

## For other agents — operating manual

If you are an AI agent picking up this repo cold, here is everything you need
to operate it without breaking anything.

### Read these first, in order

1. **`HONEST_STATUS.md`** — current research-mode state, what the audit
   closed, what is still pending. **The single source of truth for whether
   the bot is safe to act on.**
2. **`README.md`** (this file) — system overview.
3. **`docs/PAYOUT_RULES.md`** — official payout tables. If this file does
   not exist, the payout model is **not externally verified** — the bot is
   in research-only mode and stays there.
4. **`data/results.json`** — settled picks. If `settled_legs_count < 50`,
   calibration is insufficient and research-mode cannot be lifted.

### Guardrails — DO NOT violate these

- **Never set `_PAYOUT_MODEL_VERIFIED = True`** without:
  - Verifying the official Underdog Fantasy payout rules and archiving
    them in `docs/PAYOUT_RULES.md`.
  - Adding a regression test that re-fetches the rules and asserts.
- **Never re-enable `_VERIFIED_LABELS`** (STRONG PLAY, LEAN, etc.) until:
  - `data/results.json` has ≥50 settled HIT/MISS legs.
  - Brier score and log-loss are within the calibration bands set by the
    maintainer.
- **Never modify `flex_math.py`** without re-running `--self-test` AND the
  full pytest suite. The break-even solver is the most fragile part of the
  system; every Wave 1 fix was in this file or its callers.
- **Never remove or weaken the sharp-authoritative comparison in
  `compare.py`** — Wave 2A made fantasy lines defer to sharp books by
  design. Reverting this is the fastest way to reintroduce phantom edges.
- **Never delete `data/results.json`** — it is the calibration history.
  Treat it like a database; rotate, never purge.
- **Never change `--min-edge-pp` or `--min-true-prob` defaults** without
  a corresponding calibration argument. They exist to filter phantom edges.

### How to extend the bot safely

- **Add a new entry type**: edit `flex_math.PAYOUTS`, add the break-even
  solver case if it's not a simple linear payout, add a unit test for the
  solver, run `--self-test`. Then add a `--entry` choice in `__main__.py`.
- **Add a new sharp-book source**: implement a fetcher in
  `sharp_books_client.py` that returns `SharpLine` objects, wire it into
  `compare.py`'s pipeline, add a `--source` CLI flag, add tests with
  synthetic data, **do not** lower the freshness / tolerance thresholds.
- **Add a new dashboard card**: extend the `/api/opportunities` response
  shape in `dashboard/app.py`, update `static/app.js`, mirror the change
  in `static/index.html`, add a Playwright/curl smoke test to
  `scripts/smoke_dashboard.sh`.
- **Change the stale detector thresholds**: update defaults in
  `stale_pricing.py`, add a `tests/test_stale_pricing_*.py` case, and
  re-run `--snapshot --stale-report` end-to-end.

### How to verify the bot is healthy

```bash
# 1. Lint clean?
ruff check .

# 2. Tests green?
pytest -q

# 3. Math sanity?
python -m ud_edge --self-test

# 4. Pipeline smoke?
python -m ud_edge --dry-run

# 5. Dashboard smoke?
bash scripts/smoke_dashboard.sh

# 6. Calibration status?
python -m ud_edge --calibration
```

If any of these fail, the bot is not safe to use for live picks. Fix the
failing gate first, then resume work.

### How to ship a change

1. Branch from `main` (or `fix/audit-remediation-v2` if the audit is still
   in flight).
2. Commit in TDD: red test → green impl → refactor. Include the
   corresponding test in the same commit.
3. Run all gates locally (`ruff`, `pytest`, `--self-test`, `--dry-run`,
   `smoke_dashboard.sh`).
4. Open a PR. The CI workflow runs the same gates plus `pip-audit`.
5. After green CI, request review. Do **not** self-merge into `main` if
   the change touches `flex_math.py`, `compare.py`, `safety_gate.py`, or
   `dashboard/app.py` — these are the audit-critical files.

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/lineups` returns `{"lineups": []}` | Threshold too high OR slate too thin | Lower `min_true_prob` and `min_edge_pp`; check `data/ud_lines_cache.json` is fresh |
| All legs flagged `RESEARCH ESTIMATE` | Research mode is on | Settle ≥50 picks via `--settle`, then re-check `--calibration` |
| Sharp comparison silently disabled | Sharp data older than `SHARP_MAX_AGE_MINUTES` | Pull fresh PropLine/Odds API data, or update CSV |
| `ruff` complains about unused imports | Wave 4 ruff pass left nothing; if it does, the import is actually used somewhere — run `ruff --fix --unsafe-fixes` |
| `pytest` hangs on `test_stale_pricing_*.py` | Snapshot DB lock from a live `--snapshot` run | `rm data/line_snapshots.sqlite3-journal` (if any) |
| Dashboard 500 on `/api/opportunities?entry=bogus` | Should be 400 (Wave 0 fix); regression? | Check `compare.py:validate_entry_type` |
| `underround > 1.10` | Book has heavy vig on this market; leg still valid but edge compressed | Lower `min_edge_pp` or skip |
| Same player appearing on multiple lineups | Disjoint-enough logic in `matcher.build_lineups` lost a leg | Check `_lineups_share_leg` guard in `compare.py` |

### Tailscale exposure pattern

The dashboard binds on `--host 0.0.0.0` to expose on the tailnet:

```bash
python -m ud_edge --serve --host 0.0.0.0 --port 8787
```

Find your tailnet IP with `tailscale ip -4`. Other tailnet devices hit
`http://<your-tailnet-ip>:8787`. **Do not** port-forward 8787 to the public
internet — there is no auth. Tailscale ACLs are the only access control.

---

## Roadmap to verified mode

All three conditions must hold before lifting research-only mode:

- [x] Payout model mathematically verified (Wave 1).
- [ ] ≥50 settled HIT/MISS legs in `data/results.json`.
- [ ] Brier score and log-loss within acceptable calibration bands.

When all three are met:

1. Verify official Underdog payout rules, archive to `docs/PAYOUT_RULES.md`.
2. Set `_PAYOUT_MODEL_VERIFIED = True` in `safety_gate.py` and add a
   regression test that re-fetches rules and asserts.
3. Re-enable recommendation labels in `_VERIFIED_LABELS`.
4. Schedule the daily cron (`scripts/ud_edge_daily.sh`, 17:00 UTC,
   `--entries 4 --full-game-only`).

Until then: every recommendation label in the dashboard reads
`RESEARCH ESTIMATE (unverified model)`. Do not pretend otherwise.

---

## Disclaimer

Decision-support tool, not financial advice. Track every pick. Calibrate
after 50+ settled legs. Past performance does not predict future results.
Sports betting is gambling; bet only what you can afford to lose.

---

## Source

Built on top of the unauthenticated `/beta/v5/over_under_lines` endpoint
discovered via
[aidanhall21/underdog-fantasy-pickem-scraper](https://github.com/aidanhall21/underdog-fantasy-pickem-scraper)
(verified alive 2026-07-18).