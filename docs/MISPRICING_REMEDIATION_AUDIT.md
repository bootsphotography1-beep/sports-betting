# Remediation Audit — tip `fa6d27b` (post-`85d6722`)

**Scope:** Commits `4875acb` … `fa6d27b` on `fix/audit-remediation-v2`, which claim to close the six P0/P1 findings from `docs/MISPRICING_STRATEGY_AUDIT.md`.

**Tip commit:** `fa6d27b` — ruff unused-import cleanup only (no behavior change). The substantive work is the seven commits beneath it.

**Verdict:** The remediations **mostly close the named findings in production code**, with real improvements to probability contract, lineup EV, poller identity, and PropLine budget accounting. They are **not fully closed**: several new pinning tests fail as written, CLI/`deliver` EV is only half-fixed, and strategy-level blockers (side-flip, payout verification, calibration) remain. **Still research-only; still not live-money ready.**

---

## 1. Commit inventory

| Commit | Claimed fix | Status |
|--------|-------------|--------|
| `4875acb` | Replace `max(fantasy, sharp)` with `effective_true_prob` in `deliver.py` | **Code OK**; CLI console path still fantasy-only |
| `4eef2e6` | Lineup EV → `effective_true_prob` + `expected_value_per_card` | **OK** in `compare.py` / dashboard; **not** in `deliver.py` |
| `f210afa` | Docs break-evens match `UD_PAYOUTS` | **Tables OK**; README remediations section reintroduces `52.40%` |
| `0e7b4a0` | Poller budget records real PropLine HTTP count | **Code OK**; pinning test fails without env key |
| `91df91d` | Poller uses live `RankedLeg` via `return_ranked=True` | **Code OK**; pinning tests fail without env key |
| `dba1bcb` | Configurable `line_tolerance` + `match_distance` | **Partial** — CLI/`rank_legs` only; not dashboard/poller |
| `24b24e4` | README “Audit remediations” section | **Overclaims** “all 6 closed”; self-breaks doc guardrail |
| `fa6d27b` | Ruff unused imports in P0 tests | Cosmetics only |

---

## 2. Finding-by-finding re-score

### P0 #1 — `max(fantasy, sharp)` overstate — **CLOSED in deliver reports**

`build_report` / `build_multi_report` now call `effective_true_prob`. Source-grep guard in `tests/test_audit_p0_probability_contract.py` is the right shape.

**Residual:** `ud_edge/__main__.py` per-entry console summary still uses raw `picked_true_prob` + homogeneous `expected_value` (lines ~318–347). Operators watching CLI output still see the old overstated board.

### P0 #2 — lineup EV contract — **CLOSED for dashboard/API; OPEN for Markdown deliver**

`compare.py` and `/api/lineups` correctly build per-leg `effective_true_prob` lists and call `expected_value_per_card`.

`deliver.py` still does:

```text
avg = mean(effective_true_prob(...))
ev  = expected_value(entry, avg)   # homogeneous
```

Worked example (mixed legs): deliver-style **+0.485** vs per-card **+0.447** (~4¢/$ overstate). Smaller than the old max() bug, but the heterogeneous contract is not unified.

Dashboard tests that import FastAPI fail in a minimal env (`ModuleNotFoundError: fastapi`) — environment gap, not necessarily a code bug.

### P1 #3 — stale break-even docs — **MOSTLY CLOSED; guardrail self-owns**

Entry-type tables in README / METHODOLOGY now match live `UD_PAYOUTS` (6-flex **54.21%**, etc.).

`test_readme_no_stale_break_evens_or_test_counts["52.40%"]` **fails** because `24b24e4` quotes `52.40%` inside the remediations table describing the old bug. The guardrail is absolute-string; the README patch violates its own test.

### P1 #4 — poller budget under-count — **CLOSED in code; tests don’t exercise it**

Production path is sound:

1. `PropLineClient._get` increments `calls_made` on real HTTP (cache hits skip)
2. `build_propline_indexes` → `meta["propline_calls"]`
3. `compare_fantasy_vs_sharp` copies into `sharp_meta["propline_calls"]` when a PropLine key is present
4. Poller `budget.record(propline_calls)`

**Test defect:** `_run_poll_cycle` early-returns when `PROPLINE_API_KEY` is unset **before** calling the stubbed `compare_fantasy_vs_sharp`. Pinning tests never set the key → `delta=0` instead of 47. With `PROPLINE_API_KEY=testkey`, the same tests pass. Commit messages claiming “all N pass” were true only in an env that already exported a key.

