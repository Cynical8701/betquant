"""
Fetch team statistics from football-data.org.
Uses each team's last 10 finished matches to compute goal averages.
Note: football-data.org free tier does not provide xG or shots data;
the model will use goals-per-game as the underlying metric and flag this clearly.
Writes: data/stats_raw.json
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
FIXTURES_PATH = os.path.join(DATA_DIR, "fixtures_raw.json")
SAMPLE_PATH = os.path.join(DATA_DIR, "sample", "stats.json")
OUT_PATH = os.path.join(DATA_DIR, "stats_raw.json")

BASE_URL = "https://api.football-data.org/v4"
MATCHES_SAMPLE = 10  # last N finished matches per team


def fetch_team_matches(team_id: int, api_key: str) -> list:
    headers = {"X-Auth-Token": api_key}
    params = {"status": "FINISHED", "limit": MATCHES_SAMPLE}
    try:
        resp = requests.get(
            f"{BASE_URL}/teams/{team_id}/matches",
            headers=headers, params=params, timeout=15
        )
        if resp.status_code == 429:
            log.warning(f"Rate limited fetching team {team_id} — waiting 60s")
            time.sleep(60)
            return []
        if resp.status_code == 404:
            log.warning(f"Team {team_id} not found")
            return []
        resp.raise_for_status()
        return resp.json().get("matches", [])
    except requests.RequestException as e:
        log.error(f"Failed to fetch matches for team {team_id}: {e}")
        return []


def compute_stats(team_id: int, team_name: str, matches: list) -> dict:
    goals_for = []
    goals_against = []

    for m in matches:
        score = m.get("score", {}).get("fullTime", {})
        home_id = m.get("homeTeam", {}).get("id")
        home_g = score.get("home")
        away_g = score.get("away")
        if home_g is None or away_g is None:
            continue
        if home_id == team_id:
            goals_for.append(home_g)
            goals_against.append(away_g)
        else:
            goals_for.append(away_g)
            goals_against.append(home_g)

    n = len(goals_for) or 1
    gf_avg = sum(goals_for) / n
    ga_avg = sum(goals_against) / n

    # Cards: football-data.org free tier doesn't provide card stats per team
    # Use empirical league averages as defaults (approx 1.8 yellows/game per team)
    return {
        "team_id": team_id,
        "team_name": team_name,
        "games_sampled": n,
        "goals_for_avg": round(gf_avg, 3),
        "goals_against_avg": round(ga_avg, 3),
        "shots_on_target_for_pg": None,
        "shots_on_target_against_pg": None,
        "yellow_cards_avg": 1.8,
        "red_cards_avg": 0.05,
        "clean_sheet_rate": round(sum(1 for g in goals_against if g == 0) / n, 3),
        "failed_to_score_rate": round(sum(1 for g in goals_for if g == 0) / n, 3),
        "xg_available": False,
        "xg_for_avg": None,
        "xg_against_avg": None,
        "top_scorers": [],  # not available from football-data.org free tier
    }


def default_stats(team_id: int, team_name: str) -> dict:
    return {
        "team_id": team_id,
        "team_name": team_name,
        "games_sampled": 0,
        "goals_for_avg": 1.3,
        "goals_against_avg": 1.3,
        "shots_on_target_for_pg": None,
        "shots_on_target_against_pg": None,
        "yellow_cards_avg": 1.8,
        "red_cards_avg": 0.05,
        "clean_sheet_rate": 0.28,
        "failed_to_score_rate": 0.22,
        "xg_available": False,
        "xg_for_avg": None,
        "xg_against_avg": None,
        "top_scorers": [],
    }


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

    api_key = os.environ.get("FOOTBALL_DATA_KEY")
    if not api_key:
        log.error("FOOTBALL_DATA_KEY env var not set")
        sys.exit(1)

    with open(FIXTURES_PATH) as f:
        fixtures = json.load(f)

    stats: dict[str, dict] = {}
    seen: set[int] = set()
    call_count = 0

    for fix in fixtures:
        for team_id, team_name in [
            (fix["home_team_id"], fix["home_team"]),
            (fix["away_team_id"], fix["away_team"]),
        ]:
            if team_id in seen:
                continue
            seen.add(team_id)

            log.info(f"  Fetching recent form: {team_name} (id {team_id})")
            # Respect the 10 req/min free tier limit
            if call_count > 0 and call_count % 9 == 0:
                log.info("  Pausing 65s to respect rate limit...")
                time.sleep(65)

            matches = fetch_team_matches(team_id, api_key)
            call_count += 1

            if matches:
                stats[str(team_id)] = compute_stats(team_id, team_name, matches)
                log.info(
                    f"  {team_name}: {stats[str(team_id)]['games_sampled']} matches — "
                    f"avg {stats[str(team_id)]['goals_for_avg']:.2f} for / "
                    f"{stats[str(team_id)]['goals_against_avg']:.2f} against"
                )
            else:
                log.warning(f"  No match data for {team_name} — using defaults")
                stats[str(team_id)] = default_stats(team_id, team_name)

    with open(OUT_PATH, "w") as f:
        json.dump(stats, f, indent=2)
    log.info(f"Saved stats for {len(stats)} teams → {OUT_PATH} ({call_count} API calls)")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
