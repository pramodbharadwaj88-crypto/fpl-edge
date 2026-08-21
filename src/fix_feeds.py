"""FPL Edge — extended Fantasy Football Fix feeds (authenticated, personal use).

Feeds (all cookie-authed, cached, graceful on failure):
  algo():     /algorithm_predictions/ embedded player_data JSON
              -> data/fix_proj_full.json  {web_name: {"gw": base_gw, "pts": [gw1..gwN]}}
              Used as an automated calibration benchmark for our xP model.
  lineups():  /lineups/{fixture_id}/ pages (sequential ids), next GW's matches
              -> data/fix_lineups.json {"teams": [...], "xi": [{"name","team"}], "warnings": [...]}
              Predicted-XI evidence feeding xMins directly.
  prices():   /price_change_json/1/ -> data/fix_prices.json (risers/fallers;
              "LOCKED" pre-season)
  injuries(): /injuries_json/1/ + /2/ -> data/fix_injuries.json

Politeness: lineups = ~10 fetches, cached 6h; everything else single fetches.
"""
from __future__ import annotations
import json, pathlib, re, time
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BASE = "https://www.fantasyfootballfix.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "X-Requested-With": "XMLHttpRequest"}


def _session() -> requests.Session | None:
    try:
        ck = json.loads((ROOT / "config.json").read_text()).get("fix_sessionid")
    except Exception:
        return None
    if not ck:
        return None
    s = requests.Session()
    s.cookies.set("sessionid", ck, domain=".fantasyfootballfix.com")
    return s


def _fresh(name: str, max_age: int):
    f = DATA / name
    if f.exists() and time.time() - f.stat().st_mtime < max_age:
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


def _nmap():
    from fix_data import _fpl_name_map
    return _fpl_name_map()


def _wn(name, nmap):
    from fix_data import _to_web_name
    return _to_web_name(name or "", nmap)


# ---------------------------------------------------------------- algo model
def algo(max_age: int = 12 * 3600) -> dict | None:
    cached = _fresh("fix_proj_full.json", max_age)
    if cached:
        return cached
    s = _session()
    if not s:
        return None
    try:
        h = s.get(BASE + "/algorithm_predictions/", headers=UA, timeout=40).text
        m = re.search(r'var player_data\s*=\s*(\{.*?\})\s*;', h, re.S)
        if not m:
            return None
        raw = json.loads(m.group(1))
        gws = raw.get("gw")
        gw_base = int(gws[0]) if isinstance(gws, list) and gws else int(gws or 1)
        n = int(raw.get("gw_len") or 5)
        nmap = _nmap()
        out = {"gw": gw_base, "players": {}}
        for p in raw.get("aaData", []):
            wn = _wn(p.get("name"), nmap) or _wn(p.get("full_name"), nmap)
            if not wn:
                continue
            pts = [round(float(p.get(f"pts_{i}") or 0), 2) for i in range(1, n + 1)]
            out["players"][wn] = dict(pts=pts, team=p.get("team"),
                                      status=p.get("status"))
        (DATA / "fix_proj_full.json").write_text(json.dumps(out, ensure_ascii=False))
        return out
    except Exception:
        return None


# ---------------------------------------------------------------- lineups
def _fix_title_to_fpl(title: str, fpl_teams: dict) -> tuple[int, str] | None:
    """Map a Fix page team title ('Palace', 'Man City') to (fpl_team_id, short)."""
    alias = {"Palace": "Crystal Palace", "Forest": "Nott'm Forest",
             "Villa": "Aston Villa", "Coventry": "Coventry City",
             "Hull": "Hull City", "Ipswich": "Ipswich Town"}
    name = alias.get(title.strip(), title.strip())
    for tid, (tname, tshort) in fpl_teams.items():
        if tname.lower() == name.lower():
            return tid, tshort
    # prefix fallback ("Sunderland" vs "Sunderland AFC" style drift)
    for tid, (tname, tshort) in fpl_teams.items():
        if tname.lower().startswith(name.lower()) or name.lower().startswith(tname.lower()):
            return tid, tshort
    return None


