"""
league_shot_pipeline.py
-----------------------
Pulls shot data for all 32 NHL teams for the 2025/26 season.
Outputs: nhl_shots_20252026.json

Structure:
{
  "season": "20252026",
  "generated": "YYYY-MM-DD",
  "league_summary": { per-team summary rows for the main page table },
  "teams": {
    "UTA": { "name": "...", "game_summaries": [...], "shots": [...] },
    ...
  }
}

Runtime: ~5-6 hours. Saves a checkpoint every 4 teams so progress
is not lost if interrupted. Resume by running again — already-completed
teams are skipped automatically.

Usage:
    python league_shot_pipeline.py
    python league_shot_pipeline.py --teams UTA COL DAL   # specific teams only
"""

import sys, time, json, math, requests, argparse
from datetime import date
from pathlib import Path
sys.path.insert(0, './src')

from nhl_client import get_team_schedule
from schedule_analysis import build_schedule_features

SEASON      = '20252026'
OUTPUT      = 'nhl_shots_20252026.json'
CHECKPOINT  = 'nhl_shots_checkpoint.json'
DELAY       = 0.4
NHL_API     = 'https://api-web.nhle.com/v1'

SHOT_EVENTS = {'shot-on-goal', 'missed-shot', 'blocked-shot', 'goal'}

ALL_TEAMS = [
    ("ANA","Anaheim Ducks"),    ("BOS","Boston Bruins"),
    ("BUF","Buffalo Sabres"),   ("CAR","Carolina Hurricanes"),
    ("CBJ","Columbus Blue Jackets"), ("CGY","Calgary Flames"),
    ("CHI","Chicago Blackhawks"),("COL","Colorado Avalanche"),
    ("DAL","Dallas Stars"),     ("DET","Detroit Red Wings"),
    ("EDM","Edmonton Oilers"),  ("FLA","Florida Panthers"),
    ("LAK","Los Angeles Kings"),("MIN","Minnesota Wild"),
    ("MTL","Montreal Canadiens"),("NJD","New Jersey Devils"),
    ("NSH","Nashville Predators"),("NYI","New York Islanders"),
    ("NYR","New York Rangers"), ("OTT","Ottawa Senators"),
    ("PHI","Philadelphia Flyers"),("PIT","Pittsburgh Penguins"),
    ("SEA","Seattle Kraken"),   ("SJS","San Jose Sharks"),
    ("STL","St. Louis Blues"),  ("TBL","Tampa Bay Lightning"),
    ("TOR","Toronto Maple Leafs"),("UTA","Utah Mammoth"),
    ("VAN","Vancouver Canucks"),("VGK","Vegas Golden Knights"),
    ("WSH","Washington Capitals"),("WPG","Winnipeg Jets"),
]

# ── XG MODEL ──────────────────────────────────────────────────────────────
def calc_xg(distance, shot_type='', event_type=''):
    """
    Calibrated from 2025/26 actual shooting percentages by distance:
      0-10ft: 20.0%  |  10-20ft: 18.2%  |  20-30ft: 14.5%
      30-40ft: 8.9%  |  40-55ft:  5.8%  |  55ft+:    2.9%
    Blocked and missed shots get xG=0 — only SOG and goals carry xG.
    """
    if event_type in ('blocked-shot', 'missed-shot'):
        return 0.0
    if distance is None:
        return 0.115
    if   distance <= 10: base = 0.200
    elif distance <= 20: base = 0.182
    elif distance <= 30: base = 0.145
    elif distance <= 40: base = 0.089
    elif distance <= 55: base = 0.058
    else:                base = 0.029
    mult = {'deflected':1.4,'tip-in':1.35,'wrap-around':0.9,
            'backhand':0.85,'snap':1.05,'slap':0.9,'wrist':1.0
           }.get((shot_type or '').lower(), 1.0)
    return min(round(base * mult, 4), 0.95)


# ── SITUATION ─────────────────────────────────────────────────────────────
def parse_situation(code, is_home_shot):
    if not code or len(code) != 4: return 'unknown'
    away_s, home_s = int(code[1]), int(code[2])
    for_s  = home_s if is_home_shot else away_s
    agst_s = away_s if is_home_shot else home_s
    if for_s == agst_s == 5: return '5v5'
    if for_s > agst_s:       return 'pp'
    if for_s < agst_s:       return 'pk'
    return 'other'


