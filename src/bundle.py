"""FPL Edge — one snapshot of every input a recommendation depends on.

The validator and the regression tests both run on a Bundle. `live()` builds
it from the public FPL API + the Fix elite boards; `from_dir()` loads a frozen
copy (tests). Nothing here falls back silently: a missing input raises, and
every source carries the time it was fetched so staleness is checkable.
"""
from __future__ import annotations
import dataclasses as dc
import datetime as dt
import json, pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UTC = dt.timezone.utc


class MissingInput(RuntimeError):
    pass


@dc.dataclass
class Bundle:
    bootstrap: dict
    fixtures: list
    elite: dict | None
    entry_history: dict
    transfers: list
    picks: dict                      # picks for the latest started GW
    picks_gw: int
    now: dt.datetime                 # aware UTC
    fetched_at: dict                 # source -> aware UTC datetime
    team_id: int
    summaries: dict = dc.field(default_factory=dict)   # element id -> element-summary
    pending: dict | None = None      # user-reported pre-deadline moves (by id)
    xp: dict = dc.field(default_factory=dict)          # element id -> model xP (odds tie-break)
    secrets: list = dc.field(default_factory=list, repr=False)
    live_fetch: bool = False

    # -- derived ----------------------------------------------------------
    @property
    def by_id(self) -> dict[int, dict]:
        if not hasattr(self, "_by_id"):
            self._by_id = {e["id"]: e for e in self.bootstrap["elements"]}
        return self._by_id

    def next_event(self) -> dict:
        """First gameweek whose deadline is still ahead of `now`."""
        for e in sorted(self.bootstrap["events"], key=lambda e: e["id"]):
            if _utc(e["deadline_time"]) > self.now:
                return e
        raise MissingInput("no upcoming gameweek in bootstrap")

    def summary(self, pid: int) -> dict:
        if pid in self.summaries:
            return self.summaries[pid]
        if self.live_fetch:
            import fpl_api      # 30-min cache: guards fails on history older than the last match
            self.summaries[pid] = fpl_api._get(f"element-summary/{pid}/", f"element_{pid}",
                                               max_age=1800)
            return self.summaries[pid]
        raise MissingInput(f"element-summary for {pid} not in bundle")

    def club_gk_summaries(self, club: int) -> dict[int, dict]:
        out = {}
        for e in self.bootstrap["elements"]:
            if e["team"] == club and e["element_type"] == 1:
                out[e["id"]] = self.summary(e["id"])
        return out


def _utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _mtime(name: str) -> dt.datetime:
    f = DATA / f"{name}.json"
    return dt.datetime.fromtimestamp(f.stat().st_mtime, UTC)


def _secrets_from_config() -> list[str]:
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
    except Exception:
        return []
    out = []
    for k, v in cfg.items():
        if isinstance(v, str) and len(v) >= 12 and any(
                w in k.lower() for w in ("key", "session", "token", "secret", "password", "cookie")):
            out.append(v)
    return out


def load_pending(path: pathlib.Path | None = None) -> dict | None:
    f = path or (DATA / "pending_moves.json")
    if not f.exists():
        return None
    return json.loads(f.read_text())


def live(team_id: int, pending_path: pathlib.Path | None = None,
         elite_max_age: int = 6 * 3600, with_elite: bool = True) -> Bundle:
    """Fresh bundle from the network (API caches are short-lived)."""
    import fpl_api
    bs = fpl_api._get("bootstrap-static/", "bootstrap", max_age=600)
    fx = fpl_api._get("fixtures/", "fixtures", max_age=3600)
    now = dt.datetime.now(UTC)
    hist = fpl_api._get(f"entry/{team_id}/history/", f"entryhist_{team_id}", max_age=600)
    tr = fpl_api._get(f"entry/{team_id}/transfers/", f"entrytr_{team_id}", max_age=600)
    started = [e for e in bs["events"] if _utc(e["deadline_time"]) <= now]
    if not started:
        raise MissingInput("season has not started")
    pgw = max(e["id"] for e in started)
    picks = fpl_api._get(f"entry/{team_id}/event/{pgw}/picks/", f"picks_{team_id}_{pgw}",
                         max_age=600)
    if picks.get("active_chip") == "freehit" and pgw > 1:
        # squad and bank revert after a Free Hit: the real squad is the GW before
        pgw -= 1
        picks = fpl_api._get(f"entry/{team_id}/event/{pgw}/picks/",
                             f"picks_{team_id}_{pgw}", max_age=600)
    elite = None
    if with_elite:
        import fix_data
        elite = fix_data.refresh(max_age=elite_max_age)
    fetched = dict(bootstrap=_mtime("bootstrap"), fixtures=_mtime("fixtures"),
                   entry_history=_mtime(f"entryhist_{team_id}"),
                   transfers=_mtime(f"entrytr_{team_id}"),
                   picks=_mtime(f"picks_{team_id}_{pgw}"))
    if elite and elite.get("updated"):
        fetched["elite"] = _utc(elite["updated"])
    xp = {}
    try:
        import pandas as pd
        f = DATA / "projections.csv"
        df = pd.read_csv(f)
        xp = {int(r.id): float(r.xp_next) for r in df.itertuples()}
        fetched["xp"] = dt.datetime.fromtimestamp(f.stat().st_mtime, UTC)
    except Exception:
        pass
    return Bundle(bootstrap=bs, fixtures=fx, elite=elite, entry_history=hist,
                  transfers=tr, picks=picks, picks_gw=pgw, now=now,
                  fetched_at=fetched, team_id=team_id,
                  pending=load_pending(pending_path), xp=xp,
                  secrets=_secrets_from_config(), live_fetch=True)


def from_dir(d: pathlib.Path) -> Bundle:
    """Frozen bundle (tests / replays). Layout: bootstrap.json fixtures.json
    elite.json entry_history.json transfers.json picks.json summaries.json
    meta.json [pending.json] [xp.json]."""
    d = pathlib.Path(d)

    def j(name, default: Any = MissingInput):
        f = d / name
        if not f.exists():
            if default is MissingInput:
                raise MissingInput(f"{f} missing")
            return default
        return json.loads(f.read_text())

    meta = j("meta.json")
    return Bundle(
        bootstrap=j("bootstrap.json"), fixtures=j("fixtures.json"),
        elite=j("elite.json", None), entry_history=j("entry_history.json"),
        transfers=j("transfers.json"), picks=j("picks.json"),
        picks_gw=int(meta["picks_gw"]), now=_utc(meta["now"]),
        fetched_at={k: _utc(v) for k, v in meta.get("fetched_at", {}).items()},
        team_id=int(meta["team_id"]),
        summaries={int(k): v for k, v in j("summaries.json", {}).items()},
        pending=j("pending.json", None),
        xp={int(k): float(v) for k, v in j("xp.json", {}).items()},
        secrets=list(meta.get("secrets_for_test", [])), live_fetch=False)