def lineups(max_age: int = 6 * 3600) -> dict | None:
    """Predicted XIs, TEAM-QUALIFIED (council mandate): every starter is stored
    as {"name": web_name, "team": fpl_short}; name resolution happens WITHIN
    the fixture's two FPL team rosters so duplicate surnames cannot collide.
    Emits warnings (surfaced in state) whenever a team maps != 11 starters
    or a page fails — loud degradation, never silent."""
    cached = _fresh("fix_lineups.json", max_age)
    if cached:
        return cached
    s = _session()
    if not s:
        return None
    warnings = []
    try:
        import fpl_api
        from fix_data import _to_web_name
        bs = fpl_api.bootstrap()
        fpl_teams = {t["id"]: (t["name"], t["short_name"]) for t in bs["teams"]}
        # per-team candidate name maps (full/web/known names -> web_name)
        by_team: dict[int, dict] = {tid: {} for tid in fpl_teams}
        for e in bs["elements"]:
            m = by_team[e["team"]]
            m[f"{e['first_name']} {e['second_name']}".strip().lower()] = e["web_name"]
            m[e["web_name"].lower()] = e["web_name"]
            if e.get("known_name"):
                m[e["known_name"].lower()] = e["web_name"]
        n_fixtures = sum(1 for f in fpl_api.fixtures()
                         if f.get("event") == fpl_api.next_event()["id"])
        h = s.get(BASE + "/lineups/", headers={"User-Agent": UA["User-Agent"]},
                  timeout=30).text
        m = re.search(r'/lineups/(\d+)/', h)
        if not m:
            return None
        base_id = int(m.group(1))
        xi, teams = [], []
        pages = 0
        for fid in range(base_id, base_id + max(n_fixtures, 10)):
            if pages >= max(n_fixtures, 10):
                break
            r = s.get(f"{BASE}/lineups/{fid}/",
                      headers={"User-Agent": UA["User-Agent"]}, timeout=25)
            if r.status_code != 200 or "versus" not in r.text:
                warnings.append(f"fixture page {fid}: unavailable")
                continue
            pages += 1
            title = r.text.split("<title>")[1].split("</title>")[0]
            home_t, away_t = [t.strip() for t in title.split("versus")]
            names = re.findall(r'data-name="([^"]+)"', r.text)
            if len(names) < 22:
                warnings.append(f"{title}: only {len(names)} player nodes parsed")
            for fix_title, block in ((home_t, names[:11]), (away_t, names[11:22])):
                resolved = _fix_title_to_fpl(fix_title, fpl_teams)
                if not resolved:
                    warnings.append(f"unknown team title: {fix_title}")
                    continue
                tid, tshort = resolved
                if tshort not in teams:
                    teams.append(tshort)
                mapped = 0
                for nm in block:
                    wn = _to_web_name(nm, by_team[tid])  # roster-scoped: no collisions
                    if not wn:
                        # roster-scoped lax fallback (~30 candidates, low risk)
                        import difflib
                        close = difflib.get_close_matches(
                            nm.lower(), list(by_team[tid]), n=1, cutoff=0.6)
                        wn = by_team[tid].get(close[0]) if close else None
                    if wn:
                        xi.append({"name": wn, "team": tshort})
                        mapped += 1
                    else:
                        warnings.append(f"{tshort}: unmapped player '{nm}'")
                if mapped != 11:
                    warnings.append(f"{tshort}: {mapped}/11 starters mapped")
            time.sleep(0.6)  # polite
        if not xi:
            return None
        out = dict(updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   teams=teams, xi=xi, warnings=warnings)
        (DATA / "fix_lineups.json").write_text(json.dumps(out, ensure_ascii=False))
        return out
    except Exception as e:
        (DATA / "fix_lineups.error").write_text(f"{time.time()}: {e!r}")
        return None


# ---------------------------------------------------------------- prices
def prices(max_age: int = 4 * 3600) -> dict | None:
    cached = _fresh("fix_prices.json", max_age)
    if cached:
        return cached
    s = _session()
    if not s:
        return None
    try:
        raw = s.get(BASE + "/price_change_json/1/", headers=UA, timeout=30).json()
        nmap = _nmap()
        rising, falling = [], []
        for p in raw.get("aaData", []):
            ch = str(p.get("change") or "").lower()
            if not ch or "locked" in ch or "next gameweek" in ch:
                continue  # market frozen / no imminent change
            wn = _wn(p.get("name"), nmap) or _wn(p.get("full_name"), nmap)
            rec = dict(name=wn or p.get("name"), team=p.get("team"),
                       value=p.get("value"), change=str(p.get("change")),
                       ownership=p.get("ownership"))
            if "rise" in ch or ch.startswith("+"):
                rising.append(rec)
            elif "fall" in ch or "drop" in ch or ch.startswith("-"):
                falling.append(rec)
        out = dict(updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   locked=all(str(p.get("change")) == "LOCKED"
                              for p in raw.get("aaData", [])[:50]),
                   rising=rising[:15], falling=falling[:15])
        (DATA / "fix_prices.json").write_text(json.dumps(out, ensure_ascii=False))
        return out
    except Exception:
        return None


# ---------------------------------------------------------------- injuries
def injuries(max_age: int = 6 * 3600) -> list | None:
    cached = _fresh("fix_injuries.json", max_age)
    if cached:
        return cached
    s = _session()
    if not s:
        return None
    try:
        nmap = _nmap()
        out = []
        for idx in (1, 2):  # 1 = injuries, 2 = suspensions
            raw = s.get(f"{BASE}/injuries_json/{idx}/", headers=UA, timeout=30).json()
            for p in raw.get("aaData", []):
                if isinstance(p, dict):
                    wn = _wn(p.get("name"), nmap)
                    out.append(dict(name=wn or p.get("name"), team=p.get("team"),
                                    reason=p.get("reason") or p.get("info"),
                                    ret=p.get("return") or p.get("return_date")))
                elif isinstance(p, list) and p:
                    out.append(dict(raw=[str(x)[:40] for x in p[:6]]))
        (DATA / "fix_injuries.json").write_text(json.dumps(out, ensure_ascii=False))
        return out
    except Exception:
        return None


def refresh_all() -> dict:
    st = {}
    a = algo()
    st["algo"] = f"{len(a['players'])} players, base GW{a['gw']}" if a else "off"
    l = lineups()
    st["lineups"] = f"{len(l['teams'])} teams, {len(l['xi'])} predicted starters" if l else "off"
    p = prices()
    if p:
        st["prices"] = "locked (pre-GW)" if p.get("locked") else \
            f"{len(p['rising'])} rising / {len(p['falling'])} falling"
    else:
        st["prices"] = "off"
    i = injuries()
    st["injuries"] = f"{len(i)} entries" if i else "off"
    return st


if __name__ == "__main__":
    print(json.dumps(refresh_all(), indent=1))