# ── API ────────────────────────────────────────────────────────────────────
def get_pbp(game_id):
    r = requests.get(f'{NHL_API}/gamecenter/{game_id}/play-by-play', timeout=15)
    r.raise_for_status()
    return r.json()

def get_boxscore(game_id):
    r = requests.get(f'{NHL_API}/gamecenter/{game_id}/boxscore', timeout=15)
    r.raise_for_status()
    return r.json()

def build_player_map(boxscore_data):
    players = {}
    pgs = boxscore_data.get('playerByGameStats', {})
    for side in ['homeTeam', 'awayTeam']:
        team_abbr = boxscore_data.get(side, {}).get('abbrev', '')
        for group in ['forwards', 'defense', 'goalies']:
            for p in pgs.get(side, {}).get(group, []):
                pid = p.get('playerId') or p.get('id')
                if pid:
                    name_field = p.get('name', {})
                    name = name_field.get('default', '') if isinstance(name_field, dict) else str(name_field)
                    players[pid] = {
                        'name':     name,
                        'position': p.get('position', ''),
                        'team':     team_abbr,
                    }
    return players


# ── SHOT EXTRACTOR ─────────────────────────────────────────────────────────
def extract_shots(pbp_data, game_meta, player_map, home_team):
    shots = []
    for play in pbp_data.get('plays', []):
        etype = play.get('typeDescKey', '')
        if etype not in SHOT_EVENTS:
            continue
        details     = play.get('details', {})
        period_desc = play.get('periodDescriptor', {})
        shooter_id  = (details.get('shootingPlayerId') or
                       details.get('scoringPlayerId'))
        if etype == 'blocked-shot':
            shooter_id = details.get('shootingPlayerId')

        player_info   = player_map.get(shooter_id, {})
        shooting_team = player_info.get('team', '')
        if not shooting_team:
            continue

        is_home_shot = shooting_team == home_team
        x_raw = details.get('xCoord')
        y_raw = details.get('yCoord')
        period = period_desc.get('number', 1)

        x, y = x_raw, y_raw
        if x is not None:
            shooting_right = (is_home_shot and period % 2 == 1) or \
                             (not is_home_shot and period % 2 == 0)
            if not shooting_right:
                x = -x
                y = -y if y is not None else None

        distance = None
        if x_raw is not None and y_raw is not None:
            distance = round(math.sqrt((89 - abs(x_raw))**2 + y_raw**2), 1)

        if x is None:       zone = 'unknown'
        elif x >= 69:       zone = 'crease'
        elif x >= 45:       zone = 'slot'
        elif x >= 25:       zone = 'mid'
        else:               zone = 'perimeter'

        xg = calc_xg(distance, details.get('shotType',''), etype)

        shots.append({
            'game_id':       game_meta['game_id'],
            'game_date':     game_meta['date'],
            'home_team':     game_meta['home_team'],
            'away_team':     game_meta['away_team'],
            'shooting_team': shooting_team,
            'is_home_shot':  is_home_shot,
            'period':        period,
            'period_type':   period_desc.get('periodType', 'REG'),
            'time':          play.get('timeInPeriod'),
            'time_elapsed':  _time_to_sec(play.get('timeInPeriod'), period),
            'event_type':    etype,
            'is_goal':       etype == 'goal',
            'is_on_goal':    etype in ('shot-on-goal', 'goal'),
            'x':             x,
            'y':             y,
            'distance':      distance,
            'zone':          zone,
            'shot_type':     details.get('shotType', 'unknown'),
            'situation':     parse_situation(play.get('situationCode',''), is_home_shot),
            'shooter_id':    shooter_id,
            'shooter_name':  player_info.get('name', 'Unknown'),
            'shooter_pos':   player_info.get('position', ''),
            'xg':            xg,
        })
    return shots


def _time_to_sec(t, period):
    try:
        m, s = map(int, t.split(':'))
        return (min(period, 4) - 1) * 1200 + m * 60 + s
    except:
        return 0


