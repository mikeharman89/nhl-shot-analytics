"""
schedule_analysis.py
--------------------
Builds contextual features from a team's schedule:
  - back-to-back flags
  - days of rest between games
  - travel distance between venues
  - cumulative travel load

These are the environmental inputs to a fatigue model.
"""

import pandas as pd
import numpy as np
from math import radians, sin, cos, sqrt, atan2

# Approximate arena coordinates (lat, lon)
ARENA_COORDS = {
    "UTA": (40.7683,  -111.9011),  # Delta Center, Salt Lake City
    "COL": (39.7487,  -104.9897),  # Ball Arena, Denver
    "DAL": (32.7904,  -96.8103),   # American Airlines Center
    "MIN": (44.9447,  -93.1010),   # Xcel Energy Center
    "STL": (38.6267,  -90.2025),   # Enterprise Center
    "NSH": (36.1592,  -86.7785),   # Bridgestone Arena
    "WPG": (49.8928,  -97.1438),   # Canada Life Centre
    "CHI": (41.8807,  -87.6742),   # United Center
    "VGK": (36.1029,  -115.1784),  # T-Mobile Arena
    "ANA": (33.8078,  -117.8767),  # Honda Center
    "LAK": (34.0430,  -118.2673),  # Crypto.com Arena
    "SJS": (37.3329,  -121.9010),  # SAP Center
    "SEA": (47.6220,  -122.3541),  # Climate Pledge Arena
    "CGY": (51.0374,  -114.0519),  # Scotiabank Saddledome
    "EDM": (53.5461,  -113.4938),  # Rogers Place
    "VAN": (49.2778,  -123.1088),  # Rogers Arena
    "BUF": (42.8749,  -78.8763),   # KeyBank Center
    "BOS": (42.3662,  -71.0621),   # TD Garden
    "TBL": (27.9428,  -82.4519),   # Amalie Arena
    "FLA": (26.1584,  -80.3256),   # Amerant Bank Arena
    "TOR": (43.6435,  -79.3791),   # Scotiabank Arena
    "MTL": (45.4961,  -73.5693),   # Bell Centre
    "OTT": (45.2967,  -75.9270),   # Canadian Tire Centre
    "DET": (42.3411,  -83.0554),   # Little Caesars Arena
    "CAR": (35.8031,  -78.7228),   # PNC Arena
    "CBJ": (39.9690,  -83.0061),   # Nationwide Arena
    "PHI": (39.9012,  -75.1720),   # Wells Fargo Center
    "NYI": (40.7226,  -73.5907),   # UBS Arena
    "NJD": (40.7334,  -74.1712),   # Prudential Center
    "NYR": (40.7505,  -73.9934),   # Madison Square Garden
    "WSH": (38.8981,  -77.0209),   # Capital One Arena
    "PIT": (40.4395,  -79.9892),   # PPG Paints Arena
}


def haversine_miles(coord1: tuple, coord2: tuple) -> float:
    """Great-circle distance in miles between two (lat, lon) pairs."""
    R = 3958.8  # Earth radius in miles
    lat1, lon1 = map(radians, coord1)
    lat2, lon2 = map(radians, coord2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def build_schedule_features(schedule_df: pd.DataFrame,
                             team_abbr: str) -> pd.DataFrame:
    """
    Enriches a raw schedule DataFrame with fatigue-relevant context.

    Input: output of nhl_client.get_team_schedule()
    Output: same rows + new columns:
      - days_rest         : days since last game (NaN for opener)
      - is_back_to_back   : True if days_rest == 1
      - opponent          : opposing team abbreviation
      - travel_miles      : miles travelled from previous game city
      - is_road_game      : alias for ~is_home
      - cumulative_road_miles : running total of road miles this season
      - road_trip_game_num : which game of the current road trip (1-indexed, 0 = home)
    """
    df = schedule_df.copy().sort_values("date").reset_index(drop=True)

    df["opponent"] = df.apply(
        lambda r: r["away_team"] if r["is_home"] else r["home_team"], axis=1
    )
    df["is_road_game"] = ~df["is_home"]

    # Days rest
    df["days_rest"] = df["date"].diff().dt.days

    # Back-to-back flag
    df["is_back_to_back"] = df["days_rest"] == 1

    # Travel miles: where did we play last game vs. today?
    travel_miles = [np.nan]
    for i in range(1, len(df)):
        prev = df.loc[i-1]
        curr = df.loc[i]
        prev_city = prev["home_team"]
        curr_city = curr["home_team"]
        if prev_city in ARENA_COORDS and curr_city in ARENA_COORDS:
            miles = haversine_miles(ARENA_COORDS[prev_city], ARENA_COORDS[curr_city])
            travel_miles.append(round(miles, 1))
        else:
            travel_miles.append(np.nan)
    df["travel_miles"] = travel_miles

    # Cumulative road miles
    df["cumulative_road_miles"] = df["travel_miles"].fillna(0).cumsum().round(1)

    # Road trip game number (resets to 0 on each home game)
    road_trip_num = []
    counter = 0
    for _, row in df.iterrows():
        if row["is_home"]:
            counter = 0
        else:
            counter += 1
        road_trip_num.append(counter)
    df["road_trip_game_num"] = road_trip_num

    return df


def summarise_travel_burden(schedule_df: pd.DataFrame) -> dict:
    """
    High-level travel burden summary for a team's season.
    Returns a dict suitable for printing or comparing across teams.
    """
    df = schedule_df
    return {
        "total_games":          len(df),
        "home_games":           df["is_home"].sum(),
        "road_games":           (~df["is_home"]).sum(),
        "back_to_backs":        df["is_back_to_back"].sum(),
        "total_travel_miles":   df["travel_miles"].sum(skipna=True),
        "avg_travel_per_game":  df["travel_miles"].mean(skipna=True),
        "max_single_trip":      df["travel_miles"].max(skipna=True),
        "longest_road_trip":    df["road_trip_game_num"].max(),
        "avg_days_rest":        df["days_rest"].mean(skipna=True),
        "games_on_1_day_rest":  (df["days_rest"] == 1).sum(),
        "games_on_0_day_rest":  (df["days_rest"] == 0).sum(),
    }
