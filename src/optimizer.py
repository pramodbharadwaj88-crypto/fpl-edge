"""FPL Edge — MILP squad optimizer (HiGHS via highspy).

Council-mandated properties:
  * Optimizes on `score` (strategy-adjusted) when present, else pure `xp_next`;
    all REPORTED numbers are pure xP. The two never mix in one column.
  * min-keep is a real MILP constraint row (sum of current players >= k),
    not a greedy lock heuristic.
  * Transfer budgets use true FPL sell prices (purchase + floor(profit/2)),
    computed by squad_state from the API transfer record and passed in.
Weights come from config.json "model" via model_config.
"""
from __future__ import annotations
import json, pathlib
import pandas as pd
import numpy as np
import highspy
import model_config as mc
from fpl_rules import (POS_SQUAD, POS_MIN_XI, POS_MAX_XI, BUDGET, MAX_PER_CLUB,
                       HIT_COST, sell_price, hits_for)

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def solve_squad(df: pd.DataFrame, budget: int = BUDGET,
                locked: list[int] | None = None, banned: list[int] | None = None,
                keep_ids: list[int] | None = None, min_keep: int = 0,
                price_override: dict | None = None) -> dict:
    """price_override: {player_id: tenths} — sell prices for currently-owned
    players so transfer budgets reflect the 50% sell-on rule."""
    df = df.reset_index(drop=True)
    n = len(df)
    obj_col = "score" if "score" in df.columns else "xp_next"
    xp1 = df[obj_col].to_numpy(dtype=float)
    fut = (df["horizon"] - df["xp_next"]).clip(lower=0).to_numpy(dtype=float)
    price = (df["price"] * 10).round().astype(int).to_numpy().copy()
    if price_override:
        for pid, tenths in price_override.items():
            i = df.index[df["id"] == pid]
            if len(i):
                price[int(i[0])] = int(tenths)
    etype = df["element_type"].to_numpy()
    team = df["team_id"].to_numpy()
    bench_w = mc.model("bench_weight")
    future_w = mc.model("future_weight")

    h = highspy.Highs()
    h.silent()
    inf = highspy.kHighsInf
    idx_s = list(range(0, n)); idx_l = list(range(n, 2 * n)); idx_c = list(range(2 * n, 3 * n))
    ncols = 3 * n
    obj = np.concatenate([bench_w * xp1 + future_w * fut,
                          (1 - bench_w) * xp1,
                          xp1])
    h.addVars(ncols, np.zeros(ncols), np.ones(ncols))
    h.changeColsIntegrality(ncols, np.arange(ncols, dtype=np.int32),
                            np.array([highspy.HighsVarType.kInteger] * ncols))
    h.changeColsCost(ncols, np.arange(ncols, dtype=np.int32), -obj)

    def add_row(cols, coefs, lo, hi):
        h.addRow(lo, hi, len(cols), np.array(cols, dtype=np.int32), np.array(coefs, dtype=float))

    add_row(idx_s, [1] * n, 15, 15)
    for et, cnt in POS_SQUAD.items():
        cols = [i for i in range(n) if etype[i] == et]
        add_row(cols, [1] * len(cols), cnt, cnt)
    add_row(idx_s, price.tolist(), 0, budget)
    for t in np.unique(team):
        cols = [i for i in range(n) if team[i] == t]
        add_row(cols, [1] * len(cols), 0, MAX_PER_CLUB)
    add_row(idx_l, [1] * n, 11, 11)
    for et in POS_SQUAD:
        cols = [n + i for i in range(n) if etype[i] == et]
        add_row(cols, [1] * len(cols), POS_MIN_XI[et], POS_MAX_XI[et])
    for i in range(n):
        add_row([i, n + i], [1, -1], 0, inf)
        add_row([n + i, 2 * n + i], [1, -1], 0, inf)
    add_row(idx_c, [1] * n, 1, 1)
    # honest keep-count constraint (council fix — replaces greedy lock)
    if keep_ids and min_keep > 0:
        cols = [int(i) for i in df.index[df["id"].isin(keep_ids)]]
        if cols:
            add_row(cols, [1] * len(cols), min(min_keep, len(cols)), inf)
    for pid in (locked or []):
        i = df.index[df["id"] == pid]
        if len(i):
            add_row([int(i[0])], [1], 1, 1)
    for pid in (banned or []):
        i = df.index[df["id"] == pid]
        if len(i):
            add_row([int(i[0])], [1], 0, 0)

    h.run()
    sol = np.array(h.getSolution().col_value)
    s = sol[0:n] > 0.5; l = sol[n:2 * n] > 0.5; c = sol[2 * n:3 * n] > 0.5

    squad = df[s].copy()
    squad["in_xi"] = l[s]
    squad["captain"] = c[s]
    bench = squad[~squad["in_xi"]].sort_values("xp_next", ascending=False)
    xi = squad[squad["in_xi"]].sort_values(["element_type", "xp_next"], ascending=[True, False])
    cap = squad[squad["captain"]].iloc[0]
    vice = xi[~xi["captain"]].nlargest(1, "xp_next").iloc[0]
    return dict(squad=squad, xi=xi, bench=bench, captain=cap, vice=vice,
                cost=squad["price"].sum(),
                xp_next=float(xi["xp_next"].sum() + cap["xp_next"]))


def transfer_plan(df: pd.DataFrame, current_ids: list[int], bank: float,
                  free_transfers: int, max_transfers: int = 3,
                  locked: list[int] | None = None,
                  banned: list[int] | None = None,
                  sells: dict | None = None) -> list[dict]:
    """Rank 0..max_transfers plans. Budget uses true sell prices (`sells`,
    {element id: tenths} from squad_state); without them, current prices."""
    now_tenths = {int(r.id): int(round(r.price * 10)) for r in df.itertuples()
                  if r.id in set(current_ids)}
    sells = {pid: int((sells or {}).get(pid, now_tenths.get(pid, 0))) for pid in current_ids}
    budget = int(sum(sells.values()) + round(bank * 10))
    results = []
    base = solve_squad(df, budget=budget, keep_ids=current_ids, min_keep=15,
                       price_override=sells, banned=banned)
    results.append(dict(n_transfers=0, hits=0, squad=base, net_gain=0.0, out=[], in_=[]))
    for k in range(1, max_transfers + 1):
        sol = solve_squad(df, budget=budget, keep_ids=current_ids, min_keep=15 - k,
                          price_override=sells, locked=locked, banned=banned)
        if sol is None:
            continue
        out = [int(p) for p in current_ids if p not in set(sol["squad"]["id"])]
        in_ = [int(p) for p in sol["squad"]["id"] if p not in set(current_ids)]
        if not out:
            continue
        hits = hits_for(len(out), free_transfers)
        gain = sol["xp_next"] - base["xp_next"] - hits
        results.append(dict(n_transfers=len(out), hits=hits, squad=sol,
                            net_gain=round(gain, 2), out=out, in_=in_))
    return sorted(results, key=lambda r: -r["net_gain"])


if __name__ == "__main__":
    df = pd.read_csv(DATA / "projections.csv")
    res = solve_squad(df)
    print(f"cost £{res['cost']:.1f}m  |  GW-next xP (XI + C): {res['xp_next']:.1f}")
    print(res["xi"][["name", "team", "pos", "price", "xp_next", "captain"]].to_string(index=False))
    print(res["bench"][["name", "team", "pos", "price", "xp_next"]].to_string(index=False))
