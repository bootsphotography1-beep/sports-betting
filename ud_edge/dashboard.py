"""Live HTML dashboard for mispriced Underdog legs.

Rule of thumb for this bot:
  • Signal comes from PropLine sharps (Pinnacle / DK / FD / BetMGM / Sleeper)
  • You always PLACE the pick on Underdog Fantasy — that is where the soft
    same-side price lives relative to the sharp book.
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ud_edge.deliver import _fmt_side
from ud_edge.flex_math import UD_PAYOUTS, expected_value
from ud_edge.injury_client import ESPNInjuryClient
from ud_edge.matcher import build_lineups, effective_true_prob, rank_legs
from ud_edge.models import RankedLeg
from ud_edge.propline_client import (
    BOOK_PRIORITY,
    PropLineClient,
    SPORT_MAP,
    parse_prop_outcomes_to_index_rows,
    propline_configured,
)
from ud_edge.injury_client import normalize_name
from ud_edge.ud_client import UDClient


# Deep stadium night + chartreuse signal (avoid purple / cream-terracotta clusters)
DASHBOARD_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,500;8..60,700&display=swap');

:root {
  --bg0: #07120e;
  --bg1: #0d1f18;
  --ink: #e8f5ee;
  --muted: #8aa897;
  --line: rgba(232,245,238,0.12);
  --signal: #c8f542;
  --signal-dim: #8fb82e;
  --warn: #ffb020;
  --app: #3dff9a;
  --danger: #ff5c5c;
  --panel: rgba(13, 31, 24, 0.72);
}

* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background:
    radial-gradient(1200px 600px at 10% -10%, rgba(200,245,66,0.14), transparent 55%),
    radial-gradient(900px 500px at 100% 0%, rgba(61,255,154,0.08), transparent 50%),
    linear-gradient(165deg, var(--bg0), var(--bg1) 45%, #050a08);
  color: var(--ink);
  font-family: "Source Serif 4", Georgia, serif;
  min-height: 100%;
}

body::before {
  content: "";
  position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background-image:
    linear-gradient(rgba(232,245,238,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(232,245,238,0.03) 1px, transparent 1px);
  background-size: 48px 48px;
  mask-image: radial-gradient(ellipse at center, black 30%, transparent 75%);
}

.wrap { position: relative; z-index: 1; max-width: 1100px; margin: 0 auto; padding: 28px 20px 64px; }

/* Hero — one composition */
.hero {
  display: grid;
  gap: 10px;
  padding: 8px 0 28px;
  border-bottom: 1px solid var(--line);
  animation: rise 0.7s ease-out both;
}
.brand {
  font-family: "Bebas Neue", sans-serif;
  font-size: clamp(3.4rem, 10vw, 5.6rem);
  letter-spacing: 0.04em;
  line-height: 0.9;
  color: var(--signal);
  text-shadow: 0 0 40px rgba(200,245,66,0.25);
}
.hero h1 {
  margin: 0;
  font-family: "Source Serif 4", serif;
  font-weight: 500;
  font-size: clamp(1.15rem, 2.6vw, 1.45rem);
  color: var(--ink);
  max-width: 34ch;
}
.hero p {
  margin: 0;
  color: var(--muted);
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.82rem;
  max-width: 52ch;
}
.meta {
  display: flex; flex-wrap: wrap; gap: 10px 18px;
  margin-top: 8px;
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.75rem;
  color: var(--muted);
}
.meta strong { color: var(--signal); font-weight: 600; }

/* Rule callout */
.place-rule {
  margin: 22px 0 8px;
  padding: 16px 18px;
  border-left: 3px solid var(--app);
  background: linear-gradient(90deg, rgba(61,255,154,0.10), transparent 70%);
  animation: rise 0.8s 0.1s ease-out both;
}
.place-rule .label {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.7rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--app);
  margin-bottom: 6px;
}
.place-rule h2 {
  margin: 0 0 6px;
  font-size: 1.35rem;
  font-weight: 700;
}
.place-rule p {
  margin: 0;
  color: var(--muted);
  font-size: 0.95rem;
  max-width: 60ch;
}

section { margin-top: 34px; animation: rise 0.75s ease-out both; }
section:nth-of-type(2) { animation-delay: 0.12s; }
section:nth-of-type(3) { animation-delay: 0.2s; }

.sec-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 12px; margin-bottom: 14px;
}
.sec-head h3 {
  margin: 0;
  font-family: "Bebas Neue", sans-serif;
  font-size: 1.9rem;
  letter-spacing: 0.06em;
  color: var(--ink);
}
.sec-head span {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.72rem;
  color: var(--muted);
}

.pick {
  display: grid;
  grid-template-columns: 56px 1fr auto;
  gap: 14px 18px;
  align-items: center;
  padding: 16px 0;
  border-top: 1px solid var(--line);
  transition: background 0.25s ease, transform 0.25s ease;
}
.pick:hover { background: rgba(200,245,66,0.04); transform: translateX(2px); }
.rank {
  font-family: "Bebas Neue", sans-serif;
  font-size: 2rem;
  color: var(--signal-dim);
  text-align: center;
}
.who { min-width: 0; }
.who .name {
  font-size: 1.2rem;
  font-weight: 700;
  margin: 0 0 4px;
}
.who .pick-line {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.88rem;
  color: var(--signal);
  margin: 0 0 6px;
}
.who .match {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.72rem;
  color: var(--muted);
  margin: 0;
}
.stats {
  display: grid;
  grid-auto-flow: column;
  gap: 14px;
  text-align: right;
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.72rem;
}
.stats div span {
  display: block;
  color: var(--muted);
  margin-bottom: 3px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.stats .v { font-size: 1.05rem; font-weight: 600; color: var(--ink); }
.stats .delta.pos { color: var(--signal); }
.stats .delta.neg { color: var(--danger); }

.app-cta {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 2px;
  padding: 10px 12px;
  border: 1px solid rgba(61,255,154,0.35);
  background: rgba(61,255,154,0.07);
}
.app-cta .app-name {
  font-family: "Bebas Neue", sans-serif;
  font-size: 1.35rem;
  letter-spacing: 0.05em;
  color: var(--app);
}
.app-cta .why {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.72rem;
  color: var(--muted);
}
.app-cta a {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--bg0);
  background: var(--app);
  text-decoration: none;
  padding: 8px 12px;
  border: none;
}
.app-cta a:hover { filter: brightness(1.08); }

.urgent-tag {
  display: inline-block;
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.65rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--warn);
  border: 1px solid rgba(255,176,32,0.45);
  padding: 2px 6px;
  margin-left: 8px;
  vertical-align: middle;
}

.empty {
  font-family: "IBM Plex Mono", monospace;
  color: var(--muted);
  padding: 18px 0;
  border-top: 1px solid var(--line);
}

.lineup {
  margin-top: 10px;
  padding: 14px 0 4px;
  border-top: 1px solid var(--line);
}
.lineup .ev {
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.78rem;
  color: var(--muted);
  margin-bottom: 8px;
}

.corr {
  margin-top: 12px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  background: rgba(0,0,0,0.25);
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.78rem;
  line-height: 1.45;
  color: var(--muted);
  white-space: pre-wrap;
}
.corr strong, .corr b { color: var(--signal); font-weight: 600; }
.corr .warn { color: var(--warn); }

footer {
  margin-top: 48px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.7rem;
  color: var(--muted);
}

@keyframes rise {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: none; }
}

@media (max-width: 720px) {
  .pick { grid-template-columns: 40px 1fr; }
  .stats { grid-column: 2; justify-content: start; text-align: left; margin-top: 4px; }
}
"""


