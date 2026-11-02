"""The validator and the engine, replayed on real gameweeks.

Every FAIL code in guards.py must have a mutation here that triggers it
(test_every_fail_code_has_a_mutation enforces that for future codes)."""
import copy, datetime as dt, json, pathlib, re

import pytest

import advisor as A
import guards as G
from conftest import FIX, load, pid

UTC = dt.timezone.utc
GOLDEN = pathlib.Path(__file__).resolve().parent / "golden"


def failed_codes(rep):
    return {r.code for r in rep.failures}


# --------------------------------------------------------------------------
# GW9 replay: the Dubravka error
# --------------------------------------------------------------------------
def test_gw9_dubravka_is_rejected(gw9):
    """2026-10-30: Verbruggen -> Dubravka was recommended because 5 elites
    OWNED Dubravka. Only 1 started him, he had 0 minutes, and Kinsky was
    Spurs' keeper. Cost ~11 points. The validator must refuse it."""
    st_slots = [p["element"] for p in sorted(gw9.picks["picks"], key=lambda p: p["position"])]
    v, d, jp, nj = (pid(gw9, n) for n in ("Verbruggen", "Dubravka", "João Pedro", "N.Jackson"))
    sw = lambda p: {v: d, jp: nj}.get(p, p)
    plan = dict(gw=9, team_id=3314291,
                transfers=[{"out": v, "in": d}, {"out": jp, "in": nj}],
                xi=[sw(p) for p in st_slots[:11]], bench=[sw(p) for p in st_slots[11:]],
                captain=pid(gw9, "Haaland"), vice=pid(gw9, "Cole Palmer"))
    rep = G.check(plan, gw9)
    assert not rep.passed
    assert {"IN_GK_STARTER", "IN_RECENT_MINUTES", "IN_ELITE_STARTS", "START_GAP"} <= failed_codes(rep)
    assert rep.message.startswith("VALIDATION FAILED")
    assert "Checks passed" not in rep.message


def test_gw9_kinsky_would_have_passed(gw9):
    c = A._ctx(gw9)
    res = G.incoming_results(c, pid(gw9, "Verbruggen"), pid(gw9, "Kinsky"))
    assert all(r.ok for r in res), [r for r in res if not r.ok]


def test_gw9_engine_never_buys_dubravka(gw9):
    plan = A.propose(gw9)
    assert pid(gw9, "Dubravka") not in [m["in"] for m in plan["transfers"]]
    assert G.check(plan, gw9).passed


# --------------------------------------------------------------------------
# GW10 dry run
# --------------------------------------------------------------------------
@pytest.fixture
def gw10_plan(gw10):
    return A.propose(gw10)


def test_gw10_engine_fixes_the_keeper(gw10, gw10_plan):
    rep = G.check(gw10_plan, gw10)
    assert rep.passed, rep.message
    k = pid(gw10, "Kinsky")
    assert [m["in"] for m in gw10_plan["transfers"]] == [k]
    assert gw10_plan["xi"][0] == k and gw10_plan["captain"] == pid(gw10, "Haaland")
    assert "£1.9m bank + £4.0m − £4.4m = £1.5m left" in rep.message
    lines = rep.message.splitlines()
    assert lines[1] == "Checked Mon Nov 2, 8:30 AM ET · valid until Mon Nov 2, 7:00 PM ET (price change)"
    assert lines[2].startswith("First, confirm your FPL app shows 1 free transfer and £1.9m in the bank")
    assert G.verify_stamp(rep.message, gw10_plan)
    other = dict(gw10_plan, captain=gw10_plan["vice"], vice=gw10_plan["captain"])
    assert not G.verify_stamp(rep.message, other)


