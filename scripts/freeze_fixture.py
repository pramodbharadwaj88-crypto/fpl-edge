"""Freeze a Bundle into tests/fixtures/<name>/ for regression tests.

  python3 scripts/freeze_fixture.py NAME --now 2026-10-30T20:10:00Z \
      --bootstrap PATH --picks PATH --elite PATH [--fixtures PATH]
      [--history PATH] [--transfers PATH] [--before-gw N]

Public FPL data is trimmed to the fields the code uses. Elite boards are
NEVER copied: they come from a paid account and this repository is public, so
the fixture gets synthetic boards with the same per-player counts
(scripts/synthesize_boards.py). Element summaries are fetched from the API and
cut to the rows before --before-gw so a replay sees only what was known.
"""
from __future__ import annotations
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

EL_FIELDS = ("id", "code", "web_name", "first_name", "second_name", "known_name", "team",
             "element_type", "now_cost", "cost_change_start", "status",
             "chance_of_playing_next_round", "news", "selected_by_percent",
             "price_change_projections", "minutes", "starts")
HIST_FIELDS = ("round", "fixture", "kickoff_time", "minutes", "starts", "value", "total_points")


def trim_bootstrap(bs: dict) -> dict:
    return dict(
        game_settings=bs["game_settings"],
        element_types=[{k: et[k] for k in ("id", "singular_name_short", "squad_select",
                                           "squad_min_play", "squad_max_play")}
                       for et in bs["element_types"]],
        teams=[{k: t[k] for k in ("id", "name", "short_name", "code")} for t in bs["teams"]],
        events=[{k: e.get(k) for k in ("id", "deadline_time", "finished", "is_current", "is_next")}
                for e in bs["events"]],
        elements=[{k: e.get(k) for k in EL_FIELDS} for e in bs["elements"]])


def synthetic_elite(elite: dict, bs: dict) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    from synthesize_boards import synthesize
    return synthesize(elite, {e["id"]: e for e in bs["elements"]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--now", required=True)
    ap.add_argument("--bootstrap", required=True)
    ap.add_argument("--picks", required=True)
    ap.add_argument("--elite", required=True)
    ap.add_argument("--fixtures", default=str(ROOT / "data/fixtures.json"))
    ap.add_argument("--history", default=str(ROOT / "data/entryhist_3314291.json"))
    ap.add_argument("--transfers", default=str(ROOT / "data/entrytr_3314291.json"))
    ap.add_argument("--team", type=int, default=3314291)
    ap.add_argument("--before-gw", type=int, required=True)
    a = ap.parse_args()
    import fpl_api

    out = ROOT / "tests" / "fixtures" / a.name
    out.mkdir(parents=True, exist_ok=True)
    bs = json.loads(pathlib.Path(a.bootstrap).read_text())
    picks = json.loads(pathlib.Path(a.picks).read_text())
    elite = json.loads(pathlib.Path(a.elite).read_text())
    gw = a.before_gw
    hist = json.loads(pathlib.Path(a.history).read_text())
    hist = dict(current=[r for r in hist["current"] if r["event"] < gw],
                chips=[c for c in hist.get("chips", []) if c["event"] < gw])
    tr = [t for t in json.loads(pathlib.Path(a.transfers).read_text()) if t["event"] < gw]
    fx = [{k: f.get(k) for k in ("id", "event", "team_h", "team_a", "kickoff_time", "finished")}
          for f in json.loads(pathlib.Path(a.fixtures).read_text())
          if f.get("event") in (gw - 1, gw, gw + 1)]

    by = {e["id"]: e for e in bs["elements"]}
    need = {p["element"] for p in picks["picks"]}
    for t in elite["teams"]:
        need |= {p for p in t["player_ids"] if p}
    clubs = {by[p]["team"] for p in need if p in by}
    need |= {e["id"] for e in bs["elements"] if e["element_type"] == 1 and e["team"] in clubs}
    summaries = {}
    for pid in sorted(need):
        s = fpl_api.element_summary(pid)
        summaries[pid] = dict(history=[{k: r.get(k) for k in HIST_FIELDS}
                                       for r in s["history"] if r["round"] < gw])

    w = lambda n, d: (out / n).write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    w("bootstrap.json", trim_bootstrap(bs))
    w("fixtures.json", fx)
    w("elite.json", synthetic_elite(elite, bs))
    w("entry_history.json", hist)
    w("transfers.json", tr)
    w("picks.json", {k: picks[k] for k in ("active_chip", "entry_history", "picks")})
    w("summaries.json", summaries)
    w("meta.json", dict(now=a.now, picks_gw=picks["entry_history"]["event"], team_id=a.team,
                        fetched_at={k: a.now for k in ("bootstrap", "fixtures", "entry_history",
                                                       "transfers", "picks", "elite")}))
    print(f"froze {out} ({len(summaries)} summaries)")


if __name__ == "__main__":
    main()
