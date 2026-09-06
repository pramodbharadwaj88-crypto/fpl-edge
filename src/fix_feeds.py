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
            # Fix schema (verified 2026-09-06): change in {"Tonight","Tomorrow",
            # "> 2 days","LOCKED"}; threshold = progress toward the change,
            # sign gives direction (+ rise / - fall), |100| = certain.
            ch = str(p.get("change") or "")
            thr = float(p.get("threshold") or 0.0)
            if ch not in ("Tonight", "Tomorrow"):
                continue  # not imminent / market locked
            wn = _wn(p.get("name"), nmap) or _wn(p.get("full_name"), nmap)
            rec = dict(name=wn or p.get("name"), team=p.get("team"),
                       value=p.get("value"), when=ch, progress=round(thr, 1),
                       ownership=p.get("ownership"))
            (rising if thr > 0 else falling).append(rec)
        rising.sort(key=lambda r: -r["progress"]); falling.sort(key=lambda r: r["progress"])
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
            # Fix schema (verified 2026-09-06): change in {"Tonight","Tomorrow",
            # "> 2 days","LOCKED"}; threshold = progress toward the change,
            # sign gives direction (+ rise / - fall), |100| = certain.
            ch = str(p.get("change") or "")
            thr = float(p.get("threshold") or 0.0)
            if ch not in ("Tonight", "Tomorrow"):
                continue  # not imminent / market locked
            wn = _wn(p.get("name"), nmap) or _wn(p.get("full_name"), nmap)
            rec = dict(name=wn or p.get("name"), team=p.get("team"),
                       value=p.get("value"), when=ch, progress=round(thr, 1),
                       ownership=p.get("ownership"))
            (rising if thr > 0 else falling).append(rec)
        rising.sort(key=lambda r: -r["progress"])
        falling.sort(key=lambda r: r["progress"])
        out = dict(updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   locked=all(str(p.get("change")) == "LOCKED"
                              for p in raw.get("aaData", [])[:50]),
                   rising=rising[:25], falling=falling[:25])
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
