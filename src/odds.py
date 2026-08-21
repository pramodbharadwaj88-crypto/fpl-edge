"""FPL Edge — betting-market layer (The Odds API, free tier friendly).

Markets → model inputs:
  h2h (1X2) + totals (O/U 2.5)  →  de-vigged implied (λ_home, λ_away) per fixture
                                    [captures team news the instant markets move]
  player_goal_scorer_anytime    →  per-player P(goal) → E[goals] = -ln(1-p)
                                    [per-event endpoint; fetched near deadline only]

Credit budget (free tier = 500/mo):
  match odds: 1 call/day covering all EPL events (regions=uk, markets=h2h,totals ≈ 2 credits)
  scorer props: only within 36h of deadline, ~10 events × 1 credit ≈ 10-20/GW

Config: config.json → "odds_api_key". Without a key everything degrades
gracefully to the Elo-based lambdas.
"""
from __future__ import annotations
import json, math, time, pathlib
import requests
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BASE = "https://api.the-odds-api.com/v4"
SPORT = "soccer_epl"


PM_BLEND = 0.20  # weight on prediction markets vs bookmaker consensus


def _pm_probs() -> dict:
    try:
        import prediction_markets
        return prediction_markets.refresh()
    except Exception:
        return {}


def _key() -> str | None:
    cfg = json.loads((ROOT / "config.json").read_text())
    return cfg.get("odds_api_key") or None


# ---------- implied goal expectancies from 1X2 + totals ----------------------
def _devig(probs: list[float]) -> list[float]:
    s = sum(probs)
    return [p / s for p in probs] if s > 0 else probs


def _poisson_1x2(lh: float, la: float, cap: int = 10):
    ph = pa = pd_ = 0.0
    for i in range(cap):
        for j in range(cap):
            p = (math.exp(-lh) * lh ** i / math.factorial(i)
                 * math.exp(-la) * la ** j / math.factorial(j))
            if i > j: ph += p
            elif i < j: pa += p
            else: pd_ += p
    return ph, pd_, pa


def implied_lambdas(p_home: float, p_draw: float, p_away: float,
                    total_line: float, p_over: float) -> tuple[float, float]:
    """Grid-search (λh, λa) matching de-vigged 1X2 + O/U probabilities."""
    # total goals expectation from the over prob at the line (rough inversion)
    lam_tot_guess = total_line + (p_over - 0.5) * 2.2
    best, best_err = (1.5, 1.2), 1e9
    for lam_tot in np.arange(max(1.2, lam_tot_guess - 0.8), lam_tot_guess + 0.85, 0.1):
        for share in np.arange(0.25, 0.76, 0.025):
            lh, la = lam_tot * share, lam_tot * (1 - share)
            ph, pd_, pa = _poisson_1x2(lh, la)
            err = (ph - p_home) ** 2 + (pa - p_away) ** 2 + 0.5 * (pd_ - p_draw) ** 2
            if err < best_err:
                best, best_err = (lh, la), err
    return best


def refresh_match_odds() -> dict | None:
    """Fetch h2h+totals for all upcoming EPL events; cache implied lambdas."""
    key = _key()
    if not key:
        return None
    cache = DATA / "odds_lambdas.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
        return json.loads(cache.read_text())
    r = requests.get(f"{BASE}/sports/{SPORT}/odds",
                     params=dict(apiKey=key, regions="uk", markets="h2h,totals",
                                 oddsFormat="decimal"), timeout=30)
    r.raise_for_status()
    out = {}
    for ev in r.json():
        h, a = ev["home_team"], ev["away_team"]
        # median across bookmakers
        h2h_probs, over_probs, lines = [], [], []
        for bk in ev.get("bookmakers", []):
            mk = {m["key"]: m for m in bk.get("markets", [])}
            if "h2h" in mk:
                d = {o["name"]: 1 / o["price"] for o in mk["h2h"]["outcomes"]}
                if h in d and a in d and "Draw" in d:
                    h2h_probs.append(_devig([d[h], d["Draw"], d[a]]))
            if "totals" in mk:
                outs = mk["totals"]["outcomes"]
                for o in outs:
                    if o["name"] == "Over" and abs(o.get("point", 2.5) - 2.5) < 0.01:
                        under = next((u for u in outs if u["name"] == "Under"
                                      and u.get("point") == o.get("point")), None)
                        if under:
                            po, pu = _devig([1 / o["price"], 1 / under["price"]])
                            over_probs.append(po); lines.append(o["point"])
        if not h2h_probs:
            continue
        ph, pd_, pa = np.median(np.array(h2h_probs), axis=0)
        # blend in prediction markets (Polymarket/Kalshi) when available
        hs, as_ = TEAM_MAP.get(h), TEAM_MAP.get(a)
        pm = _pm_probs().get(f"{hs}|{as_}") if hs and as_ else None
        if pm:
            w = PM_BLEND
            ph = (1 - w) * ph + w * pm["p_home"]
            pd_ = (1 - w) * pd_ + w * pm["p_draw"]
            pa = (1 - w) * pa + w * pm["p_away"]
            s = ph + pd_ + pa
            ph, pd_, pa = ph / s, pd_ / s, pa / s
        p_over = float(np.median(over_probs)) if over_probs else 0.5
        line = float(np.median(lines)) if lines else 2.5
        lh, la = implied_lambdas(ph, pd_, pa, line, p_over)
        out[f"{h}|{a}"] = dict(home=h, away=a, kickoff=ev["commence_time"],
                               lam_home=round(lh, 3), lam_away=round(la, 3),
                               p_home=round(float(ph), 3), p_draw=round(float(pd_), 3),
                               p_away=round(float(pa), 3),
                               cs_home=round(math.exp(-la), 3),
                               cs_away=round(math.exp(-lh), 3))
    cache.write_text(json.dumps(out, indent=1))
    return out


