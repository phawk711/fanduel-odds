# fanduel-odds

`fd_odds.py` pulls current **FanDuel** odds from [The Odds API](https://the-odds-api.com) (v4) and writes CSVs to `odds/`.
**Read-only. It never places bets.**

## Using it from your phone (cloud)

1. Start a Claude Code session with this repo and the **FanDuel Odds** environment selected.
2. Ask for odds in plain English, like "NFL board for today", "Chiefs player props", or "Lakers alt lines".

### One-time cloud environment setup
- **Network access:** allow `api.the-odds-api.com`. Keep "Allow package managers" ticked.
- **Environment variable:** `ODDS_API_KEY=<your key>`. Never commit the key to the repo.

## Commands

```bash
python3 fd_odds.py board nfl [--today]     # ML / spread / total for every game -> odds/nfl_board_<ts>.csv
python3 fd_odds.py props nfl "chiefs"      # player props for one game -> odds/nfl_props_<away>_<home>_<ts>.csv
python3 fd_odds.py alts nba "lakers"       # alternate spreads + totals -> odds/nba_alts_<away>_<home>_<ts>.csv
python3 fd_odds.py budget                  # requests used / remaining (free call)
```

Sports: `nfl nba nhl ncaaf ncaab mlb epl`. Props support nfl, ncaaf, nba, ncaab, nhl.
`props` finds the game by team name. Add `--player "mahomes"` to filter to one player.

## Running locally (optional)

```bash
python3 -m venv .venv && .venv/bin/pip install requests
echo "YOUR_KEY" > .odds_api_key      # or: export ODDS_API_KEY=YOUR_KEY
.venv/bin/python fd_odds.py budget
```

## Notes
- Responses are cached in `odds/cache/` for 10 minutes. A repeat run in that window costs nothing.
- Cost per fresh call: `board` = 3 requests. `props`/`alts` = 1 per market FanDuel returns.
  The game lookup and `budget` are free.
- Times are America/Chicago. EPL boards get an extra `draw_ml` column.
- Games FanDuel hasn't priced are kept as blank rows and listed in the terminal.
