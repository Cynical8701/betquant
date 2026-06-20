"""
Fetch upcoming fixtures from API-Football for all configured leagues.
Writes: data/fixtures_raw.json
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "leagues.json")
DATA_DIR = os.path.join(ROOT, "data")
SAMPLE_PATH = os.path.join(DATA_DIR, "sample", "fixtures.json")
OUT_PATH = os.path.join(DATA_DIR, "fixtures_raw.json")

BASE_URL = "https://v3.football.api-sports.io"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def current_season():
    now = datetime.now()
    return now.year if now.month >= 7 else now.year - 1


def fetch_league_fixtures(league_id: int, api_key: str, days_ahead: int) -> list:
    headers = {"x-apisports-key": api_key}
    today = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=days_ahead)
    params = {
        "league": league_id,
        "from": today.isoformat(),
        "to": end_date.isoformat(),
        "season": current_season(),
        "timezone": "UTC",
    }
    try:
        resp = requests.get(f"{BASE_URL}/fixtures", headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            log.warning(f"Rate limited for league {league_id} — skipping")
            return []
        resp.raise_for_status()
        return resp.json().get("response", [])
    except requests.RequestException as e:
        log.error(f"Failed to fetch fixtures for league {league_id}: {e}")
        return []


def normalise(raw: dict, league_meta: dict) -> dict:
    fixture = raw["fixture"]
    teams = raw["teams"]
    return {
        "fixture_id": fixture["id"],
        "league": league_meta["name"],
        "league_country": league_meta["country"],
        "api_football_id": league_meta["api_football_id"],
        "odds_api_key": league_meta["odds_api_key"],
        "home_team": teams["home"]["name"],
        "home_team_id": teams["home"]["id"],
        "away_team": teams["away"]["name"],
        "away_team_id": teams["away"]["id"],
        "kickoff_utc": fixture["date"],
    }


def main(dry_run: bool = False):
    os.makedirs(DATA_DIR, exist_ok=True)

    if dry_run:
        log.info("DRY RUN — loading sample fixtures")
        with open(SAMPLE_PATH) as f:
            fixtures = json.load(f)
        with open(OUT_PATH, "w") as f:
            json.dump(fixtures, f, indent=2)
        log.info(f"Wrote {len(fixtures)} sample fixtures → {OUT_PATH}")
        return fixtures

    config = load_config()
    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        log.error("API_FOOTBALL_KEY env var not set")
        sys.exit(1)

    days_ahead = config["settings"]["days_ahead"]
    max_fixtures = config["settings"]["max_fixtures_per_run"]
    all_fixtures = []

    for league in config["leagues"]:
        if not league["enabled"]:
            continue
        log.info(f"Fetching: {league['name']}")
        raw_list = fetch_league_fixtures(league["api_football_id"], api_key, days_ahead)
        for raw in raw_list:
            all_fixtures.append(normalise(raw, league))
        if len(all_fixtures) >= max_fixtures:
            break

    truncated = all_fixtures[:max_fixtures]
    with open(OUT_PATH, "w") as f:
        json.dump(truncated, f, indent=2)
    log.info(f"Saved {len(truncated)} fixtures → {OUT_PATH}")
    return truncated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