# ---------- anytime goalscorer props ----------------------------------------
def refresh_scorer_props(max_events: int = 10) -> dict | None:
    """Per-player P(anytime goal), de-vigged median across books. Costs ~1
    credit per event — call only near deadlines."""
    key = _key()
    if not key:
        return None
    cache = DATA / "odds_scorers.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 12 * 3600:
        return json.loads(cache.read_text())
    evs = requests.get(f"{BASE}/sports/{SPORT}/events",
                       params=dict(apiKey=key), timeout=30).json()
    out = {}
    for ev in evs[:max_events]:
        try:
            r = requests.get(f"{BASE}/sports/{SPORT}/events/{ev['id']}/odds",
                             params=dict(apiKey=key, regions="us,uk",
                                         markets="player_goal_scorer_anytime",
                                         oddsFormat="decimal"), timeout=30)
            if r.status_code != 200:
                continue
            probs: dict[str, list[float]] = {}
            for bk in r.json().get("bookmakers", []):
                for m in bk.get("markets", []):
                    if m["key"] != "player_goal_scorer_anytime":
                        continue
                    raw = {o["description"]: 1 / o["price"] for o in m["outcomes"]}
                    # de-vig within book: scale so sum matches expected total goals? too
                    # aggressive; use standard 1.08 overround haircut instead
                    for name, p in raw.items():
                        probs.setdefault(name, []).append(p / 1.08)
            for name, ps in probs.items():
                out[name] = dict(p_goal=round(float(np.median(ps)), 3),
                                 match=f"{ev['home_team']}|{ev['away_team']}")
        except Exception:
            continue
    cache.write_text(json.dumps(out, indent=1))
    return out


# ---------- name mapping to FPL ids ------------------------------------------
TEAM_MAP = {  # Odds API name -> FPL short name
    "Arsenal": "ARS", "Aston Villa": "AVL", "Bournemouth": "BOU", "Brentford": "BRE",
    "Brighton and Hove Albion": "BHA", "Chelsea": "CHE", "Coventry City": "COV",
    "Crystal Palace": "CRY", "Everton": "EVE", "Fulham": "FUL", "Hull City": "HUL",
    "Ipswich Town": "IPS", "Leeds United": "LEE", "Liverpool": "LIV",
    "Manchester City": "MCI", "Manchester United": "MUN", "Newcastle United": "NEW",
    "Nottingham Forest": "NFO", "Tottenham Hotspur": "TOT", "Sunderland": "SUN",
}


def match_player(odds_name: str, elements, team_ids: set | None = None) -> int | None:
    """Map an odds-feed player name to an FPL element id.

    Council fix: the old global fuzzy match (0.72 difflib over all 599 players)
    mis-mapped e.g. "Erling Braut Haaland" to the wrong element. Now:
      1. candidates restricted to the match's two teams when known;
      2. exact surname-token hit is required unless the full-string ratio is
         very high (>=0.85);
      3. ties broken by price (props cover starters).
    """
    import difflib, re
    target = odds_name.lower().strip()
    t_tokens = set(re.split(r"[\s\-']+", target))
    best, best_key = None, (-1, -1.0, -1.0)
    for el in elements:
        if team_ids and el["team"] not in team_ids:
            continue
        full = f"{el['first_name']} {el['second_name']}".lower()
        cands = [full, el["web_name"].lower(), (el.get("known_name") or "").lower()]
        surname_tokens = set(re.split(r"[\s\-']+", el["second_name"].lower())) | \
            set(re.split(r"[\s\-']+", el["web_name"].lower()))
        surname_hit = 1 if (t_tokens & surname_tokens) else 0
        ratio = max(difflib.SequenceMatcher(None, target, c).ratio() for c in cands if c)
        if not surname_hit and ratio < 0.85:
            continue
        key = (surname_hit, ratio, float(el["now_cost"]))
        if key > best_key:
            best, best_key = el["id"], key
    return best if best_key[1] >= 0.55 or best_key[0] else None


if __name__ == "__main__":
    ml = refresh_match_odds()
    print("match odds:", "no api key" if ml is None else f"{len(ml)} fixtures")
    if ml:
        for k, v in list(ml.items())[:3]:
            print(k, v["lam_home"], v["lam_away"], "cs:", v["cs_home"], v["cs_away"])
