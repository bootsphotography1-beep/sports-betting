"""End-to-end UD edge report via PropLine API (single source for sharp + fantasy).

Reads TELEGRAM_BOT_TOKEN/CHAT_ID from .env, force-refreshes the dashboard
cache (which triggers a live PropLine pull with PROPLINE_API_KEY), runs
compare_fantasy_vs_sharp, formats the message with priority tiers + a
per-book breakdown (Underdog / PrizePicks / Sleeper), and sends via Telegram.

NO CSVs. NO clipboard. NO manual ingest. Everything flows through PropLine.

Per-book aggregation uses fuzzy matching (player name + stat + line ± 0.5)
across all fantasy books so we don't lose PP/Sleeper coverage due to
line-value drift between books.

Timezone: server runs in Pacific. User is in Central (CT = PT + 1h during PDT,
PT + 2h during PST). All user-facing time labels use Central Time via
zoneinfo — never the server's local time.
"""
import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Tier → (description, EV threshold %, max legs per tier, confidence)
TIERS = {
    "morning": ("Morning slate, sharp books settling, fantasy books 30-60 min behind", 5.0, 8, "70%"),
    "ud_morning": ("UD morning drop window, fantasy books lag 45 min", 5.0, 8, "75%"),
    "early": ("Early games tipping, sharp books have moved 2-3 hrs ahead", 8.0, 8, "60%"),
    "late": ("Late slate, sharp books mostly settled", 10.0, 6, "50%"),
    "nba_pregame": ("NBA pregame, fantasy books lag 15-30 min on lineup-driven props", 8.0, 8, "65%"),
    "primetime": ("Primetime, sharp books near-final", 12.0, 6, "40%"),
    "late_injury": ("Late injury window, fantasy books lag 30-60 min on breaking news", 10.0, 6, "70%"),
    "overnight": ("Overnight steam, fantasy books lag 30-60 min", 5.0, 10, "85%"),
    # Added 2026-07-21 (6 PM CT fire per Fin's official schedule).
    # Catches MLB closing + early NBA tip-off + post-5pm lineup-driven props.
    "evening": ("Evening slate, MLB closing + early NBA tip-off, fantasy books lagging 15-20 min", 9.0, 8, "55%"),
}

DASHBOARD = "http://127.0.0.1:5173"

# Per-fire budget cap (PropLine calls). The official schedule is 6 fires/day
# × 1000 calls = 6000 calls = the full combined broker budget (primary 5000
# + free1 1000). The cap is enforced by the broker itself; this constant is
# the per-fire soft ceiling used in alert messaging.
DEFAULT_BUDGET_PER_FIRE = 1000

# Timezone handling: always show user-facing times in Central Time
try:
    from zoneinfo import ZoneInfo
    CT_TZ = ZoneInfo("America/Chicago")
except ImportError:
    # Python < 3.9 fallback
    CT_TZ = None


