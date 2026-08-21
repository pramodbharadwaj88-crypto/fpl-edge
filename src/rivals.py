"""FPL Edge — mini-league rival engine.

The cash-league edge: everyone else optimizes expected points; we optimize
P(finish 1st). Needs config.json with team_id + league_ids.

Pipeline per GW (once picks are public after the deadline):
  1. Pull league standings -> rival entry ids
  2. Pull each rival's picks -> league-local effective ownership (EO)
  3. Compute differential value: xP * (1 - league_EO) for coverage decisions
  4. Monte Carlo the remaining season: simulate player scores ~ Normal(xP, sigma),
     propagate to each manager, estimate P(win league)
  5. Strategy mode:  trail -> differential/variance;  lead -> shadow/block
"""
from __future__ import annotations
import json, pathlib
import numpy as np
import pandas as pd
import fpl_api

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"


def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {"team_id": None, "league_ids": [], "risk_mode": "auto"}


def league_entries(league_id: int, max_pages: int = 3) -> pd.DataFrame:
    rows = []
    for page in range(1, max_pages + 1):
        data = fpl_api.league_standings(league_id, page)
        res = data["standings"]["results"]
        rows.extend(res)
        if not data["standings"]["has_next"]:
            break
    if not rows:  # pre-GW1: members live in new_entries until first deadline
        ne = data.get("new_entries", {}).get("results", [])
        rows = [dict(entry=r["entry"], entry_name=r.get("entry_name"),
                     player_name=f"{r.get('player_first_name','')} {r.get('player_last_name','')}".strip(),
                     total=0, rank=None) for r in ne]
    return pd.DataFrame(rows)  # entry, player_name, entry_name, total, rank


def rival_picks(entry_ids: list[int], gw: int) -> dict[int, list[dict]]:
    out = {}
    for eid in entry_ids:
        try:
            out[eid] = fpl_api.entry_picks(eid, gw)["picks"]
        except Exception:
            out[eid] = []
    return out


def league_eo(picks: dict[int, list[dict]]) -> pd.Series:
    """League-local effective ownership per player id (captain counts double)."""
    n = max(len(picks), 1)
    counts: dict[int, float] = {}
    for plist in picks.values():
        for p in plist:
            w = p["multiplier"] if p["multiplier"] > 0 else 0.5  # bench ~half weight
            counts[p["element"]] = counts.get(p["element"], 0) + w
    return pd.Series(counts) / n


def simulate_league(proj: pd.DataFrame, my_ids: list[int], my_cap: int,
                    rivals: dict[int, list[dict]], my_total: float,
                    rival_totals: dict[int, float], n_sims: int = 20000,
                    gws_left: int = 1) -> dict:
    """Monte Carlo P(top of league) over remaining GWs.

    Council fix: iid Normal is wrong for FPL scores — hauls make the right tail
    heavy, and same-club players are correlated (one team performance drives
    several assets). Draws use Student-t(4) idiosyncratic shocks mixed with a
    per-club factor (rho ~ 0.4), truncated below at the appearance floor.
    """
    import model_config as mc
    rng = np.random.default_rng(7)
    xp = proj.set_index("id")["xp_next"]
    club = proj.set_index("id")["team_id"]
    sd = np.sqrt(np.maximum(xp, 0.5)) * mc.model("sim_sd_scale")
    players = list(set(my_ids) | {p["element"] for pl in rivals.values() for p in pl})
    players = [p for p in players if p in xp.index]
    idx = {p: i for i, p in enumerate(players)}
    clubs = club.loc[players].to_numpy()
    uniq_clubs = {c: j for j, c in enumerate(np.unique(clubs))}
    club_col = np.array([uniq_clubs[c] for c in clubs])
    RHO = 0.4  # share of variance from the club factor
    mu = xp.loc[players].to_numpy()[None, :]
    sig = sd.loc[players].to_numpy()[None, :]
    tot = np.zeros((n_sims, len(players)))
    for _ in range(max(gws_left, 1)):
        z_club = rng.standard_normal((n_sims, len(uniq_clubs)))[:, club_col]
        z_idio = rng.standard_t(df=4, size=(n_sims, len(players))) / np.sqrt(2.0)
        draws = mu + sig * (np.sqrt(RHO) * z_club + np.sqrt(1 - RHO) * z_idio)
        tot += np.maximum(draws, -1.0)  # floor: worst realistic single-GW score
    draws = tot

    def team_score(ids, cap):
        cols = [idx[i] for i in ids if i in idx]
        s = draws[:, cols].sum(axis=1)
        if cap in idx:
            s += draws[:, idx[cap]]
        return s

    mine = my_total + team_score(my_ids, my_cap)
    best_rival = np.full(n_sims, -1e9)
    for eid, plist in rivals.items():
        ids = [p["element"] for p in plist if p["multiplier"] > 0]
        cap = next((p["element"] for p in plist if p["is_captain"]), None)
        s = rival_totals.get(eid, 0) + team_score(ids, cap)
        best_rival = np.maximum(best_rival, s)
    p_win = float((mine > best_rival).mean())
    return dict(p_win=p_win, exp_margin=float((mine - best_rival).mean()))


def strategy_mode(my_rank: int, n_managers: int, gws_left: int, gw: int = 38) -> str:
    """Council gating: near-max-EV early season regardless of rank — variance
    is the trailer's tool for LATER. Differentials unlock after the config GW
    (default 8); shadow only with a lead late."""
    import model_config as mc
    if gw <= int(mc.model("differential_on_after_gw")):
        return "balanced"
    if my_rank == 1:
        return "shadow" if gws_left <= 8 else "balanced"
    if my_rank <= max(2, n_managers // 5) or gws_left > 15:
        return "balanced"
    return "differential"


def differential_scores(proj: pd.DataFrame, eo: pd.Series, mode: str) -> pd.DataFrame:
    """Write the strategy-adjusted objective into `score` — NEVER into xp_next.
    (Council: true xP and strategy score are separate named quantities.)
    balanced: score = xP. differential: reward low league-EO. shadow: cover."""
    p = proj.copy()
    p["league_eo"] = p["id"].map(eo).fillna(0.0)
    if mode == "differential":
        p["score"] = p["xp_next"] * (1.0 + 0.6 * (1.0 - p["league_eo"].clip(0, 2) / 2))
    elif mode == "shadow":
        p["score"] = p["xp_next"] * (0.7 + 0.6 * p["league_eo"].clip(0, 2) / 2)
    else:
        p["score"] = p["xp_next"]
    return p


if __name__ == "__main__":
    cfg = load_config()
    if not cfg.get("league_ids"):
        print("No league_ids in config.json yet — rival engine idle.")
    else:
        for lid in cfg["league_ids"]:
            df = league_entries(lid)
            print(df[["rank", "entry", "entry_name", "player_name", "total"]].head(20))
