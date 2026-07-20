# HONEST_STATUS — Wave 1 Math Legitimacy (Partial)

**Generated:** 2026-07-19
**Wave:** 1 (Math Legitimacy — partial)
**Audit baseline:** `893f9a2bd68d40ab3bc5997ebe828b495995fed4`
**Branch:** `fix/audit-remediation-v2`

---

## Research Mode — STILL ACTIVE

**is_research_mode: TRUE**

The system is still operating in **research-only mode** because calibration
(≥50 settled legs) is not yet met. Wave 1 has fixed the payout math (see below),
but the full flip to verified mode requires both:
1. ✅ Payout model verified mathematically (Wave 1 — DONE)
2. ⬜ ≥50 settled HIT/MISS legs recorded (Wave 1+ — not yet met)

---

## Wave 1 Math Fix — COMPLETED

### What Changed in Wave 1

**`ud_edge/flex_math.py`:**
- `break_even_numerical(entry)` — bisection solver (tol=1e-10, max 200 iter)
  that numerically solves for p where E[payouts] = 1, excluding 0-hit tier.
- `expected_value_per_card(entry, leg_probs)` — heterogeneous exact EV that
  enumerates all 2^N outcomes weighted by per-leg probability.
- `UD_PAYOUTS` break-even values are now **computed**, not hardcoded.
  All entry types have their break-even derived from the payout table.
- `recommend_entry()` now accepts `list[float]` for heterogeneous EV.
- `expected_value()` remains for backward compatibility (uniform per-leg prob).

**`tests/test_flex_math_wave1.py` (new):**
- 24 tests covering numerical solver, break-even consistency, heterogeneous EV,
  and mutation regression proving 0.524 would fail if restored.

### Break-Even Table (Wave 1 — Computed, Not Hardcoded)

| Entry Type       | Old (hardcoded) | New (numerical) | Δ         |
|------------------|-----------------|----------------|-----------|
| 2-man-power      | 57.00%          | ~57.74%        | +0.74 pp  |
| 3-man-power      | 54.95%          | ~55.03%        | +0.08 pp  |
| 4-man-power      | 63.00%          | ~56.23%        | −6.77 pp  |
| 3-flex           | 57.81%          | ~47.53%        | −10.28 pp |
| 4-flex           | 57.81%          | ~55.03%        | −2.78 pp  |
| 5-flex           | 57.81%          | ~42.16%        | −15.65 pp |
| **6-flex**       | **52.40%**      | **~54.21%**    | **+1.81 pp** |

The 6-flex is now correctly ~54.21% (not 52.40%). All entry types satisfy
`expected_value(entry, entry.break_even) ≈ 0` within rounding tolerance (≤1e-9).

### Model Verification Status

**is_payout_model_verified: PARTIALLY**

The mathematical correctness of the payout model is now verified internally:
- Bisection solver converges for all entry types in ≤200 iterations
- Break-even satisfies EV ≈ 0 at computed break-even
- Heterogeneous EV reduces to uniform EV when all legs equal
- Exhaustive 2^N outcome enumeration sums to probability 1
- The 0.524 regression is caught by: `test_6flex_wrong_break_even_524_would_fail_audit`

**The model remains externally unverified.** The relationship between per-leg
multipliers and entry-level payouts has not been independently confirmed against
official Underdog Fantasy rules. See audit finding SB-P0-02.

---

## What This Means in Practice

While `is_research_mode == TRUE`:

1. **Recommendation labels** in CLI reports and dashboard use
   "RESEARCH ESTIMATE" variants only — no `PLAY`, `STRONG PLAY`, or
   dollar-EV claims appear in user-facing output.

2. **EV and win-probability numbers** are computed using the mathematically
   correct heterogeneous EV. They are still research estimates because the
   payout model itself is unverified externally (SB-P0-02).

3. **CLI `--self-test`** passes all mathematical assertions including the
   Wave 1 numerical break-even tests.

---

## What Unblocks Verified Mode (Full Wave 1+)

All conditions must be met to lift research-only mode:

- [x] **Payout model mathematically verified:** Break-even derived numerically
  from payout table; tests assert `expected_value(entry, entry.break_even) ≈ 0`
  for all entry types. ✅ (Wave 1)
- [ ] **≥50 settled HIT/MISS legs** recorded in `data/results.json`.
- [ ] **Brier score and log-loss** within acceptable calibration bands.

---

## Audit Findings Addressed by This Wave

| Finding | Description | Status |
|---------|-------------|--------|
| SB-P0-01 | 6-flex break-even wrong by 1.81 pp | ✅ Fixed — numerical solver |
| SB-P0-02 | Payout model unverified | ⚠️ Math verified; externally unverified |
| SB-P0-03 | 0 settled outcomes / uncalibrated | ⬜ Pending (needs ≥50 settled legs) |

---

## Files Changed in Wave 1

- `ud_edge/flex_math.py` — numerical break-even + heterogeneous EV
- `tests/test_flex_math_wave1.py` — 24 tests (all GREEN)
- `HONEST_STATUS.md` — this file

## Files NOT Changed in Wave 1 (Later Waves)

- `ud_edge/safety_gate.py` — `_PAYOUT_MODEL_VERIFIED` flag (Wave 1 full flip)
- `ud_edge/deliver.py` — heterogeneous EV wiring for reports (Wave 2)
- `ud_edge/compare.py` — dashboard heterogeneous EV (Wave 2)
- `__main__.py` — cron scheduling (Wave 2)
- `results_tracker.py` — no changes
