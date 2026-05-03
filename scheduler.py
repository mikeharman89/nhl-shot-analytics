"""
scheduler.py
------------
Runs the NHL shot updater every morning automatically.
Uses Python's built-in schedule library — no cron needed.

Usage:
    python scheduler.py              # runs daily at 6:00 AM
    python scheduler.py --time 07:30 # custom time (24hr format)
    python scheduler.py --now        # run immediately then schedule

Keep this running in a terminal session or use a tool like
'screen' or 'tmux' to keep it alive in the background:
    screen -S nhl-updater
    python scheduler.py
    Ctrl+A, D  (detach)

Or run it as a background process:
    nohup python scheduler.py &
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime

try:
    import schedule
except ImportError:
    print("Installing schedule library...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'schedule', '--break-system-packages'])
    import schedule


def run_update():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'\n[{now}] Running daily NHL shot update...')
    try:
        result = subprocess.run(
            [sys.executable, 'update_pipeline.py'],
            capture_output=False,
            text=True
        )
        if result.returncode == 0:
            print(f'[{now}] Update completed successfully')
        else:
            print(f'[{now}] Update failed with return code {result.returncode}')
    except Exception as e:
        print(f'[{now}] Update error: {e}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='NHL shot data daily scheduler')
    parser.add_argument('--time', default='06:00', help='Daily run time in HH:MM format (default: 06:00)')
    parser.add_argument('--now', action='store_true', help='Run immediately, then schedule')
    args = parser.parse_args()

    print(f'NHL Shot Analytics Scheduler')
    print(f'Daily update scheduled for: {args.time}')
    print(f'Press Ctrl+C to stop\n')

    if args.now:
        run_update()

    schedule.every().day.at(args.time).do(run_update)

    while True:
        schedule.run_pending()
        time.sleep(60)
