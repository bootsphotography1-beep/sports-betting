# Remediation Re-Audit — tip `4855201`

**Scope:** Four commits after `fa6d27b` on `fix/audit-remediation-v2` (`e49b0d1` … `4855201`), responding to residuals in `docs/MISPRICING_REMEDIATION_AUDIT.md`.

**Verdict:** The **six original engineering findings are now closed in production code**, and **all 62 audit pinning tests pass** in this environment (including poller tests without a real PropLine key, and dashboard FastAPI tests). Strategy-level blockers (side-flip, payout verification, calibration sample) are **unchanged** — still research-only, still not live-money ready.

---

## 1. What this push closed

| Prior residual | Commit | Status |
|----------------|--------|--------|
| CLI `__main__` console used fantasy avg + homogeneous EV | `e49b0d1` | **Closed** — `effective_true_prob` + `expected_value_per_card` |
| `deliver.py` Markdown still homogeneous-avg | `d7ab071` | **Closed** — per-card EV; partial-card homogeneous fallback only when `len ≠ n_legs` |
| Poller pin tests unreachable without `PROPLINE_API_KEY` | `9b05a34` | **Closed** — tests `monkeypatch.setenv(...)` |
| README guardrail self-hit on literal `52.40%` | `9b05a34` | **Closed** — paraphrased; guardrail passes |
| `line_tolerance` not through compare/poller/dashboard; `match_distance` not on `RankedLeg` | `4855201` | **Closed** — full plumbing + JSON emit |

### Test evidence (this agent)

```text
pytest tests/test_audit_p0_*.py tests/test_audit_p1_*.py -q
→ 62 passed
```

---

## 2. Finding scorecard (cumulative)

| ID | Finding | Status at `4855201` |
|----|---------|---------------------|
| P0 #1 | `max(fantasy, sharp)` overstate | **Closed** (deliver + console) |
| P0 #2 | Homogeneous lineup EV | **Closed** (compare, dashboard, deliver, console) |
| P1 #3 | Stale break-even docs | **Closed** (+ guardrail clean) |
| P1 #4 | Poller budget under-count | **Closed** (+ tests reachable) |
| P1 #5 | Poller JSON RankedLeg rebuild | **Closed** (+ tests reachable) |
| P1 #6 | Hard-coded / unplumbed line tolerance | **Closed** (CLI, compare, poller, `/api/opportunities`, `RankedLeg.match_distance`) |
| P0 #7 | Side-flip (fantasy underdog when sharp prefers) | **Still deferred** |
| — | External payout verification | **Open** (`_PAYOUT_MODEL_VERIFIED = False`, no `PAYOUT_RULES.md`) |
| — | ≥50 settled legs / calibration | **Open** (no `results.json`) |

---

## 3. Remaining residuals (lower severity)

1. **Calibration still logs fantasy `picked_true_prob`** (`results_tracker.py`). Board EV is now sharp-authoritative; Brier/log-loss will measure the *wrong* probability if you settle without also storing `effective_true_prob`. Fix before treating Wave 4 calibration as meaningful.

2. **`/api/lineups` does not take `line_tolerance`.** It consumes `_RANKED_CACHE` from `/api/opportunities`. Operators must refresh opportunities with the desired tolerance first — fine if documented; easy to miss.

3. **Single-entry CLI thin-slate:** `__main__` calls `expected_value_per_card(et, per_leg)` without the deliver-style `len == n_legs` guard. If `top` is shorter than the entry size, console comparison can `ValueError`. Multi-entry / full cards are safe.

4. **`recommend_entry(et, avg_prob)`** in the single-entry console loop still gets a scalar average while printed EV uses per-card — label can disagree with printed EV on mixed cards.

5. **README remediations table** still describes the v1 fixes and says “all six are fixed”; it does not mention v3 residual closes. Accurate enough on outcomes, slightly stale on narrative.

6. **Soft-line semantics unchanged:** wider `line_tolerance` still compares no-vig on *different* lines without a line-gap model. Configurable plumbing ≠ correct soft-line pricing.

---

## 4. Strategy viability (updated)

| Question | Answer |
|----------|--------|
| Are the audit’s engineering defects that overstated edge fixed? | **Yes**, on dashboard, Markdown, and CLI console paths. |
| Are mispricing flags safer to trust as *candidates*? | **Yes, more than at `85d6722` / `fa6d27b`.** |
| Is the strategy live-money viable? | **No.** Side-flip still missing; payouts unverified; n=0 settled; calibration stores fantasy probs. |
| Next gate for viability | Log effective probs → settle ≥50 → archive payouts → then consider side-flip / soft-line model. |

---

## 5. Bottom line

This push **honestly closes** the remediation residuals called out last round. Treat tip `4855201` as **engineering-complete for the six P0/P1 items**. Do **not** treat it as strategy-complete: research mode remains correct, and the monetization thesis is still unproven.
