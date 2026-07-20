# HONEST_STATUS — Wave 4 (Validation & Operations)

**Generated:** 2026-07-19
**Branch:** `fix/audit-remediation-v2`
**Baseline:** `893f9a2bd68d40ab3bc5997ebe828b495995fed4`
**Commits:** 9 remediation commits layered on top of baseline

---

## Research Mode — STILL ACTIVE

**is_research_mode: TRUE**

The system is still operating in **research-only mode** because calibration
(≥50 settled legs) is not yet met. The four intermediate waves have fixed
the math, signal integrity, source identity, and operational UX, but the
full flip to verified mode requires both:
1. ✅ Payout model verified mathematically (Wave 1 — DONE)
2. ⬜ ≥50 settled HIT/MISS legs recorded (Wave 4+ — not yet met)

---

## Wave-by-Wave Summary

| Wave | Focus | Status | Findings closed |
|------|-------|--------|------------------|
| **0** | Research-only safety gate (label scrubbing, central safety status, dashboard banner) | ✅ | SB-P0-01/02/03 cosmetic guard, SB-P2-03, SB-P2-05 |
| **1** | Numerical break-even + heterogeneous per-leg EV | ✅ | SB-P0-01, SB-P1-04 |
| **2A** | Sharp-authoritative policy + exact tolerance + freshness + event identity | ✅ | SB-P1-01, SB-P1-02, SB-P1-03 |
| **2B** | Started/expired market rejection + consolidated trivial-prop filter | ✅ | SB-P1-05, SB-P1-06 |
| **3A** | Source/platform identity, canonical dedupe, valid copy targets | ✅ | SB-P1-07 |
| **3B** | Sport aliases, empty-slate guard, CSV diagnostics, results path | ✅ | SB-P1-08, SB-P2-01, SB-P2-02 |
| **4** | CI workflow, ruff clean, smoke verification | ✅ | SB-P2-05 |

---

## Audit Findings — Final Status

| Finding | Description | Status |
|---------|-------------|--------|
| SB-P0-01 | 6-flex break-even wrong by 1.81 pp | ✅ Fixed (Wave 1) |
| SB-P0-02 | Payout model unverified | ⚠️ Math verified; externally unverified |
| SB-P0-03 | 0 settled outcomes / uncalibrated | ⬜ Pending (≥50 settled legs) |
| SB-P1-01 | Sharp disagreement not vetoing fantasy | ✅ Fixed (Wave 2A) |
| SB-P1-02 | Sharp line tolerance silently doubled | ✅ Fixed (Wave 2A) |
| SB-P1-03 | Sharp data without freshness / event identity | ✅ Fixed (Wave 2A) |
| SB-P1-04 | EV uses lineup-average probability, not per-leg | ✅ Fixed (Wave 1) |
| SB-P1-05 | Normal ranking didn't reject started events | ✅ Fixed (Wave 2B) |
| SB-P1-06 | Trivial `Under 0.5 Runs` dominated default ranking | ✅ Fixed (Wave 2B) |
| SB-P1-07 | Source/platform identity lost, duplicate markets | ✅ Fixed (Wave 3A) |
| SB-P1-08 | `--sport NBA` filter mismatched, empty slate crashed | ✅ Fixed (Wave 3B) |
| SB-P1-09 | Dashboard XSS via bookmaker label | ✅ Fixed (Wave 0) |
| SB-P2-01 | CSV parser swallowed per-row errors | ✅ Fixed (Wave 3B) |
| SB-P2-02 | Result storage CWD-relative | ✅ Fixed (Wave 3B) |
| SB-P2-03 | Dashboard invalid query returned 500 | ✅ Fixed (Wave 0) |
| SB-P2-04 | README claims installed cron that does not exist | ✅ Fixed (Wave 0 — no auto-cron scheduled) |
| SB-P2-05 | No CI / lint not enforced | ✅ Fixed (Wave 4) |

---

## What Unblocks Verified Mode (Full Wave 4+)

All three conditions must hold before lifting research-only mode:

- [x] Payout model mathematically verified.
- [ ] ≥50 settled HIT/MISS legs in `data/results.json`.
- [ ] Brier score and log-loss within acceptable calibration bands.

---

## Test & Quality Gates (Wave 4)

- 298/298 tests pass locally on Windows + Python 3.11.9
- `ruff check .` is clean (0 findings)
- Smoke verification: `/`, `/api/health`, `/HONEST_STATUS.md` return 200; `/api/opportunities?entry=bogus` and `?min_true_prob=nan` return 400 JSON
- CI workflow added at `.github/workflows/ci.yml`:
  - Lint (`ruff check .`)
  - `pytest -q`
  - `--self-test`
  - `--dry-run`
  - Dashboard smoke loop
  - `pip-audit --strict`

---

## Files Changed in Wave 4

- `.github/workflows/ci.yml` — new CI pipeline
- `HONEST_STATUS.md` — this file
- ruff auto-fixes removed unused imports across all 9 wave test files

---

## What Did NOT Change in Wave 4

- No cron job was created (per Wave 0 contract: cron only after Wave 1+ acceptance gates pass)
- No live sportsbook integration was re-introduced
- No external payout rules were verified (the official model is still self-declared)

---

## Recommended Next Steps (post-remediation, before live use)

1. Verify official Underdog Fantasy payout rules and archive as `docs/PAYOUT_RULES.md`.
2. Set `_PAYOUT_MODEL_VERIFIED = True` in `ud_edge/safety_gate.py` and add a regression test that re-fetches rules and asserts.
3. Settle ≥50 real picks via `--settle` and confirm Brier/log-loss within bands.
4. Then and only then: re-enable recommendation labels in `_VERIFIED_LABELS` and schedule the daily cron.
