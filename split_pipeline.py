"""
Splits nhl_shots_20252026.json into per-team files + a league summary.

Output structure:
  data/
    nhl_league_summary.json          <- league table only (~1MB)
    nhl_shots_UTA_20252026.json      <- per-team shots + game summaries
    nhl_shots_BOS_20252026.json
    ... etc

Run once after the full pipeline completes:
    python split_pipeline.py
    python split_pipeline.py --season 20262027
"""

import json, argparse
from pathlib import Path

def run(season='20252026'):
    src = f'nhl_shots_{season}.json'
    if not Path(src).exists():
        print(f'Source file not found: {src}')
        return

    print(f'Loading {src}...')
    with open(src) as f:
        data = json.load(f)

    out_dir = Path('data')
    out_dir.mkdir(exist_ok=True)

    # Write league summary
    summary = {
        'season':       data.get('season'),
        'generated':    data.get('generated'),
        'last_updated': data.get('last_updated'),
        'league_summary': data.get('league_summary', []),
    }
    summary_file = out_dir / f'nhl_league_summary_{season}.json'
    with open(summary_file, 'w') as f:
        json.dump(summary, f)
    print(f'Written: {summary_file} ({summary_file.stat().st_size/1024:.0f} KB)')

    # Write per-team files
    for abbr, team_data in data.get('teams', {}).items():
        team_file = out_dir / f'nhl_shots_{abbr}_{season}.json'
        with open(team_file, 'w') as f:
            json.dump(team_data, f)
        size_mb = team_file.stat().st_size / 1024 / 1024
        print(f'Written: {team_file} ({size_mb:.1f} MB)')

    print(f'\nDone. {len(data["teams"])} team files written to {out_dir}/')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--season', default='20252026')
    args = parser.parse_args()
    run(args.season)
