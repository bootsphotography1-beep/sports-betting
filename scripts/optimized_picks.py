"""Optimized pick strategy — focus on stable, full-game edges."""
import sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, r'C:\Users\fin49\projects\ud-edge-bot')
from ud_edge.ud_client import UDClient
from ud_edge.matcher import rank_legs, build_lineups
from ud_edge.flex_math import UD_PAYOUTS, expected_value
from ud_edge.deliver import build_multi_report
from datetime import datetime, timezone

# Filter rules — drop low-quality legs to surface higher-conviction picks
EXCLUDE_STATS = {
    # Mid-game tennis period 1 props (resolve mid-match, noisy)
    'period_1_games_won', 'period_1_games_played',
    # MLB half-inning props (resolve mid-game)
    'period_1_strikeouts', 'period_1_batters_faced',
    'period_1_hits_allowed', 'period_1_total_runs_allowed',
    'period_1_batters_faced',
    # Period-1 soccer (first half props — resolve quickly, less data)
    'period_1_2_goals', 'period_1_2_assists',
    'period_1_2_shots_on_target', 'period_1_2_shots_attempted',
    'period_1_2_goals_assists', 'period_1_2_saves', 'period_1_goals',
    'period_1_2_first_goal_scorer', 'period_1_2_last_goalscorer',
    # Trivial half-line props (already excluded earlier, but for safety)
    'runs', 'rbis', 'hits', 'walks', 'home_runs', 'stolen_bases',
}
EXCLUDE_SPORTS = {'CS', 'LOL', 'DOTA', 'VAL', 'ESPORTS', 'RACING', 'CFL'}

c = UDClient(cache_path=Path(r'C:\Users\fin49\projects\ud-edge-bot\data\ud_lines_cache.json'))
data = c.fetch(force=False)
legs = c.parse_legs(data, sport_filter=None)

# Filter: drop excluded sports and stats
filtered = [
    l for l in legs
    if l.sport_id not in EXCLUDE_SPORTS
    and l.stat_name not in EXCLUDE_STATS
    and l.line_value > 0.5  # drop 0.0 lines (excluded by matcher but double-check)
    and not (l.line_value == 0.5 and l.stat_name in EXCLUDE_STATS)
]
print(f'Raw: {len(legs)} → Filtered: {len(filtered)} (dropped {len(legs)-len(filtered)} low-quality)')

# Rank with stricter thresholds — focus on real edge, not just "favorite"
ranked = rank_legs(filtered, break_even=0.524, min_true_prob=0.55, min_edge_pp=0.5)
print(f'+EV pool: {len(ranked)}')

sport_counts = Counter(r.leg.sport_id for r in ranked)
print('\n+EV by sport:')
for s, n in sport_counts.most_common():
    print(f'  {s:<10} {n}')

# Build 4 lineups
lineups = build_lineups(ranked, n_entries=4, n_legs=6)
entry = UD_PAYOUTS['6-flex']

print('\n' + '=' * 80)
print('OPTIMIZED 4 ENTRIES (full-game props only, mainstream sports)')
print('=' * 80)
print(f'{"Entry":<7} {"Avg%":>6} {"Floor":>6} {"EV/$1":>8} {"Win%":>6}  Verdict')
print('-' * 80)
for i, lineup in enumerate(lineups, 1):
    probs = [r.picked_true_prob for r in lineup]
    avg = sum(probs)/len(probs)
    floor = min(probs)
    ev, win, med = expected_value(entry, avg)
    rec = 'STRONG' if ev > 0.10 else ('PLAY' if ev > 0.03 else ('SMALL' if ev > 0 else 'SKIP'))
    print(f'#{i:<6} {avg:>6.2%} {floor:>6.2%} {ev:>+8.4f} {win:>6.1%}  {rec}')

print('\n' + '=' * 80)
for i, lineup in enumerate(lineups, 1):
    print(f'\n--- Entry #{i} ---')
    for j, r in enumerate(lineup, 1):
        leg = r.leg
        arrow = '↑' if r.picked_side == 'higher' else '↓'
        side = 'Over' if r.picked_side == 'higher' else 'Under'
        print(f'  {j}. {leg.sport_id:<6} {leg.player_name[:24]:<24} '
              f'{side} {leg.line_value:g} {leg.stat_name:<35} {arrow}  '
              f'{r.picked_true_prob:.1%}')

md = build_multi_report(lineups, entry_type='6-flex', n_legs=6,
                        min_true_prob=0.55, fetched_at=datetime.now(timezone.utc))
out = Path(r'C:\Users\fin49\projects\ud-edge-bot\reports\2026-07-18_6flex_multi_v3.md')
out.write_text(md)
print(f'\nSaved: {out}')