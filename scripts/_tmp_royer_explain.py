import os, sys, glob
import pandas as pd
sys.path.append(os.getcwd())
from scripts.ml_model import TennisMLModel
from scripts.stats_engine import TennisStatsEngine
from scripts.scraper_profiles import ProfileScraper
from scripts.value_detector import ValueDetector

files = sorted(glob.glob('data/scraped/*.csv'), key=os.path.getmtime, reverse=True)
df = pd.read_csv(files[0])
# try find Royer match row
cand = df[df['player1'].astype(str).str.contains('Royer', case=False, na=False) | df['player2'].astype(str).str.contains('Royer', case=False, na=False)]
print('latest_csv', files[0], 'rows_with_royer', len(cand))
if cand.empty:
    raise SystemExit('No Royer row found in latest prematch file')
row = cand.iloc[0]

p1=row['player1']; p2=row['player2']
odd1=float(row['odd_p1']) if pd.notna(row['odd_p1']) else None
odd2=float(row['odd_p2']) if pd.notna(row['odd_p2']) else None
surface='Clay' if 'rome' in str(row.get('tournament','')).lower() else 'Hard'

ps=ProfileScraper(); se=TennisStatsEngine(); ml=TennisMLModel(); ml._load_bundle_if_needed()

def pts_from_rank(rank):
    if rank <= 10: return 4000
    if rank <= 50: return 1200
    if rank <= 100: return 600
    if rank <= 200: return 300
    if rank <= 300: return 175
    if rank <= 500: return 80
    return 20

p1_id=se.get_player_id(p1); p2_id=se.get_player_id(p2)
p1_stats=se.get_player_stats(p1_id) if p1_id else {'rank':100,'age':25,'ht':185,'pts':1000,'hand':'U'}
p2_stats=se.get_player_stats(p2_id) if p2_id else {'rank':100,'age':25,'ht':185,'pts':1000,'hand':'U'}

p1_prof = ps.scrape_profile(str(row.get('p1_url','')).strip()) if pd.notna(row.get('p1_url')) else None
p2_prof = ps.scrape_profile(str(row.get('p2_url','')).strip()) if pd.notna(row.get('p2_url')) else None
for stats, prof in [(p1_stats,p1_prof),(p2_stats,p2_prof)]:
    if prof:
        if prof.get('rank') not in (None,100):
            stats['rank']=prof['rank']; stats['pts']=pts_from_rank(prof['rank'])
        if prof.get('age') not in (None,25): stats['age']=prof['age']
        if prof.get('hand') not in (None,'U'): stats['hand']=prof['hand']

h2h=se.get_h2h(p1_id,p2_id)
pred=ml.predict_match(
    surface=surface,
    p1_name=p1,p2_name=p2,
    p1_rank=p1_stats['rank'],p2_rank=p2_stats['rank'],
    p1_age=p1_stats['age'],p2_age=p2_stats['age'],
    p1_ht=p1_stats['ht'],p2_ht=p2_stats['ht'],
    p1_pts=p1_stats['pts'],p2_pts=p2_stats['pts'],
    p1_id=p1_id,p2_id=p2_id,
    p1_form_win_pct_90=(p1_prof or {}).get('win_pct',50),
    p2_form_win_pct_90=(p2_prof or {}).get('win_pct',50),
    p1_fatigue_minutes_14=(p1_prof or {}).get('fatigue_minutes',0),
    p2_fatigue_minutes_14=(p2_prof or {}).get('fatigue_minutes',0),
    p1_fatigue_matches_14=(p1_prof or {}).get('fatigue_matches',0),
    p2_fatigue_matches_14=(p2_prof or {}).get('fatigue_matches',0),
    p1_hand=p1_stats.get('hand','U'),p2_hand=p2_stats.get('hand','U'),
    h2h_p1_wins=h2h.get('p1_wins',0),h2h_p2_wins=h2h.get('p2_wins',0),
    tournament_name=row.get('tournament','')
)

print('MATCH', p1, 'vs', p2, '| tournament', row.get('tournament'), '| time', row.get('time'))
print('BOOK', odd1, odd2)
print('PRED', pred['p1_win_prob'], pred['p2_win_prob'], 'TRUE_ODDS', pred['p1_true_odd'], pred['p2_true_odd'])
print('CONF', pred.get('confidence'), 'CAL', pred.get('calibration_used'))
print('SNAP', pred.get('feature_snapshot'))
print('P1_STATS', p1_stats, 'P2_STATS', p2_stats)
print('P1_PROF', {k:(p1_prof or {}).get(k) for k in ['rank','win_pct','fatigue_minutes','fatigue_matches','form_matches']})
print('P2_PROF', {k:(p2_prof or {}).get(k) for k in ['rank','win_pct','fatigue_minutes','fatigue_matches','form_matches']})
print('H2H', h2h)
