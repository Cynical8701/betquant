"""
Statistical edge-detection analysis.

Methodology (per spec):
  1. Convert decimal odds to implied probability.
  2. De-vig by averaging across bookmakers before comparing.
  3. Build independent model probability using underlying stats (xG preferred over goals).
  4. Flag edge when model_prob >= market_prob_devigged + threshold.
  5. Tier 1 >= 5pp edge | Tier 2 >= 0pp | Tier 3 discarded.
  6. Correlation check before assembling bet builders.
  7. Builders: 2-4 legs, one per correlation group, per match only.

Writes: docs/latest.json
"""

import argparse
import json
import logging
import math
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
FIXTURES_PATH = os.path.join(DATA_DIR, "fixtures_raw.json")
STATS_PATH = os.path.join(DATA_DIR, "stats_raw.json")
ODDS_PATH = os.path.join(DATA_DIR, "odds_raw.json")
OUT_PATH = os.path.join(DOCS_DIR, "latest.json")

CONFIG_PATH = os.path.join(ROOT, "config", "leagues.json")

# ── Correlation groups: only one leg per group in a builder ──────────────────
CORR_GROUPS = {
    "goals_volume": ["btts_yes", "over_2.5_goals", "over_3.5_goals", "over_1.5_goals"],
    "goals_low":   ["btts_no", "under_2.5_goals", "under_1.5_goals"],
    "result":      ["home_win", "draw", "away_win"],
    "cards":       ["over_3.5_cards", "over_4.5_cards", "under_3.5_cards"],
    "goalscorer":  [],  # player legs share a group by team prefix added at runtime
}


def corr_group_for(market_key: str) -> str:
    for group, members in CORR_GROUPS.items():
        if market_key in members:
            return group
    if market_key.startswith("scorer_"):
        return "goalscorer"
    return market_key  # unique group → always combinable


# ── Poisson helpers ──────────────────────────────────────────────────────────

def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def p_over(threshold: float, lam: float) -> float:
    """P(X > threshold) where threshold is a .5 line."""
    return 1.0 - sum(poisson_pmf(k, lam) for k in range(int(threshold) + 1))


def p_btts(lam_home: float, lam_away: float) -> float:
    p_home_blank = math.exp(-lam_home)
    p_away_blank = math.exp(-lam_away)
    return (1 - p_home_blank) * (1 - p_away_blank)


# ── De-vig helpers ───────────────────────────────────────────────────────────

def avg_implied(quotes: list) -> float | None:
    """Average implied probability across bookmakers for one outcome."""
    valid = [1 / q["odds"] for q in quotes if q.get("odds", 0) > 1]
    return sum(valid) / len(valid) if valid else None


def devig_binary(yes_quotes: list, no_quotes: list) -> tuple[float | None, float | None]:
    """Return (fair_yes_prob, fair_no_prob) by normalising averaged implied probs."""
    p_yes = avg_implied(yes_quotes)
    p_no = avg_implied(no_quotes)
    if p_yes is None or p_no is None:
        return p_yes, p_no
    total = p_yes + p_no
    return p_yes / total, p_no / total


def devig_1x2(home_quotes: list, draw_quotes: list, away_quotes: list):
    ph = avg_implied(home_quotes)
    pd_ = avg_implied(draw_quotes)
    pa = avg_implied(away_quotes)
    parts = [p for p in [ph, pd_, pa] if p is not None]
    if not parts:
        return None, None, None
    total = sum(parts)
    return (ph / total if ph else None,
            pd_ / total if pd_ else None,
            pa / total if pa else None)


def best_odds(quotes: list) -> tuple[float, str]:
    """Return (highest decimal odds, bookmaker name) from a list of quotes."""
    if not quotes:
        return 0.0, ""
    best = max(quotes, key=lambda q: q.get("odds", 0))
    return best.get("odds", 0.0), best.get("bookmaker", "")


# ── Model probability builders ───────────────────────────────────────────────

def expected_goals(home_stats: dict, away_stats: dict, home_adv: float) -> tuple[float, float]:
    """
    Blend home attack / away defence and vice-versa.
    Prefer xG averages when available, fall back to raw goals.
    """
    def for_avg(s):
        return s["xg_for_avg"] if s.get("xg_available") and s.get("xg_for_avg") else s["goals_for_avg"]

    def against_avg(s):
        return s["xg_against_avg"] if s.get("xg_available") and s.get("xg_against_avg") else s["goals_against_avg"]

    lam_home = ((for_avg(home_stats) + against_avg(away_stats)) / 2) * home_adv
    lam_away = (for_avg(away_stats) + against_avg(home_stats)) / 2
    return max(lam_home, 0.1), max(lam_away, 0.1)


