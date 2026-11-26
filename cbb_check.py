#!/usr/bin/env python3
"""
cbb_check.py  --  college basketball moneyline check built on Bart Torvik's published data.

Data (bulk files Torvik publishes for download; one fetch per file per day, cached locally):
  https://barttorvik.com/2026_team_results.csv   team ratings (AdjOE, AdjDE, AdjTempo, Barthag)
  https://barttorvik.com/2026_results.csv        Torvik's own daily game projections (optional)

Three numbers per game:
  ours    : efficiency-margin model from the ratings (tempo-adjusted, 3.5 pt home court, N(0, 10.5))
  torvik  : Torvik's projection from 2026_results.csv if it parses, otherwise a Barthag log5 estimate
  (feed)  : the sports-feed probability is added by Claude in chat, not by this script

Usage:
  python3 cbb_check.py --inspect                    # show what the rating file looks like / what matched
  python3 cbb_check.py games.txt                    # evaluate a slate
  python3 cbb_check.py games.txt --refresh          # force re-download of Torvik files
  python3 cbb_check.py "Oklahoma Sooners @ Purdue Boilermakers" +200 -250

games.txt, one game per line (FanDuel team names are fine, mascots get stripped):
  Away Team @ Home Team, awayPrice, homePrice[, N]       N = neutral site
  Oklahoma Sooners @ Purdue Boilermakers, +200, -250
  Arkansas Razorbacks @ Michigan State Spartans, +120, -142, N

Rules baked in (from the tracker):
  - Illinois schools are excluded outright.
  - Parlay-eligible leg: price -170 or heavier, model prob >= implied + 3 pts,
    ours and torvik within 10 pts of each other, both teams rated.
  - Early season: ratings are part preseason prior through roughly mid-December. Printed every run.
"""

import argparse
import csv
import difflib
import io
import json
import math
import os
import re
import sys
import time
from datetime import date

SEASON = 2026
BASE = "https://barttorvik.com"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "torvik")
CACHE_HOURS = 20
_LAST_FETCH = 0.0

HOME_COURT = 3.5      # points, full HCA (Torvik/KenPom-style values run 3.0 to 3.75)
MARGIN_SD = 10.5      # points, std dev of CBB margin around the projection
EDGE_MIN = 0.03       # model prob must beat implied prob by this much
AGREE_MAX = 0.10      # ours vs torvik disagreement that kills a leg
PARLAY_MAX_PRICE = -170

ILLINOIS = {
    # exact Torvik spellings (matched after normalization, so "Illinois St." == "Illinois State")
    "illinois", "northwestern", "depaul", "loyola chicago", "illinois st.", "bradley",
    "southern illinois", "siu edwardsville", "siue", "northern illinois", "illinois chicago",
    "uic", "chicago st.", "eastern illinois", "western illinois", "illinois-chicago",
}
# Northwestern St. (Louisiana) is NOT on this list and must not be caught by it.

# FanDuel / feed spellings -> Torvik spellings (normalized form). Extend as mismatches show up.
ALIASES = {
    "uconn": "connecticut",
    "connecticut huskies": "connecticut",
    "nc state": "n.c. state", "north carolina state": "n.c. state",
    "ole miss": "mississippi",
    "pitt": "pittsburgh",
    "miami fl": "miami fl", "miami (fl)": "miami fl", "miami florida": "miami fl",
    "miami oh": "miami oh", "miami (oh)": "miami oh", "miami ohio": "miami oh",
    "saint marys": "saint mary's", "st marys": "saint mary's",
    "st johns": "st. john's", "saint johns": "st. john's",
    "st josephs": "saint joseph's", "saint josephs": "saint joseph's",
    "st bonaventure": "st. bonaventure",
    "lsu": "lsu", "smu": "smu", "tcu": "tcu", "ucf": "ucf", "vcu": "vcu", "byu": "byu",
    "unlv": "unlv", "utep": "utep", "usc": "usc", "ucla": "ucla", "utsa": "utsa",
    "fgcu": "florida gulf coast", "fiu": "fiu", "lmu": "loyola marymount",
    "uc davis": "uc davis", "uc irvine": "uc irvine", "uc riverside": "uc riverside",
    "cal state bakersfield": "cal st. bakersfield", "cal state northridge": "cal st. northridge",
    "cal state fullerton": "cal st. fullerton", "long beach state": "long beach st.",
    "grand canyon": "grand canyon", "hawaii": "hawaii", "hawai'i": "hawaii",
    "texas a&m": "texas a&m", "texas am": "texas a&m",
    "umass": "massachusetts", "umbc": "umbc", "unc": "north carolina",
    "unc wilmington": "unc wilmington", "unc asheville": "unc asheville",
    "unc greensboro": "unc greensboro", "charleston": "college of charleston",
    "appalachian state": "appalachian st.", "app state": "appalachian st.",
    "mississippi state": "mississippi st.", "michigan state": "michigan st.",
    "ohio state": "ohio st.", "penn state": "penn st.", "iowa state": "iowa st.",
    "kansas state": "kansas st.", "oklahoma state": "oklahoma st.", "arizona state": "arizona st.",
    "florida state": "florida st.", "colorado state": "colorado st.", "utah state": "utah st.",
    "san diego state": "san diego st.", "boise state": "boise st.", "fresno state": "fresno st.",
    "nc state wolfpack": "n.c. state",
}

