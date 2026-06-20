"""
Fetch team and player statistics from API-Football for each fixture.
Caches by team_id within a run to stay well under the 100 req/day free limit.
Writes: data/stats_raw.json
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
FIXTURES_PATH = os.path.join(DATA_DIR, "fixtures_raw.json")
SAMPLE_PATH = os.path.join(DATA_DIR, "sample", "stats.json")
OUT_PATH = os.path.join(DATA_DIR, "stats_raw.json")

BASE_URL = "https://v3.football.api-sports.io"


def current_season():
    now = datetime.now()
    return now.year if now.month >= 7 else now.year - 1


def api_get(endpoint: str, params: dict, api_key: str) -> dict | None:
    headers = {"x-apisports-key": api_key}
    try:
        resp = requests.get(f"{BASE_URL}/{endpoint}", headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            log.warning(f"Rate limited on {endpoint} — skipping")
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error(f"API error on {endpoint}: {e}")
        return None


def sum_card_periods(card_dict: dict) -> int:
    total = 0
    for period_data in card_dict.values():
        if isinstance(period_data, dict):
            total += period_data.get("total") or 0
    return total


def extract_team_stats(response: dict, team_id: int, team_name: str) -> dict:
    r = response.get("response", {})
    games = r.get("fixtures", {}).get("played", {}).get("total", 1) or 1
    goals = r.get("goals", {})
    shots = r.get("shots", {})
    cards = r.get("cards", {})

    goals_for = float(goals.get("for", {}).get("average", {}).get("total", 0) or 0)
    goals_against = float(goals.get("against", {}).get("average", {}).get("total", 0) or 0)

    shots_on_total = shots.get("on", {}).get("total", {}).get("total") or 0
    shots_on_for_pg = shots_on_total / games

    shots_on_against_total = shots.get("on", {}).get("total", {})
    # API-Football shots.on structure is nested differently for against — use shots.against if present
    shots_against_raw = r.get("shots_against", {})
    shots_on_against_pg = (shots_against_raw.get("on", {}).get("total") or 0) / games

    yellows_total = sum_card_periods(cards.get("yellow", {}))
    reds_total = sum_card_periods(cards.get("red", {}))

    clean_sheets = r.get("clean_sheet", {}).get("total", 0) or 0
    failed_to_score = r.get("failed_to_score", {}).get("total", 0) or 0

    return {
        "team_id": team_id,
        "team_name": team_name,
        "games_played": games,
        "goals_for_avg": round(goals_for, 3),
        "goals_against_avg": round(goals_against, 3),
        "shots_on_target_for_pg": round(shots_on_for_pg, 2),
        "shots_on_target_against_pg": round(shots_on_against_pg, 2),
        "yellow_cards_avg": round(yellows_total / games, 3),
        "red_cards_avg": round(reds_total / games, 3),
        "clean_sheet_rate": round(clean_sheets / games, 3),
        "failed_to_score_rate": round(failed_to_score / games, 3),
        "xg_available": False,
        "xg_for_avg": None,
        "xg_against_avg": None,
    }


def extract_top_scorers(response: dict, team_id: int, league_id: int) -> list:
    players = []
    for item in response.get("response", []):
        player = item.get("player", {})
        stats_list = item.get("statistics", [])
        # Find stats for the matching league
        stat = next(
            (s for s in stats_list if s.get("league", {}).get("id") == league_id),
            stats_list[0] if stats_list else None,
        )
        if not stat:
            continue
        minutes = stat.get("games", {}).get("minutes") or 0
        if minutes < 90:
            continue
        goals = stat.get("goals", {}).get("total") or 0
        shots_on = stat.get("shots", {}).get("on") or 0
        per90 = minutes / 90
        players.append(
            {
                "player_id": player.get("id"),
                "name": player.get("name"),
                "goals": goals,
                "goals_per90": round(goals / per90, 3) if per90 else 0,
                "shots_on_target_per90": round(shots_on / per90, 3) if per90 else 0,
                "minutes": minutes,
            }
        )
    players.sort(key=lambda p: p["goals"], reverse=True)
    return players[:5]


def fetch_team_stats(team_id: int, league_id: int, season: int, api_key: str) -> dict | None:
    data = api_get("teams/statistics", {"league": league_id, "season": season, "team": team_id}, api_key)
    if not data:
        return None
    team_name = data.get("response", {}).get("team", {}).get("name", str(team_id))
    return extract_team_stats(data, team_id, team_name)


def fetch_top_scorers(team_id: int, league_id: int, season: int, api_key: str) -> list:
    data = api_get("players", {"team": team_id, "league": league_id, "season": season}, api_key)
    if not data:
        return []
    return extract_top_scorers(data, team_id, league_id)


def main(dry_run: bool = False):
    os.makedirs(DATA_DIR, exist_ok=True)

    if dry_run:
        log.info("DRY RUN — loading sample stats")
        with open(SAMPLE_PATH) as f:
            stats = json.load(f)
        with open(OUT_PATH, "w") as f:
            json.dump(stats, f, indent=2)
        log.info(f"Wrote sample stats → {OUT_PATH}")
        return stats

    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        log.error("API_FOOTBALL_KEY env var not set")
        sys.exit(1)

    with open(FIXTURES_PATH) as f:
        fixtures = json.load(f)

    # Build a per-league season map so World Cup (season=2026) is handled correctly
    league_season_map = {
        league["api_football_id"]: league.get("season") or current_season()
        for league in json.load(open(os.path.join(ROOT, "config", "leagues.json")))["leagues"]
    }
    stats: dict[str, dict] = {}
    seen_team_ids: set[int] = set()
    request_count = 0

    for fix in fixtures:
        league_id = fix["api_football_id"]
        season = league_season_map.get(league_id, current_season())
        for team_id, team_name in [
            (fix["home_team_id"], fix["home_team"]),
            (fix["away_team_id"], fix["away_team"]),
        ]:
            if team_id in seen_team_ids:
                log.info(f"  Cache hit: {team_name}")
                continue
            seen_team_ids.add(team_id)

            log.info(f"  Stats: {team_name} (league {league_id}, season {season})")
            team_stat = fetch_team_stats(team_id, league_id, season, api_key)
            request_count += 1

            if team_stat is None:
                log.warning(f"  No stats returned for {team_name} — using defaults")
                team_stat = {
                    "team_id": team_id,
                    "team_name": team_name,
                    "games_played": 0,
                    "goals_for_avg": 1.3,
                    "goals_against_avg": 1.3,
                    "shots_on_target_for_pg": 4.0,
                    "shots_on_target_against_pg": 4.0,
                    "yellow_cards_avg": 1.8,
                    "red_cards_avg": 0.05,
                    "clean_sheet_rate": 0.3,
                    "failed_to_score_rate": 0.2,
                    "xg_available": False,
                    "xg_for_avg": None,
                    "xg_against_avg": None,
                }

            log.info(f"  Players: {team_name}")
            scorers = fetch_top_scorers(team_id, league_id, season, api_key)
            request_count += 1
            team_stat["top_scorers"] = scorers

            stats[str(team_id)] = team_stat
            log.info(f"  [{request_count} API calls used so far]")

    with open(OUT_PATH, "w") as f:
        json.dump(stats, f, indent=2)
    log.info(f"Saved stats for {len(stats)} teams → {OUT_PATH} ({request_count} API calls)")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
