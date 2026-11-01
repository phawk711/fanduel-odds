#!/usr/bin/env python3
"""fd_odds.py - pull current FanDuel odds from The Odds API (v4) into CSV files.

READ-ONLY: this tool only reads odds. It never places bets.

Commands:
  python3 fd_odds.py board <sport> [--today] [--upcoming]
  python3 fd_odds.py props <sport> "<team substring>" [--player "<name substring>"]
  python3 fd_odds.py alts  <sport> "<team substring>"
  python3 fd_odds.py budget
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://api.the-odds-api.com"
BASE_PARAMS = {"regions": "us", "bookmakers": "fanduel", "oddsFormat": "american"}
CACHE_MAX_AGE_SEC = 10 * 60

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "odds")
CACHE_DIR = os.path.join(OUT_DIR, "cache")
KEY_FILE = os.path.join(HERE, ".odds_api_key")

SPORTS = {
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
    "nhl": "icehockey_nhl",
    "ncaaf": "americanfootball_ncaaf",
    "ncaab": "basketball_ncaab",
    "mlb": "baseball_mlb",
    "epl": "soccer_epl",
}

FOOTBALL_PROPS = ["player_pass_yds", "player_rush_yds", "player_reception_yds",
                  "player_receptions", "player_anytime_td"]
BASKETBALL_PROPS = ["player_points", "player_rebounds", "player_assists"]
HOCKEY_PROPS = ["player_shots_on_goal", "player_points"]
PROP_MARKETS = {
    "nfl": FOOTBALL_PROPS, "ncaaf": FOOTBALL_PROPS,
    "nba": BASKETBALL_PROPS, "ncaab": BASKETBALL_PROPS,
    "nhl": HOCKEY_PROPS,
}


class OddsError(Exception):
    """A user-facing error with a clear message."""


# --------------------------------------------------------------------------- time
def _chicago_tz():
    """America/Chicago via zoneinfo; fall back to US DST rules if tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Chicago")
    except Exception:
        return None


_CT = _chicago_tz()


def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def to_chicago(dt_utc):
    if _CT is not None:
        return dt_utc.astimezone(_CT)
    # Fallback: CDT (UTC-5) from 2nd Sun Mar 2am local to 1st Sun Nov 2am local, else CST (UTC-6).
    y = dt_utc.year
    dst_start = _nth_sunday(y, 3, 2).replace(hour=8, tzinfo=timezone.utc)   # 2am CST = 08Z
    dst_end = _nth_sunday(y, 11, 1).replace(hour=7, tzinfo=timezone.utc)    # 2am CDT = 07Z
    off = -5 if dst_start <= dt_utc < dst_end else -6
    return dt_utc.astimezone(timezone(timedelta(hours=off)))


def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def now_ct():
    return to_chicago(datetime.now(timezone.utc))


def stamp():
    return now_ct().strftime("%Y%m%d_%H%M")


