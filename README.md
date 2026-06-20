# BetQuant

Automated football statistical bet scanner. Runs daily on GitHub Actions, publishes results to a GitHub Pages dashboard, and pings you on Discord when Tier-1 edges are found.

**Free infrastructure only** — no paid hosting, no paid database, no paid odds feed.

---

## How it works

```
GitHub Actions (daily cron)
  │
  ├─ fetch_fixtures.py   → API-Football (fixtures for next 7 days)
  ├─ fetch_stats.py      → API-Football (team xG, shots, cards; player goals per 90)
  ├─ fetch_odds.py       → The Odds API  (pre-match odds: 1X2, BTTS, totals)
  ├─ analyze.py          → Poisson model + de-vig + edge detection → docs/latest.json
  └─ notify.py           → Discord webhook (Tier-1 bets only)
                                │
                          GitHub Pages → dashboard
```

**Statistical methodology in brief:**
- Expected goals estimated via blended home/away xG averages (Poisson model)
- Odds de-vigged by averaging implied probabilities across bookmakers
- Tier 1 = model probability at least 5 percentage points above de-vigged market
- Tier 2 = model at or above market but below 5pp — fair price, no real edge
- Bet builders: 2–4 legs per match, one per correlation group, correlation clearly labelled

---

## Setup (one-time)

### Step 1 — Create free accounts and get API keys

| Service | URL | What to get | Free tier |
|---|---|---|---|
| API-Football | [rapidapi.com/api-sports/api/api-football](https://rapidapi.com/api-sports/api/api-football) | RapidAPI key (X-RapidAPI-Key) | 100 req/day |
| The Odds API | [the-odds-api.com](https://the-odds-api.com) | API key | 500 credits/month |
| Discord | discord.com | Webhook URL (see below) | Free |

**Discord webhook setup:**
1. Open your Discord server → right-click a channel → **Edit Channel**
2. **Integrations** → **Webhooks** → **New Webhook**
3. Copy the webhook URL

### Step 2 — Create your GitHub repo

1. Create a **public** repository on GitHub (GitHub Pages requires public on the free plan)
2. Push this entire folder to it:
   ```bash
   git init
   git add .
   git commit -m "initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```

### Step 3 — Add secrets to GitHub

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these three secrets:

| Secret name | Value |
|---|---|
| `API_FOOTBALL_KEY` | Your RapidAPI key |
| `ODDS_API_KEY` | Your The Odds API key |
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL |

### Step 4 — Enable GitHub Pages

Repo → **Settings** → **Pages** → Source: **Deploy from a branch** → Branch: `main` / folder: `/docs` → **Save**

Your dashboard will be live at `https://YOUR_USERNAME.github.io/YOUR_REPO/`

### Step 5 — Trigger the first run

Repo → **Actions** → **BetQuant Daily Scan** → **Run workflow** → **Run workflow**

After it completes (~2 minutes), refresh your GitHub Pages URL.

---

## Testing locally (dry-run)

No API keys required. Uses sample data in `data/sample/`.

```bash
pip install -r requirements.txt

python scripts/fetch_fixtures.py --dry-run
python scripts/fetch_stats.py    --dry-run
python scripts/fetch_odds.py     --dry-run
python scripts/analyze.py        --dry-run
python scripts/notify.py         --dry-run
```

Then open `docs/index.html` directly in a browser to see the dashboard.

---

## Adding or removing leagues

Edit `config/leagues.json`. Each entry has an `"enabled"` flag — set to `false` to pause a league without deleting it.

```json
{
  "name": "Ligue 1",
  "country": "France",
  "api_football_id": 61,
  "odds_api_key": "soccer_france_ligue_one",
  "enabled": true
}
```

**Common league IDs:**

| League | api_football_id | odds_api_key |
|---|---|---|
| Premier League | 39 | soccer_epl |
| La Liga | 140 | soccer_spain_la_liga |
| Bundesliga | 78 | soccer_germany_bundesliga |
| Serie A | 135 | soccer_italy_serie_a |
| Ligue 1 | 61 | soccer_france_ligue_one |
| Champions League | 2 | soccer_uefa_champs_league |
| Europa League | 3 | soccer_uefa_europa_league |
| Championship | 40 | soccer_england_championship |

---

## Adjusting the edge threshold

In `config/leagues.json` → `"settings"`:

```json
"min_edge_tier1": 0.05,   ← 5 percentage points (default)
"min_edge_tier2": 0.0,    ← anything at or above market fair value
"home_advantage_factor": 1.08
```

Raise `min_edge_tier1` to 0.07 or 0.10 to filter only the strongest signals.

---

## API credit budget (free tiers)

| API | Daily budget | Typical usage |
|---|---|---|
| API-Football | 100 req/day | ~2 req/team × up to 20 teams = ~40–60 req |
| The Odds API | 500 credits/month (~16/day) | ~1–3 credits per league per call |

The scan script logs a running API call count. If you approach the limit, reduce `max_fixtures_per_run` in `config/leagues.json`.

---

## Responsible use

BetQuant is a **statistical decision-support tool**, not a guarantee of profit.

- Model probabilities are estimates derived from historical averages. They carry uncertainty and will be wrong a meaningful percentage of the time.
- A positive edge in the model does not mean a bet will win. It means the model believes the true probability is higher than the market implies — over a large sample this may be profitable, but individual results are random.
- Betting carries **real financial risk**. Never stake more than you can afford to lose. Set a stake limit and stick to it.
- This tool does not constitute financial or betting advice. Use it to inform your own decisions, not to automate them.
- In some jurisdictions, sports betting is restricted or prohibited. Know the laws where you live.

---

## Roadmap / known limitations

- **Corners market**: requires per-match fixture stats (extra API calls). Deferred to v2 — budget the extra ~20 req/day and add an `/fixtures/statistics` call per recent match.
- **xG at season level**: API-Football's free tier returns season averages for goals/shots but not xG. xG is available per-fixture via `/fixtures/statistics`. Current model uses goals-per-game when xG is unavailable and labels the source on the dashboard.
- **Player anytime scorer odds**: The Odds API's free tier rarely returns player prop markets. Model estimates are computed and logged but not shown as flagged bets without matching odds.
- **Referee card tendencies**: Card model currently uses team averages only. Adding referee ID → historical card rate would improve card market accuracy.
