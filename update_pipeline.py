"""
update_pipeline.py
------------------
Incremental updater for nhl_shots_{season}.json.
Only pulls games played since the last update — skips completed games.

Usage:
    python update_pipeline.py                    # update current season
    python update_pipeline.py --season 20262027  # specific season
    python update_pipeline.py --teams UTA BOS    # specific teams only
    python update_pipeline.py --force            # re-pull all games (full rebuild)

Designed to run daily via cron or scheduler.py.
"""

import sys, time, json, math, requests, argparse
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, './src')

from nhl_client import get_team_schedule
from schedule_analysis import build_schedule_features

DELAY   = 0.4
NHL_API = 'https://api-web.nhle.com/v1'

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


# ── AUTO-DETECT CURRENT SEASON ────────────────────────────────────────────
def get_current_season():
    """Ask the NHL API what the current season is."""
    try:
        r = requests.get(f'{NHL_API}/season', timeout=10)
        data = r.json()
        # Returns list of seasons, last one is current
        seasons = data if isinstance(data, list) else data.get('data', [])
        if seasons:
            current = seasons[-1]
            sid = str(current.get('id') or current.get('seasonId') or '')
            if len(sid) == 8:
                return sid
    except:
        pass
    # Fallback: derive from today's date
    today = date.today()
    if today.month >= 9:
        return f'{today.year}{today.year+1}'
    else:
        return f'{today.year-1}{today.year}'


# ── XG MODEL ──────────────────────────────────────────────────────────────
def calc_xg(distance, shot_type='', event_type=''):
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
        'game_id':       game_meta['game_id'],
        'date':          game_meta['date'],
        'is_home':       is_home,
        'opponent':      opponent,
        'team_score':    team_score,
        'opp_score':     opp_score,
        'team_won':      team_won,
        'xgf':           xgf,
        'xga':           xga,
        'xg_diff':       round(xgf - xga, 2),
        'xg_won':        xgf > xga,
        'outperformed':  team_won and xgf <= xga,
        'underperformed':not team_won and xgf > xga,
        'team_sog':      sum(1 for s in for_shots if s['is_on_goal']),
        'opp_sog':       sum(1 for s in agst_shots if s['is_on_goal']),
        'team_sh_pct':   round(sum(1 for s in for_shots if s['is_goal']) /
                               max(sum(1 for s in for_shots if s['is_on_goal']),1) * 100, 1),
        'opp_sh_pct':    round(sum(1 for s in agst_shots if s['is_goal']) /
                               max(sum(1 for s in agst_shots if s['is_on_goal']),1) * 100, 1),
    }


# ── LEAGUE ROW ─────────────────────────────────────────────────────────────
def build_league_row(abbr, name, summaries):
    wins   = sum(1 for g in summaries if g['team_won'])
    xgf_t  = round(sum(g['xgf'] for g in summaries), 1)
    xga_t  = round(sum(g['xga'] for g in summaries), 1)
    gf     = sum(g['team_score'] or 0 for g in summaries)
    ga     = sum(g['opp_score']  or 0 for g in summaries)
    sog_f  = sum(g['team_sog'] for g in summaries)
    sog_a  = sum(g['opp_sog']  for g in summaries)
    return {
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
        'sh_pct':        round(gf / max(sog_f,1) * 100, 1),
        'sv_pct':        round(1 - ga / max(sog_a,1), 3),
    }


# ── PROCESS TEAM (incremental) ─────────────────────────────────────────────
def process_team_update(abbr, name, season, existing_game_ids, force=False):
    """
    Pull only new games for a team.
    existing_game_ids: set of game_ids already in the JSON.
    Returns (new_shots, new_summaries, failed_ids)
    """
    raw   = get_team_schedule(abbr, season)
    sched = build_schedule_features(raw, abbr)
    sched = sched[
        (sched['game_type'] == 2) &
        (sched['game_state'].isin(['FINAL','OFF']))
    ].reset_index(drop=True)

    # Filter to only new games unless force rebuild
    if not force:
        sched = sched[~sched['game_id'].astype(int).isin(existing_game_ids)]

    if sched.empty:
        print(f'  {abbr}: no new games')
        return [], [], []

    print(f'  {abbr}: {len(sched)} new games to pull')
    new_shots, new_summaries, failed = [], [], []

    for _, game in sched.iterrows():
        gid      = int(game['game_id'])
        opp      = game['away_team'] if game['is_home'] else game['home_team']
        date_str = str(game['date'].date())
        print(f'    {date_str} vs {opp}', end=' … ')

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

            shots   = extract_shots(pbp, game_meta, player_map, str(game['home_team']))
            summary = build_game_summary(game_meta, shots, boxscore, abbr)
            new_shots.extend(shots)
            new_summaries.append(summary)
            print(f"{summary['team_score']}-{summary['opp_score']} | xGF {summary['xgf']} xGA {summary['xga']}")

        except Exception as e:
            print(f'ERROR: {e}')
            failed.append(gid)

    return new_shots, new_summaries, failed


