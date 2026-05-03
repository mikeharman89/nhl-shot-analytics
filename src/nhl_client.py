"""
nhl_client.py
-------------
Thin wrapper around the NHL public API and nhl-api-py.
Handles fetching, basic error handling, and response normalisation.

NHL API base: https://api-web.nhle.com/v1/
EDGE base:    https://api.nhle.com/stats/rest/en/
"""

import requests
import pandas as pd
from typing import Optional

NHL_API   = "https://api-web.nhle.com/v1"
STATS_API = "https://api.nhle.com/stats/rest/en"


def _get(url: str, params: dict = None) -> dict:
    """Raw GET with basic error handling."""
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def get_teams() -> pd.DataFrame:
    """All current NHL franchises as a DataFrame."""
    data = _get(f"{NHL_API}/standings/now")
    rows = []
    for entry in data.get("standings", []):
        rows.append({
            "team_abbr":    entry["teamAbbrev"]["default"],
            "team_name":    entry["teamName"]["default"],
            "conference":   entry["conferenceName"],
            "division":     entry["divisionName"],
            "wins":         entry["wins"],
            "losses":       entry["losses"],
            "points":       entry["points"],
        })
    return pd.DataFrame(rows)


def get_roster(team_abbr: str, season: str = "20242025") -> pd.DataFrame:
    """
    Full roster for a team.

    Parameters
    ----------
    team_abbr : str   e.g. "UTA", "COL", "DAL"
    season    : str   e.g. "20242025"
    """
    data = _get(f"{NHL_API}/roster/{team_abbr}/{season}")
    rows = []
    for pos_group in ("forwards", "defensemen", "goalies"):
        for p in data.get(pos_group, []):
            rows.append({
                "player_id":   p["id"],
                "first_name":  p["firstName"]["default"],
                "last_name":   p["lastName"]["default"],
                "position":    p.get("positionCode", pos_group[0].upper()),
                "jersey":      p.get("sweaterNumber"),
                "shoots":      p.get("shootsCatches"),
                "birth_date":  p.get("birthDate"),
                "birth_city":  p.get("birthCity", {}).get("default"),
                "nationality": p.get("birthCountry"),
                "height_in":   p.get("heightInInches"),
                "weight_lb":   p.get("weightInPounds"),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def get_team_schedule(team_abbr: str, season: str = "20242025") -> pd.DataFrame:
    """
    Full regular-season schedule for a team.
    Includes opponent, home/away, game date, and result where available.
    """
    data = _get(f"{NHL_API}/club-schedule-season/{team_abbr}/{season}")
    rows = []
    for g in data.get("games", []):
        home = g["homeTeam"]["abbrev"]
        away = g["awayTeam"]["abbrev"]
        rows.append({
            "game_id":      g["id"],
            "date":         g["gameDate"],
            "home_team":    home,
            "away_team":    away,
            "is_home":      home == team_abbr,
            "venue":        g.get("venue", {}).get("default"),
            "game_type":    g.get("gameType"),         # 2 = regular, 3 = playoff
            "game_state":   g.get("gameState"),        # "OFF" = final
            "home_score":   g["homeTeam"].get("score"),
            "away_score":   g["awayTeam"].get("score"),
        })
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------------------------------------------------------------------------
# Game-level stats
# ---------------------------------------------------------------------------

def get_boxscore(game_id: int) -> dict:
    """Raw boxscore dict for a single game."""
    return _get(f"{NHL_API}/gamecenter/{game_id}/boxscore")


def get_play_by_play(game_id: int) -> pd.DataFrame:
    """
    Play-by-play for a game as a flat DataFrame.
    Each row is one event: shot, goal, hit, faceoff, etc.
    """
    data = _get(f"{NHL_API}/gamecenter/{game_id}/play-by-play")
    plays = data.get("plays", [])
    rows = []
    for p in plays:
        details = p.get("details", {})
        rows.append({
            "event_id":      p.get("eventId"),
            "period":        p.get("periodDescriptor", {}).get("number"),
            "time_in_period":p.get("timeInPeriod"),
            "time_remaining":p.get("timeRemaining"),
            "event_type":    p.get("typeDescKey"),
            "zone":          details.get("zoneCode"),         # "O", "N", "D"
            "x_coord":       details.get("xCoord"),
            "y_coord":       details.get("yCoord"),
            "shot_type":     details.get("shotType"),
            "scoring_player_id": details.get("scoringPlayerId"),
            "shooting_player_id": details.get("shootingPlayerId"),
            "goalie_id":     details.get("goalieInNetId"),
            "home_score":    p.get("homeTeamDefendingSide"),  # placeholder
            "situation_code":p.get("situationCode"),          # e.g. "1551" = 5v5
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Player stats (season-level)
# ---------------------------------------------------------------------------

def get_skater_stats(season: str = "20242025",
                     game_type: int = 2,
                     limit: int = 500) -> pd.DataFrame:
    """
    League-wide skater summary stats for a season.
    game_type: 2 = regular season, 3 = playoffs
    """
    params = {
        "cayenneExp": f"seasonId={season} and gameTypeId={game_type}",
        "limit": limit,
        "start": 0,
    }
    data = _get(f"{STATS_API}/skater/summary", params=params)
    return pd.DataFrame(data.get("data", []))


def get_player_game_log(player_id: int,
                        season: str = "20242025",
                        game_type: int = 2) -> pd.DataFrame:
    """
    Game-by-game stats for a single player — the key input for
    tracking performance over time (our fatigue signal).
    """
    data = _get(f"{NHL_API}/player/{player_id}/game-log/{season}/{game_type}")
    df = pd.DataFrame(data.get("gameLog", []))
    if df.empty:
        return df
    df["gameDate"] = pd.to_datetime(df["gameDate"])
    return df.sort_values("gameDate").reset_index(drop=True)


# ---------------------------------------------------------------------------
# EDGE tracking data
# ---------------------------------------------------------------------------

def get_edge_skater_stats(season: str = "20242025",
                          game_type: int = 2,
                          limit: int = 500) -> pd.DataFrame:
    """
    NHL EDGE skating metrics: speed, distance, acceleration events.
    This is the physical load data that doesn't exist in standard stats.

    Key columns returned:
      - playerName, teamAbbrevs
      - avgSpeed, maxSpeed            (km/h)
      - totalDistance                 (metres)
      - hardAccelerations, hardDecelerations
      - shakesCount                   (direction changes)
      - gamesPlayed, timeOnIcePerGame
    """
    params = {
        "cayenneExp": f"seasonId={season} and gameTypeId={game_type}",
        "limit": limit,
    }
    data = _get(f"{STATS_API}/skater/skating", params=params)
    return pd.DataFrame(data.get("data", []))


def get_edge_by_game(player_id: int,
                     season: str = "20242025") -> pd.DataFrame:
    """
    Per-game EDGE skating data for a single player.
    Useful for plotting physical output across the season.
    """
    params = {
        "cayenneExp": f"playerId={player_id} and seasonId={season} and gameTypeId=2",
        "limit": 100,
    }
    data = _get(f"{STATS_API}/skater/skatingSummary", params=params)
    return pd.DataFrame(data.get("data", []))