def test_stamp_breaks_when_the_text_is_edited(gw10, gw10_plan):
    """An LLM that rewrites the stamped message (swaps captain, adds a hit)
    must not keep a valid stamp."""
    msg = G.check(gw10_plan, gw10).message
    for edit in (msg.replace("Captain: Haaland", "Captain: Cole Palmer"),
                 msg.replace("no hit", "-8 pts hit"),
                 msg.replace("Kinsky", "Raya")):
        assert edit != msg and not G.verify_stamp(edit, gw10_plan)


def test_gw10_golden_message(gw10, gw10_plan):
    msg = G.check(gw10_plan, gw10).message
    f = GOLDEN / "gw10_message.txt"
    assert msg == f.read_text().rstrip("\n"), "message format changed: review and update golden"


def test_engine_never_plans_a_hit(gw10):
    gw10.picks["entry_history"]["bank"] = 500           # money is no constraint
    plan = A.propose(gw10)
    assert len(plan["transfers"]) <= 1


def test_cli_exit_codes(gw10_plan, tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(gw10_plan))
    assert G.main(["check", str(f), "--bundle", str(FIX / "gw10_nov2")]) == 0
    f.write_text(json.dumps(dict(gw10_plan, captain=gw10_plan["bench"][1])))
    assert G.main(["check", str(f), "--bundle", str(FIX / "gw10_nov2")]) == 2
    assert G.main(["check", str(f), "--bundle", str(tmp_path / "nope")]) == 3
    assert A.main(["run", "--bundle", str(FIX / "gw10_nov2")]) == 0


# --------------------------------------------------------------------------
# one mutation per FAIL code
# --------------------------------------------------------------------------
def _hist_zero(b, name):
    for r in b.summaries[pid(b, name)]["history"]:
        r["minutes"], r["starts"] = 0, 0


def _swap(lst, a, b_):
    return [b_ if x == a else a if x == b_ else x for x in lst]


def _move_in_lineup(p, a, b_):
    """Swap players a and b_ between XI/bench positions."""
    p["xi"], p["bench"] = [b_ if x == a else a if x == b_ else x for x in p["xi"]], \
        [b_ if x == a else a if x == b_ else x for x in p["bench"]]


def m_data_fresh(b, p):
    b.fetched_at["bootstrap"] = b.now - dt.timedelta(hours=3)


def m_gw_target(b, p):
    p["gw"] = 9


def m_team_id(b, p):
    p["team_id"] = 1


def m_rules_drift(b, p):
    b.bootstrap["game_settings"]["squad_team_limit"] = 4


def m_ids_known(b, p):
    p["vice"] = 999999


def m_elite_present(b, p):
    b.elite["gw"] = 9


def m_elite_boards(b, p):
    b.elite["teams"] = b.elite["teams"][:9]


def m_move_out_owned(b, p):
    p["transfers"] = [{"out": pid(b, "Verbruggen"), "in": pid(b, "Kinsky")}]


def m_move_in_new(b, p):
    p["transfers"] = [{"out": pid(b, "Bentley"), "in": pid(b, "Dubravka")}]


def m_move_unique(b, p):
    p["transfers"] = p["transfers"] * 2


def m_move_same_pos(b, p):
    p["transfers"] = [{"out": pid(b, "Bentley"), "in": pid(b, "Silva")}]


def m_no_buyback(b, p):
    hall, silva = pid(b, "Hall"), pid(b, "Silva")
    b.pending = dict(gw=10, moves=[{"out": hall, "in": silva}])
    p["transfers"] = [{"out": silva, "in": hall}]


def m_squad_legal(b, p):
    p["transfers"] = [{"out": pid(b, "Bentley"), "in": pid(b, "Raya")}]


def m_lineup_partition(b, p):
    p["xi"] = p["xi"][:10] + [pid(b, "Silva")]


def m_bench_gk_first(b, p):
    p["bench"] = p["bench"][::-1]


def m_budget(b, p):
    b.picks["entry_history"]["bank"] = 0