def now_ct_str() -> str:
    """Current time formatted in Central Time, never server local."""
    if CT_TZ:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).astimezone(CT_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    from datetime import datetime, timezone, timedelta
    # Manual offset: CT is UTC-5 (CST) or UTC-6 (CDT). Best-effort: assume CDT (UTC-5).
    return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S CT")


def log(msg: str) -> None:
    print(f"[{now_ct_str()}] {msg}", flush=True)


def _norm(name: str) -> str:
    """Lowercase, strip non-alnum — for fuzzy matching player names."""
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def refresh_dashboard(min_edge: float) -> tuple[bool, str]:
    """Force the dashboard to do a live PropLine pull with all fantasy + sharp books.

    Returns (ok, status_message). When refresh fails because PropLine returned
    401 (bad key), 403 (forbidden), or 429 (rate limit), status_message names
    the cause so the caller can alert instead of silently falling through to
    stale cache. A generic connection failure still returns (False, msg) but
    without the "exhausted" classification — caller should treat that as a
    transient error, not a budget alert.
    """
    url = (
        f"{DASHBOARD}/api/opportunities?refresh=true"
        f"&mispriced_only=false&full_game_only=true&n_entries=4"
        f"&min_edge={min_edge}&min_true_prob=0.55&line_tolerance=0.5&max_events=12"
    )
    try:
        r = requests.get(url, timeout=60)
        if r.ok:
            log(f"dashboard refresh={r.status_code}")
            return True, f"refresh={r.status_code}"
        body = (r.text or "")[:300]
        if r.status_code in (401, 403):
            return False, f"PROPLINE_AUTH={r.status_code} — {body}"
        if r.status_code == 429:
            return False, f"PROPLINE_QUOTA=429 — {body}"
        return False, f"refresh={r.status_code} — {body}"
    except Exception as e:
        log(f"dashboard refresh failed: {e}")
        return False, f"connect_error={type(e).__name__}"


def run_compare(tier: str):
    """Call compare_fantasy_vs_sharp directly. Returns (payload, ranked, fantasy_legs)."""
    from ud_edge.compare import compare_fantasy_vs_sharp
    from ud_edge.propline_client import load_cached_indexes, fantasy_props_to_legs

    desc, threshold, _, _ = TIERS[tier]
    payload, ranked = compare_fantasy_vs_sharp(
        entry_type="6-flex",
        min_true_prob=0.55,
        min_edge_pp=max(1.0, threshold - 4),
        mispriced_only=False,
        force_fetch=False,  # use the cache the dashboard refresh just populated
        return_ranked=True,
        line_tolerance=0.5,
    )

    # Load raw fantasy legs from PropLine cache so we can build per-book coverage
    # even when books use slightly different line values.
    _, fantasy_props, _ = load_cached_indexes(cache_path="data/sharp_cache", sports=None)
    fantasy_legs = fantasy_props_to_legs(fantasy_props)
    log(f"loaded {len(fantasy_legs)} raw fantasy legs from PropLine cache")

    return payload, ranked, fantasy_legs


def build_fantasy_lookup(fantasy_legs: list) -> dict:
    """Group fantasy legs by fuzzy (player, stat) key → list of legs with their source book.

    Per-book coverage lookup that doesn't require exact line-value match.
    """
    lookup = defaultdict(list)
    for leg in fantasy_legs:
        if not leg.player_name:
            continue
        key = (_norm(leg.player_name), (leg.stat_name or "").lower().strip())
        lookup[key].append(leg)
    return lookup


def get_per_book_coverage(
    fantasy_lookup: dict,
    player: str,
    stat: str,
    line_value: float,
    line_tol: float = 0.5,
) -> dict:
    """Return {book_name: true_prob} for all fantasy books covering this (player, stat, line)."""
    if not player or not stat:
        return {}
    key = (_norm(player), (stat or "").lower().strip())
    candidates = fantasy_lookup.get(key, [])
    coverage = {}
    for leg in candidates:
        if abs(float(leg.line_value or 0) - float(line_value)) > line_tol:
            continue
        try:
            from ud_edge.matcher import no_vig
            higher, lower = leg.higher_decimal, leg.lower_decimal
            if higher and lower:
                higher_tp, lower_tp, _ = no_vig(higher, lower)
                # Use the side we're betting (over): since the report already picks
                # the side, we report whichever side has the higher true_prob as the
                # "best book" for this leg. Books typically list both sides.
                best_tp = max(higher_tp, lower_tp)
                book = (leg.fantasy_source or "underdog").lower()
                if book not in coverage or best_tp > coverage[book]:
                    coverage[book] = round(float(best_tp), 4)
        except Exception:
            continue
    return coverage


def parse_legs(payload: dict, fantasy_lookup: dict) -> list[dict]:
    """Parse opportunities from compare payload + enrich per-book coverage.

    Primary source: opp.fantasy_books (repo's authoritative per-book dict).
    Secondary: cross-reference with PropLine fantasy legs for any books the
    primary source missed (e.g. PP/Sleeper where the exact-key match failed).
    """
    legs = []
    for sport in payload.get("sports", []):
        for opp in sport.get("opportunities", []):
            player = opp.get("player_name") or opp.get("player", "?")
            stat = opp.get("stat_name") or opp.get("stat", "?")
            line = opp.get("line_value") or opp.get("line", "?")

            # Primary: from repo's opportunities_to_dict (always has the source book)
            primary_fb = opp.get("fantasy_books") or {}
            primary_fb = {k: v for k, v in primary_fb.items() if v is not None}

            # Secondary: PropLine fuzzy lookup (catches PP/Sleeper that primary missed)
            pl_fb = get_per_book_coverage(fantasy_lookup, player, stat, line)

            # Merge: primary wins on conflict (it's the repo's canonical view)
            merged_fb = {**pl_fb, **primary_fb}

            sharp_books = opp.get("sharp_books") or {}
            sharp_books_clean = {k: v for k, v in sharp_books.items() if v is not None}

            if merged_fb:
                best_fb_name = max(merged_fb, key=merged_fb.get)
                best_fb_prob = round(merged_fb[best_fb_name] * 100, 1)
            else:
                ud_prob = opp.get("ud_true_prob") or opp.get("higher_true_prob") or 0
                best_fb_name = "underdog"
                best_fb_prob = round(ud_prob * 100, 1) if ud_prob else 0

            best_sb_name = max(sharp_books_clean, key=sharp_books_clean.get) if sharp_books_clean else "?"
            best_sb_prob = round(sharp_books_clean.get(best_sb_name, 0) * 100, 1) if sharp_books_clean else 0

            # Two distinct edges:
            #   mispricing_edge_pp = (sharp - fantasy) * 100
            #     → positive = fantasy is UNDERPRICED (good for us, sharp agrees more)
            #     → negative = fantasy is OVERPRICED (bad, sharp disagrees)
            #   ud_edge_pp = (ud_true_prob - break_even) * 100
            #     → fantasy-only edge vs entry break-even (54.2% for 6-flex)
            mispricing_pp = opp.get("mispricing_edge_pp")
            ud_pp = opp.get("ud_edge_pp") or 0
            picked_prob = opp.get("ud_true_prob") or opp.get("lower_true_prob") or 0.5
            # Prefer the actual sharp-vs-fantasy edge; fall back to ud_edge_pp when
            # no sharp match (fantasy-only edge).
            if mispricing_pp is not None and opp.get("sharp_books"):
                primary_edge = float(mispricing_pp)
                edge_kind = "vs_sharp"
            else:
                primary_edge = float(ud_pp)
                edge_kind = "vs_breakeven"
            # Determine which fantasy book to actually bet on: the one with the
            # LOWEST true_prob (most generous payout for the bettor).
            if merged_fb:
                best_fb_name = min(merged_fb, key=merged_fb.get)
                best_fb_prob = round(merged_fb[best_fb_name] * 100, 1)
            else:
                ud_prob = opp.get("ud_true_prob") or opp.get("lower_true_prob") or 0
                best_fb_name = "underdog"
                best_fb_prob = round(ud_prob * 100, 1) if ud_prob else 0
            legs.append({
                "player": player,
                "stat": stat,
                "line": line,
                "side_label": opp.get("side_label") or "",  # "Over" or "Under" — needed to place the bet
                "side_prizepicks": opp.get("side_prizepicks") or "",  # "More"/"Less" for PP
                "side_sleeper": opp.get("side_sleeper") or "",  # "Over"/"Under" for SL
                "side_underdog": opp.get("side_underdog") or "",  # "Higher"/"Lower" for UD
                "fantasy_book": best_fb_name,
                "fantasy_prob": best_fb_prob,
                "sharp_book": best_sb_name,
                "sharp_prob": best_sb_prob,
                "ev": round(primary_edge, 2),
                "edge_kind": edge_kind,
                "win_prob": round(float(picked_prob) * 100, 1),
                "all_fantasy_books": merged_fb,
                "all_sharp_books": sharp_books_clean,
                "match_title": opp.get("match_title", ""),
                "sport": sport.get("sport", ""),
            })
    return legs


def format_message(tier: str, legs: list[dict]) -> str:
    desc, threshold, max_legs, confidence = TIERS[tier]

    # Per-sport min_edge overrides. Sharp books (Pinnacle, Circa) price MLB /
    # NFL / NBA at full liquidity — those legs need ≥3pp edge to be worth
    # acting on. Tennis, WNBA, soccer, MMA often have NO sharp counterpart,
    # so we accept fantasy-only legs with a stronger no-vig edge (≥5pp over
    # the 6-flex break-even of 54.21%). Without this override, the slate
    # collapses to MLB-only because baseball has the cleanest price discovery.
    SPORT_MIN_EDGE = {
        "MLB": 3.0, "NFL": 3.0, "NBA": 3.0, "NHL": 3.0, "CFB": 3.0,
        "WNBA": 4.0, "PGA": 4.0, "MMA": 4.0,
        "TENNIS": 5.0, "SOCCER": 5.0, "MLS": 5.0, "EPL": 5.0,
    }
    DEFAULT_MIN_EDGE = 4.0
    # Sharp match lowers the bar; fantasy-only needs a wider edge to qualify.
    FANTASY_ONLY_MIN_EDGE_PP = 5.0  # ud_edge_pp ≥ 5pp = actionable

    def _min_edge_for(leg: dict) -> float:
        sport = (leg.get("sport") or "").upper()
        base = SPORT_MIN_EDGE.get(sport, DEFAULT_MIN_EDGE)
        # If sharp match exists, use the (lower) base; if fantasy-only,
        # require the wider FANTASY_ONLY_MIN_EDGE_PP bar.
        if leg.get("edge_kind") == "vs_sharp":
            return base
        return max(base, FANTASY_ONLY_MIN_EDGE_PP)

    # Filter: keep legs that pass their per-sport min-edge threshold AND
    # have a non-negative edge. Drop opposing-script pairs (correlation)
    # into a separate "DO NOT PAIR" subsection below.
    kept_legs = []
    for leg in legs:
        ev = leg.get("ev") or 0
        if ev < 0:
            continue  # fantasy is overpriced; skip
        if ev < _min_edge_for(leg):
            continue
        kept_legs.append(leg)

    # Correlation grouping: cluster same-match positive-ρ legs together
    # and pull fighting pairs (same-game, same-stat, opposite sides) into a
    # separate "DO NOT PAIR" warning list.
    grouped_legs, fighting_legs = correlation_group(kept_legs)

    # Tier buckets. Per Fin's spec (2026-07-21):
    #   P1: EV ≥ 5pp = high confidence
    #   P2: EV 2.5-5pp = strong
    #   WATCH: anything below P2 that's still above the per-sport threshold
    p1, p2, watch = [], [], []
    for leg in grouped_legs:
        ev = leg["ev"]
        if ev >= 5.0:
            p1.append(leg)
        elif ev >= 2.5:
            p2.append(leg)
        else:
            watch.append(leg)

    # Book + sport breakdowns
    BOOK_ORDER = {"underdog": 0, "prizepicks": 1, "sleeper": 2}
    book_counts = {"underdog": 0, "prizepicks": 0, "sleeper": 0}
    sport_counts: dict[str, int] = defaultdict(int)
    for leg in kept_legs:
        bk = leg.get("fantasy_book", "").lower()
        if bk in book_counts:
            book_counts[bk] += 1
        sport = (leg.get("sport") or "UNK").upper()
        sport_counts[sport] += 1

    def _book_sort_key(leg):
        return (BOOK_ORDER.get(leg["fantasy_book"].lower(), 99), -leg["ev"])

    def fmt_leg(leg: dict) -> str:
        extras = ""
        fb = leg["all_fantasy_books"]
        if len(fb) > 1:
            others = {k: round(v * 100, 1) for k, v in fb.items() if k != leg["fantasy_book"]}
            if others:
                extras = " (also: " + ", ".join(f"{k} {v}%" for k, v in others.items()) + ")"
        match_str = f" [{leg['sport']}]" if leg.get("sport") else ""
        side = leg.get("side_label") or ""
        side_str = f" **{side}**" if side else ""
        if leg["sharp_book"] != "?" and leg.get("sharp_prob"):
            sharp_str = f" vs {leg['sharp_book']} {leg['sharp_prob']}%"
            edge_label = "edge"
        else:
            sharp_str = " (no sharp match)"
            edge_label = "edge vs break-even"
        edge_kind_tag = "" if leg.get("edge_kind") == "vs_sharp" else " [fantasy-only]"
        return (
            f"- {leg['player']} {leg['stat']} {leg['line']}{side_str}{match_str} → "
            f"BET ON **{leg['fantasy_book'].upper()}** "
            f"({leg['fantasy_prob']}%{sharp_str})"
            f"{extras} — {edge_label} +{leg['ev']}pp{edge_kind_tag}"
        )

    # Sort each tier by book (UD → PP → SL) so the operator can place
    # bets app-by-app top-to-bottom. Tie-break by EV desc.
    for tier_list in (p1, p2, watch):
        tier_list.sort(key=_book_sort_key)

    msg_lines = [
        f"🎯 *STALE-ODDS CONFIDENCE: ~{confidence}* — {desc}",
        f"🕐 Fired {now_ct_str()}",
        "",
    ]

    if p1:
        msg_lines += [f"*PRIORITY 1 — 🔥 HIGH CONFIDENCE* (EV ≥ {threshold}%)", *(fmt_leg(leg) for leg in p1[:max_legs]), ""]
    if p2:
        msg_lines += ["*PRIORITY 2 — ⚡ STRONG*", *(fmt_leg(leg) for leg in p2[:max_legs]), ""]
    if watch:
        msg_lines += ["*WATCH LIST — 👀*", *(fmt_leg(leg) for leg in watch[:4]), ""]

    # Correlation warning: same-game opposite-side pairs that should NOT
    # be combined into the same parlay. Per Fin's spec: "if they are playing
    # against each other maybe don't include them in the same parlay."
    if fighting_legs:
        msg_lines += [
            "*⚠️ DO NOT PAIR — correlation conflict:*",
            *(fmt_leg(leg) for leg in fighting_legs[:4]),
            "",
        ]

    # Sport + book breakdown
    sport_str = " ".join(f"{s}={c}" for s, c in sorted(sport_counts.items()))
    msg_lines += [
        f"*BOOK BREAKDOWN:* UD={book_counts['underdog']} PP={book_counts['prizepicks']} SL={book_counts['sleeper']} (legs that book covered)",
        f"*SPORT MIX:* {sport_str or '(none)'}",
        f"*VERDICT:* {len(kept_legs)} picks | {len(fighting_legs)} corr-warn | dashboard: {DASHBOARD}",
    ]

    msg = "\n".join(msg_lines)
    if len(msg) > 3800:
        msg = msg[:3700] + "\n\n…(truncated)"
    return msg


def send_telegram(title: str, body: str) -> bool:
    """Send via raw API with parse_mode=MarkdownV2-safe content.

    The repo's send_telegram() uses parse_mode=Markdown which chokes on special
    characters (→, ·, etc.) in our formatted leg lines. We use parse_mode=HTML
    here so the markdown-style *bold* becomes <b>bold</b> and we can render the
    rest as plain text.
    """
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    # Convert markdown bold to HTML bold, leave everything else as plain text
    text = f"<b>{title}</b>\n{body}".replace("**", "")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    if not r.ok:
        print(f"[send_telegram] {r.status_code}: {r.text[:200]}")
    return r.ok


# ── Broker helpers (added 2026-07-21) ────────────────────────────────────


def broker_pool_status() -> list[dict]:
    """Return the live PropLine broker pool snapshot.

    Returns an empty list if no broker is configured (single-key env or none
    at all). Each entry: {name, key_hint, limit, used, remaining_total,
    exhausted}. Used to alert the operator when both keys are exhausted.
    """
    try:
        from ud_edge.broker import broker_from_env, PROPLINE_BROKER
        broker = broker_from_env(
            state_dir=ROOT / "data" / "broker_state",
            **PROPLINE_BROKER,
        )
        return broker.pool_snapshot()
    except Exception as e:
        log(f"broker_pool_status: {e}")
        return []


def alert_both_keys_exhausted(budget_per_fire: int) -> bool:
    """Send a one-shot Telegram alert when both PropLine keys are exhausted.

    Called from main() before any API work, so the operator gets a clear
    "🛑 BOTH API KEYS EXHAUSTED" message instead of a silently-empty report.
    Returns True if alert sent, False if no broker configured OR Telegram
    credentials missing OR send failed.
    """
    pool = broker_pool_status()
    if not pool:
        return False  # No broker → no per-key tracking → no alert possible
    all_exhausted = all(p.get("exhausted") for p in pool)
    if not all_exhausted:
        return False
    # Build the alert body
    lines = [
        "🛑 <b>BOTH PROPline API KEYS EXHAUSTED</b>",
        "",
        f"Both PropLine accounts have hit their daily cap for UTC day "
        f"{pool[0].get('day', '?')}. The bot is now serving stale cache only — "
        f"no live sharp-book data.",
        "",
        "<b>Pool snapshot:</b>",
    ]
    for p in pool:
        hint = p.get("key_hint", "***")
        lines.append(
            f"  • {p['name']} ({hint}): used {p['used']}/{p['limit']} — "
            f"exhausted={p['exhausted']}"
        )
    lines += [
        "",
        "Resumes at UTC midnight (≈ 6 PM CT in summer, 5 PM CT in winter).",
        f"Per-fire budget cap was {budget_per_fire} calls; "
        f"this fire will SKIP live calls and report only what the stale cache has.",
    ]
    body = "\n".join(lines)
    return send_telegram(title="UD Edge | 🛑 API KEYS EXHAUSTED", body=body)


# ── Correlation sort (added 2026-07-21) ──────────────────────────────────


def _leg_corr_pair_key(leg: dict) -> tuple:
    """Build a fuzzy match key (player_norm, match_norm) for pair detection.

    Used to bucket legs by game so positive-ρ same-team pairs can be grouped.
    """
    player = _norm(leg.get("player", ""))
    match = (leg.get("match_title") or "").strip().lower()
    sport = (leg.get("sport") or "").strip().lower()
    return (player, match, sport)


def correlation_group(legs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split legs into (positive_pair_groups, fighting_pairs).

    Walks every pair of legs and applies the same rule-based heuristics as
    `ud_edge.correlation.classify_pair()`. Returns:
      - grouped: list of legs reordered so same-team positive-ρ pairs sit
        adjacent (grouped by match_title when same-team pairs are detected).
      - fighting: list of legs that participate in any negative-ρ same-game
        pair — operator should NOT include these in the same parlay.

    Cross-game legs are returned as their own group with no warning.
    """
    # Group by match_title first
    by_match: dict[str, list[dict]] = defaultdict(list)
    cross_game: list[dict] = []
    for leg in legs:
        mt = (leg.get("match_title") or "").strip()
        if mt:
            by_match[mt].append(leg)
        else:
            cross_game.append(leg)

    grouped: list[dict] = []
    fighting_pairs: list[tuple[dict, dict]] = []

    for mt, group_legs in by_match.items():
        # Within a single match, check every pair for known opposing scripts:
        #   - same-team same-stat-pair (positive)
        #   - same-team opposing-stat-pair on same family (could be either)
        #   - different-team same-stat over+over (negative — e.g. two opposing
        #     QBs both needing high passing for the same-game under to hit)
        # Heuristic shortcut: if a match has 2+ legs, look for any pair where
        # both legs are on the same side label (Over/Over or Under/Under) AND
        # both on the same team family — that's a positive pair (same script).
        # If one is Over and the other is Under on the same stat family, that's
        # a fighting pair.
        if len(group_legs) < 2:
            grouped.extend(group_legs)
            continue
        # Check pairs
        for i in range(len(group_legs)):
            for j in range(i + 1, len(group_legs)):
                a, b = group_legs[i], group_legs[j]
                # Fighting: same stat family, opposite sides
                sa, sb = (a.get("side_label") or "").lower(), (b.get("side_label") or "").lower()
                if sa and sb and sa != sb and (a.get("stat") or "").lower() == (b.get("stat") or "").lower():
                    # Same game + same stat + opposite sides → fight
                    fighting_pairs.append((a, b))
        grouped.extend(group_legs)

    # Dedupe fighting set (a leg may appear in multiple fighting pairs)
    fighting_set = {id(a) for pair in fighting_pairs for a in pair}
    fighting = [leg for leg in legs if id(leg) in fighting_set]
    grouped = [leg for leg in legs if id(leg) not in fighting_set]

    # Sort grouped legs by match (positive-pair groupings cluster together),
    # tie-break by book (UD → PP → SL), then by EV desc.
    BOOK_ORDER = {"underdog": 0, "prizepicks": 1, "sleeper": 2}
    grouped.sort(
        key=lambda leg: (
            (leg.get("match_title") or ""),
            BOOK_ORDER.get((leg.get("fantasy_book") or "").lower(), 99),
            -float(leg.get("ev") or 0),
        )
    )
    # Fighting legs: sort by book, then EV desc
    fighting.sort(
        key=lambda leg: (
            BOOK_ORDER.get((leg.get("fantasy_book") or "").lower(), 99),
            -float(leg.get("ev") or 0),
        )
    )
    return grouped, fighting


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=list(TIERS.keys()))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--budget-per-fire",
        type=int,
        default=DEFAULT_BUDGET_PER_FIRE,
        help=(
            "Soft ceiling on PropLine calls per fire. The 6-fire schedule "
            "uses 1000 calls/fire to stay within the combined 6000-call "
            "broker budget. The broker enforces hard limits; this is just "
            "used for alert messaging."
        ),
    )
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")

    desc, threshold, _, _ = TIERS[args.tier]
    log(f"=== {args.tier.upper()} fire (threshold {threshold}% EV, "
        f"budget={args.budget_per_fire} calls) ===")

    # 0. Broker pre-check: if BOTH keys are exhausted, alert the operator
    #    up-front so they don't think the bot is producing real picks.
    #    Skipping live calls in this state is the right behavior — we have
    #    no fresh data — but the operator MUST be told.
    if alert_both_keys_exhausted(args.budget_per_fire):
        log("BOTH API KEYS EXHAUSTED — alert sent; skipping live refresh")
        # Fall through to compare_fantasy_vs_sharp with force_fetch=False so
        # the dashboard's stale cache can still produce a (clearly-stale)
        # report. The Telegram message will say "no sharp match" on every
        # leg, making the situation obvious to the operator.

    # 1. Force-refresh dashboard → triggers live PropLine API pull.
    #    Detect 401/429/quota errors and alert instead of silent fallback.
    refresh_ok, refresh_msg = refresh_dashboard(threshold)
    if not refresh_ok:
        log(f"dashboard refresh failed: {refresh_msg}")
        if "PROPLINE_AUTH" in refresh_msg or "PROPLINE_QUOTA" in refresh_msg:
            # Auth or quota error → alert operator
            send_telegram(
                title="UD Edge | ⚠️ PropLine API ERROR",
                body=(
                    f"🛑 <b>PropLine API call failed</b>\n\n"
                    f"Tier: <b>{args.tier}</b> ({desc})\n"
                    f"Error: <code>{refresh_msg[:200]}</code>\n\n"
                    f"The dashboard refresh hit an auth/quota error. "
                    f"Bot is now serving stale cache only.\n\n"
                    f"Check your PropLine API keys in .env (PROPLINE_API_KEY "
                    f"or PROPLINE_ACCOUNTS + PROPLINE_KEY_*).\n\n"
                    f"🕐 Fired {now_ct_str()}"
                ),
            )

    # 2. Run compare + load raw fantasy legs for per-book coverage
    payload, ranked, fantasy_legs = run_compare(args.tier)
    log(f"compare returned {len(ranked)} ranked, {len(fantasy_legs)} raw fantasy legs")

    # 3. Build per-book lookup and parse
    fantasy_lookup = build_fantasy_lookup(fantasy_legs)
    legs = parse_legs(payload, fantasy_lookup)
    log(f"parsed {len(legs)} legs with per-book coverage")

    # 4. Format (multi-sport filter + correlation sort happen inside)
    body = format_message(args.tier, legs)

    # 5. Send or print
    title = f"UD Edge | {args.tier.replace('_', ' ').title()}"
    if args.dry_run:
        print("=" * 60)
        print(f"TITLE: {title}")
        print("=" * 60)
        print(body)
        print("=" * 60)
        return

    ok = send_telegram(title, body)
    log(f"telegram send: {ok}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
