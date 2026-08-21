"""FPL Edge — expected points (xP) model.

Decomposition per player per GW:
    xP = P(play)·appearance + attack + clean sheet + saves + DEFCON + bonus
scaled by fixture goal-expectancies (betting-market implied where available,
Elo baseline otherwise).

Council-mandated properties (2026-08-20 deliberation):
  * NO hard-coded season denominators — stats blend last-season baseline with
    current season via minutes-based shrinkage (baseline.py), denominators use
    games elapsed.
  * Appearance points are conditioned on P(playing at all) — no flat floor.
  * `xp_*` columns are TRUE expected points; strategy tilts live in a separate
    `score` column added downstream (rivals.differential_scores). Never mix.
  * ep_next blend is a small prior (weights in config.json "model").
  * Scorer-prop E[goals] are normalized per match against market total goals
    (proper de-vig), not a flat haircut.
All tuning constants: config.json "model" via model_config.
"""
from __future__ import annotations
import json, math, pathlib
import pandas as pd
import numpy as np
import fpl_api, baseline, model_config as mc

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"

LEAGUE_AVG_GOALS_HOME = 1.60
LEAGUE_AVG_GOALS_AWAY = 1.30
PROMOTED_DEFAULTS = dict(elo=1650, att_h=960, att_a=1040, def_h=1040, def_a=1060)

GOAL_PTS = {1: 6, 2: 6, 3: 5, 4: 4}
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}
DEFCON_THRESHOLD = {1: None, 2: 10, 3: 12, 4: 12}
DEFCON_PTS = 2


def _num(s):
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


# ---- team strength ---------------------------------------------------------
def team_strength() -> pd.DataFrame:
    bs = fpl_api.bootstrap()
    teams = pd.DataFrame(bs["teams"])[["id", "name", "short_name"]]
    prev = pd.read_csv(DATA / "ci/data/2025-2026/teams.csv")
    prev = prev[["name", "elo", "strength_attack_home", "strength_attack_away",
                 "strength_defence_home", "strength_defence_away"]]
    m = teams.merge(prev, on="name", how="left")
    for col, default in [("elo", PROMOTED_DEFAULTS["elo"]),
                         ("strength_attack_home", PROMOTED_DEFAULTS["att_h"]),
                         ("strength_attack_away", PROMOTED_DEFAULTS["att_a"]),
                         ("strength_defence_home", PROMOTED_DEFAULTS["def_h"]),
                         ("strength_defence_away", PROMOTED_DEFAULTS["def_a"])]:
        m[col] = m[col].fillna(default)
    for c in ["strength_attack_home", "strength_attack_away",
              "strength_defence_home", "strength_defence_away"]:
        m[c + "_mult"] = m[c] / m[c].mean()
    return m.set_index("id")


def _odds_lookup(ts: pd.DataFrame) -> dict:
    """{(home_short, away_short): (lam_h, lam_a)} from cached market odds."""
    import odds as odds_mod
    f = DATA / "odds_lambdas.json"
    if not f.exists():
        return {}
    try:
        raw = json.loads(f.read_text())
    except Exception:
        return {}
    out = {}
    for v in raw.values():
        hs = odds_mod.TEAM_MAP.get(v["home"])
        as_ = odds_mod.TEAM_MAP.get(v["away"])
        if hs and as_:
            out[(hs, as_)] = (v["lam_home"], v["lam_away"])
    return out


def fixture_lambdas(horizon: int | None = None) -> dict:
    horizon = horizon or mc.model("horizon")
    odds_blend = mc.model("odds_blend")
    ts = team_strength()
    short = ts["short_name"].to_dict()
    market = _odds_lookup(ts)
    fx = pd.DataFrame(fpl_api.fixtures())
    nxt = fpl_api.next_event()["id"]
    gws = list(range(nxt, nxt + horizon))
    out = {tid: {gw: [] for gw in gws} for tid in ts.index}
    for _, f in fx[fx.event.isin(gws)].iterrows():
        h, a, gw = int(f.team_h), int(f.team_a), int(f.event)
        lam_h = (LEAGUE_AVG_GOALS_HOME
                 * ts.loc[h, "strength_attack_home_mult"]
                 / ts.loc[a, "strength_defence_away_mult"])
        lam_a = (LEAGUE_AVG_GOALS_AWAY
                 * ts.loc[a, "strength_attack_away_mult"]
                 / ts.loc[h, "strength_defence_home_mult"])
        mk = market.get((short[h], short[a]))
        if mk:
            lam_h = odds_blend * mk[0] + (1 - odds_blend) * lam_h
            lam_a = odds_blend * mk[1] + (1 - odds_blend) * lam_a
        out[h][gw].append((a, True, lam_h, lam_a))
        out[a][gw].append((h, False, lam_a, lam_h))
    return {"gws": gws, "lam": out, "teams": ts}