def m_no_unapproved_hits(b, p):
    hall, silva = pid(b, "Hall"), pid(b, "Silva")
    p["transfers"] = p["transfers"] + [{"out": hall, "in": silva}]
    p["bench"] = [silva if x == hall else x for x in p["bench"]]
    p["hits_approved_by_user"] = True          # the plan cannot approve its own hit


def m_ft_gw(b, p):
    # history lags a GW behind (API just after a deadline / stale cache)
    b.entry_history["current"] = b.entry_history["current"][:-1]


def m_sell_elite_owned(b, p):
    # City blank this week; elites keep Haaland (benched) — selling him must fail
    mci = b.by_id[pid(b, "Haaland")]["team"]
    b.fixtures = [f for f in b.fixtures if not (f["event"] == 10 and mci in (f["team_h"], f["team_a"]))]
    p["transfers"] = p["transfers"] + [{"out": pid(b, "Haaland"), "in": pid(b, "Calvert-Lewin")}]


def m_elite_boards_current(b, p):
    for t in b.elite["teams"]:
        t["last_updated"] = "Oct. 20, 2026, 9:00 a.m."          # before the GW9 deadline


def m_elite_boards_max(b, p):
    extra = copy.deepcopy(b.elite["teams"][0])
    extra["manager"] = "S13"
    b.elite["teams"].append(extra)


def m_pending_priced(b, p):
    b.pending = dict(gw=10, moves=[{"out": pid(b, "Hall"), "in": pid(b, "Silva")}], app_ft_left=1)


def m_squad_replay(b, p):
    b.opening = [pid(b, "Raya") if x == b.opening[0] else x for x in b.opening]


def _tie_pair(b, p):
    """A benched outfielder within one elite start of a starter (legal swap)."""
    import advisor
    c = advisor._ctx(b)
    for q in p["bench"][1:]:
        for x in p["xi"]:
            if b.by_id[x]["element_type"] == 1 or not c.playing(q):
                continue
            if abs(c.eff_starts(q) - c.eff_starts(x)) <= 1 and not __import__("fpl_rules").xi_violations(
                    [q if y == x else y for y in p["xi"]], b.by_id):
                return q, x
    raise AssertionError("no tie pair in fixture")


def m_xi_tie_odds(b, p):
    q, x = _tie_pair(b, p)
    b.xp = {e["id"]: 3.0 for e in b.bootstrap["elements"]}
    b.xp[q], b.xp[x] = 9.0, 2.0
    b.fetched_at["xp"] = b.now


def m_bench_order(b, p):
    p["bench"] = p["bench"][:1] + p["bench"][1:][::-1]


def m_engine_agrees(b, p):
    # two free transfers: the engine spends the 2nd on Silva (7 starts);
    # a plan spending it on Van Hecke (3) is legal but clearly weaker
    b.pending = dict(gw=10, moves=[], app_ft_left=2)
    hall, vh = pid(b, "Hall"), pid(b, "Van Hecke")
    p["transfers"] = p["transfers"] + [{"out": hall, "in": vh}]
    p["bench"] = [vh if x == hall else x for x in p["bench"]]


def m_history_fresh(b, p):
    # Spurs played (and finished) a match the cached history doesn't have yet
    tot = b.by_id[pid(b, "Kinsky")]["team"]
    f = next(f for f in b.fixtures if f["event"] == 10 and tot in (f["team_h"], f["team_a"]))
    f.update(finished=True, kickoff_time="2026-11-02T10:00:00Z")


def m_no_chip(b, p):
    p["chip"] = "wildcard"


def m_in_fit(b, p):
    b.by_id[pid(b, "Kinsky")].update(status="i", chance_of_playing_next_round=0)


def m_in_fixture(b, p):
    club = b.by_id[pid(b, "Kinsky")]["team"]
    b.fixtures = [f for f in b.fixtures if not (f["event"] == 10 and club in (f["team_h"], f["team_a"]))]


def m_in_recent_minutes(b, p):
    _hist_zero(b, "Kinsky")


