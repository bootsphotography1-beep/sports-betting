# Mispricing Strategy Audit — tip `85d6722`

**Scope:** Commits on `cursor/audit-remediation-v2` after main (`397387f` → `85d6722`), plus the underlying sharp-vs-fantasy pipeline those commits operate on.

**Verdict:** The **sharp-vs-fantasy same-line disagreement** thesis is a viable *research* signal. It is **not yet a viable live-money strategy**. Reported EV / “mispriced” lineups systematically overstate edge, payouts remain externally unverified, and there are **0 settled legs** for calibration.

---

## 1. Commits under review

| Commit | Summary | Role in strategy |
|--------|---------|------------------|
| `7ed0359` | Restore poller, budget, notify, correlation, lineup selector | Ops loop: find misprices near tip-off, alert, build cards |
| `e3e2e93` | Batch dashboard APIs + Tailscale bind | Surface props / lineups / alerts / budget |
| `b045758` | `/api/lineups` RankedLeg cache fix | Unblocks real 6-flex construction (was collapsing to 0) |
| `85d6722` | README operating manual | Documents pipeline; no code change |

Net: ~3.8k lines of restored live ops on top of the Wave 0–4 remediations already on main. The **edge thesis itself** was not redesigned in these four commits — they wire detection → alert → lineup → dashboard.

---

## 2. Strategy as implemented

### 2.1 Dual signal model

```
Fantasy board (UD / PropLine fantasy / CSV)
        │
        ├─► no_vig(higher, lower) ──► pick favorite side
        │         │
        │         └─► edge_pp = true_prob − entry break_even
        │
        └─► match sharp (Pinnacle/DK/FD via PropLine or CSV)
                  │
                  ├─► delta_pp = sharp_same_side − fantasy_same_side
                  ├─► delta < −2pp  → QUARANTINE (drop leg)
                  ├─► delta ≥ −2pp  → keep; filter thresholds use sharp prob
                  └─► delta ≥ +2pp  → flag mispriced (sort/alert boost)
```

**Parallel track (Phase 1):** snapshot DB + cross-source *stale* detection (unchanged line on source A + recent move on source B). That is a different, evidence-backed edge class; the new commits barely touch it.

### 2.2 What “mispricing” means here

```text
mispricing_edge_pp = (sharp_true_prob − fantasy_true_prob) × 100
is_mispriced       ≈ mispricing_edge_pp ≥ 2.0
```

Only evaluated on the **fantasy favorite side already chosen**. Sharp-authoritative quarantine drops legs where sharp is ≥2pp more bearish on that side.

### 2.3 Monetization path

Fixed fantasy payouts (power / flex), not sportsbook odds:

| Entry | Code payouts | Numerical break-even |
|-------|--------------|----------------------|
| 3-man-power | 3/3 → 6× | **55.03%** |
| 4-flex | 4/4→6×, 3/4→1.5× | **55.03%** |
| 5-flex | 5/5→10×, 4/5→4×, 3/5→2× | **42.16%** |
| 6-flex | 6/6→25×, 5/6→2×, 4/6→0.4× | **54.21%** |

At uniform p=55%, 6-flex EV ≈ **+0.075** / $1; at p=60% ≈ **+0.66**. Those numbers are only as good as (a) payout tables and (b) probability estimates.

### 2.4 What the new commits add

- **Poller:** adaptive cadence (45s / 3m / 10m / 15m by tip proximity), 5k/day PropLine budget, alerts when `mispricing_edge_pp ≥ alert threshold` and sharp ≥52%.
- **Correlation:** rule-based ρ (QB–WR, pitcher–batter, same-game, etc.) → fighting-pair warnings; prefer power vs flex heuristics.
- **Lineup selector:** 6-flex first, drop worst fighting leg ≤3 retries, else 4-flex fallback.
- **Dashboard:** `/api/props`, `/api/lineups`, `/api/alerts/recent`, `/api/budget`.

---

## 3. Viability analysis

### 3.1 When the thesis can work

Real edge exists **only if all** of these hold:

