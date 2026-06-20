"""
Fetch pre-match odds from The Odds API for all configured leagues.
One API call per league (efficient credit usage).
Matches events to our fixtures by fuzzy team-name comparison.
Writes: data/odds_raw.json
"""

import argparse
import json
import logging
import os
import sys
from difflib import SequenceMatcher

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "config", "leagues.json")
FIXTURES_PATH = os.path.join(DATA_DIR, "fixtures_raw.json")
SAMPLE_PATH = os.path.join(DATA_DIR, "sample", "odds.json")
OUT_PATH = os.path.join(DATA_DIR, "odds_raw.json")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# Requesting h2h + totals + btts in one call per league
# Credit cost = number_of_markets_requested (approx 3 credits per league call)
MARKETS = "h2h,totals,btts"
REGIONS = "eu"
ODDS_FORMAT = "decimal"


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def best_match(target_home: str, target_away: str, events: list) -> dict | None:
    best_score = 0.0
    best_event = None
    for event in events:
        home_sim = name_similarity(target_home, event.get("home_team", ""))
        away_sim = name_similarity(target_away, event.get("away_team", ""))
        score = (home_sim + away_sim) / 2
        if score > best_score:
            best_score = score
            best_event = event
    if best_score >= 0.65:
        return best_event
    return None


def parse_bookmaker_odds(event: dict) -> dict:
    """
    Convert The Odds API event structure into a clean market dict.
    Returns: {"h2h": {"home": [...], "draw": [...], "away": [...]},
              "btts": {"yes": [...], "no": [...]},
              "totals": {"over_2.5": [...], "under_2.5": [...], ...}}
    """
    markets: dict = {"h2h": {}, "btts": {}, "totals": {}}

    for bookmaker in event.get("bookmakers", []):
        bk_name = bookmaker["key"]
        for market in bookmaker.get("markets", []):
            key = market["key"]
            if key == "h2h":
                outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                home_name = event["home_team"]
                away_name = event["away_team"]
                if home_name in outcomes:
                    markets["h2h"].setdefault("home", []).append(
                        {"bookmaker": bk_name, "odds": outcomes[home_name]}
                    )
                if away_name in outcomes:
                    markets["h2h"].setdefault("away", []).append(
                        {"bookmaker": bk_name, "odds": outcomes[away_name]}
                    )
                if "Draw" in outcomes:
                    markets["h2h"].setdefault("draw", []).append(
                        {"bookmaker": bk_name, "odds": outcomes["Draw"]}
                    )

            elif key == "btts":
                for outcome in market.get("outcomes", []):
                    slot = "yes" if outcome["name"].lower() == "yes" else "no"
                    markets["btts"].setdefault(slot, []).append(
                        {"bookmaker": bk_name, "odds": outcome["price"]}
                    )

            elif key == "totals":
                for outcome in market.get("outcomes", []):
                    name = outcome["name"].lower()  # "over" or "under"
                    point = outcome.get("point", 2.5)
                    slot = f"{name}_{point}"
                    markets["totals"].setdefault(slot, []).append(
                        {"bookmaker": bk_name, "odds": outcome["price"]}
                    )

    return markets


def list_active_sports(api_key: str) -> set:
    """Return the set of active sport keys from The Odds API."""
    try:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports",
            params={"apiKey": api_key},
            timeout=10
        )
        resp.raise_for_status()
        active = {s["key"] for s in resp.json() if s.get("active")}
        soccer = sorted(k for k in active if "soccer" in k)
        log.info(f"Active soccer sport keys: {soccer}")
        return active
    except requests.RequestException as e:
        log.warning(f"Could not list sports: {e}")
        return set()


def fetch_sport_odds(sport_key: str, api_key: str) -> list:
    params = {
        "apiKey": api_key,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": "iso",
    }
    try:
        resp = requests.get(f"{ODDS_API_BASE}/sports/{sport_key}/odds", params=params, timeout=15)
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")
        log.info(f"  Credits used: {used} | remaining: {remaining}")
        if resp.status_code == 401:
            log.error("Invalid Odds API key")
            return []
        if resp.status_code == 422:
            log.warning(f"Sport key '{sport_key}' not found or not active")
            return []
        if resp.status_code == 429:
            log.warning(f"Odds API rate limit hit for {sport_key}")
            return []
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error(f"Failed to fetch odds for {sport_key}: {e}")
        return []


def main(dry_run: bool = False):
    os.makedirs(DATA_DIR, exist_ok=True)

    if dry_run:
        log.info("DRY RUN — loading sample odds")
        with open(SAMPLE_PATH) as f:
            odds = json.load(f)
        with open(OUT_PATH, "w") as f:
            json.dump(odds, f, indent=2)
        log.info(f"Wrote sample odds → {OUT_PATH}")
        return odds

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        log.error("ODDS_API_KEY env var not set")
        sys.exit(1)

    with open(FIXTURES_PATH) as f:
        fixtures = json.load(f)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # Build a set of unique odds_api_keys from the fixtures we actually fetched
    sport_keys_needed = {f["odds_api_key"] for f in fixtures}

    # Discover what sport keys are actually active (costs 1 request, no credits)
    active_sports = list_active_sports(api_key)

    # Fetch odds per sport (one call per sport = very credit-efficient)
    events_by_sport: dict[str, list] = {}
    for league in config["leagues"]:
        sport_key = league["odds_api_key"]
        if sport_key not in sport_keys_needed:
            continue
        if not league["enabled"]:
            continue
        if active_sports and sport_key not in active_sports:
            log.warning(f"Skipping {league['name']}: '{sport_key}' not active on The Odds API")
            continue
        log.info(f"Fetching odds: {league['name']} ({sport_key})")
        events_by_sport[sport_key] = fetch_sport_odds(sport_key, api_key)

    # Match odds events to our fixtures by team name similarity
    odds_output: dict[str, dict] = {}
    for fix in fixtures:
        sport_key = fix["odds_api_key"]
        events = events_by_sport.get(sport_key, [])
        matched = best_match(fix["home_team"], fix["away_team"], events)
        if not matched:
            log.warning(f"No odds match for {fix['home_team']} vs {fix['away_team']}")
            continue
        markets = parse_bookmaker_odds(matched)
        odds_output[str(fix["fixture_id"])] = {
            "fixture_id": fix["fixture_id"],
            "home_team": fix["home_team"],
            "away_team": fix["away_team"],
            "event_id": matched.get("id"),
            "commence_time": matched.get("commence_time"),
            "markets": markets,
        }
        log.info(f"  Matched odds: {fix['home_team']} vs {fix['away_team']}")

    with open(OUT_PATH, "w") as f:
        json.dump(odds_output, f, indent=2)
    log.info(f"Saved odds for {len(odds_output)} fixtures → {OUT_PATH}")
    return odds_output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