def model_cards(home_stats: dict, away_stats: dict) -> float:
    return home_stats.get("yellow_cards_avg", 1.8) + away_stats.get("yellow_cards_avg", 1.8)


# ── Edge calculation & tiering ───────────────────────────────────────────────

def tier(edge: float, config: dict) -> int:
    if edge >= config["settings"]["min_edge_tier1"]:
        return 1
    if edge >= config["settings"]["min_edge_tier2"]:
        return 2
    return 3


def make_bet(market_key: str, label: str, model_prob: float, fair_market_prob: float,
             quote_list: list, config: dict, reason: str) -> dict | None:
    if model_prob is None or fair_market_prob is None:
        return None
    edge = round(model_prob - fair_market_prob, 4)
    t = tier(edge, config)
    if t == 3:
        return None
    dec_odds, bk = best_odds(quote_list)
    return {
        "market": market_key,
        "label": label,
        "model_probability": round(model_prob, 4),
        "market_probability_devigged": round(fair_market_prob, 4),
        "edge": edge,
        "best_odds": dec_odds,
        "best_bookmaker": bk,
        "tier": t,
        "reason": reason,
    }


# ── Builder assembly ─────────────────────────────────────────────────────────

def assemble_builder(bets: list) -> dict | None:
    """
    Pick up to 4 legs, at most one per correlation group.
    Sort by edge descending so the strongest bets get priority.
    """
    sorted_bets = sorted(bets, key=lambda b: b["edge"], reverse=True)
    legs = []
    used_groups: set[str] = set()
    corr_notes: list[str] = []

    for bet in sorted_bets:
        group = corr_group_for(bet["market"])
        if group in used_groups:
            continue
        legs.append(bet)
        used_groups.add(group)
        if len(legs) == 4:
            break

    if len(legs) < 2:
        return None

    # Flag known correlated pairings even if they're in different groups
    goal_legs = [b["market"] for b in legs if b["market"] in CORR_GROUPS["goals_volume"]]
    if len(goal_legs) >= 2:
        corr_notes.append("Some goal-market legs are correlated — treat combined odds with caution.")

    combined_odds = 1.0
    for leg in legs:
        if leg["best_odds"] > 1:
            combined_odds *= leg["best_odds"]

    return {
        "legs": [{"label": b["label"], "market": b["market"],
                  "odds": b["best_odds"], "tier": b["tier"]} for b in legs],
        "combined_odds": round(combined_odds, 2),
        "correlation_note": " ".join(corr_notes) if corr_notes else None,
    }


# ── Per-fixture analysis ──────────────────────────────────────────────────────