1. **Same market identity** — player + stat + line (±0.5) + event matched correctly.
2. **Sharp no-vig ≈ true frequency** — Pinnacle/DK consensus is a decent prior.
3. **Fantasy soft on that side** — you play the side on UD/PP/Sleeper where sharp implies higher hit rate than the flex break-even.
4. **Payout table matches the app** — multipliers used in EV match live Underdog (etc.).
5. **Legs are not ruined by correlation / injuries / started games** — filters help; not complete.
6. **Probability is calibrated** — predicted hit rate ≈ realized hit rate over a large sample.

(1)–(3) are the core *mispricing* bet. (4)–(6) determine whether displayed +EV survives contact with bankroll.

### 3.2 What is sound

| Strength | Why it helps |
|----------|--------------|
| Sharp-authoritative quarantine | Stops “UD favorite at 58%” when sharp says 50% — classic false edge |
| Exact line tolerance (no silent 2×) | Wave 2A fix; reduces garbage matches |
| Freshness / event identity on sharp | Cuts stale-series and wrong-game matches |
| Trivial-prop + mid-game + started filters | Removes illiquid / non-actionable junk |
| Research-mode safety gate | Labels stay non-actionable until calibration |
| Correlation-aware card building | Reduces self-canceling scripts on flex slips |
| Tip-proximity polling | Misprices cluster near steam / late news |

### 3.3 What breaks live viability

#### P0 — Reported EV is not the strategy’s intended probability

Hot paths **do not** use `expected_value_per_card` (heterogeneous exact EV exists and is tested, then unused in CLI/dashboard).

Worse, probability sources disagree:

| Path | Prob used for EV / display |
|------|----------------------------|
| `rank_legs` filter | sharp when matched (if not quarantined) |
| `RankedLeg.picked_true_prob` stored | **always fantasy** |
| CLI multi-entry / dashboard lineups | **fantasy** `picked_true_prob` |
| `deliver.build_report` | **`max(fantasy, sharp)`** — optimistic |
| `correlation._probs` | `effective_true_prob` → sharp if present |

Demo (6-flex, six legs): fantasy 58% vs sharp 56.5% (inside quarantine band):

| Method | EV / $1 |
|--------|---------|
| Fantasy / deliver `max()` | **+0.40** |
| Sharp-authoritative | **+0.23** |

The dashboard can advertise ~1.7× the edge the sharp policy implies. That alone makes “viability from board EV” unreliable.

#### P0 — Side selection is incomplete for true mispricings

Pipeline always picks the **fantasy favorite**, then asks whether sharp agrees.

It **never flips** to the fantasy underdog when sharp prefers that side. Classic soft-book edge is often “fantasy posts ~even / soft favorite; sharp wants the other side.” Those opportunities are quarantined or never ranked — not harvested.

So “mispriced” here means: *fantasy underprices a side that is already the fantasy favorite*. That is a **subset** of sharp-vs-soft edges.

#### P0 — Unverified payouts + zero calibration

- `_PAYOUT_MODEL_VERIFIED = False` in `safety_gate.py`
- No `docs/PAYOUT_RULES.md`
- No `data/results.json` (0 HIT / 0 MISS)
- `is_research_mode()` is correctly **True**

5-flex break-even of **~42%** (because 3/5 → 2×) is extremely generous; if live UD pays less on partials, every “+EV 5-flex” is fiction. Until official tables are archived and regression-tested, EV numbers are model output only.

#### P1 — Docs still teach the wrong 6-flex break-even

Code/tests correctly use **~54.21%**. README cheat-sheet table still shows **52.40%**; `docs/METHODOLOGY.md` still shows **52.40%**. Agents following the table will think 55% legs are “easy +EV” by ~2pp more than reality.

#### P1 — Poller budget accounting under-counts PropLine spend

`docs/PROPLINE_BUDGET.md` models ~1 events call + N odds calls per sport sweep. Poller `budget.record(1)` once per `compare_fantasy_vs_sharp` cycle. Actual PropLine client hits `/events` and per-event `/odds`. Daily cap can be blown while the budget UI still looks healthy — or cadence will be wrong relative to real spend.

#### P1 — Poller still reconstructs `RankedLeg` from flat JSON