# ── MAIN ───────────────────────────────────────────────────────────────────
def run(season=None, target_teams=None, force=False):
    if not season:
        season = get_current_season()

    output_file = f'nhl_shots_{season}.json'
    print(f'NHL Shot Update — Season {season}')
    print(f'Output: {output_file}')
    print(f'{"FORCE REBUILD" if force else "Incremental update — new games only"}')
    print('=' * 55)

    # Load existing data — prefer split files, fall back to monolithic
    from pathlib import Path as _Path
    out_dir = _Path('data')
    summary_file = out_dir / f'nhl_league_summary_{season}.json'

    if not force and summary_file.exists():
        # Load from split files
        with open(summary_file) as f:
            summary_data = json.load(f)
        data = {
            'season':         summary_data.get('season', season),
            'generated':      summary_data.get('generated', str(date.today())),
            'last_updated':   summary_data.get('last_updated', ''),
            'league_summary': summary_data.get('league_summary', []),
            'teams':          {}
        }
        # Load each team file
        for team_file in out_dir.glob(f'nhl_shots_*_{season}.json'):
            abbr = team_file.name.replace(f'nhl_shots_','').replace(f'_{season}.json','')
            with open(team_file) as f:
                data['teams'][abbr] = json.load(f)
        print(f'Loaded from split files: {len(data["teams"])} teams\n')
    elif not force and Path(output_file).exists():
        with open(output_file) as f:
            data = json.load(f)
        print(f'Loaded monolithic file: {len(data.get("teams",{}))} teams\n')
    else:
        data = {
            'season':         season,
            'generated':      str(date.today()),
            'last_updated':   str(datetime.now().isoformat()),
            'league_summary': [],
            'teams':          {}
        }
        print('Starting fresh\n')

    teams = [(a,n) for a,n in ALL_TEAMS if not target_teams or a in target_teams]
    total_new = 0

    for abbr, name in teams:
        # Get existing game IDs for this team
        existing = data['teams'].get(abbr, {})
        existing_game_ids = set(g['game_id'] for g in existing.get('game_summaries', []))

        new_shots, new_summaries, failed = process_team_update(
            abbr, name, season, existing_game_ids, force=force
        )

        if not new_shots and not new_summaries:
            continue

        total_new += len(new_summaries)

        # Merge into existing data
        if abbr not in data['teams']:
            data['teams'][abbr] = {
                'name':           name,
                'games':          0,
                'game_summaries': [],
                'shots':          []
            }

        existing_summaries = [] if force else data['teams'][abbr]['game_summaries']
        existing_shots     = [] if force else data['teams'][abbr]['shots']

        merged_summaries = sorted(
            existing_summaries + new_summaries,
            key=lambda g: g['date']
        )
        merged_shots = existing_shots + new_shots

        data['teams'][abbr].update({
            'name':           name,
            'games':          len(merged_summaries),
            'game_summaries': merged_summaries,
            'shots':          merged_shots,
        })

    # Rebuild league summary from all teams
    data['league_summary'] = sorted([
        build_league_row(
            abbr,
            data['teams'][abbr]['name'],
            data['teams'][abbr]['game_summaries']
        )
        for abbr in data['teams']
    ], key=lambda r: -r['xg_diff'])

    data['last_updated'] = datetime.now().isoformat()
    data['generated']    = str(date.today())

    # Write per-team files
    teams_dir = Path('data') / season
    teams_dir.mkdir(parents=True, exist_ok=True)

    for abbr, team_data in data['teams'].items():
        team_file = teams_dir / f'nhl_shots_{abbr}.json'
        team_output = {
            'season':       season,
            'last_updated': data['last_updated'],
            'abbr':         abbr,
            'name':         team_data['name'],
            'games':        team_data['games'],
            'game_summaries': team_data['game_summaries'],
            'shots':        team_data['shots'],
        }
        with open(team_file, 'w') as f:
            json.dump(team_output, f)

    # Write league summary (small file — all teams, no shots)
    summary_file = teams_dir / 'nhl_league_summary.json'
    with open(summary_file, 'w') as f:
        json.dump({
            'season':         season,
            'last_updated':   data['last_updated'],
            'league_summary': data['league_summary'],
        }, f)

    # Also write the old single-file format for local use
    with open(output_file, 'w') as f:
        json.dump(data, f)

    print(f'\n{"="*55}')
    print(f'Update complete')
    print(f'New games added: {total_new}')
    print(f'Teams tracked:   {len(data["teams"])}')
    print(f'Last updated:    {data["last_updated"]}')
    print(f'Per-team files:  data/{season}/')
    print(f'League summary:  data/{season}/nhl_league_summary.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='NHL Shot Analytics incremental updater')
    parser.add_argument('--season', help='Season ID e.g. 20262027 (default: current season)')
    parser.add_argument('--teams', nargs='+', help='Specific team abbreviations')
    parser.add_argument('--force', action='store_true', help='Force full rebuild (re-pull all games)')
    args = parser.parse_args()
    run(season=args.season, target_teams=args.teams, force=args.force)
