"""
Send a Discord webhook notification summarising newly flagged Tier-1 bets.
Only sends when there are Tier-1 bets in the latest output.
"""

import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(ROOT, "docs")
LATEST_PATH = os.path.join(DOCS_DIR, "latest.json")

DISCORD_COLOR_TIER1 = 0x00C853   # green
DISCORD_COLOR_TIER2 = 0xFFD600   # yellow
DISCORD_COLOR_INFO  = 0x5865F2   # blurple


def build_embeds(data: dict) -> list:
    embeds = []

    # Header embed
    embeds.append({
        "title": "BetQuant Scan Complete",
        "description": (
            f"**{data['total_fixtures']}** fixtures analysed\n"
            f"**{data['total_tier1_bets']}** Tier-1 edges found  |  "
            f"**{data['total_tier2_bets']}** Tier-2 picks\n"
            f"*Generated {data['generated_at'][:16].replace('T', ' ')} UTC*"
        ),
        "color": DISCORD_COLOR_INFO,
    })

    if data["total_tier1_bets"] == 0:
        embeds.append({
            "description": "No Tier-1 edges today. Tier-2 picks are visible on the dashboard.",
            "color": DISCORD_COLOR_TIER2,
        })
        return embeds

    for fixture in data.get("fixtures", []):
        tier1_bets = [b for b in fixture.get("bets", []) if b["tier"] == 1]
        if not tier1_bets:
            continue

        kickoff = fixture["kickoff_utc"][:16].replace("T", " ")
        fields = []

        for bet in tier1_bets[:6]:  # cap at 6 fields per embed
            edge_pct = f"+{bet['edge']*100:.1f}pp"
            fields.append({
                "name": f"✅ {bet['label']}  @{bet['best_odds']:.2f}",
                "value": (
                    f"Edge: **{edge_pct}**  |  Model: {bet['model_probability']:.1%}  "
                    f"vs Market: {bet['market_probability_devigged']:.1%}\n"
                    f"_{bet['reason']}_"
                ),
                "inline": False,
            })

        builder = fixture.get("builder")
        if builder and len(builder.get("legs", [])) >= 2:
            legs_text = "  +  ".join(
                f"{l['label']} @{l['odds']:.2f}" for l in builder["legs"]
            )
            corr = f"\n⚠️ {builder['correlation_note']}" if builder.get("correlation_note") else ""
            fields.append({
                "name": f"🔗 Builder ({builder['combined_odds']:.2f})",
                "value": legs_text + corr,
                "inline": False,
            })

        embeds.append({
            "title": f"{fixture['home_team']} vs {fixture['away_team']}",
            "description": f"{fixture['league']}  |  {kickoff} UTC",
            "color": DISCORD_COLOR_TIER1,
            "fields": fields,
        })

    return embeds


def send(webhook_url: str, embeds: list):
    # Discord allows max 10 embeds per message; chunk if needed
    for i in range(0, len(embeds), 10):
        payload = {"embeds": embeds[i:i+10]}
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            log.error(f"Discord webhook failed: {resp.status_code} {resp.text}")
        else:
            log.info(f"Discord notification sent (batch {i//10 + 1})")


def main(dry_run: bool = False):
    if dry_run:
        log.info("DRY RUN — skipping Discord notification (would send in production)")
        with open(LATEST_PATH) as f:
            data = json.load(f)
        embeds = build_embeds(data)
        log.info(f"Would send {len(embeds)} embed(s) to Discord")
        for e in embeds:
            log.info(f"  [{e.get('title', 'embed')}] {e.get('description', '')[:80]}")
        return

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping notification")
        return

    with open(LATEST_PATH) as f:
        data = json.load(f)

    embeds = build_embeds(data)
    send(webhook_url, embeds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