def m_in_gk_starter(b, p):
    # Dubravka started Spurs' last match, Kinsky was dropped for it
    k = sorted(b.summaries[pid(b, "Kinsky")]["history"], key=lambda r: r["kickoff_time"])
    d = sorted(b.summaries[pid(b, "Dubravka")]["history"], key=lambda r: r["kickoff_time"])
    k[-1].update(minutes=0, starts=0)
    d[-1].update(minutes=90, starts=1)


def m_in_elite_starts(b, p):
    p["transfers"] = [{"out": pid(b, "Bentley"), "in": pid(b, "Verbruggen")}]
    p["xi"] = [pid(b, "Verbruggen") if x == pid(b, "Kinsky") else x for x in p["xi"]]


def m_start_gap(b, p):
    mbeumo, ode = pid(b, "Mbeumo"), pid(b, "Ødegaard")
    for t in b.elite["teams"]:                      # demote Mbeumo on one board: 4 -> 3 starts
        ids = t["player_ids"]
        if mbeumo in ids[:11]:
            sub = next((x for x in ids[12:] if b.by_id[x]["element_type"] == 3), None)
            if sub and t.get("captain_id") != mbeumo and t.get("vice_id") != mbeumo:
                t["player_ids"] = _swap(ids, mbeumo, sub)
                break
    b.xp = {}
    p["transfers"] = p["transfers"] + [{"out": ode, "in": mbeumo}]
    p["bench"] = [mbeumo if x == ode else x for x in p["bench"]]


def m_xi_legal(b, p):
    defs = [x for x in p["xi"] if b.by_id[x]["element_type"] == 2][:2]
    mids = [x for x in p["bench"] if b.by_id[x]["element_type"] == 3][:2]
    for a, c in zip(defs, mids):
        _move_in_lineup(p, a, c)


def m_xi_fit(b, p):
    b.by_id[pid(b, "Rogers")].update(status="i", chance_of_playing_next_round=0)


def m_xi_no_long_shots(b, p):
    b.by_id[pid(b, "Cole Palmer")].update(status="d", chance_of_playing_next_round=50)


def m_xi_has_fixture(b, p):
    club = b.by_id[pid(b, "Rogers")]["team"]
    b.fixtures = [f for f in b.fixtures if not (f["event"] == 10 and club in (f["team_h"], f["team_a"]))]


def m_xi_playing(b, p):
    _hist_zero(b, "Muharemović")     # 4/12 elite starts: no "back from injury" pass


def m_xi_gk_starter(b, p):
    _move_in_lineup(p, pid(b, "Kinsky"), pid(b, "Dubravka"))


def m_bench_elite_order(b, p):
    _move_in_lineup(p, pid(b, "Cole Palmer"), pid(b, "Ødegaard"))
    p["vice"] = pid(b, "Rogers")


def m_captain_valid(b, p):
    p["captain"] = p["bench"][1]


def m_vice_valid(b, p):
    p["vice"] = p["captain"]


def m_captain_elite(b, p):
    p["captain"], p["vice"] = pid(b, "Cole Palmer"), pid(b, "Haaland")


def m_autosub_cover(b, p):
    b.by_id[pid(b, "Rogers")].update(status="d", chance_of_playing_next_round=75)
    for x in p["bench"][1:]:
        _hist_zero(b, b.by_id[x]["web_name"])


def m_before_deadline(b, p):
    dl = dt.datetime.fromisoformat(b.next_event()["deadline_time"].replace("Z", "+00:00"))
    b.now = dl - dt.timedelta(minutes=5)
    b.fetched_at = {k: b.now for k in b.fetched_at}


def m_state_build(b, p):
    b.pending = dict(gw=10, moves=[{"out": "Palmer", "in": "Kinsky"}])


def m_input_missing(b, p):
    del b.summaries[pid(b, "Kinsky")]


