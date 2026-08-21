"""FPL Edge — briefing generator: runs the whole pipeline and emits
out/briefing.md + out/state.json (consumed by the dashboard builder).

Invariants (council):
  * proj["xp_next"] is TRUE expected points end-to-end; the optimizer runs on
    proj["score"] (strategy-adjusted). They are never mixed.
  * Every external data source reports freshness in state["sources"].
  * Every run appends to the prediction ledger; finished GWs are settled
    against actuals automatically.
"""
from __future__ import annotations
import json, pathlib, datetime, time
import pandas as pd
import fpl_api, projections, optimizer, rivals, ledger, baseline
import model_config as mc

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)


def _age_min(path: pathlib.Path) -> int | None:
    return int((time.time() - path.stat().st_mtime) / 60) if path.exists() else None


def _sources() -> dict:
    return {name: _age_min(DATA / f) for name, f in [
        ("bootstrap", "bootstrap.json"), ("match_odds", "odds_lambdas.json"),
        ("prediction_markets", "pm_probs.json"), ("scorer_props", "odds_scorers.json"),
        ("baseline_prev_season", "baseline_prev.json"),
        ("understat_finishing", "understat_finishing.json"),
        ("xmins_overrides", "xmins_overrides.json"), ("wc_fatigue", "wc_fatigue.json"),
        ("fix_elite_reveal", "elite_teams.json"), ("fix_projections", "fix_proj5.json"),
    ]}