# ---- overrides & edge inputs ------------------------------------------------
def _load_json(name: str) -> dict:
    f = DATA / name
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _xmins_overrides() -> dict:
    """xmins_overrides.json: {"web_name or code": minutes 0-90}. Written by the
    LLM news layer (press conferences, injury aggregators, Reddit) pre-deadline."""
    return _load_json("xmins_overrides.json")


def _wc_fatigue(gw: int) -> dict:
    """wc_fatigue.json: {"web_name or code": depth} where depth in {1,2,3} ≈
    (group/R16, QF/SF, final). Discount decays linearly to zero by GW6."""
    raw = _load_json("wc_fatigue.json")
    if not raw or gw > 6:
        return {}
    fade = max(0.0, (7 - gw) / 6.0)
    # depth: 3 = played the final, 2 = semifinal, 1 = quarterfinal exit
    return {k: min(0.20, 0.05 * float(v)) * fade for k, v in raw.items()}


def _finishing() -> dict:
    """understat_finishing.json: {"code": multiplier} — shrunk goals-vs-xG
    finishing skill from shot-level data (understat_priors.py)."""
    return _load_json("understat_finishing.json")


def _fix_lineups() -> tuple[set, set]:
    """((web_name, team_short) starter pairs, team_shorts covered) from Fix's
    predicted lineups. TEAM-QUALIFIED (council: identity invariant) and
    STALENESS-GUARDED: entries older than 24h are ignored entirely."""
    d = _load_json("fix_lineups.json")
    if not d:
        return set(), set()
    try:
        import datetime as _dt
        upd = _dt.datetime.fromisoformat(d["updated"].replace("Z", "+00:00"))
        age_h = (_dt.datetime.now(_dt.timezone.utc) - upd).total_seconds() / 3600
        if age_h > 24:
            return set(), set()
    except Exception:
        return set(), set()
    xi = d.get("xi")
    if isinstance(xi, dict):  # legacy schema — cannot trust identities
        return set(), set()
    pairs = {(e["name"], e["team"]) for e in xi or []}
    return pairs, {t for _, t in pairs}


# ---- expected minutes -------------------------------------------------------
def _xmins(el: pd.Series, bl: dict | None, overrides: dict, fatigue: dict,
           lineup_xi: set = frozenset(), lineup_teams: set = frozenset(),
           team_name: str = "") -> float:
    key_name, key_code = el["web_name"], str(el["code"])
    if key_name in overrides or key_code in overrides:
        base_val = float(overrides.get(key_name, overrides.get(key_code)))
        return float(np.clip(base_val, 0, 90))
    status = el["status"]
    chance = el["chance_of_playing_next_round"]
    if status in ("i", "s", "u", "n"):
        avail = 0.0 if chance in (None, 0) or pd.isna(chance) else float(chance) / 100.0
    elif status == "d":
        avail = 0.75 if chance is None or pd.isna(chance) else float(chance) / 100.0
    else:
        avail = 1.0
    if bl is None:
        epn = float(el["ep_next"] or 0)
        base_val = 68.0 if epn >= 1.5 else (30.0 if epn > 0 else 5.0)
        return base_val * avail
    exp = (bl["start_share"] * bl["mins_per_start"]
           + (1 - bl["start_share"]) * 12.0)  # sub cameo expectation
    # fit premium regulars shouldn't be dragged by last season's injury gaps
    if status == "a" and float(el["now_cost"]) >= 75 and float(el["ep_next"] or 0) >= 2.5:
        exp = max(exp, 72.0)
    # Fix predicted-lineup evidence (only when this player's team is covered).
    # TEAM-QUALIFIED membership; single-source evidence moves HALFWAY toward
    # the lineup implication (council: no single feed dominates). Weights and
    # anchors from config: lineup_blend / lineup_start_mins / lineup_bench_mins.
    w_lu = mc.model("lineup_blend")
    if (el["web_name"], team_name) in lineup_xi:
        exp = max(exp, (1 - w_lu) * exp + w_lu * mc.model("lineup_start_mins"))
    elif team_name and team_name in lineup_teams and status == "a":
        exp = min(exp, (1 - w_lu) * exp + w_lu * mc.model("lineup_bench_mins"))
    disc = fatigue.get(key_name, fatigue.get(str(el["code"]), 0.0))
    return float(np.clip(exp * (1 - disc), 0, 90)) * avail