MUTATIONS = {
    "DATA_FRESH": m_data_fresh, "GW_TARGET": m_gw_target, "TEAM_ID": m_team_id,
    "RULES_DRIFT": m_rules_drift, "IDS_KNOWN": m_ids_known, "ELITE_PRESENT": m_elite_present,
    "ELITE_BOARDS": m_elite_boards, "MOVE_OUT_OWNED": m_move_out_owned,
    "MOVE_IN_NEW": m_move_in_new, "MOVE_UNIQUE": m_move_unique, "MOVE_SAME_POS": m_move_same_pos,
    "NO_BUYBACK": m_no_buyback, "SQUAD_LEGAL": m_squad_legal,
    "LINEUP_PARTITION": m_lineup_partition, "BENCH_GK_FIRST": m_bench_gk_first,
    "BUDGET": m_budget, "NO_UNAPPROVED_HITS": m_no_unapproved_hits, "NO_CHIP": m_no_chip,
    "IN_FIT": m_in_fit, "IN_FIXTURE": m_in_fixture, "IN_RECENT_MINUTES": m_in_recent_minutes,
    "IN_GK_STARTER": m_in_gk_starter, "IN_ELITE_STARTS": m_in_elite_starts,
    "START_GAP": m_start_gap, "XI_LEGAL": m_xi_legal, "XI_FIT": m_xi_fit,
    "XI_NO_LONG_SHOTS": m_xi_no_long_shots, "XI_HAS_FIXTURE": m_xi_has_fixture,
    "XI_PLAYING": m_xi_playing, "XI_GK_STARTER": m_xi_gk_starter,
    "BENCH_ELITE_ORDER": m_bench_elite_order, "CAPTAIN_VALID": m_captain_valid,
    "VICE_VALID": m_vice_valid, "CAPTAIN_ELITE": m_captain_elite,
    "AUTOSUB_COVER": m_autosub_cover, "BEFORE_DEADLINE": m_before_deadline,
    "STATE_BUILD": m_state_build, "INPUT_MISSING": m_input_missing,
    "FT_GW": m_ft_gw, "SELL_ELITE_OWNED": m_sell_elite_owned, "BENCH_ORDER": m_bench_order,
    "ENGINE_AGREES": m_engine_agrees, "HISTORY_FRESH": m_history_fresh,
    "ELITE_BOARDS_CURRENT": m_elite_boards_current, "ELITE_BOARDS_MAX": m_elite_boards_max,
    "PENDING_PRICED": m_pending_priced, "SQUAD_REPLAY": m_squad_replay, "XI_TIE_ODDS": m_xi_tie_odds,
}
MESSAGE_CODES = {"NO_SECRETS", "CONFIRM_LINE", "ET_ONLY", "PRICE_WINDOW_LINE"}
META_CODES = {"RENDER_ERROR"}                  # plus CHECK_ERROR_<group>


@pytest.mark.parametrize("code", sorted(MUTATIONS))
def test_mutation_triggers_code(code):
    b = load("gw10_nov2")
    plan = copy.deepcopy(A.propose(load("gw10_nov2")))
    MUTATIONS[code](b, plan)
    rep = G.check(plan, b)
    assert not rep.passed
    assert code in failed_codes(rep), rep.message
    assert "Checks passed" not in rep.message


def test_every_fail_code_has_a_mutation():
    src = (pathlib.Path(G.__file__)).read_text()
    codes = set()
    for m in re.finditer(r'Result\(\s*"([A-Z_]+)"', src):
        tail = src[m.end(): m.end() + 600]
        lv = re.search(r"\b(FAIL|WARN)\b", tail)
        if lv and lv.group(1) == "FAIL":
            codes.add(m.group(1))
    missing = codes - set(MUTATIONS) - MESSAGE_CODES - META_CODES
    assert not missing, f"FAIL codes without a regression mutation: {sorted(missing)}"


# --------------------------------------------------------------------------
# message-level checks
# --------------------------------------------------------------------------
@pytest.fixture
def ctx_and_msg(gw10, gw10_plan):
    rep = G.check(gw10_plan, gw10)
    c = G._Ctx(gw10_plan, gw10, rep.state)
    G._checks_money(c)
    return c, rep.message


