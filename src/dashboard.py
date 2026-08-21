"""FPL Edge — dashboard builder. Renders out/dashboard.html from out/state.json.

Self-contained single-file HTML styled in the official FPL visual language:
purple #37003c, cyan→green gradient accents (#04f5ff → #00ff87), magenta
#e90052 for alerts, kit-coloured shirts on a striped pitch with white player
plates, FPL FDR colours in the fixture ticker, chip bar, Cup-style tables.
"""
from __future__ import annotations
import json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "out"

# FPL official-ish FDR colours (1 easiest → 5 hardest)
FDR = {1: ("#257d5a", "#fff"), 2: ("#00ff86", "#37003c"), 3: ("#ebebe4", "#37003c"),
       4: ("#ff005a", "#fff"), 5: ("#861d46", "#fff")}

# primary kit colours per club (fill, trim)
KIT = {
    "ARS": ("#ef0107", "#ffffff"), "AVL": ("#67002f", "#95bfe5"),
    "BOU": ("#b50e12", "#000000"), "BRE": ("#e30613", "#ffffff"),
    "BHA": ("#0057b8", "#ffffff"), "CHE": ("#034694", "#ffffff"),
    "COV": ("#63b3e4", "#ffffff"), "CRY": ("#1b458f", "#c4122e"),
    "EVE": ("#003399", "#ffffff"), "FUL": ("#ffffff", "#000000"),
    "HUL": ("#f5a12d", "#000000"), "IPS": ("#0044a9", "#ffffff"),
    "LEE": ("#ffffff", "#1d428a"), "LIV": ("#c8102e", "#ffffff"),
    "MCI": ("#6caddf", "#ffffff"), "MUN": ("#da291c", "#000000"),
    "NEW": ("#241f20", "#ffffff"), "NFO": ("#dd0000", "#ffffff"),
    "TOT": ("#ffffff", "#132257"), "SUN": ("#eb172b", "#ffffff"),
}
GK_KIT = ("#ebff00", "#333333")


def _shirt(team: str, gk: bool = False) -> str:
    fill, trim = GK_KIT if gk else KIT.get(team, ("#888888", "#ffffff"))
    return (f'<svg viewBox="0 0 40 38"><path d="M8 2 L15 0 Q20 5 25 0 L32 2 40 9 '
            f'35 15 30 12 30 38 10 38 10 12 5 15 0 9Z" fill="{fill}" '
            f'stroke="{trim}" stroke-width="1.4"/></svg>')