MASCOT_WORDS = {
    # last words FanDuel appends; stripped progressively, so this list only speeds matching
    "aggies", "badgers", "bears", "bearcats", "blue", "devils", "boilermakers", "bruins", "buckeyes",
    "bulldogs", "cardinals", "cardinal", "cavaliers", "cougars", "cowboys", "crimson", "tide",
    "cyclones", "demons", "ducks", "eagles", "fighting", "irish", "illini", "friars", "gators",
    "gaels", "hawkeyes", "hoosiers", "hoyas", "huskies", "hurricanes", "jayhawks", "knights",
    "longhorns", "mountaineers", "musketeers", "nittany", "lions", "orange", "panthers", "pirates",
    "rams", "razorbacks", "rebels", "red", "storm", "sooners", "spartans", "sun", "tar", "heels",
    "tigers", "trojans", "volunteers", "wildcats", "wolfpack", "wolverines", "yellow", "jackets",
    "terrapins", "gamecocks", "miners", "lobos", "waves", "roadrunners", "mavericks", "bobcats",
    "golden", "gophers", "flames", "owls", "bluejays", "horned", "frogs", "cornhuskers", "seminoles",
    "racers", "flyers", "billikens", "dukes", "explorers", "hawks", "bison", "coyotes", "jackrabbits",
    "lopes", "runnin", "rebels", "buffaloes", "utes", "beavers", "vandals", "broncos", "falcons",
    "colonels", "catamounts", "keydets", "moccasins", "paladins", "phoenix", "privateers", "peacocks",
    "greyhounds", "mocs", "minutemen", "retrievers", "seawolves", "anteaters", "highlanders",
    "matadors", "titans", "tritons", "toreros", "dons", "zags", "pilots", "lumberjacks", "vikings",
    "thunderbirds", "trailblazers", "hornets", "beach", "antelopes", "jaguars", "leathernecks",
    "redbirds", "salukis", "braves", "ramblers", "blue demons", "bluedemons",
}


# ----------------------------------------------------------------------------- helpers
def norm(s):
    s = s.lower().strip()
    s = s.replace("&amp;", "&")
    s = re.sub(r"[’`]", "'", s)
    s = re.sub(r"\s+", " ", s)
    return s


def key(s):
    """Loose key: drop punctuation and the word 'state'->'st.' normalization both ways."""
    s = norm(s)
    s = s.replace("st.", "state").replace(" st ", " state ")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\bsaint\b", "st", s)
    return re.sub(r"\s+", " ", s).strip()


ILLINOIS_KEYS = {key(t) for t in ILLINOIS}


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def american_to_implied(price):
    p = int(price)
    return 100.0 / (p + 100.0) if p > 0 else (-p) / (-p + 100.0)


def american_profit(price, stake=10.0):
    p = int(price)
    return stake * p / 100.0 if p > 0 else stake * 100.0 / (-p)


def fmt_price(price):
    p = int(price)
    return f"+{p}" if p > 0 else str(p)


def fetch(url, cache_name, refresh=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_name)
    if not refresh and os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h < CACHE_HOURS:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(), f"cache ({age_h:.1f}h old)"
    # barttorvik.com robots.txt asks for a 10-second crawl delay; honor it between downloads
    global _LAST_FETCH
    wait = 10.0 - (time.time() - _LAST_FETCH)
    if wait > 0:
        time.sleep(wait)
    _LAST_FETCH = time.time()
    try:
        import requests  # type: ignore
        r = requests.get(url, timeout=30, headers={"User-Agent": "cbb_check/1.0 (personal use, one pull per day)"})
        r.raise_for_status()
        text = r.text
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "cbb_check/1.0 (personal use, one pull per day)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text, "downloaded"