`b045758` fixed this for `/api/lineups` via `return_ranked` + `_RANKED_CACHE`. Poller still rebuilds from serialized `flat` dicts and does not call `return_ranked=True`. Same class of field-loss / identity bugs can under-alert or mis-key cooldown.

#### P1 — Line matching leaves soft-*line* edges on the table

`LINE_TOLERANCE = 0.5`. Soft fantasy lines that differ by 1.0+ from sharp (common) are unmatched → no mispricing flag; ranking falls back to fantasy no-vig alone (weaker signal).

#### P2 — Correlation ρ is invented, not fit

Useful as a **heuristic veto** for fighting pairs; not a calibrated joint model. Do not treat `joint_boost` as priced EV.

#### P2 — Independence still assumed in card EV

Even with correlation warnings, published lineup EV is binomial-independent (and often homogeneous-average). Positive correlation raises wipeout risk; flex mid-bins get rarer — EV can flip negative while the board still looks green.

---

## 4. Commit-specific review

### `7ed0359` — live restore

**Good:** Re-hooks sharp_authoritative path; adaptive polling is the right spend shape for steam; alert dedupe + platform “PLACE ON → …” is operationally useful; correlation/lineup selector address real flex failure modes.

**Concerns:** Budget `record(1)` vs multi-call PropLine; JSON RankedLeg rebuild; poller `min_true_prob=0.55` with fantasy-stored probs for alerts; no settlement path in the poll loop.

### `e3e2e93` — batch API

**Good:** Correct product direction (one slate fetch vs thousands of card requests); CORS scoped to Tailscale CGNAT + localhost.

**Concerns:** Bind default `0.0.0.0` + no auth is fine on Tailscale only — README warns correctly; do not expose publicly.

### `b045758` — RankedLeg cache

**Good:** Critical correctness fix. Without it, “mispricing strategy” could not even emit 6-flex cards from the dashboard.

**Residual:** Fallback JSON reconstruction path remains for cold start; poller not updated to the same pattern.

### `85d6722` — README

**Good:** Operating manual, guardrails, endpoint inventory, research-mode honesty.

**Concerns:** Internal contradiction on 6-flex break-even (52.40% vs 0.5421); architecture tree still cites ~298 tests (tip has ~351 test funcs); poll flags (`--poll`, `--poll-once`) underspecified relative to restored CLI.

---

## 5. Bottom line on finding mispricings

| Question | Answer |
|----------|--------|
| Can the bot **detect** sharp-vs-fantasy disagreement? | **Yes**, when PropLine/CSV sharp matches same player/stat/line/event and freshness holds. |
| Is that disagreement a **real** edge on fantasy flex? | **Sometimes**, if sharp probs beat flex break-even *and* payouts are real *and* correlation/injuries don’t eat the slip. Unproven here. |
| Are current “+EV / mispriced” cards trustworthy? | **No** — EV paths overstate, side selection incomplete, payouts unverified, **n=0** settled. |
| Are the four new commits net positive? | **Yes for ops** (alerts, lineups, cache fix). They do **not** make the edge thesis production-viable. |
| Live bankroll? | **Keep research-only.** Use the board to *hunt candidates*, manually verify sharp line + UD payout + injury, log settlements. |

### Minimum path to viability

1. Archive official payouts → `docs/PAYOUT_RULES.md`; flip `_PAYOUT_MODEL_VERIFIED` only with regression fetch/assert.
2. One probability contract everywhere: `effective_true_prob` (sharp when matched & not quarantined) for filter, sort, EV, logging, calibration.
3. Use `expected_value_per_card` on every card EV; stop `max(fantasy, sharp)` and stop homogeneous averages in hot paths.
4. Optional but important: evaluate **both sides** vs sharp (or line-gap soft pricing), not only fantasy favorites.
5. Fix PropLine call accounting to match real HTTP usage.
6. Settle ≥50 legs; require Brier / log-loss bands before lifting research mode.
7. Fix README / METHODOLOGY break-even tables to 54.21% (6-flex) and refresh other entry rows from `UD_PAYOUTS`.

Until (1)–(6), treat mispricing flags as **hypotheses**, not bankable +EV.