# --------------------------------------------------------------------------- api
def load_key():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key and os.path.isfile(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise OddsError("No API key found. Set ODDS_API_KEY or put the key in "
                        f"{KEY_FILE} (see README.md).")
    return key


def _cache_key(path, params):
    p = {k: v for k, v in params.items() if k != "apiKey"}  # never write the key to disk
    raw = path + "?" + "&".join(f"{k}={p[k]}" for k in sorted(p))
    slug = re.sub(r"[^A-Za-z0-9]+", "_", path.strip("/"))[:80]
    return f"{slug}_{hashlib.sha1(raw.encode()).hexdigest()[:10]}"


def _find_cache(key):
    if not os.path.isdir(CACHE_DIR):
        return None
    best = None
    for name in os.listdir(CACHE_DIR):
        if name.startswith(key + "_") and name.endswith(".json"):
            full = os.path.join(CACHE_DIR, name)
            if best is None or os.path.getmtime(full) > os.path.getmtime(best):
                best = full
    if best and time.time() - os.path.getmtime(best) < CACHE_MAX_AGE_SEC:
        return best
    return None


def _print_budget(headers, cached=False, cached_age=None):
    rem = headers.get("x-requests-remaining", "?")
    used = headers.get("x-requests-used", "?")
    last = headers.get("x-requests-last", "?")
    if cached:
        print(f"[cache hit, {cached_age:.0f}s old - 0 requests spent] "
              f"remaining as of that call: {rem}")
    else:
        print(f"[requests] this call cost: {last} | used: {used} | remaining: {rem}")


def api_get(path, params=None, use_cache=True):
    params = dict(params or {})
    key = _cache_key(path, params)
    if use_cache:
        hit = _find_cache(key)
        if hit:
            with open(hit, encoding="utf-8") as f:
                blob = json.load(f)
            _print_budget(blob.get("headers", {}), cached=True,
                          cached_age=time.time() - os.path.getmtime(hit))
            return blob["data"], blob.get("headers", {})

    params["apiKey"] = load_key()
    try:
        r = requests.get(API_BASE + path, params=params, timeout=20)
    except requests.RequestException as e:
        # Strip the URL (which contains the key) from the message.
        raise OddsError(f"Network error talking to The Odds API: {type(e).__name__}") from None

    headers = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith("x-requests")}
    try:
        data = r.json()
    except ValueError:
        data = None

    if r.status_code != 200:
        msg = (data or {}).get("message", "") if isinstance(data, dict) else ""
        code = (data or {}).get("error_code", "") if isinstance(data, dict) else ""
        blob = f"{code} {msg}".upper()
        if "OUT_OF_USAGE" in blob or "QUOTA" in blob or headers.get("x-requests-remaining") == "0":
            raise OddsError("Quota exceeded: your monthly request budget is used up "
                            f"(API said: {msg or code}).")
        if r.status_code == 401:
            raise OddsError(f"Bad or inactive API key (401). API said: {msg or code or 'unauthorized'}")
        if r.status_code == 429:
            raise OddsError("Rate limited (429) - too many requests too fast. Wait a few seconds and retry.")
        if r.status_code == 422:
            raise OddsError(f"Request rejected (422): {msg or code}")
        if r.status_code == 404:
            raise OddsError(f"Not found (404): {msg or path}")
        raise OddsError(f"HTTP {r.status_code} from The Odds API: {msg or code}")

    _print_budget(headers)
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fname = os.path.join(CACHE_DIR, f"{key}_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({"path": path, "params": {k: v for k, v in params.items() if k != "apiKey"},
                       "headers": headers, "data": data}, f)
    return data, headers


# --------------------------------------------------------------------------- helpers
def sport_key(sport):
    s = sport.lower()
    if s not in SPORTS:
        raise OddsError(f"Unknown sport '{sport}'. Use one of: {', '.join(SPORTS)}")
    return SPORTS[s]


def fanduel(game):
    for b in game.get("bookmakers") or []:
        if b.get("key") == "fanduel":
            return b
    return None


def market(book, key):
    for m in (book or {}).get("markets", []):
        if m.get("key") == key:
            return m.get("outcomes", [])
    return []


def fmt_price(p):
    if p is None or p == "":
        return ""
    p = int(round(float(p)))
    return f"+{p}" if p > 0 else str(p)


def fmt_point(x, signed=False):
    if x is None or x == "":
        return ""
    x = float(x)
    s = f"{x:g}"
    return f"+{s}" if signed and x > 0 else s


def safe(s):
    return re.sub(r"[^A-Za-z0-9]+", "", s or "")[:20] or "x"


def write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows -> {os.path.relpath(path)}")


