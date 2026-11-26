# CLAUDE.md: FanDuel odds helper

Paul uses this repo from his phone, in Claude Code cloud sessions. He asks for odds in plain English.
Your job is to run `fd_odds.py`, show him the results and send him the CSV.

## Hard rules
- **Read-only.** This tool only reads odds. Never place, suggest placing, or automate bets.
- **Never print, log, or commit the API key.** It comes from the `ODDS_API_KEY` environment variable,
  which is set in the cloud environment.
- **Don't commit generated output.** `odds/` is gitignored; leave it that way.

## How to answer odds requests
1. Map the request to a command:
   - "board", "lines", "today's games" → `python3 fd_odds.py board <sport> [--today] [--upcoming]` (use `--upcoming` for "pregame only" / "games that haven't started"; the `live` column marks in-game lines - say so in the summary)
   - "props", "player props", or a player name → `python3 fd_odds.py props <sport> "<team>" [--today] [--player "<name>"]`
     (the game is found by **team** name; if he only names a player, pick that player's team)
   - "alt lines", "alternate spreads/totals" → `python3 fd_odds.py alts <sport> "<team>" [--today]`
   - Add `--today` to props/alts when he means tonight's game. Team matching prefers whole names
     ("Kentucky" = Kentucky Wildcats, not Eastern Kentucky). If the tool says a name is ambiguous it has
     spent no requests: re-run with the fuller name (e.g. "Los Angeles Lakers") and say which you picked.
   - "how many requests left", "budget" → `python3 fd_odds.py budget`
   - Sports: nfl, nba, nhl, ncaaf, ncaab, mlb, epl. Props only for nfl, ncaaf, nba, ncaab, nhl.
2. Run it. Then reply in this order:
   - a short summary first;
   - the table as markdown, trimmed to what he asked about;
   - FanDuel-missing games, if any;
   - the `remaining` request count.
3. Send the CSV the tool wrote with `SendUserFile` (`display: "attach"`) so he can open or share it.

## cbb_check.py (college basketball model check)
- **Never run `cbb_check.py` in a cloud session.** It downloads Bart Torvik's files from barttorvik.com, and that
  site's robots.txt blocks Anthropic's agents (`ClaudeBot`, `anthropic-ai`) from the whole site. Paul runs it on
  his Mac. In a cloud session, only build `games.txt` from `fd_odds.py board ncaab --today` and print it for him.
- `games.txt` format: `Away Team @ Home Team, awayPrice, homePrice[, N]` (N = neutral site), games not yet
  started only, FanDuel names exactly.

## Budget
- The free tier is 500 requests a month.
- `board` costs 3 requests. `props` and `alts` cost 1 per market returned. `budget` and the game lookup are free.
- Responses are cached in `odds/cache/` for 10 minutes, so a repeat within one session is free.
  Each cloud session starts with an empty cache.
- If `remaining` drops under 50, mention it.

## If something fails
- **"No API key found":** `ODDS_API_KEY` isn't set in this session's environment. Paul needs the
  `FanDuel Odds` environment selected, or the variable added to that environment's settings.
- **Network or proxy error:** the environment's network access must allow `api.the-odds-api.com`.
- **401 or quota error:** relay the message plainly.
