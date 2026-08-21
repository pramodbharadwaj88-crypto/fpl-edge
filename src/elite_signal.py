"""FPL Edge — elite-manager consensus signal (Fantasy Football Fix "Reveal").

Pramod has paid access to fantasyfootballfix.com/reveal (pro/elite managers'
teams posted weekly). That content is ingested MANUALLY (pasted into the chat
and parsed by Claude into data/elite_teams.json) or via best-effort authed
fetch if a session cookie is configured. Schema:

data/elite_teams.json:
{
  "gw": 1,
  "updated": "2026-08-21T10:00:00Z",
  "teams": [
    {"manager": "Name/handle", "players": ["Haaland","Saka", ... 15 web_names],
     "xi": ["..."] (optional), "captain": "Haaland"}
  ]
}

Signals derived:
  elite_eo(): per-player elite effective ownership (captain double-weighted)
  captain_share(): captaincy distribution among elite teams
  divergence(): where our squad/captain differs from elite consensus

Used for: dashboard panel, briefing divergence flags, a small configurable
`elite_tilt` on the strategy `score` (NEVER on xp_next), and as the rival-
behavior prior before league picks become public each GW.
"""
from __future__ import annotations
import json, pathlib, time
import pandas as pd

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
FILE = DATA / "elite_teams.json"


def load(gw: int | None = None) -> dict | None:
    if not FILE.exists():
        return None
    try:
        d = json.loads(FILE.read_text())
    except Exception:
        return None
    if gw is not None and d.get("gw") != gw:
        return None  # stale — reveals are per-GW
    return d if (d.get("teams") or d.get("ownership")) else None


def _name_to_id(proj: pd.DataFrame) -> dict:
    m = {}
    for r in proj.itertuples():
        m[r.name.lower()] = int(r.id)
    return m


def elite_eo(proj: pd.DataFrame, gw: int | None = None) -> pd.Series:
    """Elite effective ownership per element id (captain counts double).

    Schema v2: when the file carries a direct "ownership" dict ({web_name:
    share 0-1}) and "captains" ({web_name: share}), use those (this matches
    FF Fix's own Elite XI ownership table). Falls back to per-team lists."""
    d = load(gw)
    if not d:
        return pd.Series(dtype=float)
    nm = _name_to_id(proj)
    if d.get("ownership"):
        caps = d.get("captains", {})
        out = {}
        for name, share in d["ownership"].items():
            pid = nm.get(name.lower())
            if pid is not None:
                out[pid] = float(share) + float(caps.get(name, 0.0))
        return pd.Series(out)
    counts: dict[int, float] = {}
    n = len(d["teams"])
    for t in d["teams"]:
        cap = (t.get("captain") or "").lower()
        for p in t.get("players", []):
            pid = nm.get(p.lower())
            if pid is None:
                continue
            w = 2.0 if p.lower() == cap else 1.0
            counts[pid] = counts.get(pid, 0.0) + w
    return pd.Series(counts) / max(n, 1)


def captain_share(gw: int | None = None) -> dict:
    d = load(gw)
    if not d:
        return {}
    if d.get("captains"):
        return {k: round(float(v), 2) for k, v in
                sorted(d["captains"].items(), key=lambda x: -x[1])}
    caps: dict[str, int] = {}
    for t in d["teams"]:
        c = t.get("captain")
        if c:
            caps[c] = caps.get(c, 0) + 1
    n = max(sum(caps.values()), 1)
    return {k: round(v / n, 2) for k, v in
            sorted(caps.items(), key=lambda x: -x[1])}


def divergence(proj: pd.DataFrame, squad_names: list[str], my_captain: str,
               gw: int | None = None, top_n: int = 8) -> dict | None:
    """Where we differ from the elite template."""
    eo = elite_eo(proj, gw)
    if eo.empty:
        return None
    d = load(gw)
    byid = proj.set_index("id")
    ranked = eo.sort_values(ascending=False)
    template = [byid.loc[i, "name"] for i in ranked.head(15).index if i in byid.index]
    ours = {s.lower() for s in squad_names}
    missing = [n for n in template[:top_n] if n.lower() not in ours]
    caps = captain_share(gw)
    n_teams = d.get("n_teams") or len(d.get("teams", []))
    return dict(n_teams=n_teams, top_template=template[:top_n],
                we_lack=missing, elite_captains=caps, our_captain=my_captain,
                captain_backed=round(caps.get(my_captain, 0.0), 2))