### P1 #5 — poller JSON RankedLeg rebuild — **CLOSED in code; same test env bug**

Poller now passes `return_ranked=True` and unpacks `(payload, ranked)`. No flat-dict reconstruction. Same early-return means `test_poller_passes_return_ranked_true` sees `captured["return_ranked"] is None` without a key.

### P1 #6 — hard-coded line tolerance — **PARTIALLY CLOSED**

- Module `LINE_TOLERANCE`, env `UD_LINE_TOLERANCE`, `rank_legs(line_tolerance=...)`, CLI `--line-tolerance` — **done**
- `SharpMatch.match_distance` — **computed**, never copied onto `RankedLeg` or dashboard JSON — README overclaims “dashboard can surface fuzzy confidence”
- `compare_fantasy_vs_sharp` / dashboard / poller **do not accept or forward** `line_tolerance` — only `--once` CLI path does

Raising tolerance without line-gap adjustment also risks treating alt-lines as same-market (soft-line ≠ soft-price). Configurable is necessary but not sufficient for the soft-*line* edge class.

---

## 3. What the tip (`fa6d27b`) changes

Removes unused `datetime` / `UD_PAYOUTS` imports from two P0 test modules. No strategy or runtime impact. Safe; does not address the failing tests above.

---

## 4. Strategy viability update (vs prior audit)

| Prior blocker | After remediations |
|---------------|-------------------|
| EV overstated via `max(fantasy,sharp)` | **Fixed** in Markdown deliver; CLI console still wrong |
| Homogeneous lineup EV on board | **Fixed** on `/api/lineups` + compare payload; deliver still averages |
| Poller field loss / under-alert | **Fixed** in code |
| Budget 60–80× under-count | **Fixed** in code |
| Stale 52.40% docs | **Tables fixed**; remediations blurb reintroduces string |
| Soft lines >0.5 unmatched | **Opt-in CLI only**; default still 0.5; no UI surface |
| Fantasy-favorite-only side pick | **Still deferred** (README admits P0 #7) |
| Unverified payouts / 0 settled legs | **Unchanged** — research mode correctly still on |

**Net:** Mispricing *detection* is more trustworthy on the dashboard path. Mispricing *monetization* is still not viable: no side-flip, no payout archive, no calibration sample, residual EV path drift on CLI/Markdown.

---

## 5. Test reality check (this environment)

Ran the six new audit test modules:

| Result | Count | Notes |
|--------|------:|-------|
| Passed | 42 | Core unit contracts |
| Failed | 6 | See below |

Failures:

1. `test_readme_no_stale_break_evens_or_test_counts[52.40%]` — README remediations section
2–3. Dashboard FastAPI tests — missing `fastapi` in bare env
4. `test_poller_records_actual_propline_calls` — needs `PROPLINE_API_KEY` for early-return gate
5–6. Poller `return_ranked` tests — same gate

**Recommendation:** tests should `monkeypatch.setenv("PROPLINE_API_KEY", "test")` (or refactor the gate so stubs are reachable). Doc guardrail should allow historical mentions inside an “old value was X” prose block, or paraphrase without the literal `52.40%`.

---

## 6. Remaining work (priority)

1. **P0 residual:** Wire `expected_value_per_card` + `effective_true_prob` through `deliver.py` and `__main__.py` console summaries; log `effective` prob into `results_tracker` for calibration.
2. **Test hygiene:** Set PropLine key in poller pinning tests; fix README guardrail self-hit; install dashboard extras in CI for FastAPI tests (or mark skip).
3. **P1 #6 completion:** Plumb `line_tolerance` through `compare_fantasy_vs_sharp` / dashboard / poller; attach `match_distance` to `RankedLeg` if claiming UI confidence.
4. **Deferred P0 #7:** Evaluate both sides vs sharp (or explicit soft-line / line-gap model) — still the biggest missing edge class.
5. **Viability gates unchanged:** `docs/PAYOUT_RULES.md` + `_PAYOUT_MODEL_VERIFIED`, ≥50 settled legs, Brier/log-loss bands.

---

## 7. Bottom line

Treat `fa6d27b` / this remediation wave as a **solid partial close** of the prior audit’s engineering defects — especially dashboard EV and poller correctness — **not** as “all 6 closed” or as a green light to bet. Re-run the pinning suite with an explicit PropLine test key before trusting the “413 tests pass” claim on a clean CI agent.
