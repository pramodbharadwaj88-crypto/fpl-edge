"""FPL Edge — shot-level finishing priors from Understat (best effort).

Finishing skill: goals vs xG over the last season, shrunk toward 1.0
(multiplier = (goals + K) / (xG + K), K=10 — a strong prior; only persistent
over/under-performers move the needle, capped downstream by finishing_cap).

Output: data/understat_finishing.json {fpl_code: multiplier}
Graceful: any failure leaves no file; projections then use raw xG.
"""
from __future__ import annotations
import json, re, pathlib, difflib
import requests

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
K = 10.0


def fetch_league_players(season: int = 2025) -> list[dict] | None:
    try:
        r = requests.get(f"https://understat.com/league/EPL/{season}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        m = re.search(r"playersData\s*=\s*JSON\.parse\('(.+?)'\)", r.text)
        if not m:
            return None
        return json.loads(m.group(1).encode().decode("unicode_escape"))
    except Exception:
        return None


def build() -> dict | None:
    import fpl_api
    players = fetch_league_players()
    if not players:
        return None
    els = fpl_api.bootstrap()["elements"]
    names = {f"{e['first_name']} {e['second_name']}".lower(): e for e in els}
    web = {e["web_name"].lower(): e for e in els}
    out = {}
    for p in players:
        goals, xg = float(p.get("goals") or 0), float(p.get("xG") or 0)
        npg = float(p.get("npg") or goals)
        npxg = float(p.get("npxG") or xg)
        if npxg < 2:  # too little signal
            continue
        mult = (npg + K) / (npxg + K)
        nm = p["player_name"].lower()
        el = names.get(nm) or web.get(nm.split()[-1])
        if not el:
            cand = difflib.get_close_matches(nm, list(names), n=1, cutoff=0.85)
            el = names.get(cand[0]) if cand else None
        if el:
            out[str(el["code"])] = round(mult, 3)
    if out:
        (DATA / "understat_finishing.json").write_text(json.dumps(out))
    return out


if __name__ == "__main__":
    res = build()
    print("understat finishing priors:", "unavailable" if res is None else f"{len(res)} players")