def analyse_fixture(fix: dict, home_stats: dict, away_stats: dict,
                    odds_data: dict | None, config: dict) -> dict:
    home_adv = config["settings"]["home_advantage_factor"]
    lam_home, lam_away = expected_goals(home_stats, away_stats, home_adv)
    lam_total = lam_home + lam_away
    lam_cards = model_cards(home_stats, away_stats)

    using_xg = home_stats.get("xg_available") and away_stats.get("xg_available")
    stat_label = "xG" if using_xg else "goal avg"

    markets_odds = odds_data.get("markets", {}) if odds_data else {}
    h2h_odds = markets_odds.get("h2h", {})
    btts_odds = markets_odds.get("btts", {})
    totals_odds = markets_odds.get("totals", {})

    bets: list[dict] = []

    # ── BTTS ────────────────────────────────────────────────────────────────
    btts_model = p_btts(lam_home, lam_away)
    yes_q = btts_odds.get("yes", [])
    no_q = btts_odds.get("no", [])
    if yes_q and no_q:
        fair_yes, fair_no = devig_binary(yes_q, no_q)
        bet = make_bet(
            "btts_yes", "Both Teams to Score: Yes",
            btts_model, fair_yes, yes_q, config,
            f"{fix['home_team']} ({stat_label} {lam_home:.2f}) and {fix['away_team']} "
            f"({stat_label} {lam_away:.2f}) both project to score."
        )
        if bet:
            bets.append(bet)
        bet = make_bet(
            "btts_no", "Both Teams to Score: No",
            1 - btts_model, fair_no, no_q, config,
            f"One or both sides showing low attacking output "
            f"(home xpG {lam_home:.2f}, away xpG {lam_away:.2f})."
        )
        if bet:
            bets.append(bet)

    # ── Over/Under 2.5 goals ────────────────────────────────────────────────
    over25_model = p_over(2.5, lam_total)
    over25_q = totals_odds.get("over_2.5", [])
    under25_q = totals_odds.get("under_2.5", [])
    if over25_q and under25_q:
        fair_over, fair_under = devig_binary(over25_q, under25_q)
        bet = make_bet(
            "over_2.5_goals", "Over 2.5 Goals",
            over25_model, fair_over, over25_q, config,
            f"Model projects {lam_total:.2f} total goals ({fix['home_team']} "
            f"{lam_home:.2f} + {fix['away_team']} {lam_away:.2f})."
        )
        if bet:
            bets.append(bet)
        bet = make_bet(
            "under_2.5_goals", "Under 2.5 Goals",
            1 - over25_model, fair_under, under25_q, config,
            f"Low-scoring game expected: model projects {lam_total:.2f} total goals."
        )
        if bet:
            bets.append(bet)

    # ── Over/Under 3.5 goals ────────────────────────────────────────────────
    over35_model = p_over(3.5, lam_total)
    over35_q = totals_odds.get("over_3.5", [])
    under35_q = totals_odds.get("under_3.5", [])
    if over35_q and under35_q:
        fair_over35, fair_under35 = devig_binary(over35_q, under35_q)
        bet = make_bet(
            "over_3.5_goals", "Over 3.5 Goals",
            over35_model, fair_over35, over35_q, config,
            f"High-scoring model output ({lam_total:.2f} projected goals favours 4+ goals)."
        )
        if bet:
            bets.append(bet)

    # ── 1X2 result ──────────────────────────────────────────────────────────
    home_q = h2h_odds.get("home", [])
    draw_q = h2h_odds.get("draw", [])
    away_q = h2h_odds.get("away", [])
    if home_q and draw_q and away_q:
        # Simple Dixon-Coles approximation for win/draw/loss from Poisson marginals
        p_home_win = sum(
            poisson_pmf(h, lam_home) * sum(poisson_pmf(a, lam_away) for a in range(h))
            for h in range(15)
        )
        p_away_win = sum(
            poisson_pmf(a, lam_away) * sum(poisson_pmf(h, lam_home) for h in range(a))
            for a in range(15)
        )
        p_draw_val = 1 - p_home_win - p_away_win

        fair_home, fair_draw, fair_away = devig_1x2(home_q, draw_q, away_q)

        for mkey, mlabel, mprob, fprob, mquotes in [
            ("home_win", f"{fix['home_team']} Win", p_home_win, fair_home, home_q),
            ("draw",     "Draw",                   p_draw_val,  fair_draw,  draw_q),
            ("away_win", f"{fix['away_team']} Win", p_away_win, fair_away, away_q),
        ]:
            if fprob is None:
                continue
            reason = (
                f"Model: home {p_home_win:.1%} / draw {p_draw_val:.1%} / away {p_away_win:.1%} "
                f"from {stat_label} inputs."
            )
            bet = make_bet(mkey, mlabel, mprob, fprob, mquotes, config, reason)
            if bet:
                bets.append(bet)

    # ── Cards (Over 3.5 yellow cards) ───────────────────────────────────────
    over35c_model = p_over(3.5, lam_cards)
    over35c_q = totals_odds.get("over_3.5_cards", [])  # bookmaker-specific key varies
    # Also try alternate keys used by some bookmakers
    if not over35c_q:
        over35c_q = totals_odds.get("over_3.5", [])  # may overlap with goals — skip if same
    under35c_q = totals_odds.get("under_3.5_cards", [])
    if over35c_q and under35c_q:
        fair_over_c, _ = devig_binary(over35c_q, under35c_q)
        bet = make_bet(
            "over_3.5_cards", "Over 3.5 Cards",
            over35c_model, fair_over_c, over35c_q, config,
            f"{fix['home_team']} avg {home_stats['yellow_cards_avg']:.1f} yellows/game, "
            f"{fix['away_team']} avg {away_stats['yellow_cards_avg']:.1f}."
        )
        if bet:
            bets.append(bet)

    # ── Anytime goalscorer (top scorer per side) ─────────────────────────────
    for side, stats, lam in [
        ("home", home_stats, lam_home),
        ("away", away_stats, lam_away),
    ]:
        for scorer in (stats.get("top_scorers") or [])[:2]:
            if scorer.get("goals_per90", 0) < 0.2:
                continue
            # Approximate anytime scorer probability via Poisson shot share
            # P(player scores) ≈ 1 - e^(-(goals_per90 × 90/90))
            player_lam = scorer["goals_per90"]
            model_prob_scorer = 1 - math.exp(-player_lam)
            market_key = f"scorer_{scorer['player_id']}"
            # Odds API rarely has player scorer odds in the free tier;
            # we log the estimate for reference and skip if no odds found.
            scorer_name = scorer["name"]
            log.info(
                f"  {scorer_name}: model anytime scorer prob = {model_prob_scorer:.1%} "
                f"({scorer['goals_per90']:.2f} goals/90)"
            )

    # Sort: Tier 1 first, then by edge descending
    bets.sort(key=lambda b: (-b["tier"] == -1, -b["edge"]))
    bets.sort(key=lambda b: (b["tier"], -b["edge"]))

    builder = assemble_builder(bets)

    # When no odds are available, still show model projections as informational output
    p_home_win = sum(
        poisson_pmf(h, lam_home) * sum(poisson_pmf(a, lam_away) for a in range(h))
        for h in range(15)
    )
    p_away_win = sum(
        poisson_pmf(a, lam_away) * sum(poisson_pmf(h, lam_home) for h in range(a))
        for a in range(15)
    )
    p_draw_val = 1 - p_home_win - p_away_win

    model_projections = {
        "btts_yes": round(p_btts(lam_home, lam_away), 4),
        "over_2_5_goals": round(p_over(2.5, lam_total), 4),
        "over_1_5_goals": round(p_over(1.5, lam_total), 4),
        "home_win": round(p_home_win, 4),
        "draw": round(p_draw_val, 4),
        "away_win": round(p_away_win, 4),
    }

    return {
        "fixture_id": fix["fixture_id"],
        "home_team": fix["home_team"],
        "away_team": fix["away_team"],
        "kickoff_utc": fix["kickoff_utc"],
        "league": fix["league"],
        "league_country": fix.get("league_country", ""),
        "model_lambdas": {
            "home_expected_goals": round(lam_home, 3),
            "away_expected_goals": round(lam_away, 3),
            "total_expected_goals": round(lam_total, 3),
            "expected_cards": round(lam_cards, 3),
        },
        "model_projections": model_projections,
        "using_xg": using_xg,
        "bets": bets,
        "builder": builder,
        "odds_available": odds_data is not None,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main(dry_run: bool = False):
    os.makedirs(DOCS_DIR, exist_ok=True)

    with open(FIXTURES_PATH) as f:
        fixtures = json.load(f)
    with open(STATS_PATH) as f:
        stats = json.load(f)
    with open(ODDS_PATH) as f:
        odds = json.load(f)
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    results = []
    for fix in fixtures:
        home_s = stats.get(str(fix["home_team_id"]))
        away_s = stats.get(str(fix["away_team_id"]))
        if not home_s or not away_s:
            log.warning(f"Missing stats for {fix['home_team']} vs {fix['away_team']} — skipping")
            continue
        odds_data = odds.get(str(fix["fixture_id"]))
        if not odds_data:
            log.warning(f"No odds for {fix['home_team']} vs {fix['away_team']} — analysing without odds")

        log.info(f"Analysing: {fix['home_team']} vs {fix['away_team']}")
        result = analyse_fixture(fix, home_s, away_s, odds_data, config)
        results.append(result)

    tier1_total = sum(1 for r in results for b in r["bets"] if b["tier"] == 1)
    tier2_total = sum(1 for r in results for b in r["bets"] if b["tier"] == 2)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "total_fixtures": len(results),
        "total_tier1_bets": tier1_total,
        "total_tier2_bets": tier2_total,
        "fixtures": results,
    }

    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Written → {OUT_PATH}  ({tier1_total} Tier-1, {tier2_total} Tier-2 bets)")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