# ---- DEFCON (overdispersed counts: negative binomial, not Poisson) ---------
def _defcon_ev(rate90: float, thr: int, xm: float) -> float:
    lam = rate90 * xm / 90.0
    if lam <= 0 or not thr:
        return 0.0
    k = mc.model("defcon_dispersion_k")
    try:
        from scipy.stats import nbinom
        p = k / (k + lam)
        prob = 1.0 - nbinom.cdf(thr - 1, k, p)
    except ImportError:
        prob = 1.0 - math.exp(-lam) * sum(lam ** i / math.factorial(i) for i in range(thr))
    return DEFCON_PTS * float(prob)


# ---- scorer props with proper per-match de-vig ------------------------------
def _scorer_prop_ev(els: pd.DataFrame, xm_map: dict | None = None) -> dict:
    """{element_id: market-implied E[goals]}, normalized so each match's summed
    implied goals equals the market total-goals expectation (lam_h + lam_a).

    COUNCIL FIX: the normalization SUM includes only players expected to play
    (xm >= 30) — bookmakers price whole squads, and normalizing over 40+ names
    (many of whom won't feature) forces a huge k that crushes favourites."""
    raw = _load_json("odds_scorers.json")
    if not raw:
        return {}
    lambdas = _load_json("odds_lambdas.json")
    match_total = {k: v["lam_home"] + v["lam_away"] for k, v in lambdas.items()}
    import odds as odds_mod
    import fpl_api as _api
    recs = els.to_dict("records")
    short_to_id = {t["short_name"]: t["id"] for t in _api.bootstrap()["teams"]}

    def _match_teams(match_key: str) -> set:
        ids = set()
        for full_name in match_key.split("|"):
            sh = odds_mod.TEAM_MAP.get(full_name)
            if sh and sh in short_to_id:
                ids.add(short_to_id[sh])
        return ids

    by_match: dict[str, list] = {}
    for name, v in raw.items():
        if 0 < v["p_goal"] < 0.95:
            pid = odds_mod.match_player(name, recs, _match_teams(v["match"]) or None)
            if pid:
                by_match.setdefault(v["match"], []).append((pid, -math.log(1 - v["p_goal"])))
    out = {}
    for match, pairs in by_match.items():
        # pairs carry raw -ln(1-p); recover p, then POWER de-vig: p' = p^k with k
        # solved so the match's summed implied goals equals market total goals.
        # Power (not linear) because scorer-market overround concentrates in
        # longshots — favourites keep most of their probability.
        ps = [(pid, 1 - math.exp(-g)) for pid, g in pairs]
        target = match_total.get(match)
        # only likely players count toward the match's implied-goal budget
        likely = [(pid, p) for pid, p in ps
                  if xm_map is None or xm_map.get(pid, 0) >= 30]
        norm_set = likely if likely else ps

        def implied_total(k):
            return sum(-math.log(1 - p ** k) for _, p in norm_set)

        if target and implied_total(1.0) > target:
            lo, hi = 1.0, 3.5
            for _ in range(40):
                mid = (lo + hi) / 2
                if implied_total(mid) > target:
                    lo = mid
                else:
                    hi = mid
            k = (lo + hi) / 2
        else:
            k = 1.15  # no market total cached: mild default haircut
        for pid, p in ps:
            out[pid] = -math.log(1 - p ** k)
    return out