def _codes(results):
    return {r.code for r in results if not r.ok}


def test_message_checks_pass_on_engine_output(ctx_and_msg):
    c, msg = ctx_and_msg
    assert _codes(G._checks_message(c, msg)) == set()


def test_secret_leak_blocked(ctx_and_msg):
    c, msg = ctx_and_msg
    c.b.secrets = ["fake-test-secret-NOT-REAL-000000"]
    assert "NO_SECRETS" in _codes(G._checks_message(c, msg + "\n" + c.b.secrets[0]))
    assert "NO_SECRETS" in _codes(G._checks_message(c, msg + "\nsessionid=x"))


def test_confirm_line_required(ctx_and_msg):
    c, msg = ctx_and_msg
    assert "CONFIRM_LINE" in _codes(G._checks_message(c, msg.replace("confirm your FPL app", "")))


def test_et_only(ctx_and_msg):
    c, msg = ctx_and_msg
    assert "ET_ONLY" in _codes(G._checks_message(c, msg + "\nDeadline 18:30 UTC"))


def test_price_window_line_required_when_prices_move(gw10, gw10_plan):
    k = pid(gw10, "Kinsky")
    gw10.by_id[k]["price_change_projections"] = [dict(offset=0, projected_percent="104")]
    rep = G.check(gw10_plan, gw10)
    assert rep.passed, rep.message
    assert "Make the transfers before Mon Nov 2, 7:00 PM ET (price change)." in rep.message
    c = G._Ctx(gw10_plan, gw10, rep.state)
    G._checks_money(c)
    assert "PRICE_WINDOW_LINE" in _codes(G._checks_message(c, rep.message.replace("Mon Nov 2, 7:00 PM ET", "")))


# --------------------------------------------------------------------------
# review findings (2026-11-02 adversarial review) kept as regressions
# --------------------------------------------------------------------------
def test_hits_need_pramods_approval_in_the_pending_note(gw10, gw10_plan):
    hall, silva = pid(gw10, "Hall"), pid(gw10, "Silva")
    plan = dict(gw10_plan, transfers=gw10_plan["transfers"] + [{"out": hall, "in": silva}],
                bench=[silva if x == hall else x for x in gw10_plan["bench"]])
    gw10.pending = dict(gw=10, moves=[], approved_hits=4)
    rep = G.check(plan, gw10)
    assert "NO_UNAPPROVED_HITS" not in failed_codes(rep)


def test_blank_gameweek_is_not_an_injury(gw10, gw10_plan):
    """MCI blank: elites bench Haaland but keep him. Selling him must fail."""
    mci = gw10.by_id[pid(gw10, "Haaland")]["team"]
    gw10.fixtures = [f for f in gw10.fixtures
                     if not (f["event"] == 10 and mci in (f["team_h"], f["team_a"]))]
    c = A._ctx(gw10)
    h = pid(gw10, "Haaland")
    assert not c.playing(h) and c.will_play(h) and c.out_starts(h) > 0
    res = {r.code: r.ok for r in G.incoming_results(c, h, pid(gw10, "Calvert-Lewin"))}
    assert res["SELL_ELITE_OWNED"] is False


def test_gk_pair_follows_elite_starts(gw10, gw10_plan):
    """Two No.1 keepers: start the one the elites start."""
    rush = pid(gw10, "Rushworth")
    for r in gw10.summaries[rush]["history"]:
        r.update(minutes=0, starts=0)
    for r in gw10.summaries[pid(gw10, "Bentley")]["history"][-3:]:
        r.update(minutes=90, starts=1)
    plan = copy.deepcopy(gw10_plan)
    _move_in_lineup(plan, pid(gw10, "Kinsky"), pid(gw10, "Bentley"))
    assert "BENCH_ELITE_ORDER" in failed_codes(G.check(plan, gw10))