def run() -> dict:
    cfg = mc.full()
    bs = fpl_api.bootstrap()
    ev = fpl_api.next_event(bs)
    gw = ev["id"]
    deadline = ev["deadline_time"]

    # freeze last-season baseline if pre-season (no-op afterwards)
    try:
        baseline.snapshot()
    except Exception:
        pass

    # betting-market layer (best-effort)
    odds_status = "off (no api key)"
    try:
        import odds, datetime as _dt
        ml = odds.refresh_match_odds()
        if ml is not None:
            odds_status = f"match odds: {len(ml)} fixtures"
            dl = _dt.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            hrs = (dl - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 3600
            if 0 < hrs <= 36:
                sp = odds.refresh_scorer_props()
                odds_status += f" + scorer props: {len(sp or {})} players"
    except Exception as e:
        odds_status = f"error: {e}"

    # extended Fix feeds BEFORE projections: predicted lineups feed xMins;
    # algo projections / prices / injuries feed reporting
    fix_feeds_status = {}
    try:
        import fix_feeds
        fix_feeds_status = fix_feeds.refresh_all()
    except Exception as e:
        fix_feeds_status = {"error": str(e)}

    proj = projections.build_projections()

    state: dict = dict(generated=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       gw=gw, deadline=deadline, mode="balanced", odds=odds_status,
                       team_id=cfg.get("team_id"), sources=_sources(),
                       fix_feeds=fix_feeds_status)

    # automated calibration diff vs Fix's algorithm (the council's check):
    # Spearman rank-corr of our xp_next vs Fix pts_1 over likely starters
    try:
        fpf = json.loads((DATA / "fix_proj_full.json").read_text())
        fixp = {k: v["pts"][0] for k, v in fpf.get("players", {}).items() if v.get("pts")}
        sub = proj[(proj.xmins >= 60) & proj.name.isin(fixp)]
        if len(sub) >= 30:
            ours = sub["xp_next"].rank()
            theirs = sub["name"].map(fixp).rank()
            rho = float(ours.corr(theirs, method="spearman")
                        if hasattr(ours, "corr") else 0)
            thr = float(mc.model("fix_drift_threshold"))
            # council ruling: Fix feeds our inputs, so this is a DRIFT/BREAKAGE
            # detector, NOT an accuracy metric (ledger MAE vs actuals is that).
            state["calibration_vs_fix"] = dict(
                n=len(sub), spearman=round(rho, 3),
                flag="steady" if rho >= thr else "drift — check joins/feeds")
    except Exception:
        pass

    # surface lineup-join warnings loudly (council: no silent degradation)
    try:
        lw = json.loads((DATA / "fix_lineups.json").read_text()).get("warnings", [])
        if lw:
            state.setdefault("fix_feeds", {})["lineup_warnings"] = lw[:10]
    except Exception:
        pass

    # settle finished GWs against actuals (prediction ledger)
    try:
        settled = ledger.settle()
        if settled:
            state["last_settle"] = settled
        cal = ledger.calibration()
        if cal:
            state["calibration"] = cal
    except Exception as e:
        state["ledger_error"] = str(e)

    # --- rival layer → strategy `score` column (xp_next stays pure) ---------
    eo = pd.Series(dtype=float)
    mode = "balanced"
    if cfg.get("league_ids") and cfg.get("team_id"):
        try:
            lid = cfg["league_ids"][0]
            entries = rivals.league_entries(lid)
            my = entries[entries["entry"] == cfg["team_id"]]
            raw_rank = my["rank"].iloc[0] if len(my) else None
            my_rank = int(raw_rank) if raw_rank is not None and not pd.isna(raw_rank) \
                else max(len(entries) // 2, 1)
            picks = rivals.rival_picks(
                [e for e in entries["entry"] if e != cfg["team_id"]], max(gw - 1, 1))
            eo = rivals.league_eo(picks)
            mode = cfg.get("risk_mode", "auto")
            if mode == "auto":
                mode = rivals.strategy_mode(my_rank, len(entries), 39 - gw, gw)
            state["league"] = dict(
                id=lid, n=len(entries),
                my_rank=my_rank if raw_rank is not None else None,
                picks_available=any(len(v) for v in picks.values()),
                roster=[dict(entry=int(r.entry), team=r.entry_name, manager=r.player_name,
                             total=int(r.total or 0))
                        for r in entries.itertuples()])
        except Exception as e:
            state["league_error"] = str(e)
    proj = rivals.differential_scores(proj, eo, mode)

    # auto-refresh elite reveal from Fantasy Football Fix (session cookie)
    try:
        import fix_data
        fx = fix_data.refresh()
        state["fix"] = (f"live: {fx['n_teams']} elite teams, GW{fx['gw']}"
                        if fx else "unavailable (cookie expired? re-paste sessionid)")
    except Exception as e:
        state["fix"] = f"error: {e}"


    # elite-manager consensus (FF Fix Reveal, manually ingested per GW):
    # small tilt on `score` only + divergence report. Doubles as the
    # rival-behavior prior before league picks go public.
    try:
        import elite_signal
        e_eo = elite_signal.elite_eo(proj, gw)
        if not e_eo.empty:
            tilt = float(cfg.get("model", {}).get("elite_tilt", 0.05))
            proj["elite_eo"] = proj["id"].map(e_eo).fillna(0.0)
            proj["score"] = proj["score"] * (1.0 + tilt * proj["elite_eo"].clip(0, 2) / 2)
            state["elite_loaded"] = True
    except Exception as ex:
        state["elite_error"] = str(ex)

    if mode == "balanced" and eo.empty:
        # template-shield: when rival picks are unknown, tilt the OBJECTIVE
        # slightly toward high-global-ownership players (rank protection).
        # Applies to score only — xp_next untouched.
        proj["score"] = proj["score"] * (1.0 + 0.10 * proj["sel_pct"] / 100.0)
        mode = "balanced+shield"
    state["mode"] = mode

    # --- current squad (if team exists) vs fresh pick ------------------------
    have_team = False
    picks = None
    if cfg.get("team_id"):
        try:
            picks = fpl_api.entry_picks(cfg["team_id"], gw - 1) if gw > 1 else None
            if picks:
                have_team = True
        except Exception:
            pass

    locked = cfg.get("locked") or []
    banned = cfg.get("banned") or []

    if have_team:
        ids = [p["element"] for p in picks["picks"]]
        # maintain purchase-price memory for true sell values
        pf = DATA / "purchases.json"
        try:
            purch = {int(k): int(v) for k, v in json.loads(pf.read_text()).items()} \
                if pf.exists() else {}
        except Exception:
            purch = {}
        now_t = {int(r.id): int(round(r.price * 10)) for r in proj.itertuples()}
        for pid in ids:
            purch.setdefault(pid, now_t.get(pid, 0))
        purch = {k: v for k, v in purch.items() if k in set(ids)}
        pf.write_text(json.dumps(purch))

        ent = fpl_api.entry(cfg["team_id"])
        bank = ent.get("last_deadline_bank", 0) / 10.0
        plans = optimizer.transfer_plan(proj, ids, bank, free_transfers=1,
                                        locked=locked, banned=banned)
        state["plans"] = [
            dict(n=p["n_transfers"], hits=p["hits"], gain=p["net_gain"],
                 out=proj[proj.id.isin(p["out"])]["name"].tolist(),
                 in_=proj[proj.id.isin(p["in_"])]["name"].tolist()) for p in plans[:4]]
        best = plans[0]["squad"]
    else:
        best = optimizer.solve_squad(proj, locked=locked, banned=banned)

    def prow(r):
        return dict(id=int(r["id"]), name=r["name"], team=r["team"], pos=r["pos"],
                    price=float(r["price"]), xp=round(float(r["xp_next"]), 2),
                    horizon=float(r["horizon"]), xmins=float(r["xmins"]),
                    sel=float(r["sel_pct"]), cap=bool(r.get("captain", False)))

    state["squad"] = dict(
        xi=[prow(r) for _, r in best["xi"].iterrows()],
        bench=[prow(r) for _, r in best["bench"].iterrows()],
        captain=best["captain"]["name"], vice=best["vice"]["name"],
        cost=float(best["cost"]), xp=round(best["xp_next"], 1))

    # --- CHIP-PLAN AUTHORITY (council: single consumed source of truth) -----
    # If data/chip_plan.json targets this GW, it OVERRIDES the solver output:
    # state.squad reflects the locked plan; the solver's pick is kept only as
    # model_alt for reference. Downstream (briefing, dashboard, triggers) all
    # read state.squad, so the plan is what every surface shows.
    try:
        plan_f = DATA / "chip_plan.json"
        if plan_f.exists():
            plan = json.loads(plan_f.read_text())
            if int(plan.get("gw", -1)) == gw:
                rows = []
                for p in plan["squad15"]:
                    m = proj[(proj.name == p["name"]) & (proj.team == p["team"])]
                    if len(m):
                        rows.append(m.iloc[0])
                if len(rows) == 15:
                    pdf = pd.DataFrame(rows)
                    xi_names = set(plan["xi"])
                    pdf["in_xi"] = pdf["name"].isin(xi_names)
                    xi_df = pdf[pdf.in_xi]
                    bench_df = pdf[~pdf.in_xi]
                    cap = pdf[pdf.name == plan["captain"]].iloc[0]
                    state["model_alt"] = dict(captain=best["captain"]["name"],
                                              xi=[r["name"] for _, r in best["xi"].iterrows()],
                                              xp=round(best["xp_next"], 1))
                    state["squad"] = dict(
                        xi=[prow(r) for _, r in xi_df.iterrows()],
                        bench=[prow(r) for _, r in bench_df.iterrows()],
                        captain=plan["captain"], vice=plan["vice"],
                        cost=float(pdf["price"].sum()),
                        xp=round(float(pdf["xp_next"].sum() + cap["xp_next"])
                                 if plan.get("chip") == "bench_boost"
                                 else float(xi_df["xp_next"].sum() + cap["xp_next"]), 1))
                    state["locked_plan"] = dict(gw=gw, chip=plan.get("chip"),
                                                rationale=plan.get("rationale", "")[:200])
                else:
                    state["locked_plan_error"] = f"resolved {len(rows)}/15 plan players"
    except Exception as e:
        state["locked_plan_error"] = str(e)

    # elite divergence report (needs the chosen squad)
    try:
        import elite_signal
        div = elite_signal.divergence(
            proj, [r["name"] for _, r in best["squad"].iterrows()],
            best["captain"]["name"], gw)
        if div:
            state["elite"] = div
    except Exception:
        pass

    # prediction ledger: record this run's projections + the decision context
    try:
        ledger.record(gw, proj, set(best["squad"]["id"]), int(best["captain"]["id"]))
        ledger.record_decision(state)
    except Exception as e:
        state["ledger_error"] = str(e)

    # watchlist, flags, transfer-market buzz
    top = proj.nlargest(40, "horizon")
    state["watchlist"] = [prow(r) for _, r in top.head(25).iterrows()]
    flags = proj[(proj.status != "a") & (proj.sel_pct > 3)]
    state["flags"] = [dict(name=r["name"], team=r["team"], status=r["status"],
                           news=str(r["news"])[:120]) for _, r in flags.iterrows()]
    buzz = proj[proj.xmins > 30].copy()
    buzz["net_transfers"] = buzz["transfers_in_event"] - buzz["transfers_out_event"]
    movers = buzz.nlargest(8, "net_transfers")
    state["buzz"] = [dict(name=r["name"], team=r["team"], price=float(r["price"]),
                          net=int(r["net_transfers"]), xp=round(float(r["xp_next"]), 2))
                     for _, r in movers.iterrows()]

    # fixture ticker
    fx = pd.DataFrame(fpl_api.fixtures())
    teams = {t["id"]: t["short_name"] for t in bs["teams"]}
    gws = list(range(gw, gw + 6))
    tick = {tid: [] for tid in teams}
    for _, f in fx[fx.event.isin(gws)].iterrows():
        tick[int(f.team_h)].append(dict(gw=int(f.event), opp=teams[int(f.team_a)],
                                        home=True, fdr=int(f.team_h_difficulty)))
        tick[int(f.team_a)].append(dict(gw=int(f.event), opp=teams[int(f.team_h)],
                                        home=False, fdr=int(f.team_a_difficulty)))
    state["fixtures"] = {teams[tid]: v for tid, v in tick.items()}
    state["gws"] = gws

    (OUT / "state.json").write_text(json.dumps(state, indent=1))

    # --- markdown briefing ---------------------------------------------------
    try:
        from zoneinfo import ZoneInfo
        _dl = datetime.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        deadline_et = _dl.astimezone(ZoneInfo("America/New_York")).strftime("%a %b %d, %-I:%M %p ET")
    except Exception:
        deadline_et = deadline
    state["deadline_et"] = deadline_et
    md = [f"# FPL Edge briefing — GW{gw}",
          f"Deadline: **{deadline_et}** ({deadline} UTC) • mode: **{state['mode']}** • {odds_status}", ""]
    src_line = ", ".join(f"{k}:{'—' if v is None else str(v)+'m'}"
                         for k, v in state["sources"].items())
    md += [f"_Data freshness (minutes since refresh): {src_line}_", ""]
    if state.get("calibration"):
        c = state["calibration"]
        md += [f"_Model calibration: {c['gws']} GWs settled, MAE {c['mae']}, bias {c['bias']:+}_", ""]
    sq = state["squad"]
    if state.get("locked_plan"):
        lp = state["locked_plan"]
        md += [f"## LOCKED PLAN — GW{lp['gw']}"
               + (f" · chip: {lp['chip'].upper()}" if lp.get("chip") else ""),
               f"_{lp['rationale']}_", ""]
    md += [f"## {'Locked squad' if state.get('locked_plan') else 'Recommended XI'} "
           f"(cost £{sq['cost']:.1f}m, xP {sq['xp']})",
           "", "| Player | Team | Pos | £ | xP |", "|---|---|---|---|---|"]
    for r in sq["xi"]:
        cap = " (C)" if r["name"] == sq["captain"] else (" (V)" if r["name"] == sq["vice"] else "")
        md.append(f"| {r['name']}{cap} | {r['team']} | {r['pos']} | {r['price']} | {r['xp']} |")
    md += ["", "**Bench:** " + ", ".join(f"{r['name']} ({r['pos']})" for r in sq["bench"])]
    if state.get("plans"):
        md += ["", "## Transfer options"]
        for p in state["plans"]:
            md.append(f"- {p['n']} transfer(s), hit {p['hits']}: OUT {p['out']} → IN {p['in_']} (net {p['gain']:+.1f} xP)")
    if state["flags"]:
        md += ["", "## Injury/availability flags"]
        for f in state["flags"][:12]:
            md.append(f"- **{f['name']}** ({f['team']}) [{f['status']}] {f['news']}")
    if state.get("buzz"):
        md += ["", "## Market buzz (net transfers in, this GW)"]
        for b in state["buzz"]:
            md.append(f"- {b['name']} ({b['team']}, £{b['price']}m) net +{b['net']:,} — xP {b['xp']}")
    # Fix price alerts (in-season) + calibration line
    try:
        fpx = json.loads((DATA / "fix_prices.json").read_text())
        if not fpx.get("locked") and (fpx.get("rising") or fpx.get("falling")):
            md += ["", "## Price alerts (Fix predictor)"]
            for r in fpx.get("rising", [])[:6]:
                md.append(f"- RISE soon: {r['name']} ({r['team']}, £{r['value']}m) — {r['change']}")
            for r in fpx.get("falling", [])[:6]:
                md.append(f"- FALL soon: {r['name']} ({r['team']}, £{r['value']}m) — {r['change']}")
    except Exception:
        pass
    if state.get("calibration_vs_fix"):
        c = state["calibration_vs_fix"]
        md += ["", f"_Model vs Fix algorithm: rank-corr {c['spearman']} over {c['n']} starters — {c['flag']}_"]
    if state.get("elite"):
        e = state["elite"]
        md += ["", f"## Elite consensus (FF Fix Reveal, {e['n_teams']} pro teams)",
               f"- Template core: {', '.join(e['top_template'])}",
               f"- We lack: {', '.join(e['we_lack']) if e['we_lack'] else 'nothing — fully covered'}",
               f"- Elite captains: " + ", ".join(f"{k} {v:.0%}" for k, v in list(e['elite_captains'].items())[:4]),
               f"- Our captain {e['our_captain']} backed by {e['captain_backed']:.0%} of elite teams"]
    (OUT / "briefing.md").write_text("\n".join(md))
    return state


if __name__ == "__main__":
    s = run()
    print(json.dumps({k: v for k, v in s.items()
                      if k not in ("watchlist", "fixtures")}, indent=1)[:2500])