# ---- main -------------------------------------------------------------------
def build_projections(horizon: int | None = None) -> pd.DataFrame:
    horizon = horizon or mc.model("horizon")
    bs = fpl_api.bootstrap()
    fl = fixture_lambdas(horizon)
    gws, lam, ts = fl["gws"], fl["lam"], fl["teams"]
    els = pd.DataFrame(bs["elements"])
    els = els[els["removed"] == False] if "removed" in els else els

    base = baseline.load()
    n_games = baseline.games_elapsed(bs)
    shrink = mc.model("shrinkage_mins")
    overrides = _xmins_overrides()
    fatigue = _wc_fatigue(gws[0])
    finishing = _finishing()
    fin_cap = mc.model("finishing_cap")
    # precompute xmins so scorer-prop de-vig normalizes over likely players only
    lineup_xi, lineup_teams = _fix_lineups()
    fpl_short = {t["id"]: t["short_name"] for t in bs["teams"]}
    xm_map = {}
    for _, el in els.iterrows():
        bl0 = baseline.blended_stats(el, base, n_games, shrink)
        xm_map[int(el["id"])] = _xmins(el, bl0, overrides, fatigue,
                                       lineup_xi, lineup_teams,
                                       fpl_short.get(int(el["team"]), ""))
    scorer_ev = _scorer_prop_ev(els, xm_map)
    sp_blend = mc.model("scorer_prop_blend")
    w_ep_rel = mc.model("ep_next_weight_reliable")
    w_ep_thin = mc.model("ep_next_weight_thin")
    decay = mc.model("decay")
    avg_lam_for = np.mean([LEAGUE_AVG_GOALS_HOME, LEAGUE_AVG_GOALS_AWAY])

    rows = []
    for _, el in els.iterrows():
        et = int(el["element_type"])
        team = int(el["team"])
        price = el["now_cost"] / 10.0
        epn = float(el["ep_next"] or 0)
        bl = baseline.blended_stats(el, base, n_games, shrink)
        xm = xm_map[int(el["id"])]

        if bl is not None and bl["eff_mins"] >= 400:
            xg90 = bl["expected_goals_p90"]
            xa90 = bl["expected_assists_p90"]
            saves90 = bl["saves_p90"]
            bonus90 = bl["bonus_p90"]
            cbit90 = bl["clearances_blocks_interceptions_p90"] + bl["tackles_p90"]
            cbirt90 = cbit90 + bl["recoveries_p90"]
            reliable = True
        else:
            prior = {1: (0.0, 0.0), 2: (0.05, 0.08), 3: (0.18, 0.15), 4: (0.30, 0.12)}[et]
            scale = max(0.6, min(price / {1: 5.0, 2: 5.0, 3: 7.0, 4: 7.5}[et], 1.8))
            xg90, xa90 = prior[0] * scale, prior[1] * scale
            saves90 = 3.0 if et == 1 else 0.0
            bonus90 = 0.15 * scale
            cbit90 = 7.0 if et == 2 else 0.0
            cbirt90 = 8.0 if et in (3, 4) else 0.0
            reliable = False

        # finishing skill multiplier (shot-level prior, clipped)
        fin = finishing.get(str(el["code"]))
        if fin:
            xg90 *= float(np.clip(fin, 1 - fin_cap, 1 + fin_cap))

        # appearance probabilities — conditioned on actually playing
        p_any = 1.0 - math.exp(-xm / 30.0) if xm > 0 else 0.0
        p60 = min(xm / 60.0, 1.0) * (0.9 if xm >= 60 else 0.5)

        gw_xp = {}
        for gw in gws:
            fixtures_gw = lam[team][gw]
            xp = 0.0
            for (opp, home, lam_for, lam_against) in fixtures_gw:
                app = p_any * 1.0 + p60 * 1.0
                att_scale = lam_for / avg_lam_for
                goals = xg90 * xm / 90.0 * att_scale
                if gw == gws[0] and int(el["id"]) in scorer_ev:
                    goals = (1 - sp_blend) * goals + sp_blend * scorer_ev[int(el["id"])]
                assists = xa90 * xm / 90.0 * att_scale
                att = goals * GOAL_PTS[et] + assists * 3.0
                p_cs = math.exp(-lam_against)
                p_60 = min(xm / 90.0, 1.0) if xm >= 60 else 0.0
                cs = CS_PTS[et] * p_cs * p_60
                gc = -0.5 * lam_against * (xm / 90.0) if et in (1, 2) else 0.0
                sv = (saves90 * (lam_against / LEAGUE_AVG_GOALS_AWAY) / 3.0
                      * xm / 90.0) if et == 1 else 0.0
                thr = DEFCON_THRESHOLD.get(et)
                dc = _defcon_ev(cbit90 if et == 2 else cbirt90, thr, xm) if thr else 0.0
                xp += app + att + cs + gc + sv + bonus90 * xm / 90.0 * p_any + dc
            if not fixtures_gw:
                xp = 0.0
            if gw == gws[0] and epn > 0:
                w_ep = w_ep_rel if reliable else w_ep_thin
                xp = (1 - w_ep) * xp + w_ep * epn
            gw_xp[gw] = round(xp, 3)

        horizon_score = sum(decay ** k * gw_xp[gw] for k, gw in enumerate(gws))
        rows.append(dict(
            id=int(el["id"]), code=int(el["code"]), name=el["web_name"], team_id=team,
            team=ts.loc[team, "short_name"], pos={1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}[et],
            element_type=et, price=price, xmins=round(xm, 1),
            status=el["status"], news=el["news"] or "",
            sel_pct=float(el["selected_by_percent"]),
            transfers_in_event=int(el.get("transfers_in_event") or 0),
            transfers_out_event=int(el.get("transfers_out_event") or 0),
            ep_next=epn, reliable=reliable,
            **{f"xp_gw{gw}": v for gw, v in gw_xp.items()},
            xp_next=gw_xp[gws[0]], horizon=round(horizon_score, 2),
        ))
    df = pd.DataFrame(rows)
    df.to_csv(DATA / "projections.csv", index=False)
    return df


if __name__ == "__main__":
    df = build_projections()
    print(df.sort_values("xp_next", ascending=False)
          .head(20)[["name", "team", "pos", "price", "xmins", "xp_next", "horizon"]].to_string())