def print_table(cols, rows, headers=None):
    headers = headers or cols
    data = [[str(r.get(c, "")) for c in cols] for r in rows]
    widths = [max([len(h)] + [len(d[i]) for d in data]) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for d in data:
        print("  ".join(v.ljust(w) for v, w in zip(d, widths)))


def ct_str(iso):
    return to_chicago(parse_utc(iso)).strftime("%Y-%m-%d %H:%M CT")


def find_event(sk, needle):
    """Match a game by team-name substring using /events (does not cost quota)."""
    events, _ = api_get(f"/v4/sports/{sk}/events", {})
    n = needle.lower()
    hits = [e for e in events if n in e["home_team"].lower() or n in e["away_team"].lower()]
    if not hits:
        teams = sorted({t for e in events for t in (e["home_team"], e["away_team"])})
        raise OddsError(f"No upcoming game matches '{needle}'. "
                        f"{len(events)} games listed; teams include: {', '.join(teams[:30])}"
                        + (" ..." if len(teams) > 30 else ""))
    hits.sort(key=lambda e: e["commence_time"])
    ev = hits[0]
    if len(hits) > 1:
        others = "; ".join(f"{e['away_team']} @ {e['home_team']} ({ct_str(e['commence_time'])})"
                           for e in hits[1:4])
        print(f"Note: {len(hits)} games match '{needle}'. Using the soonest. Others: {others}")
    live = " - IN PROGRESS, these are live in-game lines" if parse_utc(ev["commence_time"]) <= datetime.now(timezone.utc) else ""
    print(f"Game: {ev['away_team']} @ {ev['home_team']}  ({ct_str(ev['commence_time'])}){live}")
    return ev


def event_odds(sk, ev, markets):
    params = dict(BASE_PARAMS, markets=",".join(markets))
    data, _ = api_get(f"/v4/sports/{sk}/events/{ev['id']}/odds", params)
    book = fanduel(data)
    if not book:
        print(f"FanDuel has no odds posted for {ev['away_team']} @ {ev['home_team']} "
              f"in markets: {', '.join(markets)}")
    return book


# --------------------------------------------------------------------------- commands
BOARD_COLS = ["game_id", "commence_time_ct", "away", "home", "away_ml", "home_ml",
              "spread_home", "spread_home_price", "spread_away_price",
              "total", "over_price", "under_price", "live"]


def cmd_board(args):
    sk = sport_key(args.sport)
    params = dict(BASE_PARAMS, markets="h2h,spreads,totals")
    games, _ = api_get(f"/v4/sports/{sk}/odds", params)
    cols = BOARD_COLS + (["draw_ml"] if args.sport.lower() == "epl" else [])

    today = now_ct().date()
    now_utc = datetime.now(timezone.utc)
    rows, missing, skipped_live = [], [], 0
    for g in sorted(games, key=lambda g: g["commence_time"]):
        start = to_chicago(parse_utc(g["commence_time"]))
        if args.today and start.date() != today:
            continue
        is_live = parse_utc(g["commence_time"]) <= now_utc
        if args.upcoming and is_live:
            skipped_live += 1
            continue
        home, away = g["home_team"], g["away_team"]
        row = {"game_id": g["id"], "commence_time_ct": start.strftime("%Y-%m-%d %H:%M"),
               "away": away, "home": home, "live": "yes" if is_live else "no"}
        book = fanduel(g)
        if not book:
            missing.append(f"{away} @ {home} ({start:%a %H:%M} CT)")
        for o in market(book, "h2h"):
            if o["name"] == home:
                row["home_ml"] = fmt_price(o["price"])
            elif o["name"] == away:
                row["away_ml"] = fmt_price(o["price"])
            elif o["name"].lower() == "draw":
                row["draw_ml"] = fmt_price(o["price"])
        for o in market(book, "spreads"):
            if o["name"] == home:
                row["spread_home"] = fmt_point(o.get("point"), signed=True)
                row["spread_home_price"] = fmt_price(o["price"])
            elif o["name"] == away:
                row["spread_away_price"] = fmt_price(o["price"])
        for o in market(book, "totals"):
            if o["name"] == "Over":
                row["total"] = fmt_point(o.get("point"))
                row["over_price"] = fmt_price(o["price"])
            elif o["name"] == "Under":
                row["under_price"] = fmt_price(o["price"])
        rows.append(row)

    if skipped_live:
        print(f"Skipped {skipped_live} game(s) already in progress (--upcoming).")
    if not rows:
        scope = "today (Chicago time)" if args.today else "right now"
        extra = " that haven't started yet" if args.upcoming else ""
        print(f"No {args.sport.upper()} games{extra} listed {scope}.")
        return
    tag = ("_today" if args.today else "") + ("_upcoming" if args.upcoming else "")
    path = os.path.join(OUT_DIR, f"{args.sport.lower()}_board{tag}_{stamp()}.csv")
    print()
    tcols = ["live", "commence_time_ct", "away", "home", "away_ml", "home_ml", "spread_home",
             "spread_home_price", "spread_away_price", "total", "over_price", "under_price"]
    theads = ["live", "start CT", "away", "home", "a ML", "h ML", "h sprd", "h px", "a px",
              "total", "o px", "u px"]
    if "draw_ml" in cols:
        tcols.append("draw_ml"); theads.append("draw")
    print_table(tcols, rows, theads)
    if missing:
        print(f"\nFanDuel has NO odds for {len(missing)} game(s) (kept as blank rows):")
        for m in missing:
            print(f"  - {m}")
    write_csv(path, cols, rows)


PROP_COLS = ["game", "player", "market", "line", "over_price", "under_price"]


def cmd_props(args):
    sport = args.sport.lower()
    sk = sport_key(sport)
    if sport not in PROP_MARKETS:
        raise OddsError(f"Props aren't configured for {sport}. Supported: {', '.join(PROP_MARKETS)}")
    ev = find_event(sk, args.query)
    book = event_odds(sk, ev, PROP_MARKETS[sport])
    game = f"{ev['away_team']} @ {ev['home_team']}"
    grouped = {}
    for m in (book or {}).get("markets", []):
        for o in m.get("outcomes", []):
            player = o.get("description") or o.get("name")
            k = (player, m["key"], o.get("point"))
            row = grouped.setdefault(k, {"game": game, "player": player, "market": m["key"],
                                         "line": fmt_point(o.get("point"))})
            side = (o.get("name") or "").lower()
            if side in ("under", "no"):
                row["under_price"] = fmt_price(o["price"])
            else:  # Over / Yes (anytime TD has no line, just a Yes price)
                row["over_price"] = fmt_price(o["price"])
    rows = sorted(grouped.values(), key=lambda r: (r["market"], r["player"], float(r["line"] or 0)))
    if args.player:
        rows = [r for r in rows if args.player.lower() in r["player"].lower()]
    if not rows:
        print("No FanDuel player props found for this game"
              + (f" matching player '{args.player}'." if args.player else "."))
        return
    print()
    print_table(["player", "market", "line", "over_price", "under_price"], rows,
                ["player", "market", "line", "over/yes", "under/no"])
    path = os.path.join(OUT_DIR, f"{sport}_props_{safe(ev['away_team'])}_{safe(ev['home_team'])}_{stamp()}.csv")
    write_csv(path, PROP_COLS, rows)


ALT_COLS = ["game", "market", "line", "home_or_over_price", "away_or_under_price"]


def cmd_alts(args):
    sport = args.sport.lower()
    sk = sport_key(sport)
    ev = find_event(sk, args.query)
    book = event_odds(sk, ev, ["alternate_spreads", "alternate_totals"])
    home, away = ev["home_team"], ev["away_team"]
    game = f"{away} @ {home}"
    rows = {}
    # Spreads: one row per home line; the away price is the mirrored line (-point).
    sp = market(book, "alternate_spreads")
    away_px = {float(o["point"]): o["price"] for o in sp if o["name"] == away and o.get("point") is not None}
    for o in sp:
        if o["name"] == home and o.get("point") is not None:
            pt = float(o["point"])
            rows[("spread", pt)] = {"game": game, "market": f"alt_spread ({home})",
                                    "line": fmt_point(pt, signed=True),
                                    "home_or_over_price": fmt_price(o["price"]),
                                    "away_or_under_price": fmt_price(away_px.get(-pt))}
    for pt, px in away_px.items():  # away lines with no home mirror
        if ("spread", -pt) not in rows:
            rows[("spread", -pt)] = {"game": game, "market": f"alt_spread ({home})",
                                     "line": fmt_point(-pt, signed=True),
                                     "away_or_under_price": fmt_price(px)}
    for o in market(book, "alternate_totals"):
        if o.get("point") is None:
            continue
        pt = float(o["point"])
        r = rows.setdefault(("total", pt), {"game": game, "market": "alt_total",
                                            "line": fmt_point(pt)})
        if o["name"] == "Over":
            r["home_or_over_price"] = fmt_price(o["price"])
        elif o["name"] == "Under":
            r["away_or_under_price"] = fmt_price(o["price"])
    out = [rows[k] for k in sorted(rows)]
    if not out:
        print("No FanDuel alternate lines found for this game.")
        return
    print()
    print_table(["market", "line", "home_or_over_price", "away_or_under_price"], out,
                ["market", "line (home sprd / total)", "home/over", "away/under"])
    path = os.path.join(OUT_DIR, f"{sport}_alts_{safe(away)}_{safe(home)}_{stamp()}.csv")
    write_csv(path, ALT_COLS, out)


def cmd_budget(args):
    _, headers = api_get("/v4/sports", {}, use_cache=False)
    print(f"Requests used: {headers.get('x-requests-used', '?')}  |  "
          f"remaining: {headers.get('x-requests-remaining', '?')}  (free tier = 500/month)")


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only FanDuel odds -> CSV (The Odds API v4).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("board", help="moneyline / spread / total for every game")
    b.add_argument("sport", help=", ".join(SPORTS))
    b.add_argument("--today", action="store_true", help="only games starting today (Chicago)")
    b.add_argument("--upcoming", action="store_true", help="skip games already in progress")
    b.set_defaults(func=cmd_board)

    p = sub.add_parser("props", help="player props for one game")
    p.add_argument("sport")
    p.add_argument("query", help="team name substring used to find the game")
    p.add_argument("--player", help="optional: keep only players matching this substring")
    p.set_defaults(func=cmd_props)

    a = sub.add_parser("alts", help="alternate spreads/totals for one game")
    a.add_argument("sport")
    a.add_argument("query", help="team name substring")
    a.set_defaults(func=cmd_alts)

    g = sub.add_parser("budget", help="show requests used/remaining (free call)")
    g.set_defaults(func=cmd_budget)

    args = ap.parse_args(argv)
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        args.func(args)
    except OddsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