# ----------------------------------------------------------------------------- ratings loader
def _is_num(x):
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def load_ratings(text):
    """
    Returns (ratings dict keyed by Torvik team name -> {oe, de, tempo, barthag, conf}, info string).
    Column detection: header names first, value ranges second.
    """
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if r and any(c.strip() for c in r)]
    if not rows:
        raise ValueError("ratings file is empty")

    header = rows[0]
    has_header = sum(1 for c in header if not _is_num(c)) >= len(header) * 0.6 and \
        any(re.search(r"adj|barthag|team|tempo", c, re.I) for c in header)
    data = rows[1:] if has_header else rows

    idx = {}
    if has_header:
        hn = [key(c) for c in header]
        def find(*pats):
            for i, h in enumerate(hn):
                for p in pats:
                    if re.fullmatch(p, h):
                        return i
            return None
        idx["team"] = find(r"team", r"teamname", r"school")
        idx["conf"] = find(r"conf", r"conference")
        idx["oe"] = find(r"adjoe", r"adj o", r"adjo", r"adj oe", r"adj_o", r"adjusted offense")
        idx["de"] = find(r"adjde", r"adj d", r"adjd", r"adj de", r"adj_d", r"adjusted defense")
        idx["tempo"] = find(r"adjt", r"adj t", r"adjtempo", r"adj tempo", r"adj_t", r"tempo")
        idx["barthag"] = find(r"barthag")

    # value-range fallback for anything missing
    sample = data[: min(60, len(data))]
    ncol = max(len(r) for r in sample)

    def col_vals(i):
        out = []
        for r in sample:
            if i < len(r) and _is_num(r[i]):
                out.append(float(r[i]))
        return out

    def col_in(lo, hi, exclude=()):
        for i in range(ncol):
            if i in exclude:
                continue
            v = col_vals(i)
            if len(v) >= len(sample) * 0.9 and all(lo <= x <= hi for x in v) and (max(v) - min(v)) > 2:
                return i
        return None

    if idx.get("team") is None:
        for i in range(ncol):
            v = [r[i] for r in sample if i < len(r)]
            if v and all(re.search(r"[A-Za-z]", x) for x in v) and len(set(v)) == len(v):
                idx["team"] = i
                break
    used = {idx.get("team")}
    if idx.get("oe") is None:
        idx["oe"] = col_in(85, 135, exclude=used)
    used.add(idx.get("oe"))
    if idx.get("de") is None:
        idx["de"] = col_in(85, 135, exclude=used)
    used.add(idx.get("de"))
    if idx.get("tempo") is None:
        idx["tempo"] = col_in(55, 82, exclude=used)
    used.add(idx.get("tempo"))
    if idx.get("barthag") is None:
        for i in range(ncol):
            if i in used:
                continue
            v = col_vals(i)
            if len(v) >= len(sample) * 0.9 and all(0.0 <= x <= 1.0 for x in v) and (max(v) - min(v)) > 0.3:
                idx["barthag"] = i
                break
    if idx.get("conf") is None and has_header is False:
        for i in range(ncol):
            if i in used:
                continue
            v = [r[i] for r in sample if i < len(r)]
            if v and all(re.fullmatch(r"[A-Za-z0-9 .\-]{2,8}", x) for x in v) and len(set(v)) < len(v) * 0.6:
                idx["conf"] = i
                break

    missing = [k for k in ("team", "oe", "de", "tempo") if idx.get(k) is None]
    if missing:
        raise ValueError(f"could not identify columns {missing}; run --inspect and set them by hand "
                         f"(detected: {idx}, header={'yes' if has_header else 'no'})")

    ratings = {}
    for r in data:
        try:
            team = r[idx["team"]].strip()
            oe, de, tempo = float(r[idx["oe"]]), float(r[idx["de"]]), float(r[idx["tempo"]])
        except (IndexError, ValueError):
            continue
        if not team:
            continue
        barthag = None
        if idx.get("barthag") is not None and idx["barthag"] < len(r) and _is_num(r[idx["barthag"]]):
            barthag = float(r[idx["barthag"]])
        conf = r[idx["conf"]].strip() if idx.get("conf") is not None and idx["conf"] < len(r) else ""
        ratings[team] = {"oe": oe, "de": de, "tempo": tempo, "barthag": barthag, "conf": conf}

    # sanity: offense should correlate with Barthag; defense inversely. If reversed, swap.
    if len(ratings) > 50 and any(v["barthag"] is not None for v in ratings.values()):
        pairs = [(v["oe"], v["de"], v["barthag"]) for v in ratings.values() if v["barthag"] is not None]
        top = sorted(pairs, key=lambda t: -t[2])[:25]
        bot = sorted(pairs, key=lambda t: t[2])[:25]
        oe_gap = sum(t[0] for t in top) / 25 - sum(t[0] for t in bot) / 25
        de_gap = sum(t[1] for t in top) / 25 - sum(t[1] for t in bot) / 25
        if oe_gap < 0 and de_gap > 0:
            for v in ratings.values():
                v["oe"], v["de"] = v["de"], v["oe"]
            idx["oe"], idx["de"] = idx["de"], idx["oe"]

    info = (f"{len(ratings)} teams; header={'yes' if has_header else 'no'}; columns used: "
            f"team={idx['team']} oe={idx['oe']} de={idx['de']} tempo={idx['tempo']} "
            f"barthag={idx.get('barthag')} conf={idx.get('conf')}")
    return ratings, info