def test_unowned_elite_captain_does_not_block_advice(gw10):
    """Elites switch the armband to a player Pramod doesn't own: the engine
    must still produce a passing plan (captain chosen among his starters)."""
    silva = pid(gw10, "Silva")
    for t in gw10.elite["teams"]:
        if silva in t["player_ids"][:11]:
            t["captain_id"] = silva
            if t.get("vice_id") == silva:
                t["vice_id"] = pid(gw10, "Haaland")
    plan = A.propose(gw10)
    assert G.check(plan, gw10).passed


def test_engine_respects_club_cap_per_pair(gw10):
    gw10.picks["entry_history"]["bank"] = 500
    c = A._ctx(gw10)
    by = gw10.by_id
    for o, i, _ in A.candidate_moves(c):
        clubs = [by[p]["team"] for p in c.st.slots if p != o] + [by[i]["team"]]
        assert clubs.count(by[i]["team"]) <= 3


def test_duplicate_and_free_hit_boards_are_dropped(gw10, gw10_plan):
    gw10.elite["teams"][-1] = copy.deepcopy(gw10.elite["teams"][0])
    gw10.elite["teams"][1]["chip"] = "FH"
    c = G._Ctx(gw10_plan, gw10, A._ctx(gw10).st)
    assert c.ec["n"] == 10 and len(c.elite_bad) == 2


def test_cli_marks_snapshot_replays(gw10_plan, tmp_path, capsys):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(gw10_plan))
    G.main(["check", str(f), "--bundle", str(FIX / "gw10_nov2")])
    assert capsys.readouterr().out.startswith("REPLAY of a saved snapshot")


def test_rolling_at_five_free_transfers_warns(gw10):
    gw10.pending = dict(gw=10, moves=[], app_ft_left=5)
    gw10.picks["entry_history"]["bank"] = 0          # nothing affordable -> roll
    plan = dict(A.propose(gw10), transfers=[])
    rep = G.check(plan, gw10)
    assert "FT_NOT_WASTED" in {r.code for r in rep.warnings}


# --------------------------------------------------------------------------
# GPT / Gemini cross-family review (2026-11-02) kept as regressions
# --------------------------------------------------------------------------
def test_engine_applies_the_odds_tie_break(gw10, gw10_plan):
    q, x = _tie_pair(gw10, gw10_plan)
    gw10.xp = {e["id"]: 3.0 for e in gw10.bootstrap["elements"]}
    gw10.xp[q], gw10.xp[x] = 9.0, 2.0
    gw10.fetched_at["xp"] = gw10.now
    plan = A.propose(gw10)
    assert q in plan["xi"] and x not in plan["xi"]
    assert G.check(plan, gw10).passed


def test_stale_odds_decide_nothing(gw10):
    gw10.xp = {e["id"]: 3.0 for e in gw10.bootstrap["elements"]}
    gw10.fetched_at["xp"] = gw10.now - dt.timedelta(days=5)
    c = A._ctx(gw10)
    assert not c.xp_fresh and c.xp(pid(gw10, "Haaland")) is None


def test_hits_already_taken_count_against_the_approval(gw10, gw10_plan):
    hall, silva, gw_, tav = (pid(gw10, n) for n in ("Hall", "Silva", "Gibbs-White", "Tavernier"))
    gw10.pending = dict(gw=10, moves=[{"out": hall, "in": silva, "in_cost": 50},
                                      {"out": gw_, "in": tav, "in_cost": 66}],
                        approved_hits=4, app_bank_tenths=20)
    plan = A.propose(gw10)
    plan["transfers"] = [{"out": pid(gw10, "Dubravka"), "in": pid(gw10, "Kinsky")}]
    rep = G.check(plan, gw10)
    assert "NO_UNAPPROVED_HITS" in failed_codes(rep)          # the approved -4 is used up