def _parse_sched(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _mins_until(scheduled_at: Optional[str], now: datetime) -> Optional[float]:
    t = _parse_sched(scheduled_at)
    if t is None:
        return None
    return (t - now).total_seconds() / 60.0


def _build_sharp_index_mlb(cache_path: Path) -> dict:
    """Lean PropLine fetch for MLB mispricing dashboard."""
    if not propline_configured():
        return {}
    pl = PropLineClient(cache_path=cache_path / "propline", ttl_seconds=300, timeout=90)
    events = pl.fetch_bulk_odds(
        SPORT_MAP["MLB"],
        markets="pitcher_strikeouts,batter_hits,batter_total_bases,batter_home_runs,batter_rbis",
        bookmakers="pinnacle,draftkings,fanduel,betmgm,sleeper",
    )
    index: dict = {}
    for ev in events:
        for p in parse_prop_outcomes_to_index_rows(ev, for_true_prob=True, sport_id="MLB"):
            key = f"{normalize_name(p['player'])}|{p['stat']}"
            new_pri = BOOK_PRIORITY.get(p.get("book_key", ""), 0)
            old = index.get(key)
            if old is not None:
                old_book = (old.get("source") or "").replace("propline-", "")
                if new_pri < BOOK_PRIORITY.get(old_book, 0):
                    continue
            index[key] = {
                "over_decimal": p["over_decimal"],
                "under_decimal": p["under_decimal"],
                "bookmaker": p["bookmaker"],
                "line_value": p["line"],
                "source": p.get("source", "propline"),
            }
    return index


def collect_mispriced(
    *,
    min_mispricing_pp: float = 1.5,
    cache_path: Path = Path("data"),
) -> tuple[list[RankedLeg], list[tuple[float, RankedLeg]], dict]:
    """Return (all_mispriced, urgent[(mins, leg)], meta)."""
    now = datetime.now(timezone.utc)
    ud = UDClient(cache_path=cache_path / "ud_lines_cache.json")
    legs = ud.parse_legs(ud.fetch(force=True), sport_filter={"MLB"})

    injury_index = None
    try:
        injury_index = ESPNInjuryClient(
            cache_path=cache_path / "injury_cache", ttl_seconds=1800
        ).fetch_all_sports()
    except Exception:
        pass

    sharp = _build_sharp_index_mlb(cache_path / "sharp_cache")
    entry = UD_PAYOUTS["6-flex"]
    ranked = rank_legs(
        legs,
        break_even=entry.break_even,
        min_true_prob=0.52,
        min_edge_pp=0.0,
        injury_index=injury_index,
        sharp_book_index=sharp,
        full_game_only=True,
    )
    mispriced = [
        r for r in ranked
        if r.sharp_true_prob is not None
        and (r.mispricing_edge_pp or 0) >= min_mispricing_pp
        and r.sharp_true_prob >= 0.52
    ]
    urgent: list[tuple[float, RankedLeg]] = []
    for r in mispriced:
        mins = _mins_until(r.leg.scheduled_at, now)
        if mins is not None and -45 <= mins <= 240:
            urgent.append((mins, r))
    urgent.sort(key=lambda x: (-(x[1].mispricing_edge_pp or 0), x[0]))
    meta = {
        "now": now,
        "n_legs": len(legs),
        "n_sharp": len(sharp),
        "n_ranked": len(ranked),
        "n_mispriced": len(mispriced),
        "n_urgent": len(urgent),
        "injury_index": injury_index,
    }
    return mispriced, urgent, meta


def _pick_block(r: RankedLeg, mins: Optional[float], rank: int) -> str:
    leg = r.leg
    side_label = _fmt_side(leg, r.picked_side)
    delta = r.mispricing_edge_pp or 0
    delta_cls = "pos" if delta >= 0 else "neg"
    urgent = ""
    tips = "—"
    if mins is not None:
        tips = f"{mins:.0f}m"
        if -45 <= mins <= 240:
            urgent = '<span class="urgent-tag">last-minute</span>'
    return f"""
<article class="pick">
  <div class="rank">{rank:02d}</div>
  <div class="who">
    <p class="name">{html.escape(leg.player_name)}{urgent}</p>
    <p class="pick-line">{html.escape(side_label)}</p>
    <p class="match">{html.escape(leg.match_title or '—')} · tips in {html.escape(tips)}</p>
  </div>
  <div class="stats">
    <div><span>UD</span><div class="v">{r.picked_true_prob*100:.1f}%</div></div>
    <div><span>Sharp</span><div class="v">{(r.sharp_true_prob or 0)*100:.1f}%</div></div>
    <div><span>Edge</span><div class="v delta {delta_cls}">{delta:+.1f}pp</div></div>
    <div><span>Signal</span><div class="v">{html.escape(r.sharp_book or '?')}</div></div>
  </div>
  <div class="app-cta">
    <div>
      <div class="app-name">PLACE ON → UNDERDOG FANTASY</div>
      <div class="why">Signal from {html.escape(r.sharp_book or 'sharp')} · soft price is on Underdog</div>
    </div>
    <a href="https://underdogfantasy.com" target="_blank" rel="noopener">Open Underdog ↗</a>
  </div>
</article>
"""


def render_dashboard_html(
    mispriced: list[RankedLeg],
    urgent: list[tuple[float, RankedLeg]],
    meta: dict,
) -> str:
    now: datetime = meta["now"]
    entry = UD_PAYOUTS["6-flex"]

    urgent_blocks = []
    if urgent:
        for i, (mins, r) in enumerate(urgent[:12], 1):
            urgent_blocks.append(_pick_block(r, mins, i))
    else:
        urgent_blocks.append('<p class="empty">No last-minute misprices in the next 4 hours.</p>')

    all_blocks = []
    for i, r in enumerate(mispriced[:20], 1):
        mins = _mins_until(r.leg.scheduled_at, now)
        all_blocks.append(_pick_block(r, mins, i))
    if not all_blocks:
        all_blocks.append('<p class="empty">No mispriced legs cleared the ≥1.5pp same-side gate.</p>')

    # Correlation on last-minute stack (if 2+ legs)
    corr_html = ""
    try:
        from ud_edge.correlation import analyze_and_format
        import html as _html
        if len(urgent) >= 2:
            urgent_legs = [r for _, r in urgent[:6]]
            corr_txt = analyze_and_format(urgent_legs, entry_type="6-flex")
            corr_html = f"""
<section>
  <div class="sec-head">
    <h3>Correlation · last-minute stack</h3>
    <span>possible outcomes if legs share a script</span>
  </div>
  <div class="corr">{_html.escape(corr_txt)}</div>
</section>
"""
    except Exception as e:
        corr_html = f'<section><p class="empty">Correlation skipped: {e}</p></section>'

    # Diversified lineup from mispriced pool
    lineup_html = ""
    lineups = build_lineups(
        mispriced, n_entries=1, n_legs=6, min_floor_prob=0.53, diversify=True
    )
    if lineups:
        lineup = lineups[0]
        probs = [effective_true_prob(r.picked_true_prob, r.sharp_true_prob) for r in lineup]
        avg = sum(probs) / len(probs)
        ev, win, _ = expected_value(entry, avg)
        rows = []
        for j, r in enumerate(lineup, 1):
            mins = _mins_until(r.leg.scheduled_at, now)
            rows.append(_pick_block(r, mins, j))
        try:
            from ud_edge.correlation import analyze_and_format
            import html as _html
            corr_lineup = _html.escape(analyze_and_format(lineup, entry_type="6-flex"))
            corr_block = f'<div class="corr">{corr_lineup}</div>'
        except Exception:
            corr_block = ""
        lineup_html = f"""
<section>
  <div class="sec-head">
    <h3>Suggested 6-flex</h3>
    <span>avg {avg:.1%} · EV {ev:+.3f}/$1 · win ~{win:.0%}</span>
  </div>
  <div class="lineup">
    <p class="ev">Build this card in <strong style="color:var(--app)">Underdog Fantasy</strong> — check correlation before submitting.</p>
    {''.join(rows)}
    {corr_block}
  </div>
</section>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>UD EDGE — MLB Mispricing</title>
  <style>{DASHBOARD_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="brand">UD EDGE</div>
      <h1>MLB mispriced props — place every pick on Underdog.</h1>
      <p>Sharp books set the fair probability. When Underdog is soft on the same side, that is the +EV ticket.</p>
      <div class="meta">
        <span>as of <strong>{now.strftime('%Y-%m-%d %H:%M UTC')}</strong></span>
        <span>UD legs <strong>{meta['n_legs']}</strong></span>
        <span>sharp lines <strong>{meta['n_sharp']}</strong></span>
        <span>mispriced <strong>{meta['n_mispriced']}</strong></span>
        <span>last-minute <strong>{meta['n_urgent']}</strong></span>
      </div>
    </header>

    <div class="place-rule">
      <div class="label">App to use</div>
      <h2>Underdog Fantasy — every time</h2>
      <p>
        Mispricing means Underdog’s price is soft vs Pinnacle / DraftKings / FanDuel / BetMGM.
        Open <strong>Underdog</strong>, enter the Higher/Lower shown below, and ignore the sharp book’s sportsbook slip —
        those apps are the <em>signal</em>, not where this edge is bet.
      </p>
    </div>

    <section>
      <div class="sec-head">
        <h3>Last-minute board</h3>
        <span>tips within 4 hours · same-side sharp ≥ +1.5pp</span>
      </div>
      {''.join(urgent_blocks)}
    </section>

    {corr_html}

    {lineup_html}

    <section>
      <div class="sec-head">
        <h3>Full mispriced slate</h3>
        <span>sorted by same-side edge</span>
      </div>
      {''.join(all_blocks)}
    </section>

    <footer>
      Decision-support only. Calibrate after 50+ settled legs.
      Same-game stacks (e.g. multiple LAD bats) are correlated — don’t treat EV as independent.
    </footer>
  </div>
</body>
</html>
"""


def write_dashboard(
    out_path: Path = Path("reports/dashboard.html"),
    min_mispricing_pp: float = 1.5,
) -> Path:
    mispriced, urgent, meta = collect_mispriced(min_mispricing_pp=min_mispricing_pp)
    html_doc = render_dashboard_html(mispriced, urgent, meta)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path
