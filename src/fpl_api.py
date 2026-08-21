"""FPL Edge — API client. All public (unauthenticated) endpoints.

Read-only: this bot suggests moves; the human applies them in the app.
"""
from __future__ import annotations
import json, time, pathlib
import requests

BASE = "https://fantasy.premierleague.com/api"
CACHE = pathlib.Path(__file__).resolve().parent.parent / "data"
CACHE.mkdir(exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def _get(path: str, cache_name: str | None = None, max_age: int = 900):
    """GET with simple file cache (seconds)."""
    if cache_name:
        f = CACHE / f"{cache_name}.json"
        if f.exists() and (time.time() - f.stat().st_mtime) < max_age:
            return json.loads(f.read_text())
    r = requests.get(f"{BASE}/{path}", headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_name:
        (CACHE / f"{cache_name}.json").write_text(json.dumps(data))
    return data


def bootstrap():
    return _get("bootstrap-static/", "bootstrap", max_age=900)


def fixtures():
    return _get("fixtures/", "fixtures", max_age=900)


def element_summary(pid: int):
    return _get(f"element-summary/{pid}/", f"element_{pid}", max_age=6 * 3600)


def event_live(gw: int):
    return _get(f"event/{gw}/live/", f"live_{gw}", max_age=300)


def entry(team_id: int):
    return _get(f"entry/{team_id}/", f"entry_{team_id}", max_age=3600)


def entry_history(team_id: int):
    return _get(f"entry/{team_id}/history/", f"entryhist_{team_id}", max_age=3600)


def entry_picks(team_id: int, gw: int):
    return _get(f"entry/{team_id}/event/{gw}/picks/", f"picks_{team_id}_{gw}", max_age=3600)


def league_standings(league_id: int, page: int = 1):
    return _get(f"leagues-classic/{league_id}/standings/?page_standings={page}",
                f"league_{league_id}_p{page}", max_age=3600)


def set_piece_notes():
    return _get("team/set-piece-notes/", "setpieces", max_age=24 * 3600)


def next_event(bs=None):
    bs = bs or bootstrap()
    for e in bs["events"]:
        if e.get("is_next"):
            return e
    for e in bs["events"]:
        if not e.get("finished"):
            return e
    return None
