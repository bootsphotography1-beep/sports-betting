# HONEST_STATUS — Wave 0 Research-Only Safety Gate

**Generated:** 2026-07-19
**Wave:** 0 (Research-Only Safety Gate)
**Audit baseline:** `893f9a2bd68d40ab3bc5997ebe828b495995fed4`
**Branch:** `fix/audit-remediation-v2`

---

## Research Mode — ACTIVE

**is_research_mode: TRUE**

The system is currently operating in **research-only mode**. All recommendation labels
display "RESEARCH ESTIMATE" instead of `PLAY` / `STRONG PLAY`. This is intentional
and expected until the conditions below are met.

---

## Why Research Mode Is Active

### 1. Payout Model — Unverified
**is_payout_model_verified: FALSE**

The 6-flex break-even stored in `flex_math.py` (52.40%) is **1.81 pp optimistic**
relative to the exact solution for the declared payout table (54.21%). See audit
finding SB-P0-01. Using an optimistic break-even means the tool can recommend
cards that are negative EV under its own stated payout schedule.

**Audit counts (pre-fix):**
| Entry Type | Stored BE | Exact BE | Direction |
|---|---|---|---|
| 2-man-power | 57.00% | 57.74% | optimistic +0.74pp |
| 3-man-power | 54.95% | 55.03% | optimistic +0.08pp |
| 4-man-power | 63.00% | 56.23% | conservative −6.77pp |
| 3-flex | 57.81% | 47.53% | conservative −10.28pp |
| 4-flex | 57.81% | 55.03% | conservative −2.78pp |
| 5-flex | 57.81% | 42.16% | conservative −15.65pp |
| **6-flex** | **52.40%** | **54.21%** | **optimistic −1.81pp** |

The payout model itself is also unverified — the relationship between per-leg
multipliers and entry-level payouts has not been independently confirmed. See
audit finding SB-P0-02.

### 2. Calibration — Insufficient Sample
**is_calibration_sufficient: FALSE**

Current settled-leg counts (from `data/results.json`):
- **HIT:** 0
- **MISS:** 0
- **Pending:** 24
- **Total logged:** 24
- **Settled (HIT+MISS):** 0
- **Minimum required:** 50

The system has 0 settled outcomes. With fewer than 50 settled legs, the
true calibration of the no-vig probability estimates is unknown. See audit
finding SB-P0-03.

---

## What This Means in Practice

While `is_research_mode == TRUE`:

1. **Recommendation labels** in CLI reports and dashboard use
   "RESEARCH ESTIMATE" variants only — no `PLAY`, `STRONG PLAY`, or
   dollar-EV claims appear in user-facing output.

2. **EV and win-probability numbers** are still computed and available
   as **unverified research estimates** (internal diagnostic data that
   Wave 1 will validate). They must NOT be treated as actionable.

3. **CLI `--self-test`** passes all mathematical assertions. The no-vig
   transformation itself is correct. The problem is solely with the
   payout-model break-even constants and the absence of settled outcomes.

---

## What Unblocks Research Mode (Wave 1)

All three conditions must be met to lift research-only mode:

- [ ] **Payout model verified:** Official Underdog rules obtained and archived;
  break-even derived numerically from payout table (not hardcoded); tests
  assert `expected_value(entry, entry.break_even) ≈ 0` for all entry types.
- [ ] **≥50 settled HIT/MISS legs** recorded in `data/results.json`.
- [ ] **Brier score and log-loss** within acceptable calibration bands.

---

## Wave 1 — Not Scheduled Here

Wave 1 will fix the payout math, schedule cron, and flip
`is_payout_model_verified` to `True`. That flip is the **only** thing that
unblocks verified-mode labels. Cron scheduling is out of scope for Wave 0.

---

## Audit Findings Addressed by This Wave

| Finding | Description | Status |
|---|---|---|
| SB-P0-01 | 6-flex break-even wrong by 1.81 pp | Gate active — numeric fix deferred to Wave 1 |
| SB-P0-02 | Payout model unverified | Gate active — verification deferred to Wave 1 |
| SB-P0-03 | 0 settled outcomes / uncalibrated | Gate active — 0 < 50 threshold |

---

## Files Changed

- `ud_edge/safety_gate.py` — new, centralises all safety-state logic
- `ud_edge/compare.py` — added `safety_status` to dashboard payload
- `deliver.py` — labels downgraded in research mode (via `recommendation_label`)
- `tests/test_safety_gate.py` — 15 tests: RED → GREEN (12 self, 3 integration)

## Files NOT Changed (Wave 0 scope)

- `flex_math.py` — payout math NOT modified (Wave 1)
- `matcher.py` — correctness logic NOT modified (Wave 1)
- `__main__.py` — no cron scheduled (Wave 1)
- `results_tracker.py` — no changes