def test_bad_pending_note_is_rejected(gw10):
    gw10.pending = dict(gw=10, moves=[], app_ft_left=99)
    rep = G.check(A.propose(load("gw10_nov2")), gw10)
    assert "STATE_BUILD" in failed_codes(rep)


def test_free_hit_rollback_is_aligned(gw10, gw10_plan):
    gw10.entry_history["chips"].append({"name": "freehit", "event": 9})
    gw10.picks_gw = 8                                       # bundle.live loads the pre-FH picks
    rep = G.check(gw10_plan, gw10)
    assert "FT_GW" not in failed_codes(rep)


def test_doubtful_50_counts_as_not_playing(gw10):
    c = A._ctx(gw10)
    h = pid(gw10, "Haaland")
    assert c.will_play(h)
    gw10.by_id[h].update(status="d", chance_of_playing_next_round=50)
    assert not A._ctx(gw10).will_play(h)


def test_captain_band_needs_odds_for_everyone(gw10, gw10_plan):
    c = G._Ctx(gw10_plan, gw10, A._ctx(gw10).st)
    assert G.captain_options(c, gw10_plan["xi"]) == [pid(gw10, "Haaland")]


def test_injured_no1_hands_the_gloves_to_the_elites_choice(gw10):
    k, d = pid(gw10, "Kinsky"), pid(gw10, "Dubravka")
    gw10.by_id[k].update(status="i", chance_of_playing_next_round=0)
    c = A._ctx(gw10)
    sg = c.starter_gk(gw10.by_id[k]["team"])
    assert sg["starter"] in (d, None)
    if c.ec["start"][d] >= 1:
        assert sg["starter"] == d


def test_vice_must_play_this_week(gw10, gw10_plan):
    rogers = pid(gw10, "Rogers")
    plan = dict(gw10_plan, vice=rogers)
    che = gw10.by_id[rogers]["team"]
    gw10.fixtures = [f for f in gw10.fixtures if not (f["event"] == 10 and che in (f["team_h"], f["team_a"]))]
    assert "VICE_VALID" in failed_codes(G.check(plan, gw10))


def test_playing_bench_players_come_before_non_playing(gw10, gw10_plan):
    plan = copy.deepcopy(gw10_plan)
    hall = pid(gw10, "Hall")
    for r in gw10.summaries[hall]["history"]:
        r.update(minutes=0, starts=0)                        # Hall stops playing
    out = [x for x in plan["bench"][1:] if x != hall]
    plan["bench"] = plan["bench"][:1] + [hall] + out         # ...but sits first on the bench
    assert "BENCH_ORDER" in failed_codes(G.check(plan, gw10))


def test_bootstrap_older_than_the_price_change_is_stale(gw10, gw10_plan):
    gw10.now = F_parse("2026-11-03T01:00:00Z")               # after Mon 7 PM ET window
    gw10.fetched_at = {k: F_parse("2026-11-02T23:30:00Z") for k in gw10.fetched_at}
    rep = G.check(gw10_plan, gw10)
    assert "DATA_FRESH" in failed_codes(rep)


def F_parse(s):
    import facts
    return facts.parse_utc(s)


def test_stamp_claiming_few_checks_is_rejected(gw10_plan):
    body = "anything at all"
    fake = body + "\n\nChecks passed 0/0 · plan " + G.plan_hash(gw10_plan, body)
    assert not G.verify_stamp(fake, gw10_plan)


def test_msg_out_never_keeps_an_old_message(tmp_path):
    f = tmp_path / "message.txt"
    f.write_text("Checks passed 44/44 · plan old")
    assert A.main(["run", "--bundle", str(tmp_path / "missing"), "--msg-out", str(f)]) == 3
    assert not f.exists()


def test_display_names_disambiguate(gw10):
    import facts
    n = facts.display_names(gw10.bootstrap)
    assert n[154] == "Palmer (CHE)" and n[pid(gw10, "Haaland")] == "Haaland"
