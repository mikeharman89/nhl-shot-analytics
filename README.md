# NHL Shot Analytics

An interactive league-wide shot analytics dashboard covering all 32 NHL teams for the 2025–26 season. Built on the NHL's public API — no keys, no paid data sources.

**[View the dashboard →](https://mikeharman89.github.io/nhl-shot-analytics/)**

---

## What this is

A data pipeline and interactive dashboard that pulls every shot attempt from every NHL game, computes expected goals (xG) for each shot, and aggregates it into a league-wide analytics tool with three levels of detail:

**League table** — all 32 teams ranked by xG differential, with actual goals, shooting %, lucky wins, and unlucky losses side by side.

**Team page** — click any team to see their full season xG game score breakdown. Every game shows actual score vs. expected score, with a running xG timeline chart and zone-level shot breakdown for each game.

**Shot map** — an interactive rink diagram showing every shot attempt for any team, filterable by game, player, situation, period, and shot result. Toggle between the team's offensive shots, opponent shots, or both teams simultaneously.

---

## What's in this repo

```
shot_pipeline.py         # Data pipeline — pulls all 32 teams
nhl_shot_data.html       # Interactive dashboard (single file, self-contained)
src/
  nhl_client.py          # NHL API wrapper
  schedule_analysis.py   # Schedule parsing and travel calculations
README.md
```

---

## Getting started

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

Or reuse an existing venv:

```bash
source ../NHL_Fatigue/venv/bin/activate
```

### 2. Run the pipeline

```bash
python shot_pipeline.py
```

Pulls shot data for all 32 teams and writes `nhl_shots_20252026.json`. Runtime is approximately 5–6 hours due to API rate limiting.

**Test with a few teams first:**
```bash
python shot_pipeline.py --teams UTA COL BOS
```

**Resume an interrupted run** — the pipeline checkpoints every 4 teams. If it gets interrupted, just run it again and it'll skip already-completed teams automatically.

### 3. Open the dashboard

```bash
python -m http.server 8000
```

Open `http://localhost:8000/nhl_shot_data.html` in your browser. The dashboard reads the JSON file via `fetch()` — it won't work if you open the HTML directly without a local server.

---

## The xG model

Expected goals (xG) is calculated per shot based on distance from the net and shot type. Only shots on goal and goals carry xG — blocked shots and missed shots are tracked for shot map purposes but contribute 0.0 to xG totals, since they never gave the goalie a save opportunity.

**Base probabilities (calibrated from 2025/26 actual shooting percentages):**

| Distance | Base xG | Actual sh% |
|----------|---------|------------|
| 0–10 ft  | 0.200   | 20.0%      |
| 10–20 ft | 0.182   | 18.2%      |
| 20–30 ft | 0.145   | 14.5%      |
| 30–40 ft | 0.089   | 8.9%       |
| 40–55 ft | 0.058   | 5.8%       |
| 55+ ft   | 0.029   | 2.9%       |

**Shot type multipliers:**

| Shot type    | Multiplier |
|--------------|------------|
| Deflected    | 1.40×      |
| Tip-in       | 1.35×      |
| Snap         | 1.05×      |
| Wrist        | 1.00×      |
| Slap         | 0.90×      |
| Wrap-around  | 0.90×      |
| Backhand     | 0.85×      |

---

## Key metrics

**xGF (expected goals for)** — sum of xG values for all shots on goal and goals by the team. Represents how many goals the team *should* have scored based on shot quality.

**xGA (expected goals against)** — same, but for shots allowed. Measures defensive performance and goaltending.

**xG diff** — xGF minus xGA. The single best predictor of a team's underlying performance. Positive means the team is generating better chances than they allow.

**xG record** — wins and losses based on which team had higher xGF in each game. A team can win games while losing the xG battle (lucky wins) and lose games while winning the xG battle (unlucky losses).

**Lucky wins** — games where the team won despite the opponent having higher xGF. Indicates games where shooting luck or goaltending outperformed the underlying chances.

**Unlucky losses** — games where the team lost despite having higher xGF. The most useful signal for identifying teams that are better than their record suggests.

**Sh% (shooting %)** — goals divided by shots on goal. Teams with a Sh% significantly above their historical average are likely to regress.

---

## Shot colors on the rink map

| Color | Meaning |
|-------|---------|
| Green | Goal |
| White/Lavender | Shot on goal (saved) |
| Red | Missed shot |
| Yellow | Blocked shot |

---

## Data source

All data pulled from the NHL's public API — no authentication required:

- `https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play` — shot events with coordinates
- `https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore` — player rosters and scores
- `https://api-web.nhle.com/v1/club-schedule-season/{team}/{season}` — team schedules

Coordinates use the standard NHL rink system: center ice at (0,0), rink is 200×85 ft. All shots are normalized to fire toward positive x so the shot map always shows the offensive direction regardless of period or home/away.

---

## Limitations

- The xG model uses distance and shot type only. Professional models incorporate 10–15 additional features including rush shots, whether the shot came off a pass, pre-shot movement, traffic in front of the net, and goalie positioning. Ours will underpredict high-danger rush chances and overpredict static perimeter shots.
- Blocked shots and missed shots are included in shot maps but excluded from xG calculations. This is intentional — they represent volume but not genuine scoring chances.
- NHL EDGE tracking data (skating speed, acceleration, distance per shift) would allow physical load analysis on top of shot quality. That data remains inaccessible via the public API.

---

## Related

The Road Trip Fatigue analysis — a separate project measuring how players hold up deep in road trips:
**[nhl-road-fatigue](https://github.com/yourusername/nhl-road-fatigue)**