# ----------------------------------------------------------------------------- team matching
class Matcher:
    def __init__(self, ratings):
        self.ratings = ratings
        self.keys = {key(t): t for t in ratings}

    def match(self, name):
        """Returns (torvik_name, how) or (None, reason)."""
        n = norm(name)
        n = re.sub(r"\((\w+)\)", r"\1", n)          # "miami (fl)" -> "miami fl"
        n = re.sub(r"\s+", " ", n).strip()
        if n in ALIASES:
            cand = key(ALIASES[n])
            if cand in self.keys:
                return self.keys[cand], "alias"
        k = key(n)
        if k in self.keys:
            return self.keys[k], "exact"
        words = k.split()
        # strip trailing mascot words one at a time
        for cut in range(1, min(4, len(words))):
            short = " ".join(words[:-cut])
            if short in ALIASES and key(ALIASES[short]) in self.keys:
                return self.keys[key(ALIASES[short])], "alias"
            if short in self.keys:
                return self.keys[short], f"prefix(-{cut})"
        # fuzzy, only on the de-mascoted prefix, high bar
        best = None
        for cut in range(0, min(4, len(words))):
            short = " ".join(words[: len(words) - cut]) if cut else k
            hits = difflib.get_close_matches(short, list(self.keys), n=1, cutoff=0.88)
            if hits:
                best = (self.keys[hits[0]], f"fuzzy({short!r}->{hits[0]!r})")
                break
        if best:
            return best
        return None, "no match (non-D1, or add to ALIASES)"


# ----------------------------------------------------------------------------- the model
def our_prob(home, away, ratings, neutral=False):
    """Home win probability from efficiency margin."""
    avg_oe = sum(v["oe"] for v in ratings.values()) / len(ratings)
    avg_t = sum(v["tempo"] for v in ratings.values()) / len(ratings)
    h, a = ratings[home], ratings[away]
    poss = h["tempo"] * a["tempo"] / avg_t
    home_pts = (h["oe"] * a["de"] / avg_oe) * poss / 100.0
    away_pts = (a["oe"] * h["de"] / avg_oe) * poss / 100.0
    margin = home_pts - away_pts + (0.0 if neutral else HOME_COURT)
    return phi(margin / MARGIN_SD), margin, home_pts, away_pts


def log5(pa, pb):
    if pa is None or pb is None:
        return None
    return (pa - pa * pb) / (pa + pb - 2 * pa * pb) if (pa + pb - 2 * pa * pb) else 0.5


def torvik_barthag_prob(home, away, ratings, neutral=False):
    """Torvik-style estimate: Barthag log5, then home court applied in margin space."""
    hb, ab = ratings[home]["barthag"], ratings[away]["barthag"]
    p = log5(hb, ab)
    if p is None:
        return None
    if neutral:
        return p
    # convert to margin, add HCA, convert back
    p = min(max(p, 1e-4), 1 - 1e-4)
    z = math.sqrt(2) * _erfinv(2 * p - 1)
    return phi((z * MARGIN_SD + HOME_COURT) / MARGIN_SD)


def _erfinv(y):
    # Winitzki approximation, good to ~1e-3, plenty for this
    a = 0.147
    ln = math.log(1 - y * y)
    t = 2 / (math.pi * a) + ln / 2
    return math.copysign(math.sqrt(math.sqrt(t * t - ln / a) - t), y)


