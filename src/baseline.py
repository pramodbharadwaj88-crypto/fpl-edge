"""FPL Edge — last-season baseline snapshot (fixes the season-reset time bomb).

The bootstrap `elements` fields (minutes, starts, xG/xA per 90, DEFCON counts,
bonus, saves) are CUMULATIVE FOR THE CURRENT SEASON and reset to zero once
GW1 kicks off. This module freezes the pre-kickoff snapshot (= full 2025/26
season) to data/baseline_prev.json, keyed by player `code` (stable across
seasons, unlike element id).

Downstream, projections blend baseline per-90 rates with current-season rates
using minutes-based shrinkage: w_cur = cur_mins / (cur_mins + SHRINK_MINS).
Denominators use games actually elapsed, never a hard-coded 38.
"""
from __future__ import annotations
import json, pathlib
import fpl_api

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
FILE = DATA / "baseline_prev.json"

FIELDS = ["minutes", "starts", "goals_scored", "assists", "bonus", "saves",
          "clean_sheets", "expected_goals", "expected_assists",
          "clearances_blocks_interceptions", "tackles", "recoveries",
          "defensive_contribution", "total_points"]


def snapshot(force: bool = False) -> dict:
    """Freeze current bootstrap element stats as the previous-season baseline.
    Safe to call any time BEFORE the first GW kicks off; refuses to overwrite
    once a season is underway unless force=True."""
    bs = fpl_api.bootstrap()
    started = any(e.get("finished") or e.get("is_current") for e in bs["events"])
    if FILE.exists() and started and not force:
        return json.loads(FILE.read_text())
    out = {"season": "2025-26", "games": 38, "players": {}}
    for el in bs["elements"]:
        if float(el.get("minutes") or 0) <= 0:
            continue
        out["players"][str(el["code"])] = {f: float(el.get(f) or 0) for f in FIELDS}
    # COUNCIL REFUSAL GUARD: never clobber a healthy baseline with a zeroed or
    # gutted one (e.g. FPL wipes stats before events are flagged current).
    if FILE.exists():
        old = json.loads(FILE.read_text())
        old_mins = sum(p["minutes"] for p in old.get("players", {}).values())
        new_mins = sum(p["minutes"] for p in out["players"].values())
        if old_mins > 0 and new_mins < 0.2 * old_mins and not force:
            return old
    FILE.write_text(json.dumps(out))
    return out


def load() -> dict:
    if FILE.exists():
        return json.loads(FILE.read_text())
    return snapshot()


def games_elapsed(bs=None) -> int:
    """Number of GWs finished this season (0 pre-season)."""
    bs = bs or fpl_api.bootstrap()
    return sum(1 for e in bs["events"] if e.get("finished"))


def blended_stats(el: dict, base: dict, n_games: int, shrink_mins: float) -> dict | None:
    """Combine current-season cumulative stats with last-season baseline into
    per-90 rates. Returns None if neither source has minutes (no PL history).

    w_cur = cur_mins / (cur_mins + shrink_mins): ~0 early season, ->1 as the
    current season accumulates. Baseline per-90s anchor the early weeks."""
    cur_mins = float(el.get("minutes") or 0)
    prev = base.get("players", {}).get(str(el["code"]))
    prev_mins = float(prev["minutes"]) if prev else 0.0
    # COUNCIL GUARD (boundary invariant): before any GW has finished, the
    # bootstrap's "current season" fields still hold LAST season's cumulative
    # stats (the snapshot's own premise). Using them as current-season data
    # double-counts and, with cur_games=1, saturates start_share at 1.0 for
    # every fringe player. Pre-season: baseline only.
    if n_games <= 0:
        cur_mins = 0.0
    if cur_mins <= 0 and prev_mins <= 0:
        return None

    def per90(src_val, src_mins):
        return src_val / src_mins * 90.0 if src_mins > 0 else 0.0

    def cur(f):
        return float(el.get(f) or 0)

    def prv(f):
        return float(prev[f]) if prev else 0.0

    w = cur_mins / (cur_mins + shrink_mins) if cur_mins > 0 else 0.0
    if prev_mins <= 0:
        w = 1.0  # new player: current season only, however thin

    out = {}
    for f in ["expected_goals", "expected_assists", "bonus", "saves",
              "clearances_blocks_interceptions", "tackles", "recoveries"]:
        out[f + "_p90"] = w * per90(cur(f), cur_mins) + (1 - w) * per90(prv(f), prev_mins)

    # minutes structure, per game elapsed (never /38 hard-coded)
    cur_games = max(n_games, 1)
    prev_games = base.get("games", 38)
    start_share = (w * (cur("starts") / cur_games)
                   + (1 - w) * (prv("starts") / prev_games))
    mins_per_start_cur = cur_mins / max(cur("starts"), 1.0) if cur("starts") else 0.0
    mins_per_start_prv = prev_mins / max(prv("starts"), 1.0) if prev and prv("starts") else 0.0
    mins_per_start = w * mins_per_start_cur + (1 - w) * mins_per_start_prv
    if mins_per_start <= 0:
        mins_per_start = 65.0
    eff_mins = w * cur_mins + (1 - w) * prev_mins  # reliability measure
    out.update(start_share=min(start_share, 1.0),
               mins_per_start=min(mins_per_start, 90.0),
               eff_mins=eff_mins, w_current=w)
    return out


if __name__ == "__main__":
    b = snapshot()
    print(f"baseline players: {len(b['players'])}, games={b['games']}")
    print("games elapsed this season:", games_elapsed())