# ── GAME SUMMARY ───────────────────────────────────────────────────────────
def build_game_summary(game_meta, shots, boxscore_data, team):
    is_home   = game_meta['home_team'] == team
    opponent  = game_meta['away_team'] if is_home else game_meta['home_team']
    for_shots = [s for s in shots if s['shooting_team'] == team]
    agst_shots= [s for s in shots if s['shooting_team'] != team]

    home_score = boxscore_data.get('homeTeam', {}).get('score')
    away_score = boxscore_data.get('awayTeam', {}).get('score')
    team_score = home_score if is_home else away_score
    opp_score  = away_score if is_home else home_score

    xgf = round(sum(s['xg'] for s in for_shots), 2)
    xga = round(sum(s['xg'] for s in agst_shots), 2)
    team_won = (team_score or 0) > (opp_score or 0)

    return {
        'game_id':      game_meta['game_id'],
        'date':         game_meta['date'],
        'is_home':      is_home,
        'opponent':     opponent,
        'team_score':   team_score,
        'opp_score':    opp_score,
        'team_won':     team_won,
        'xgf':          xgf,
        'xga':          xga,
        'xg_diff':      round(xgf - xga, 2),
        'xg_won':       xgf > xga,
        'outperformed': team_won and xgf <= xga,
        'underperformed': not team_won and xgf > xga,
        'team_sog':     sum(1 for s in for_shots if s['is_on_goal']),
        'opp_sog':      sum(1 for s in agst_shots if s['is_on_goal']),
        'team_sh_pct':  round(sum(1 for s in for_shots if s['is_goal']) /
                              max(sum(1 for s in for_shots if s['is_on_goal']),1) * 100, 1),
        'opp_sh_pct':   round(sum(1 for s in agst_shots if s['is_goal']) /
                              max(sum(1 for s in agst_shots if s['is_on_goal']),1) * 100, 1),
    }


# ── TEAM PIPELINE ──────────────────────────────────────────────────────────
def process_team(abbr, name):
    print(f'\n  Fetching schedule…')
    raw  = get_team_schedule(abbr, SEASON)
    sched = build_schedule_features(raw, abbr)
    sched = sched[
        (sched['game_type'] == 2) &
        (sched['game_state'].isin(['FINAL','OFF']))
    ].reset_index(drop=True)
    print(f'  {len(sched)} games')

    all_shots, summaries, failed = [], [], []

    for i, (_, game) in enumerate(sched.iterrows()):
        gid      = int(game['game_id'])
        opp      = game['away_team'] if game['is_home'] else game['home_team']
        date_str = str(game['date'].date())
        print(f'  [{i+1}/{len(sched)}] {date_str} vs {opp}', end=' … ')

        try:
            boxscore   = get_boxscore(gid);  time.sleep(DELAY)
            pbp        = get_pbp(gid);       time.sleep(DELAY)
            player_map = build_player_map(boxscore)

            game_meta = {
                'game_id':   gid,
                'date':      date_str,
                'home_team': str(game['home_team']),
                'away_team': str(game['away_team']),
            }

            shots    = extract_shots(pbp, game_meta, player_map, str(game['home_team']))
            summary  = build_game_summary(game_meta, shots, boxscore, abbr)
            all_shots.extend(shots)
            summaries.append(summary)

            print(f"{summary['team_score']}-{summary['opp_score']} | "
                  f"xGF {summary['xgf']} xGA {summary['xga']}")

        except Exception as e:
            print(f'ERROR: {e}')
            failed.append(gid)

    wins  = sum(1 for g in summaries if g['team_won'])
    xgf_t = round(sum(g['xgf'] for g in summaries), 1)
    xga_t = round(sum(g['xga'] for g in summaries), 1)
    gf    = sum(g['team_score'] or 0 for g in summaries)
    ga    = sum(g['opp_score']  or 0 for g in summaries)

    league_row = {
        'abbr':          abbr,
        'name':          name,
        'games':         len(summaries),
        'wins':          wins,
        'losses':        len(summaries) - wins,
        'gf':            gf,
        'ga':            ga,
        'goal_diff':     gf - ga,
        'xgf':           xgf_t,
        'xga':           xga_t,
        'xg_diff':       round(xgf_t - xga_t, 1),
        'xg_wins':       sum(1 for g in summaries if g['xg_won']),
        'outperformed':  sum(1 for g in summaries if g['outperformed']),
        'underperformed':sum(1 for g in summaries if g['underperformed']),
        'avg_xgf':       round(xgf_t / max(len(summaries),1), 2),
        'avg_xga':       round(xga_t / max(len(summaries),1), 2),
        'sh_pct':        round(gf / max(sum(g['team_sog'] for g in summaries),1) * 100, 1),
        'sv_pct':        round(1 - ga / max(sum(g['opp_sog'] for g in summaries),1), 3),
    }

    print(f'  ✓ {abbr}: {wins}W  xGF {xgf_t}  xGA {xga_t}  GF {gf}  GA {ga}  failed={len(failed)}')

    return {
        'name':           name,
        'games':          len(summaries),
        'game_summaries': summaries,
        'shots':          all_shots,
        'league_row':     league_row,
        'failed_games':   failed,
    }


