"""DFS vs sharp sportsbook mispricing scanner.

Compares Underdog / Sleeper / PrizePicks / Dabble player props against
sharp books (Pinnacle, DK, FD, BetMGM, …) and surfaces every soft line.

Data source is intentionally flexible — PropLine free tier already returns
all four DFS books + sharps in two bulk calls. Owned scrapers can fill
sharp gaps when PropLine is unavailable.

Two mispricing signals:
  1. LINE gap — DFS posts an easier total than the sharp book
     (e.g. Over 0.5 hits on DFS while Pinnacle is 1.5).
  2. PROB gap — same line, DFS no-vig side probability is softer than sharp
     (skipped for PrizePicks ±100 even-money shells).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from ud_edge.injury_client import normalize_name as _normalize_name
from ud_edge.no_vig import no_vig


DFS_BOOKS = ("underdog", "sleeper", "prizepicks", "dabble")
SHARP_BOOKS = ("pinnacle", "draftkings", "fanduel", "betmgm", "betrivers", "bovada")

# Strip PropLine "Player (TEAM)" suffixes before matching.
_TEAM_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def clean_player_name(name: str) -> str:
    return _TEAM_SUFFIX.sub("", (name or "").strip()).strip()


def prop_key(player: str, stat: str, line: float | None = None) -> str:
    base = f"{_normalize_name(clean_player_name(player))}|{stat}"
    if line is None:
        return base
    return f"{base}|{line:g}"


def is_even_money_shell(over_decimal: float, under_decimal: float) -> bool:
    return abs(over_decimal - 2.0) < 1e-9 and abs(under_decimal - 2.0) < 1e-9


@dataclass
class Misprice:
    dfs_book: str
    sharp_book: str
    player: str
    stat: str
    sport: str
    dfs_line: float
    sharp_line: float
    side: str  # "over" | "under"
    line_gap: float  # positive = DFS easier for this side (pts)
    dfs_prob: Optional[float]
    sharp_prob: Optional[float]
    prob_edge_pp: Optional[float]  # positive = sharp more confident than DFS price
    kind: str  # "line" | "prob" | "both"

    def score(self) -> float:
        """Rank key: prefer large favorable line gaps, then prob edges."""
        line_score = max(self.line_gap, 0.0) * 10.0
        prob_score = max(self.prob_edge_pp or 0.0, 0.0)
        return line_score + prob_score


def _side_probs(over_dec: float, under_dec: float) -> Optional[tuple[float, float]]:
    try:
        o, u, _ = no_vig(over_dec, under_dec)
        return o, u
    except (ValueError, ZeroDivisionError):
        return None


def _index_sharp_props(sharp_props: list[dict]) -> dict[str, list[dict]]:
    """Index sharp props by player|stat for fuzzy line matching."""
    by_ps: dict[str, list[dict]] = {}
    for p in sharp_props:
        player = clean_player_name(p.get("player") or "")
        stat = p.get("stat")
        if not player or not stat or p.get("line") is None:
            continue
        if p.get("over_decimal") is None or p.get("under_decimal") is None:
            continue
        key = prop_key(player, stat)
        by_ps.setdefault(key, []).append({
            **p,
            "player": player,
            "norm": _normalize_name(player),
        })
    return by_ps


def _consensus_sharp_props(
    sharp_props: list[dict],
    *,
    outlier_pp: float = 12.0,
    min_sharp_overround: float = 0.98,
    max_sharp_overround: float = 1.18,
) -> list[dict]:
    """Collapse sharp books to a consensus row per player|stat|line.

    Drops books whose no-vig Over is >outlier_pp from the median (catches
    flipped Over/Under feeds). Lone BetMGM/FanDuel/Bovada rows with no
    Pinnacle/DK backup are discarded.
    """
    from statistics import median

    groups: dict[tuple, list[dict]] = {}
    for p in sharp_props:
        player = clean_player_name(p.get("player") or "")
        stat = p.get("stat")
        line = p.get("line")
        bk = (p.get("bookmaker") or "").lower()
        if not player or not stat or line is None or bk not in SHARP_BOOKS:
            continue
        if p.get("over_decimal") is None or p.get("under_decimal") is None:
            continue
        probs = _side_probs(p["over_decimal"], p["under_decimal"])
        if probs is None:
            continue
        try:
            _, _, ov = no_vig(p["over_decimal"], p["under_decimal"])
        except (ValueError, ZeroDivisionError):
            continue
        if ov < min_sharp_overround or ov > max_sharp_overround:
            continue
        key = (_normalize_name(player), stat, float(line))
        groups.setdefault(key, []).append({
            **p,
            "player": player,
            "bookmaker": bk,
            "_probs": probs,
        })

    trusted_solo = {"pinnacle", "draftkings"}
    out: list[dict] = []
    for (norm, stat, line), rows in groups.items():
        overs = [r["_probs"][0] for r in rows]
        anchors = [r for r in rows if r["bookmaker"] in trusted_solo]
        med = median([r["_probs"][0] for r in anchors]) if anchors else median(overs)
        kept = [r for r in rows if abs(r["_probs"][0] - med) * 100 <= outlier_pp]
        if not kept:
            kept = anchors or rows
        if len(kept) == 1 and kept[0]["bookmaker"] not in trusted_solo:
            continue
        med_over = median([r["_probs"][0] for r in kept])
        med_under = 1.0 - med_over
        # Prefer naming the highest-priority book that survived
        rank = {b: i for i, b in enumerate(SHARP_BOOKS)}
        best_bk = min(kept, key=lambda r: rank.get(r["bookmaker"], 99))["bookmaker"]
        label = best_bk if len(kept) == 1 else f"consensus:{best_bk}"
        out.append({
            "player": kept[0]["player"],
            "stat": stat,
            "line": line,
            # Unit overround so no_vig → median probs exactly
            "over_decimal": 1.0 / max(med_over, 1e-6),
            "under_decimal": 1.0 / max(med_under, 1e-6),
            "bookmaker": label,
            "sport": kept[0].get("sport") or "",
            "_n_books": len(kept),
        })
    return out


def find_misprices(
    dfs_props: list[dict],
    sharp_props: list[dict],
    *,
    min_line_gap: float = 0.5,
    max_line_gap: float = 2.0,
    min_prob_edge_pp: float = 2.0,
    line_match_tolerance: float = 0.0,
    min_sharp_overround: float = 0.98,
    max_sharp_overround: float = 1.18,
) -> list[Misprice]:
    """Compare every DFS prop against consensus sharp props.

    Sharp books are first collapsed to a median consensus per
    player|stat|line (outlier books like flipped BetMGM feeds dropped).
    LINE shopping uses that book's main line (closest to 50/50);
    PROB shopping requires an exact line match.
    """
    consensus = _consensus_sharp_props(
        sharp_props,
        min_sharp_overround=min_sharp_overround,
        max_sharp_overround=max_sharp_overround,
    )
    # Treat consensus labels as sharp
    sharp_index = _index_sharp_props(consensus)
    out: list[Misprice] = []

    for dfs in dfs_props:
        dfs_book = (dfs.get("bookmaker") or "").lower()
        if dfs_book not in DFS_BOOKS:
            continue
        player = clean_player_name(dfs.get("player") or "")
        stat = dfs.get("stat")
        dfs_line = dfs.get("line")
        if not player or not stat or dfs_line is None:
            continue
        over_d = dfs.get("over_decimal")
        under_d = dfs.get("under_decimal")
        if over_d is None or under_d is None:
            continue

        candidates = sharp_index.get(prop_key(player, stat)) or []
        if not candidates:
            continue

        dfs_even = is_even_money_shell(over_d, under_d)
        dfs_probs = None if dfs_even else _side_probs(over_d, under_d)
        dfs_line_f = float(dfs_line)

        # Main line among consensus rows for this player|stat
        mains = []
        for sharp in candidates:
            probs = _side_probs(sharp["over_decimal"], sharp["under_decimal"])
            if probs is None:
                continue
            mains.append({**sharp, "_probs": probs})
        if not mains:
            continue
        main = min(mains, key=lambda r: abs(r["_probs"][0] - 0.5))
        sharp_book = str(main.get("bookmaker") or "consensus")
        sharp_line = float(main["line"])
        line_gap_over = sharp_line - dfs_line_f
        line_gap_under = dfs_line_f - sharp_line

        for side, gap in (("over", line_gap_over), ("under", line_gap_under)):
            if gap < min_line_gap or gap > max_line_gap + 1e-9:
                continue
            out.append(Misprice(
                dfs_book=dfs_book,
                sharp_book=sharp_book,
                player=player,
                stat=stat,
                sport=str(dfs.get("sport") or main.get("sport") or ""),
                dfs_line=dfs_line_f,
                sharp_line=sharp_line,
                side=side,
                line_gap=gap,
                dfs_prob=None,
                sharp_prob=None,
                prob_edge_pp=None,
                kind="line",
            ))

        if dfs_probs is None:
            continue
        for sharp in mains:
            sharp_line = float(sharp["line"])
            if abs(dfs_line_f - sharp_line) > line_match_tolerance + 1e-9:
                continue
            sharp_probs = sharp["_probs"]
            for side, idx in (("over", 0), ("under", 1)):
                dfs_p = dfs_probs[idx]
                sharp_p = sharp_probs[idx]
                prob_edge = (sharp_p - dfs_p) * 100.0
                if prob_edge < min_prob_edge_pp:
                    continue
                out.append(Misprice(
                    dfs_book=dfs_book,
                    sharp_book=str(sharp.get("bookmaker") or "consensus"),
                    player=player,
                    stat=stat,
                    sport=str(dfs.get("sport") or sharp.get("sport") or ""),
                    dfs_line=dfs_line_f,
                    sharp_line=sharp_line,
                    side=side,
                    line_gap=0.0,
                    dfs_prob=dfs_p,
                    sharp_prob=sharp_p,
                    prob_edge_pp=prob_edge,
                    kind="prob",
                ))

    best: dict[tuple, Misprice] = {}
    for m in out:
        key = (
            m.dfs_book,
            _normalize_name(m.player),
            m.stat,
            f"{m.dfs_line:g}",
            m.side,
            m.kind,
        )
        prev = best.get(key)
        if prev is None or m.score() > prev.score():
            best[key] = m

    return sorted(best.values(), key=lambda m: (-m.score(), m.dfs_book, m.player))


def format_misprice_report(
    misprices: list[Misprice],
    *,
    top_n: int = 40,
    title: str = "DFS vs Sharp Misprices",
) -> str:
    lines = [f"# {title}", ""]
    if not misprices:
        lines.append("_No misprices found above thresholds._")
        return "\n".join(lines)

    by_dfs: dict[str, list[Misprice]] = {}
    for m in misprices:
        by_dfs.setdefault(m.dfs_book, []).append(m)

    lines.append(
        f"Found **{len(misprices)}** soft edges across "
        f"{', '.join(f'{k}={len(v)}' for k, v in sorted(by_dfs.items()))}."
    )
    lines.append("")
    header = (
        f"{'#':>3}  {'DFS':<11} {'PLAYER':<22} {'SIDE':<28} "
        f"{'DFS':>5} {'SHARP':>5} {'GAP':>5} {'Δpp':>6}  SHARP"
    )
    lines.append("```")
    lines.append(header)
    lines.append("-" * len(header))
    for i, m in enumerate(misprices[:top_n], 1):
        side = f"{m.side.title()} {m.dfs_line:g} {m.stat.replace('_', ' ')}"
        gap = f"{m.line_gap:+.1f}" if m.line_gap else "  —"
        dpp = f"{m.prob_edge_pp:+.1f}" if m.prob_edge_pp is not None else "   —"
        lines.append(
            f"{i:>3}  {m.dfs_book:<11} {m.player[:22]:<22} {side[:28]:<28} "
            f"{m.dfs_line:>5.1f} {m.sharp_line:>5.1f} {gap:>5} {dpp:>6}  "
            f"{m.sharp_book}"
        )
    lines.append("```")
    return "\n".join(lines)


def fetch_dfs_and_sharp_props(
    *,
    propline_key: str,
    sports: list[str],
    cache_path: Optional[Path] = None,
    include_scrapers: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Pull DFS + sharp props (PropLine preferred; scrapers augment sharp)."""
    from ud_edge.sharp_books_client import PropLineClient

    pl = PropLineClient(propline_key, cache_path=cache_path)
    dfs_props: list[dict] = []
    sharp_props: list[dict] = []

    for sport in sports:
        dfs_props.extend(pl.fetch_props_by_books(sport, list(DFS_BOOKS), collapse=False))
        sharp_props.extend(pl.fetch_props_by_books(sport, list(SHARP_BOOKS[:4]), collapse=False))

    if include_scrapers and ("MLB" in sports or not sports):
        try:
            from ud_edge.book_scrapers import fetch_owned_sharp_props
            scraped = fetch_owned_sharp_props(sports=["MLB"], cache_path=cache_path)
            for p in scraped:
                p = dict(p)
                p.setdefault("sport", "MLB")
                # Skip synthetic milestone shells — they poison prob edges
                if p.get("source", "").endswith("milestone"):
                    continue
                sharp_props.append(p)
        except Exception as e:
            print(f"[misprice] scrapers skipped: {e}")

    return dfs_props, sharp_props


def misprices_to_dicts(misprices: list[Misprice]) -> list[dict]:
    return [asdict(m) for m in misprices]
