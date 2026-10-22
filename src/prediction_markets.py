"""FPL Edge — prediction-market layer (Polymarket + Kalshi, free, no auth).

Both expose 1X2 probabilities for EPL matches:
  Polymarket gamma API: event "X FC vs. Y FC" with 3 Yes/No markets whose
    Yes-prices are already ~de-vigged probabilities.
  Kalshi trade API v2: series KXEPLGAME, three legs per event (home/away/TIE),
    prob = mid(yes_bid, yes_ask) or last_price, read from the *_dollars
    decimal strings (legacy integer-cent fields still accepted). Often
    illiquid early — legs without quotes are skipped.

Output: data/pm_probs.json  {"HOME_SHORT|AWAY_SHORT": {p_home,p_draw,p_away,src}}
Blended into the bookmaker 1X2 consensus in odds.refresh_match_odds().
"""
from __future__ import annotations
import json, re, time, pathlib
import requests
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# full-name → FPL short (covers Polymarket "X FC" and Kalshi sub_titles)
NAME_MAP = {
    "arsenal": "ARS", "aston villa": "AVL", "bournemouth": "BOU",
    "afc bournemouth": "BOU", "brentford": "BRE", "brighton": "BHA",
    "brighton and hove albion": "BHA", "brighton & hove albion": "BHA",
    "chelsea": "CHE", "coventry": "COV", "coventry city": "COV",
    "crystal palace": "CRY", "everton": "EVE", "fulham": "FUL",
    "hull": "HUL", "hull city": "HUL", "ipswich": "IPS", "ipswich town": "IPS",
    "leeds": "LEE", "leeds united": "LEE", "liverpool": "LIV",
    "manchester city": "MCI", "manchester united": "MUN",
    "newcastle": "NEW", "newcastle united": "NEW",
    "nottingham forest": "NFO", "tottenham": "TOT", "tottenham hotspur": "TOT",
    "sunderland": "SUN",
}


def _short(name: str) -> str | None:
    # strip a trailing club suffix ("FC", "AFC") — e.g. "Hull City AFC",
    # "Sunderland AFC", "Arsenal FC" — which the reveal/Polymarket titles carry
    n = re.sub(r"\s+a?fc$", "", name.strip().lower()).strip()
    return NAME_MAP.get(n)


def polymarket_match_probs() -> dict:
    out = {}
    try:
        evs = requests.get("https://gamma-api.polymarket.com/events",
                           params={"limit": 100, "closed": "false",
                                   "tag_slug": "epl"}, timeout=25).json()
    except Exception:
        return out
    for e in evs:
        t = e.get("title", "")
        if " vs. " not in t or any(x in t for x in ("Half", "Champion", "Winner",
                                                    "Relegat", "Top", "Golden")):
            continue
        home_raw, away_raw = t.split(" vs. ", 1)
        h, a = _short(home_raw), _short(away_raw)
        if not h or not a:
            continue
        ph = pd_ = pa = None
        for m in e.get("markets", []):
            try:
                yes = float(json.loads(m["outcomePrices"])[0]) if isinstance(
                    m.get("outcomePrices"), str) else float(m["outcomePrices"][0])
            except Exception:
                continue
            git = (m.get("groupItemTitle") or "").lower()
            if git.startswith("draw"):
                pd_ = yes
            elif _short(git) == h:
                ph = yes
            elif _short(git) == a:
                pa = yes
        if None in (ph, pd_, pa) or not 0.9 < ph + pd_ + pa < 1.1:
            continue
        s = ph + pd_ + pa
        out[f"{h}|{a}"] = dict(p_home=round(ph / s, 4), p_draw=round(pd_ / s, 4),
                               p_away=round(pa / s, 4), src="polymarket",
                               end=e.get("endDate"))
    return out


def _kalshi_price(m: dict, field: str) -> float | None:
    """Price as a 0-1 probability. Current API: "<field>_dollars" decimal
    strings ("0.4300"); legacy API: integer cents in "<field>". 0 = no quote."""
    v = m.get(f"{field}_dollars")
    try:
        p = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        p = None
    if p is None and isinstance(m.get(field), (int, float)):
        p = m[field] / 100.0
    return p if p and 0 < p < 1 else None


