"""FPL Edge — Fantasy Football Fix ingestion (authenticated, personal use).

Uses Pramod's own paid-account session cookie (config.json "fix_sessionid",
git-ignored) to pull the Elite XI Reveal page and derive:

  data/elite_teams.json (schema v2): per-GW elite ownership + captain shares
                                     + per-team squads/captains
  data/fix_proj5.json:               Fix's 5-GW projected points for players
                                     in the elite ownership table (bonus feed)

Polite: one page fetch per refresh, cached 6h. Cookie invalid -> returns None
and report.py surfaces "fix: cookie expired" in sources; the manual PDF-paste
path keeps working as fallback.
"""
from __future__ import annotations
import json, pathlib, re, time
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
URL = "https://www.fantasyfootballfix.com/reveal/"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def _cookie() -> str | None:
    try:
        return json.loads((ROOT / "config.json").read_text()).get("fix_sessionid")
    except Exception:
        return None


def fetch_page() -> str | None:
    ck = _cookie()
    if not ck:
        return None
    s = requests.Session()
    s.cookies.set("sessionid", ck, domain=".fantasyfootballfix.com")
    r = s.get(URL, headers=UA, timeout=40)
    if r.status_code != 200 or "Elite XI" not in r.text:
        return None
    # paywalled content check: ownership table only renders for subscribers
    if "League ownership" not in r.text:
        return None
    return r.text


def _fpl_name_map():
    import fpl_api
    els = fpl_api.bootstrap()["elements"]
    m = {}
    for e in els:
        full = f"{e['first_name']} {e['second_name']}".strip().lower()
        m[full] = e["web_name"]
        m[e["web_name"].lower()] = e["web_name"]
        if e.get("known_name"):
            m[e["known_name"].lower()] = e["web_name"]
    return m


def _to_web_name(name: str, nmap: dict) -> str | None:
    n = name.strip().lower()
    if n in nmap:
        return nmap[n]
    # try last token(s)
    toks = n.split()
    for k in range(len(toks) - 1, 0, -1):
        cand = " ".join(toks[k:])
        if cand in nmap:
            return nmap[cand]
    import difflib
    close = difflib.get_close_matches(n, list(nmap), n=1, cutoff=0.85)
    return nmap[close[0]] if close else None


def parse(html: str) -> dict | None:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    gw_m = re.search(r"Gameweek (\d+)", html)
    gw = int(gw_m.group(1)) if gw_m else None
    nmap = _fpl_name_map()

    # --- ownership table (+ Fix 5-GW projections) ---------------------------
    ownership, proj5 = {}, {}
    table = soup.find(id="table_ownership")
    if table:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            a = tr.find("a", class_="open-playermodal")
            raw = (a.get("data-name") if a else tds[1].get_text(" ", strip=True)) or ""
            wn = _to_web_name(raw, nmap)
            if not wn:
                continue
            def num(td):
                t = td.get_text(strip=True).replace("%", "")
                try:
                    return float(t)
                except ValueError:
                    return None
            vals = [num(td) for td in tds]
            nums = [v for v in vals if v is not None]
            # columns: value, proj5, ownership%, above%, below% — ownership is
            # the first value that looks like a percentage of the league
            if len(nums) >= 3:
                proj5[wn] = nums[1]
                ownership[wn] = round(nums[2] / 100.0, 4)

    # --- per-manager squads + captains (structural, per flip-card block) ----
    teams = []
    for b in soup.select(".flip-card-wrap"):
        classes = " ".join(sum((el.get("class") or [] for el in b.find_all("div", limit=6)), []))
        is_consensus = "consensus" in classes
        name = None
        for t in b.find_all(["h2", "h3", "h4", "strong"]):
            txt = t.get_text(strip=True)
            if txt and 2 < len(txt) < 35 and "XI" not in txt and "Bio" not in txt \
                    and "Update" not in txt:
                name = txt
                break
        players, cap, vice, last = [], None, None, None
        for a in b.find_all("a"):
            cls = a.get("class") or []
            if "open-playermodal" in cls:
                nm = _to_web_name(a.get("data-name") or "", nmap)
                if nm and nm not in players and len(players) < 15:
                    players.append(nm)
                last = nm
            elif a.get("title") == "Captain" and last and cap is None:
                cap = last
            elif a.get("title") == "Vice-captain" and last and vice is None:
                vice = last
        if len(players) >= 11 and not is_consensus:
            teams.append(dict(manager=name or "unknown", players=players,
                              captain=cap, vice=vice))

    caps = {}
    for t in teams:
        if t.get("captain"):
            caps[t["captain"]] = caps.get(t["captain"], 0) + 1
    ncap = max(sum(caps.values()), 1)
    captains = {k: round(v / ncap, 2) for k, v in
                sorted(caps.items(), key=lambda x: -x[1])}

    if not ownership and not teams:
        return None
    return dict(gw=gw, updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                source="fantasyfootballfix.com/reveal (auto, session cookie)",
                n_teams=len(teams) or None,
                captains=captains, ownership=ownership,
                teams=teams), proj5


def refresh(max_age: int = 6 * 3600) -> dict | None:
    out_f = DATA / "elite_teams.json"
    if out_f.exists() and time.time() - out_f.stat().st_mtime < max_age:
        try:
            d = json.loads(out_f.read_text())
            if d.get("source", "").startswith("fantasyfootballfix.com"):
                return d
        except Exception:
            pass
    html = fetch_page()
    if not html:
        return None
    parsed = parse(html)
    if not parsed:
        return None
    data, proj5 = parsed
    out_f.write_text(json.dumps(data, ensure_ascii=False))
    if proj5:
        (DATA / "fix_proj5.json").write_text(json.dumps(proj5, ensure_ascii=False))
    return data


if __name__ == "__main__":
    d = refresh(max_age=0)
    if d is None:
        print("Fix fetch failed (no cookie / expired / paywall)")
    else:
        print(f"GW{d['gw']} | teams parsed: {d['n_teams']} | ownership rows: {len(d['ownership'])}")
        print("captains:", d["captains"])
        top = sorted(d["ownership"].items(), key=lambda x: -x[1])[:10]
        print("top owned:", top)