def build() -> pathlib.Path:
    state = json.loads((OUT / "state.json").read_text())
    if not state.get("deadline_et"):
        try:
            import datetime
            from zoneinfo import ZoneInfo
            _dl = datetime.datetime.fromisoformat(state["deadline"].replace("Z", "+00:00"))
            state["deadline_et"] = _dl.astimezone(
                ZoneInfo("America/New_York")).strftime("%a %b %d, %-I:%M %p ET")
        except Exception:
            pass
    sq = state["squad"]
    pos_order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
    xi = sorted(sq["xi"], key=lambda r: (pos_order[r["pos"]], -r["xp"]))
    rows_by_pos = {p: [r for r in xi if r["pos"] == p] for p in pos_order}

    def player_card(r, bench=False):
        cap = ""
        if r["name"] == sq["captain"]:
            cap = '<span class="cbadge">C</span>'
        elif r["name"] == sq["vice"]:
            cap = '<span class="cbadge v">V</span>'
        return (f'<div class="p" title="{r["name"]} ({r["team"]}) £{r["price"]}m — '
                f'xP {r["xp"]}, xMins {r["xmins"]:.0f}, owned {r["sel"]}%">'
                f'{cap}<div class="shirt">{_shirt(r["team"], r["pos"] == "GKP")}</div>'
                f'<div class="pnm">{r["name"]}</div>'
                f'<div class="pinfo">{r["team"]} · £{r["price"]}m · {r["xp"]}</div></div>')

    pitch_rows = "".join(
        f'<div class="prow">{"".join(player_card(r) for r in rows_by_pos[p])}</div>'
        for p in ["GKP", "DEF", "MID", "FWD"])
    bench_row = "".join(player_card(r, bench=True) for r in sq["bench"])

    # chip bar
    lp = state.get("locked_plan") or {}
    chip_names = [("WC", "wildcard"), ("FH", "freehit"), ("BB", "bench_boost"), ("TC", "3xc")]
    chips_html = "".join(
        f'<span class="fplchip{" on" if lp.get("chip") == full else ""}">'
        f'{"BENCH BOOST — ACTIVE" if (lp.get("chip") == full and full == "bench_boost") else ab}</span>'
        for ab, full in chip_names)
    locked_banner = ""
    if lp:
        locked_banner = (f'<div class="locked">LOCKED PLAN · GW{lp["gw"]}'
                         + (f' · {lp["chip"].replace("_", " ").upper()}' if lp.get("chip") else "")
                         + '</div>')

    # watchlist
    wl = state["watchlist"][:20]
    max_h = max(r["horizon"] for r in wl) or 1
    wl_rows = ""
    for r in wl:
        w = 100 * r["horizon"] / max_h
        wl_rows += (f'<tr title="{r["name"]}: next-GW xP {r["xp"]}, 6-GW horizon {r["horizon"]:.1f}">'
                    f'<td class="strong">{r["name"]}</td><td class="mut">{r["team"]} {r["pos"]}</td>'
                    f'<td class="num">£{r["price"]}</td><td class="num">{r["xp"]}</td>'
                    f'<td class="barcell"><div class="bar" style="width:{w:.0f}%"></div>'
                    f'<span class="barlabel">{r["horizon"]:.1f}</span></td></tr>')

    # fixture ticker with FPL FDR colours
    gws = state["gws"]
    fx = state["fixtures"]

    def avg_fdr(v):
        fl = [f["fdr"] for f in v] or [3]
        return sum(fl) / len(fl)

    heat_rows = ""
    for team, v in sorted(fx.items(), key=lambda kv: avg_fdr(kv[1])):
        cells = ""
        by_gw = {}
        for f in v:
            by_gw.setdefault(f["gw"], []).append(f)
        for gw in gws:
            fs = by_gw.get(gw, [])
            if not fs:
                cells += '<td class="cell blank" title="Blank GW">—</td>'
            else:
                f0 = fs[0]
                bg, ink = FDR[f0["fdr"]]
                lab = f'{f0["opp"]}{"" if f0["home"] else " (A)"}'
                extra = f' +{len(fs)-1}' if len(fs) > 1 else ""
                cells += (f'<td class="cell" style="background:{bg};color:{ink}" '
                          f'title="GW{gw}: {"home vs" if f0["home"] else "away at"} {f0["opp"]}, FDR {f0["fdr"]}">'
                          f'{lab}{extra}</td>')
        heat_rows += f'<tr><th class="rowhead">{team}</th>{cells}</tr>'

    plans_html = ""
    if state.get("plans"):
        items = ""
        for p in state["plans"]:
            if p["n"] == 0:
                items += '<li><strong>Roll transfer</strong> — keep squad (baseline)</li>'
            else:
                hit_txt = f' (-{p["hits"]} hit)' if p["hits"] else ""
                items += (f'<li><strong>{p["n"]} transfer(s)</strong>{hit_txt}: '
                          f'OUT {", ".join(p["out"])} → IN {", ".join(p["in_"])} '
                          f'<span class="mut">net {p["gain"]:+.1f} xP</span></li>')
        plans_html = f'<section class="card"><h2>Transfer options</h2><ul class="plans">{items}</ul></section>'

    flags_html = ""
    if state.get("flags"):
        rows = "".join(
            f'<li><span class="dot {"warn" if f["status"]=="d" else "bad"}"></span>'
            f'<strong>{f["name"]}</strong> <span class="mut">({f["team"]})</span> {f["news"] or f["status"]}</li>'
            for f in state["flags"][:14])
        flags_html = f'<section class="card"><h2>Availability flags</h2><ul class="flags">{rows}</ul></section>'

    elite_html = ""
    if state.get("elite"):
        e = state["elite"]
        caps = " · ".join(f"{k} {v:.0%}" for k, v in list(e["elite_captains"].items())[:3])
        lack = ", ".join(e["we_lack"]) if e["we_lack"] else "fully covered"
        elite_html = (f'<section class="card"><h2>Elite consensus <span class="mut">'
                      f'({e["n_teams"]} pro teams, FF Fix)</span></h2>'
                      f'<p><strong>Template core:</strong> {", ".join(e["top_template"])}</p>'
                      f'<p><strong>We lack:</strong> {lack}</p>'
                      f'<p><strong>Elite captains:</strong> {caps} — our pick '
                      f'<strong>{e["our_captain"]}</strong> backed by {e["captain_backed"]:.0%}</p></section>')

    srcs = state.get("sources", {})
    live = sum(1 for v in srcs.values() if v is not None)
    stale = [k for k, v in srcs.items() if v is not None and v > 24 * 60]
    srcs_chip = (f'<span class="hchip">data {live}/{len(srcs)}'
                 + (f' · stale: {", ".join(stale)}' if stale else '') + '</span>') if srcs else ''
    cal = state.get("calibration")
    cal_chip = (f'<span class="hchip">MAE {cal["mae"]} / {cal["gws"]} GW</span>' if cal else '')
    warn_chip = ''
    lw = (state.get("fix_feeds") or {}).get("lineup_warnings")
    if lw:
        warn_chip = f'<span class="hchip alert">{len(lw)} lineup warnings</span>'

    league_html = ""
    rivals_html = ""
    if state.get("league"):
        lg = state["league"]
        rank_txt = lg["my_rank"] if lg.get("my_rank") else "—"
        league_html = (f'<div class="tile"><div class="tl">League rank</div>'
                       f'<div class="tv">{rank_txt}<span class="tvm">/{lg["n"]}</span></div></div>')
        if lg.get("roster"):
            rows = "".join(
                f'<tr{" class=me" if r["entry"] == state.get("team_id") else ""}>'
                f'<td>{i+1}</td><td class="strong">{r["team"]}</td><td class="mut">{r["manager"]}</td>'
                f'<td class="num">{r["total"] or "—"}</td></tr>'
                for i, r in enumerate(lg["roster"]))
            note = ("" if lg.get("picks_available") else
                    '<p class="mut">Rival squads become visible after the GW1 deadline — '
                    'league-local EO, differential &amp; shadow strategy switch on then.</p>')
            rivals_html = (f'<section class="card"><h2>Bad Talks Classic Cash League '
                           f'<span class="mut">({lg["n"]} managers)</span></h2>'
                           f'{note}<table><thead><tr><th>#</th><th>Team</th><th>Manager</th>'
                           f'<th class="num">Pts</th></tr></thead><tbody>{rows}</tbody></table></section>')
    else:
        league_html = ('<div class="tile"><div class="tl">League</div>'
                       '<div class="tv" style="font-size:14px;line-height:1.3">Add team &amp; league IDs</div></div>')

    mode_label = state["mode"].replace("+", " + ")
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FPL Edge — GW{state["gw"]}</title>
<style>
  :root {{ --purple:#37003c; --purple2:#240029; --green:#00ff87; --cyan:#04f5ff;
    --magenta:#e90052; --pitchA:#6ac26a; --pitchB:#5eb85e; --line:#e8e8ee;
    --ink:#37003c; --mut:#6c6c85; }}
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:#f4f4f6; color:#242424;
    font:14px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif }}
  .wrap {{ max-width:980px; margin:0 auto; padding-bottom:40px }}
  /* ---- FPL masthead ---- */
  .mast {{ background:linear-gradient(100deg, var(--purple) 55%, #5b0060 75%, transparent 100%),
           linear-gradient(100deg, transparent 60%, var(--cyan) 82%, var(--green) 100%), var(--purple);
    color:#fff; padding:22px 22px 16px; border-radius:0 0 14px 14px }}
  .mast h1 {{ font-size:26px; font-weight:800; font-style:italic; letter-spacing:-.5px }}
  .mast h1 span {{ color:var(--green) }}
  .mastrow {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; align-items:center }}
  .hchip {{ background:#ffffff22; border:1px solid #ffffff44; color:#fff; border-radius:999px;
    padding:3px 12px; font-size:12px; font-weight:600 }}
  .hchip.alert {{ background:var(--magenta); border-color:var(--magenta) }}
  .locked {{ display:inline-block; background:var(--green); color:var(--purple); font-weight:800;
    font-size:12px; letter-spacing:.05em; border-radius:6px; padding:4px 12px; margin-top:10px }}
  .gen {{ color:#ffffff99; font-size:11px; margin-top:8px }}
  /* ---- chip bar ---- */
  .chipbar {{ background:#fff; display:flex; gap:8px; justify-content:center; padding:10px;
    border-bottom:1px solid var(--line); flex-wrap:wrap }}
  .fplchip {{ font-size:11px; font-weight:800; border:1.5px solid #d5d5dd; border-radius:16px;
    padding:5px 14px; color:#9b9bab; letter-spacing:.04em }}
  .fplchip.on {{ background:var(--green); border-color:var(--green); color:var(--purple) }}
  /* ---- KPI tiles ---- */
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px;
    margin:14px 14px 0 }}
  .tile {{ background:var(--purple); color:#fff; border-radius:12px; padding:12px 14px;
    background-image:linear-gradient(135deg, transparent 70%, #04f5ff33 100%) }}
  .tl {{ font-size:11px; color:#ffffffbb; text-transform:uppercase; letter-spacing:.06em }}
  .tv {{ font-size:26px; font-weight:800; color:var(--green) }}
  .tvm {{ font-size:14px; font-weight:400; color:#ffffff99 }}
  .hero {{ font-size:30px }}
  /* ---- cards ---- */
  .card {{ background:#fff; border-radius:12px; padding:16px; margin:14px 14px 0;
    box-shadow:0 1px 4px #37003c14 }}
  h2 {{ font-size:15px; margin-bottom:10px; color:var(--ink); font-weight:800 }}
  /* ---- pitch ---- */
  .pitchwrap {{ margin:14px 14px 0; border-radius:12px; overflow:hidden;
    box-shadow:0 1px 4px #37003c14 }}
  .pitchhead {{ background:var(--purple); color:#fff; padding:10px 16px; font-weight:800;
    font-size:14px }} .pitchhead span {{ color:var(--green) }}
  .pitch {{ background:repeating-linear-gradient(to bottom,var(--pitchA) 0 56px,var(--pitchB) 56px 112px);
    padding:18px 6px 12px; position:relative }}
  .pitch::before {{ content:""; position:absolute; left:50%; top:-46px; transform:translateX(-50%);
    width:240px; height:92px; border:2px solid #ffffff77; border-radius:0 0 120px 120px; border-top:none }}
  .prow {{ display:flex; justify-content:center; gap:1.5%; margin-bottom:12px; flex-wrap:wrap;
    position:relative; z-index:2 }}
  .p {{ width:86px; display:flex; flex-direction:column; align-items:center; position:relative }}
  .shirt {{ width:44px; height:41px; filter:drop-shadow(0 2px 2px #0003) }}
  .shirt svg {{ width:100%; height:100% }}
  .cbadge {{ position:absolute; top:-4px; right:8px; z-index:3; width:18px; height:18px;
    border-radius:50%; background:var(--purple); color:#fff; font-size:11px; font-weight:800;
    display:flex; align-items:center; justify-content:center; border:2px solid #fff }}
  .cbadge.v {{ background:#fff; color:var(--purple); border-color:var(--purple) }}
  .pnm {{ margin-top:4px; width:100%; text-align:center; background:#fff; font-size:11px;
    font-weight:700; color:var(--purple); padding:2.5px 2px; border-radius:3px 3px 0 0;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis }}
  .pinfo {{ width:100%; text-align:center; background:var(--purple); color:#fff; font-size:9.5px;
    padding:2px 1px; border-radius:0 0 3px 3px; white-space:nowrap }}
  .benchbar {{ background:#fff; display:flex; gap:1.5%; justify-content:center; padding:12px 6px;
    flex-wrap:wrap }}
  .benchbar .pnm {{ background:#f1f1f4 }}
  /* ---- tables ---- */
  table {{ border-collapse:collapse; width:100% }}
  td,th {{ padding:6px 8px; text-align:left; font-size:13px; border-bottom:1px solid var(--line) }}
  th {{ color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.05em }}
  tr.me td {{ background:#00ff8722; font-weight:700 }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums }}
  .mut {{ color:var(--mut); font-weight:400 }} .strong {{ font-weight:700; color:var(--ink) }}
  .barcell {{ width:40%; position:relative }}
  .bar {{ height:14px; background:linear-gradient(90deg,var(--cyan),var(--green));
    border-radius:0 4px 4px 0; display:inline-block; vertical-align:middle }}
  .barlabel {{ font-size:12px; color:var(--mut); margin-left:6px }}
  .heat td.cell {{ text-align:center; font-size:11px; font-weight:700; border:2px solid #fff;
    border-radius:4px; padding:6px 4px; min-width:62px }}
  .heat td.blank {{ color:var(--mut); background:#f4f4f6 }}
  .rowhead {{ font-weight:800; color:var(--ink); font-size:12px }}
  ul.plans, ul.flags {{ list-style:none }}
  ul.plans li, ul.flags li {{ padding:7px 0; border-bottom:1px solid var(--line); font-size:13.5px }}
  .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px }}
  .dot.warn {{ background:#ffab1f }} .dot.bad {{ background:var(--magenta) }}
  .foot {{ color:var(--mut); font-size:11px; margin:20px 14px 0; text-align:center }}
</style></head><body><div class="wrap">

<div class="mast">
  <h1>FPL <span>Edge</span></h1>
  <div class="mastrow">
    <span class="hchip">Gameweek {state["gw"]}</span>
    <span class="hchip">mode: {mode_label}</span>
    <span class="hchip">{state.get("odds", "market off")}</span>
    {srcs_chip}{cal_chip}{warn_chip}
  </div>
  {locked_banner}
  <div class="gen">generated {state["generated"][:16].replace("T", " ")} UTC · suggestion mode — you apply moves in the official app</div>
</div>

<div class="chipbar">{chips_html}</div>

<div class="kpis">
  <div class="tile"><div class="tl">Deadline{(" · " + state.get("deadline_et", "")) if state.get("deadline_et") else ""}</div>
    <div class="tv hero" id="cd">—</div><div class="tl" id="cdd"></div></div>
  <div class="tile"><div class="tl">{"15-man xP (Bench Boost)" if lp.get("chip") == "bench_boost" else "XI expected points (incl. C)"}</div>
    <div class="tv">{sq["xp"]}</div></div>
  <div class="tile"><div class="tl">Squad cost</div><div class="tv">£{sq["cost"]:.1f}<span class="tvm">m</span></div></div>
  {league_html}
</div>

<div class="pitchwrap">
  <div class="pitchhead">{"Locked squad" if lp else "Recommended team"} — captain <span>{sq["captain"]}</span> · vice {sq["vice"]}</div>
  <div class="pitch">{pitch_rows}</div>
  <div class="benchbar">{bench_row}</div>
</div>

{plans_html}

{rivals_html}

{elite_html}

<section class="card"><h2>Watchlist — 6-GW horizon xP</h2>
<table><thead><tr><th>Player</th><th></th><th class="num">£</th>
<th class="num">next xP</th><th>6-GW xP</th></tr></thead><tbody>{wl_rows}</tbody></table></section>

<section class="card"><h2>Fixture run — next {len(gws)} GWs <span class="mut">(FPL difficulty colours)</span></h2>
<table class="heat"><thead><tr><th></th>{"".join(f"<th>GW{g}</th>" for g in gws)}</tr></thead>
<tbody>{heat_rows}</tbody></table></section>

{flags_html}

<div class="foot">FPL Edge · deadline {state.get("deadline_et", state["deadline"])}</div>
</div>
<script>
  var dl = new Date("{state["deadline"]}");
  function tick() {{
    var ms = dl - Date.now();
    var el = document.getElementById('cd'), sub = document.getElementById('cdd');
    if (ms <= 0) {{ el.textContent = 'passed'; sub.textContent = dl.toLocaleString(); return; }}
    var h = Math.floor(ms/36e5), m = Math.floor(ms%36e5/6e4);
    el.textContent = (h>=24 ? Math.floor(h/24)+'d ' : '') + (h%24)+'h '+m+'m';
    sub.textContent = dl.toLocaleString();
  }}
  tick(); setInterval(tick, 30000);
</script>
</body></html>"""
    p = OUT / "dashboard.html"
    p.write_text(html)
    return p


if __name__ == "__main__":
    print(build())
