"""
Fetch upcoming fixtures from football-data.org for all configured leagues.
Free tier: 10 req/min, current season data, no credit card needed.
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

BASE_URL = "https://api.football-data.org/v4"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def fetch_league_fixtures(comp_code: str, api_key: str, days_ahead: int) -> list:
    headers = {"X-Auth-Token": api_key}
    today = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=days_ahead)
    params = {
        "status": "TIMED,SCHEDULED",
        "dateFrom": today.isoformat(),
        "dateTo": end_date.isoformat(),
    }
    try:
        resp = requests.get(
            f"{BASE_URL}/competitions/{comp_code}/matches",
            headers=headers, params=params, timeout=15
        )
        if resp.status_code == 429:
            log.warning(f"Rate limited for {comp_code} — skipping")
            return []
        if resp.status_code == 403:
            log.warning(f"Access denied for {comp_code} — not on free tier")
            return []
        resp.raise_for_status()
        matches = resp.json().get("matches", [])
        log.info(f"  Found {len(matches)} upcoming fixtures for {comp_code}")
        return matches
    except requests.RequestException as e:
        log.error(f"Failed to fetch {comp_code}: {e}")
        return []


def normalise(match: dict, league_meta: dict) -> dict:
    return {
        "fixture_id": match["id"],
        "league": league_meta["name"],
        "league_country": league_meta["country"],
        "football_data_code": league_meta["football_data_code"],
        "odds_api_key": league_meta["odds_api_key"],
        "home_team": match["homeTeam"]["name"],
        "home_team_id": match["homeTeam"]["id"],
        "away_team": match["awayTeam"]["name"],
        "away_team_id": match["awayTeam"]["id"],
        "kickoff_utc": match["utcDate"],
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
    api_key = os.environ.get("FOOTBALL_DATA_KEY")
    if not api_key:
        log.error("FOOTBALL_DATA_KEY env var not set")
        sys.exit(1)

    days_ahead = config["settings"]["days_ahead"]
    max_fixtures = config["settings"]["max_fixtures_per_run"]
    all_fixtures = []

    for league in config["leagues"]:
        if not league["enabled"]:
            continue
        log.info(f"Fetching: {league['name']}")
        raw = fetch_league_fixtures(league["football_data_code"], api_key, days_ahead)
        for match in raw:
            all_fixtures.append(normalise(match, league))
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
