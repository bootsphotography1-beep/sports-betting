# PropLine Budget Math — 5,000 calls/day, max accuracy

**Date:** 2026-07-19
**Budget:** 5,000 calls/day, 10% reserve for alerts (4,500 routine + 500 alert bursts)
**Target sports:** NBA, NFL, MLB, NHL, WNBA, CFB, EPL, MLS (8 leagues)
**Goal:** Maximize cross-book +EV picks while staying inside the daily budget.

---

## Per-call anatomy

PropLine returns **Pinnacle + DK/FD/BetMGM sharp prices** + **PrizePicks / Underdog / Sleeper fantasy lines** for the same player+stat in **one** feed. So a single call is the *whole* sharp-vs-soft comparison for one sport for one snapshot window.

| Call shape | What you get | Typical response size |
|---|---|---|
| `/sports/{sport}/events` | List of games (id, tipoff, teams) | 10–30 events |
| `/sports/{sport}/events/{id}/odds?markets=player_props` | Per-event Pinnacle + DK/FD sharp + PP/UD/Sleeper fantasy | 50–200 props |

**Two-call cycle per sport per snapshot:**
- 1× `events` → enumerates today's/tomorrow's events
- 1× `odds` per event → fills the sharp+fantasy table

For a typical slate (NBA has 10 events/day, MLB 15, NFL 12, NHL 6, WNBA 4, EPL 10, MLS 6):
- **8 sports × ~10 events ≈ 80 events/day**
- **80 events × 1 odds call each = 80 odds calls/day**

So **a single complete sweep of every sport = ~88 calls.**

That's 5,000 / 88 = **56 full sweeps per day** if we did nothing else. Plenty.

But wait — we also need **historical settlement pulls** (cron after games) and **alert-triggered re-pulls** when a sharp price moves. And we want to maximize accuracy on the *fast-moving* slate (NBA in-season evenings).

## Adaptive cadence (from deleted cursor PR a163652)

| Time-to-tipoff | Pull interval | Rationale |
|---|---|---|
| ≤ 90 min | **45 s** | Lines move fastest right before tipoff; most +EV signals exist |
| 90–240 min | **3 min** | Pre-game market discovery |
| 240–720 min | **10 min** | Lineup announcement / injury report cycle |
| > 720 min | **15 min** | Early week, lines mostly stale |

Per sport, the budget distribution:

| Window | Duration | Polls | Calls/sweep | Calls |
|---|---|---|---|---|
| ≤ 90 min pre-game | 1.5 hr | 120 polls @ 45s | 1 | 120 |
| 90–240 min | 2.5 hr | 50 polls @ 3min | 1 | 50 |
| 240–720 min | 8 hr | 48 polls @ 10min | 1 | 48 |
| > 720 min | overnight | 32 polls @ 15min | 1 | 32 |
| **Per-sport-per-day total** | ~16 hr | | | **~250 calls** |

For 8 sports: **8 × 250 = 2,000 calls/day** if we treat each sport independently. That's well inside 4,500. But we should **deduplicate across sports** (a single call can fetch multi-sport events if the API supports it — verify; if not, leave 2,000).

Plus:
- **Settlement pulls** after games (one call per completed event): ~80 calls
- **Alert-triggered refresh** when a sharp line moves >2pp: budgeted at 500/day reserve

**Total budget: 2,000 routine + 80 settle + 500 alert reserve + 2,420 headroom for expansion** = under cap.

## Accuracy-maximizing tactics

1. **Tipoff-proximity weighting.** As tipoff approaches, line moves produce more +EV. Spend the most polls in the ≤ 90 min window. Above math already does this.
2. **Single-pass multi-sport call.** Check if PropLine `/events` accepts a `sport` list. If yes, one call sweeps all 8 sports → 1× events + 80× odds = 81 calls per full sweep instead of 88×8 = 704. Saves 623 calls/day.
3. **Deduplicate.** Same player+stat+line on multiple events (parallel games) should be one lookup, not eight.
4. **Cache TTL by window.** ≤ 90 min → cache 30s (lines move fast); > 720 min → cache 5 min (lines stable). Let the cache absorb the redundant polls.
5. **Alert pull priority.** When a sharp line moves >2pp, refresh immediately (out of cycle). Reserve 500/day for these bursts so a volatile slate can't starve routine polls.
6. **Settle at low-traffic hours.** 3am ET pull for settlement (games finished), doesn't compete with the high-cadence evening window.
7. **Correlation-aware pick selection.** Even with 5k calls, the *number of picks* is the bottleneck, not the calls. Use 6-man and 4-man flex entries so each card holds 4–6 legs. Cursor's correlation analyzer (deleted by PR #4) prevents picking two QB props from the same game (or two RBs from the same backfield) into the same card. Restoring it raises EV-per-card by an order of magnitude more than the call budget matters.

## The budget table

| Category | Calls/day | Cap | Notes |
|---|---|---|---|
| Routine adaptive sweeps (all sports) | 2,000 | 4,500 | Plenty of room; could double if needed |
| Settlement pulls (post-game) | 80 | — | 3am ET batch |
| **Reserve** | | **2,920** | For alert bursts and expansion |
| Alert bursts (sharp >2pp move) | up to 500 | — | Variable; pulls from reserve |
| Test/smoke/manual debug | up to 100 | — | Pulls from reserve |

## Implication for live alerts

With this math, the bot can:
- Sweep every sport every 45s near tipoff (peak NBA = ~300 calls/hr)
- Run from 6pm ET (NBA tipoffs) through 1am ET (settle) on ~2,000 calls
- Catch every meaningful +EV signal within ~30s of it appearing
- Alert Fin via Slack on each new mispricing ≥2pp
- Never exceed the cap

That's the answer to "max accuracy within 5k": **adaptive cadence + multi-sport batching + correlation-aware pick selection**.