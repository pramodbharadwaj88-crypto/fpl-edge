"""FPL Edge — dumb post-deadline snapshot (runs in GitHub Actions, secret-free).

Council v4 degraded-hybrid: this script touches ONLY the public FPL API and
commits ONLY post-deadline facts — never recommendations, never credentials.
Appends per-GW facts to data/season/ and maintains the paired model-vs-actual
scoreboard when a pre-registered model XI reveal exists for that GW.
"""
import json, pathlib, sys, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "season"
OUT.mkdir(parents=True, exist_ok=True)

TEAM_ID = 3314291
LEAGUE_ID = 939099
BASE = "https://fantasy.premierleague.com/api"


def get(path):
    req = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    bs = get("bootstrap-static/")
    finished = [e["id"] for e in bs["events"] if e.get("finished")]
    if not finished:
        print("no finished GW yet; nothing to snapshot")
        return 0
    gw = max(finished)
    marker = OUT / f"gw{gw:02d}.json"
    if marker.exists():
        print(f"gw{gw} already snapshotted")
        return 0
    byid = {e["id"]: e for e in bs["elements"]}
    live = get(f"event/{gw}/live/")
    pts = {e["id"]: e["stats"]["total_points"] for e in live["elements"]}
    picks = get(f"entry/{TEAM_ID}/event/{gw}/picks/")
    entry = get(f"entry/{TEAM_ID}/")
    league = get(f"leagues-classic/{LEAGUE_ID}/standings/")
    my_players = [dict(id=p["element"], name=byid[p["element"]]["web_name"],
                       mult=p["multiplier"], points=pts.get(p["element"], 0))
                  for p in picks["picks"]]
    my_total = sum(p["points"] * max(p["mult"], 0) for p in my_players)
    snap = dict(
        gw=gw, chip=picks.get("active_chip"),
        my_players=my_players, my_gw_points=my_total,
        overall_points=entry.get("summary_overall_points"),
        league=[dict(rank=r["rank"], entry=r["entry"], team=r["entry_name"],
                     manager=r["player_name"], gw=r["event_total"], total=r["total"])
                for r in league["standings"]["results"]],
        # per-player actuals for ledger settlement by the main session
        actuals={str(e["id"]): e["stats"]["total_points"] for e in live["elements"]},
    )
    # paired experiment: score a pre-registered model XI if one was revealed
    reveal = OUT / f"model_reveal_gw{gw:02d}.json"
    if reveal.exists():
        mr = json.loads(reveal.read_text())
        model_pts = sum(pts.get(pid, 0) for pid in mr.get("xi_ids", []))
        cap = mr.get("captain_id")
        model_pts += pts.get(cap, 0) if cap else 0
        snap["model_xi_points"] = model_pts
        snap["paired_delta"] = my_total - model_pts
    marker.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
    # cumulative scoreboard
    board = OUT / "scoreboard.json"
    b = json.loads(board.read_text()) if board.exists() else {"gws": []}
    b["gws"] = [g for g in b["gws"] if g["gw"] != gw] + [dict(
        gw=gw, my=my_total, model=snap.get("model_xi_points"),
        delta=snap.get("paired_delta"))]
    b["cumulative_delta"] = sum(g["delta"] or 0 for g in b["gws"])
    board.write_text(json.dumps(b, indent=1))
    print(f"snapshotted gw{gw}: my {my_total} pts, model {snap.get('model_xi_points')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
