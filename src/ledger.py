"""FPL Edge — prediction ledger (council: the system must remember what it
predicted so it can be scored, calibrated and trusted in March).

data/ledger_proj.csv    append-only: one row per player per pipeline run
                        (gw, generated_ts, id, name, xp, xmins, in_squad, captain)
data/ledger_results.csv append-only: actuals joined to the LAST pre-deadline
                        snapshot for each finished GW (gw, id, xp, actual, err)
"""
from __future__ import annotations
import datetime, json, pathlib
import pandas as pd
import fpl_api

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
PROJ = DATA / "ledger_proj.csv"
RES = DATA / "ledger_results.csv"


def record(gw: int, proj: pd.DataFrame, squad_ids: set, captain_id: int | None):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    cols = ["id", "name", "xp_next", "xmins"] + (["score"] if "score" in proj else [])
    df = proj[cols].copy()
    if "score" not in df:
        df["score"] = df["xp_next"]
    df.insert(0, "gw", gw)
    df.insert(1, "generated", ts)
    df["in_squad"] = df["id"].isin(squad_ids)
    df["captain"] = df["id"] == (captain_id or -1)
    header = not PROJ.exists()
    df.to_csv(PROJ, mode="a", index=False, header=header)


def record_decision(state: dict):
    """Council mandate: the ledger must capture the DECISION context, not just
    predictions — mode, chip, captain, squad, per-source freshness, drift —
    so any pick can be reconstructed months later."""
    f = DATA / "ledger_decisions.jsonl"
    rec = dict(
        ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        gw=state.get("gw"), mode=state.get("mode"),
        chip=(state.get("locked_plan") or {}).get("chip"),
        plan_locked=bool(state.get("locked_plan")),
        captain=(state.get("squad") or {}).get("captain"),
        vice=(state.get("squad") or {}).get("vice"),
        squad=[p["name"] for p in (state.get("squad") or {}).get("xi", [])]
              + [p["name"] for p in (state.get("squad") or {}).get("bench", [])],
        sources=state.get("sources"), fix_feeds=state.get("fix_feeds"),
        odds=state.get("odds"), drift_vs_fix=state.get("calibration_vs_fix"),
        elite=state.get("elite"), league=(state.get("league") or {}).get("my_rank"),
    )
    with f.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def settle() -> dict | None:
    """For every finished GW not yet settled, join actual points to the last
    snapshot recorded before that GW. Returns summary of most recent settle."""
    if not PROJ.exists():
        return None
    bs = fpl_api.bootstrap()
    finished = [e["id"] for e in bs["events"] if e.get("finished")]
    if not finished:
        return None
    led = pd.read_csv(PROJ)
    done = set(pd.read_csv(RES)["gw"].unique()) if RES.exists() else set()
    deadlines = {e["id"]: e["deadline_time"] for e in bs["events"]}
    summary = None
    for gw in finished:
        if gw in done or gw not in set(led["gw"]):
            continue
        snap = led[led.gw == gw]
        # COUNCIL GUARD: only snapshots generated BEFORE the deadline count —
        # a post-deadline rerun must never contaminate calibration.
        dl = deadlines.get(gw)
        if dl:
            pre = snap[snap.generated <= dl]
            if not pre.empty:
                snap = pre
        snap = snap[snap.generated == snap.generated.max()]
        live = fpl_api.event_live(gw)
        actual = {el["id"]: el["stats"]["total_points"] for el in live["elements"]}
        out = snap.copy()
        out["actual"] = out["id"].map(actual)
        out = out.dropna(subset=["actual"])
        out["err"] = out["actual"] - out["xp_next"]
        header = not RES.exists()
        out.to_csv(RES, mode="a", index=False, header=header)
        played = out[out.xmins > 0]
        summary = dict(gw=gw, n=len(played),
                       mae=round(float(played["err"].abs().mean()), 2),
                       bias=round(float(played["err"].mean()), 2))
    return summary


def calibration() -> dict | None:
    """Season-to-date accuracy for the dashboard."""
    if not RES.exists():
        return None
    r = pd.read_csv(RES)
    played = r[r.xmins > 0]
    if played.empty:
        return None
    return dict(gws=int(r["gw"].nunique()),
                mae=round(float(played["err"].abs().mean()), 2),
                bias=round(float(played["err"].mean()), 2),
                squad_mae=round(float(played[played.in_squad]["err"].abs().mean()), 2)
                if played.in_squad.any() else None)