def _kalshi_leg_prob(m: dict) -> float | None:
    yb, ya = _kalshi_price(m, "yes_bid"), _kalshi_price(m, "yes_ask")
    if yb and ya:
        return (yb + ya) / 2.0
    return _kalshi_price(m, "last_price")


def _get_retry(url: str, params: dict, tries: int = 4, timeout: int = 25):
    """GET with backoff on HTTP 429 (honours Retry-After when given)."""
    for i in range(tries):
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code != 429 or i == tries - 1:
            r.raise_for_status()
            return r.json()
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except ValueError:
            wait = 2.0 * 2 ** i                    # 2s, 4s, 8s
        time.sleep(min(wait, 30.0))


def kalshi_match_probs() -> dict:
    out, cursor = {}, None
    seen = quoted = 0                       # liquidity telemetry for reporting
    try:
        for _ in range(5):  # paginate
            params = {"limit": 100, "status": "open", "series_ticker": "KXEPLGAME"}
            if cursor:
                params["cursor"] = cursor
            r = _get_retry("https://api.elections.kalshi.com/trade-api/v2/markets",
                           params)
            mkts = r.get("markets", [])
            byev: dict[str, list] = {}
            for m in mkts:
                seen += 1
                if _kalshi_leg_prob(m) is not None:
                    quoted += 1
                byev.setdefault(m["event_ticker"], []).append(m)
            for evt, legs in byev.items():
                probs, code_to_short = {}, {}
                for m in legs:
                    p = _kalshi_leg_prob(m)
                    if p is None:
                        continue
                    sub = (m.get("yes_sub_title") or "").lower()
                    key = "draw" if sub in ("tie", "draw") else _short(sub)
                    if key:
                        probs[key] = p
                        # Kalshi's own team codes (LFC, CFC, BRI) != FPL shorts
                        code_to_short[(m.get("ticker") or "").rsplit("-", 1)[-1]] = key
                # event ticker suffix: e.g. 26AUG30MUNIPS → home listed first
                suffix = evt.rsplit("-", 1)[-1]
                teams = [v for k, v in probs.items() if k != "draw"]
                if "draw" not in probs or len(teams) != 2:
                    continue
                m3 = re.findall(r"[A-Z]{3}", suffix[-6:])
                if len(m3) == 2:
                    hcode, acode = (code_to_short.get(c, c) for c in m3)
                    shorts = {k for k in probs if k != "draw"}
                    if hcode in shorts and acode in shorts:
                        s = sum(probs.values())
                        if not 0.8 < s < 1.2:
                            continue
                        out[f"{hcode}|{acode}"] = dict(
                            p_home=round(probs[hcode] / s, 4),
                            p_draw=round(probs["draw"] / s, 4),
                            p_away=round(probs[acode] / s, 4), src="kalshi")
            cursor = r.get("cursor")
            if not cursor or not mkts:
                break
    except Exception:
        pass
    try:
        (DATA / "kalshi_status.json").write_text(json.dumps(
            dict(markets_seen=seen, quoted=quoted, fixtures=len(out),
                 liquidity=("none" if quoted == 0 else "partial" if quoted < seen else "full"),
                 updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- player markets
# Polymarket season-outright markets: title substring -> our short market key.
# These price P(player LEADS the league in that stat all season) as independent
# Yes/No contracts, so they are a season-long talent/role signal, NOT per-GW xP.
POLY_PLAYER_MARKETS = {
    "top goalscorer": "top_scorer",
    "most assists": "most_assists",
    "most clean sheets": "most_clean_sheets",  # matches player & team variants
    "player of the season": "player_of_season",
}


def _poly_real(git: str, yes) -> bool:
    """Reject unpriced placeholder legs Polymarket pads markets with."""
    if yes is None or not (0.0 < yes < 1.0):
        return False
    if abs(yes - 0.5) < 1e-6:        # default/unquoted leg
        return False
    g = str(git or "").strip().lower()
    if not g or g == "other" or re.fullmatch(r"player [a-z]", g):
        return False
    return True


def polymarket_player_markets(max_age: int = 6 * 3600) -> dict:
    """Season-outright player/team markets from Polymarket.

    Returns {market_key: {"is_team": bool, "runners": [{name, prob}]}}
    sorted by prob desc, real quotes only. Cached to data/pm_players.json.
    """
    cache = DATA / "pm_players.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    out: dict = {}
    try:
        evs = requests.get("https://gamma-api.polymarket.com/events",
                           params={"limit": 100, "closed": "false",
                                   "tag_slug": "epl"}, timeout=25).json()
    except Exception:
        return out
    for e in evs:
        title = (e.get("title") or "").lower()
        key = next((v for sub, v in POLY_PLAYER_MARKETS.items() if sub in title), None)
        if not key:
            continue
        is_team = "team" in title  # "Team with Most Clean Sheets"
        mk = key + ("_team" if is_team else "")
        runners = []
        for m in e.get("markets", []):
            git = m.get("groupItemTitle")
            op = m.get("outcomePrices")
            try:
                yes = float(json.loads(op)[0]) if isinstance(op, str) else float(op[0])
            except Exception:
                yes = None
            if _poly_real(git, yes):
                runners.append({"name": git, "prob": round(yes, 4)})
        if runners:
            runners.sort(key=lambda r: -r["prob"])
            out[mk] = {"is_team": is_team, "runners": runners}
    if out:
        cache.write_text(json.dumps(out, indent=1))
    return out


def map_players_to_fpl(markets: dict, elements: list) -> dict:
    """Attach an FPL web_name + element id to each runner using the same
    full-name matcher as the scorer props (odds.match_player), against the
    supplied bootstrap `elements` (kept out of this module's fetch path so it
    stays sandbox-safe). Team markets are passed through unchanged.

    The old bare-surname index took the first element sharing the last token,
    so "Cole Palmer" -> web_name "Palmer" (ambiguous with Alex Palmer, IPS GK),
    "Morgan Gibbs-White" -> Ben White, "Bruno Fernandes" -> Mateus Fernandes."""
    from odds import match_player
    by_id = {el["id"]: el for el in elements}
    mapped = {}
    for mk, blk in markets.items():
        rows = []
        for r in blk["runners"]:
            pid = None if blk.get("is_team") else match_player(r["name"], elements)
            rows.append({**r, "fpl": by_id[pid]["web_name"] if pid else None,
                         "fpl_id": pid})
        mapped[mk] = {"is_team": blk.get("is_team", False), "runners": rows}
    return mapped


def squad_market_view(web_names: list, elements: list, max_age: int = 6 * 3600) -> dict:
    """Season-outright Polymarket standing for the manager's own players.

    Returns {market_key: [{fpl, fpl_id, name, prob, rank}]} for players in
    `web_names` (element ids also accepted — prefer them; web_names such as
    "Palmer" are ambiguous),
    plus 'top_scorer_team'/'most_clean_sheets_team' team rows untouched. Use in
    briefings to answer 'what does the sharp market think of MY players'."""
    mapped = map_players_to_fpl(polymarket_player_markets(max_age), elements)
    want = set(web_names)
    view = {}
    for mk, blk in mapped.items():
        rows = []
        for i, r in enumerate(blk["runners"], 1):
            if blk.get("is_team") or r.get("fpl_id") in want or r.get("fpl") in want:
                rows.append({**r, "rank": i})
        if rows:
            view[mk] = rows
    return view


def refresh(max_age: int = 4 * 3600) -> dict:
    cache = DATA / "pm_probs.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        return json.loads(cache.read_text())
    poly = polymarket_match_probs()
    kal = kalshi_match_probs()
    merged: dict = {}
    for k in set(poly) | set(kal):
        ps = [d for d in (poly.get(k), kal.get(k)) if d]
        merged[k] = dict(
            p_home=round(float(np.mean([d["p_home"] for d in ps])), 4),
            p_draw=round(float(np.mean([d["p_draw"] for d in ps])), 4),
            p_away=round(float(np.mean([d["p_away"] for d in ps])), 4),
            src="+".join(d["src"] for d in ps))
    cache.write_text(json.dumps(merged, indent=1))
    return merged


if __name__ == "__main__":
    m = refresh(max_age=0)
    print(f"{len(m)} fixtures from prediction markets")
    for k, v in sorted(m.items()):
        print(f"  {k:9} {v['p_home']:.0%}/{v['p_draw']:.0%}/{v['p_away']:.0%}  [{v['src']}]")