# ----------------------------------------------------------------------------- Torvik's own projections (optional)
def load_projections(text):
    """
    Best-effort parse of 2026_results.csv (daily projections). Layout is undocumented, so this
    looks for rows with two team-name cells, one probability cell (0-1) and today's date.
    Returns dict {(teamA_key, teamB_key): (prob_for_A, line)} or {} if nothing parses.
    """
    out = {}
    rows = list(csv.reader(io.StringIO(text)))
    for r in rows:
        strs = [c for c in r if re.search(r"[A-Za-z]{3}", c) and not _is_num(c)]
        probs = [float(c) for c in r if _is_num(c) and 0.0 < float(c) < 1.0]
        if len(strs) < 2 or not probs:
            continue
        # heuristic: first two alphabetic cells that look like team names
        teams = [s for s in strs if not re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", s)][:2]
        if len(teams) < 2:
            continue
        out[(key(teams[0]), key(teams[1]))] = (probs[0], r)
    return out


# ----------------------------------------------------------------------------- games input
def parse_games(args_games, path):
    games = []
    lines = []
    if path:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    elif args_games:
        lines = [", ".join(args_games)]
    for ln in lines:
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 3 or "@" not in parts[0]:
            print(f"  skip line (need 'Away @ Home, awayPrice, homePrice[, N]'): {ln}")
            continue
        away, home = [x.strip() for x in parts[0].split("@", 1)]
        try:
            ap, hp = int(parts[1].replace("+", "")), int(parts[2].replace("+", ""))
        except ValueError:
            print(f"  skip line (bad prices): {ln}")
            continue
        neutral = len(parts) > 3 and parts[3].upper().startswith("N")
        games.append({"away": away, "home": home, "away_price": ap, "home_price": hp, "neutral": neutral})
    return games


# ----------------------------------------------------------------------------- main
def main():
    global HOME_COURT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("games", nargs="*", help="games.txt path, or: 'Away @ Home' awayPrice homePrice")
    ap.add_argument("--refresh", action="store_true", help="re-download Torvik files")
    ap.add_argument("--inspect", action="store_true", help="print file shape and matched columns, then exit")
    ap.add_argument("--stake", type=float, default=10.0)
    ap.add_argument("--hca", type=float, default=HOME_COURT)
    ap.add_argument("--json", action="store_true", help="also dump results as JSON")
    a = ap.parse_args()
    HOME_COURT = a.hca

    text, how = fetch(f"{BASE}/{SEASON}_team_results.csv", f"{SEASON}_team_results.csv", a.refresh)
    ratings, info = load_ratings(text)
    print(f"Torvik ratings: {how}; {info}")

    proj = {}
    try:
        ptext, phow = fetch(f"{BASE}/{SEASON}_results.csv", f"{SEASON}_results.csv", a.refresh)
        proj = load_projections(ptext)
        print(f"Torvik projections file: {phow}; {len(proj)} rows parsed "
              f"({'using Barthag log5 as the Torvik number' if not proj else 'will try to match games'})")
    except Exception as e:  # noqa: BLE001
        print(f"Torvik projections file: unavailable ({e}); using Barthag log5 as the Torvik number")

    if a.inspect:
        print("\nFirst 3 raw rows of the ratings file:")
        for r in list(csv.reader(io.StringIO(text)))[:3]:
            print("  ", [(i, c) for i, c in enumerate(r)])
        top = sorted(ratings.items(), key=lambda kv: -(kv[1]["oe"] - kv[1]["de"]))[:10]
        print("\nTop 10 by efficiency margin (should look like a real top 10):")
        for t, v in top:
            print(f"  {t:28s} OE {v['oe']:6.1f}  DE {v['de']:6.1f}  T {v['tempo']:5.1f}  Barthag {v['barthag']}")
        return

    games_path = a.games[0] if len(a.games) == 1 and os.path.exists(a.games[0]) else None
    games = parse_games(a.games if not games_path else None, games_path)
    if not games:
        print("No games given. See --help.")
        return

    m = Matcher(ratings)
    today = date.today().strftime("%a %b %d")
    print(f"\nEARLY-SEASON NOTE: through mid-December these ratings are part preseason prior. "
          f"Treat the first weeks as a test of the model, not a license.\n")
    print(f"{'Game (' + today + ')':52s} {'side':6s} {'price':6s} {'impl':6s} {'ours':6s} {'torvik':7s} {'EV$':6s}  verdict")
    print("-" * 110)

    results = []
    for g in games:
        h, hhow = m.match(g["home"])
        aw, ahow = m.match(g["away"])
        label = f"{g['away']} @ {g['home']}" + (" (N)" if g["neutral"] else "")
        if h is None or aw is None:
            print(f"{label[:52]:52s} {'':6s} {'':6s} {'':6s} {'':6s} {'':7s} {'':6s}  SKIP: "
                  f"{'home ' + hhow if h is None else ''}{' / ' if h is None and aw is None else ''}{'away ' + ahow if aw is None else ''}")
            continue
        if key(h) in ILLINOIS_KEYS or key(aw) in ILLINOIS_KEYS:
            print(f"{label[:52]:52s} {'':6s} {'':6s} {'':6s} {'':6s} {'':7s} {'':6s}  SKIP: Illinois school (house rule)")
            continue

        p_home, margin, hp, apts = our_prob(h, aw, ratings, g["neutral"])
        t_home = None
        src = "barthag"
        pr = proj.get((key(h), key(aw))) or proj.get((key(aw), key(h)))
        if pr:
            # projection row: prob belongs to the first-listed team
            first_is_home = (key(h), key(aw)) in proj
            t_home = pr[0] if first_is_home else 1 - pr[0]
            src = "file"
        if t_home is None:
            t_home = torvik_barthag_prob(h, aw, ratings, g["neutral"])

        for side, team, p_side, t_side, price in (
            ("home", h, p_home, t_home, g["home_price"]),
            ("away", aw, 1 - p_home, (1 - t_home) if t_home is not None else None, g["away_price"]),
        ):
            impl = american_to_implied(price)
            p_use = p_side if t_side is None else min(p_side, t_side)   # conservative: the lower of the two
            ev = p_use * american_profit(price, a.stake) - (1 - p_use) * a.stake - 0.50
            agree = t_side is None or abs(p_side - t_side) <= AGREE_MAX
            verdict = []
            if price <= PARLAY_MAX_PRICE:
                if not agree:
                    verdict.append("models disagree 10+")
                elif p_use >= impl + EDGE_MIN:
                    verdict.append("PARLAY LEG OK")
                else:
                    verdict.append("no edge vs price")
            else:
                if p_use >= impl + EDGE_MIN and agree:
                    verdict.append("+EV straight (not a parlay leg)")
                else:
                    verdict.append("")
            if not verdict[0] and side == "away":
                verdict[0] = ""
            tv = f"{t_side*100:5.1f}%" if t_side is not None else "  n/a "
            print(f"{(label if side == 'home' else '')[:52]:52s} {side:6s} {fmt_price(price):6s} "
                  f"{impl*100:5.1f}% {p_side*100:5.1f}% {tv:7s} {ev:6.2f}  {verdict[0]}")
            results.append({"game": label, "side": side, "team": team, "price": price, "implied": round(impl, 4),
                            "ours": round(p_side, 4), "torvik": None if t_side is None else round(t_side, 4),
                            "torvik_source": src, "ev_at_stake": round(ev, 2), "verdict": verdict[0],
                            "proj_margin_home": round(margin, 1), "proj_score": f"{hp:.0f}-{apts:.0f}"})
        print(f"{'':52s} proj {h} {hp:.0f}, {aw} {apts:.0f}  (home margin {margin:+.1f}; "
              f"matched home={h!r} [{hhow}], away={aw!r} [{ahow}]; torvik via {src})")

    if a.json:
        print("\n" + json.dumps(results, indent=1))

    # EV note for the parlay rule (30% token, $5 cap, +$1 floor at $10)
    oks = [r for r in results if r["verdict"] == "PARLAY LEG OK"]
    print(f"\n{len(oks)} parlay-eligible leg(s).", end=" ")
    if len(oks) >= 3:
        best = sorted(oks, key=lambda r: -min(r["ours"], r["torvik"] or r["ours"]))[:3]
        p = 1.0
        dec = 1.0
        for r in best:
            p *= min(r["ours"], r["torvik"] or r["ours"])
            dec *= (1 + american_profit(r["price"], 1.0))
        boosted = 1 + (dec - 1) * 1.30
        ev10 = p * (boosted - 1) * 10 - (1 - p) * 10
        print(f"Best three: {', '.join(r['team'] for r in best)}; joint prob {p*100:.1f}%, "
              f"pays about {fmt_price(round((boosted-1)*100))} with the 30% boost, EV at $10 = {ev10:+.2f} "
              f"({'clears' if ev10 >= 1.0 else 'FAILS'} the +$1 floor; $5 cap applies either way).")
    else:
        print("Fewer than three; no parlay token today.")


if __name__ == "__main__":
    main()