# ── CHECKPOINT HELPERS ─────────────────────────────────────────────────────
def load_checkpoint():
    if Path(CHECKPOINT).exists():
        with open(CHECKPOINT) as f:
            return json.load(f)
    return {'teams': {}, 'league_summary': []}

def save_checkpoint(data):
    with open(CHECKPOINT, 'w') as f:
        json.dump(data, f)


# ── MAIN ───────────────────────────────────────────────────────────────────
def run(target_teams=None):
    teams = [(a,n) for a,n in ALL_TEAMS if not target_teams or a in target_teams]
    print(f'NHL Shot Pipeline — {SEASON}')
    print(f'Teams to process: {len(teams)}')
    print(f'Estimated runtime: ~{len(teams)*10} minutes\n')
    print('=' * 60)

    ckpt = load_checkpoint()
    completed = set(ckpt['teams'].keys())
    if completed:
        print(f'Resuming — {len(completed)} teams already done: {", ".join(sorted(completed))}\n')

    for i, (abbr, name) in enumerate(teams):
        if abbr in completed:
            print(f'[{i+1}/{len(teams)}] {abbr} — already complete, skipping')
            continue

        print(f'\n[{i+1}/{len(teams)}] {abbr} — {name}')
        print('-' * 40)

        try:
            result = process_team(abbr, name)
            ckpt['teams'][abbr] = result
            ckpt['league_summary'].append(result['league_row'])

            # Save checkpoint every 4 teams
            if (i + 1) % 4 == 0 or i == len(teams) - 1:
                save_checkpoint(ckpt)
                print(f'  [checkpoint saved — {len(ckpt["teams"])} teams complete]')

        except Exception as e:
            print(f'  TEAM FAILED: {e}')
            continue

    # Write final output
    print(f'\n{"="*60}')
    print('Writing final output…')

    # Strip shots from teams dict for the team-level data
    # Keep shots separate at top level indexed by team
    output = {
        'season':         SEASON,
        'generated':      str(date.today()),
        'league_summary': sorted(ckpt['league_summary'], key=lambda x: -x['xg_diff']),
        'teams': {
            abbr: {
                'name':           data['name'],
                'games':          data['games'],
                'game_summaries': data['game_summaries'],
                'shots':          data['shots'],
            }
            for abbr, data in ckpt['teams'].items()
        }
    }

    with open(OUTPUT, 'w') as f:
        json.dump(output, f)

    total_shots = sum(len(d['shots']) for d in ckpt['teams'].values())
    print(f'Saved {OUTPUT}')
    print(f'Teams: {len(ckpt["teams"])}')
    print(f'Total shots: {total_shots:,}')
    print(f'\nLeague xG standings (top 10):')
    for row in output['league_summary'][:10]:
        print(f"  {row['abbr']:>3}  {row['wins']:>2}W  xGF {row['xgf']:>6}  xGA {row['xga']:>6}  diff {row['xg_diff']:>+6}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--teams', nargs='+', help='Specific team abbreviations to process')
    args = parser.parse_args()
    run(target_teams=args.teams)