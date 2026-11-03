"""Derived facts: purchase prices, slot inheritance, elite counts, minutes,
starting keepers, price windows in ET."""
import datetime as dt

import pytest

import facts as F
import fpl_rules as R
from conftest import pid


def test_purchase_prices_from_api_not_first_seen(gw10):
    """purchases.json booked prices when first seen and overstated selling
    value by £1.6m (1009 vs 993 tenths). The API transfer record is right."""
    squad = [p["element"] for p in gw10.picks["picks"]]
    pp = F.purchase_prices(squad, gw10.transfers, gw10.by_id, gw10.entry_history["chips"])
    sells = F.selling_prices(pp, gw10.by_id)
    assert sum(sells.values()) == 993
    assert pp[pid(gw10, "Rogers")] == (75, "transfer record")
    assert pp[pid(gw10, "Calafiori")] == (55, "season start price")
    assert sells[pid(gw10, "Rogers")] == 78          # not 80


def test_free_hit_buys_are_not_purchase_prices(gw10):
    squad = [pid(gw10, "Rogers")]
    tr = [dict(event=5, element_in=squad[0], element_in_cost=90, element_out=1,
               element_out_cost=50, time="2026-09-20T10:00:00Z")]
    chips = [dict(name="freehit", event=5)]
    e = gw10.by_id[squad[0]]
    assert F.purchase_prices(squad, tr, gw10.by_id, chips)[squad[0]][0] == \
        e["now_cost"] - e["cost_change_start"]


def test_gw8_slot_inheritance():
    """GW8 cost ~5 pts: Barry took Wissa's slot 11 and Muharemović took
    Egan's bench slot 15. The plan must predict exactly that."""
    gw7 = [109, 31, 8, 115, 449, 124, 40, 154, 480, 411, 9001, 623, 9002, 15, 9003]
    wissa, egan, barry, muha = 9001, 9003, 249, 334
    after = F.predicted_app_slots(gw7, [(wissa, barry), (egan, muha)])
    assert after.index(barry) + 1 == 11
    assert after.index(muha) + 1 == 15


def test_slot_inheritance_rejects_unknown_sale():
    with pytest.raises(ValueError):
        F.predicted_app_slots([1, 2, 3], [(4, 5)])


def test_lineup_actions_pairs_same_position(gw10):
    by = gw10.by_id
    slots = [p["element"] for p in sorted(gw10.picks["picks"], key=lambda p: p["position"])]
    xi = [x for x in slots[:11] if x != pid(gw10, "Hall")] + [pid(gw10, "Konsa")]
    same_order = [pid(gw10, "Bentley"), pid(gw10, "Hall"), pid(gw10, "De Cuyper"), pid(gw10, "Groß")]
    acts = F.lineup_actions(slots, xi, same_order, by)
    assert acts == [("swap", pid(gw10, "Konsa"), pid(gw10, "Hall"))]
    reordered = [pid(gw10, "Bentley"), pid(gw10, "Groß"), pid(gw10, "Hall"), pid(gw10, "De Cuyper")]
    acts = F.lineup_actions(slots, xi, reordered, by)
    assert acts == [("swap", pid(gw10, "Konsa"), pid(gw10, "Hall")), ("bench_order", 0, 0)]


def test_elite_counts_gw9_dubravka_owned_not_started(gw9):
    ec = F.elite_counts(gw9.elite)
    d = pid(gw9, "Dubravka")
    assert ec["own"][d] == 5 and ec["start"][d] == 1      # ownership lied; starts didn't
    assert ec["start"][pid(gw9, "Kinsky")] == 3


def test_board_integrity_catches_wrong_namesake(gw10):
    """2026-11-02: Fix's bare "Muñoz" matched Victor Munoz (MID) instead of
    Daniel Muñoz (DEF); the board had 4 DEF / 6 MID and the XI/bench split
    was wrong. A legal board passes; the mis-mapped one is rejected."""
    by = gw10.by_id
    board = next(t for t in gw10.elite["teams"] if 201 in t["player_ids"])
    assert F.board_violations(board, by) == []
    bad = dict(board, player_ids=[377 if p == 201 else p for p in board["player_ids"]])
    assert F.board_violations(bad, by)


def test_recent_minutes_cut_at_the_gameweek(gw9):
    rd = F.recent(gw9.summaries[pid(gw9, "Dubravka")], before_gw=9)
    assert rd["minutes"] == 0 and rd["starts"] == 0
    rk = F.recent(gw9.summaries[pid(gw9, "Kinsky")], before_gw=9)
    assert rk["starts"] == 3 and rk["minutes"] >= 270


def test_club_starter_gk_spurs_is_kinsky(gw9):
    tot = gw9.by_id[pid(gw9, "Kinsky")]["team"]
    sg = F.club_starter_gk(tot, gw9.by_id, gw9.club_gk_summaries(tot), 9)
    assert sg["starter"] == pid(gw9, "Kinsky")


@pytest.mark.parametrize("status,chance,want", [
    ("a", None, "fit"), ("a", 100, "fit"), ("d", 75, "doubt"), ("d", 25, "doubt"),
    ("i", 0, "out"), ("s", None, "out"), ("u", 0, "out")])
def test_fitness(status, chance, want):
    assert F.fitness(dict(status=status, chance_of_playing_next_round=chance))[0] == want


@pytest.mark.parametrize("now,window_et", [
    ("2026-10-20T15:00:00Z", "Tue Oct 20, 7:00 PM ET"),   # BST + EDT
    ("2026-10-30T20:10:00Z", "Fri Oct 30, 8:00 PM ET"),   # GMT + EDT (UK changed first)
    ("2026-11-02T13:30:00Z", "Mon Nov 2, 7:00 PM ET"),    # GMT + EST
])
def test_price_window_in_et_across_both_clock_changes(now, window_et):
    w = F.next_price_window(F.parse_utc(now))
    assert F.fmt_et(w) == window_et


def test_price_projection_direction():
    mk = lambda pct: dict(price_change_projections=[dict(offset=0, projected_percent=pct)])
    assert F.price_direction_tonight(mk("104.2")) == 1
    assert F.price_direction_tonight(mk("-101")) == -1
    assert F.price_direction_tonight(mk("63")) == 0
    assert F.price_direction_tonight({}) == 0


def test_fixture_count(gw10):
    tot = gw10.by_id[pid(gw10, "Kinsky")]["team"]
    assert F.fixture_count(tot, 10, gw10.fixtures) == 1
    assert F.fixture_count(tot, 10, []) == 0


@pytest.mark.parametrize("text,utc", [
    ("Oct. 31, 2026, 11:59 a.m.", "2026-10-31T11:59:00+00:00"),
    ("Oct. 31, 2026, noon", "2026-10-31T12:00:00+00:00"),
    ("Sept. 5, 2026, 12:30 a.m.", "2026-09-04T23:30:00+00:00"),     # BST
    ("Nov. 2, 2026, 9 p.m.", "2026-11-02T21:00:00+00:00"),
])
def test_parse_fix_last_updated(text, utc):
    assert F.parse_fix_time(text).isoformat() == utc
    assert F.parse_fix_time("garbage") is None


def test_recent_counts_matches_by_kickoff_when_now_given(gw10):
    s = gw10.summaries[pid(gw10, "Kinsky")]
    now = F.parse_utc("2026-11-02T13:30:00Z")
    assert F.recent(s, 10, now=now) == F.recent(s, 10)
    early = F.parse_utc("2026-10-20T00:00:00Z")
    assert F.recent(s, 10, now=early)["rounds"][-1] < F.recent(s, 10)["rounds"][-1]


def test_history_freshness(gw10):
    k = pid(gw10, "Kinsky")
    tot = gw10.by_id[k]["team"]
    now = F.parse_utc("2026-11-02T13:30:00Z")
    assert F.history_is_current(gw10.summaries[k], tot, gw10.fixtures, now)
    fx = [dict(f) for f in gw10.fixtures]
    f = next(f for f in fx if f["event"] == 10 and tot in (f["team_h"], f["team_a"]))
    f.update(finished=True, kickoff_time="2026-11-02T10:00:00Z")
    assert not F.history_is_current(gw10.summaries[k], tot, fx, now)


def test_fixture_boards_are_synthetic():
    """Real elite boards come from a paid account; this repo is public."""
    import json
    from conftest import FIX
    for f in FIX.glob("*/elite.json"):
        d = json.loads(f.read_text())
        assert d["source"].startswith("synthetic"), f
        assert all(t["manager"].startswith("S") for t in d["teams"]), f


def test_synthesizer_preserves_counts_and_legality(gw10):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "synthesize_boards", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "synthesize_boards.py")
    sb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sb)
    syn = sb.synthesize(gw10.elite, gw10.by_id, seed=11)
    assert sb.same_counts(gw10.elite, syn)
    assert all(F.board_violations(t, gw10.by_id) == [] for t in syn["teams"])


def test_last_three_uses_the_clubs_fixtures():
    """A club match missing from the player's rows counts as 0 minutes."""
    mk = lambda i, ko: dict(id=i, event=i, team_h=1, team_a=2, kickoff_time=ko)
    fx = {i: mk(i, f"2026-10-{10 + i:02d}T15:00:00Z") for i in range(1, 6)}
    rows = [dict(round=i, fixture=i, kickoff_time=fx[i]["kickoff_time"], minutes=90, starts=1)
            for i in (1, 2, 3, 5)]                          # match 4 missing
    now = F.parse_utc("2026-10-30T00:00:00Z")
    rc = F.recent(dict(history=rows), 6, now=now, team=1, fixtures_by_id=fx)
    assert rc["rounds"] == [3, 4, 5] and rc["minutes"] == 180 and rc["starts"] == 2


def test_lineup_swaps_are_ordered_to_stay_legal(gw10):
    by = gw10.by_id
    g = lambda n: pid(gw10, n)
    # 3-4-3 -> 5-4-1: swapping a forward out first keeps every step legal
    app = [g("Dubravka"), g("Muharemović"), g("Hall"), g("Calafiori"), g("Ødegaard"),
           g("Cole Palmer"), g("Gibbs-White"), g("Rogers"), g("N.Jackson"), g("Haaland"), g("Barry"),
           g("Bentley"), g("Konsa"), g("De Cuyper"), g("Groß")]
    xi = [x for x in app[:11] if x not in (g("N.Jackson"), g("Barry"))] + [g("Konsa"), g("De Cuyper")]
    bench = [g("Bentley"), g("N.Jackson"), g("Barry"), g("Groß")]
    acts = F.lineup_actions(app, xi, bench, by)
    cur = list(app[:11])
    for kind, a, b in acts:
        if kind == "swap":
            cur = [a if p == b else p for p in cur]
            assert R.xi_violations(cur, by) == []


def test_transfers_ordered_by_gameweek_first(gw10):
    r = pid(gw10, "Rogers")
    tr = [dict(event=8, element_in=r, element_in_cost=81, element_out=1, element_out_cost=50, time=None),
          dict(event=2, element_in=r, element_in_cost=70, element_out=1, element_out_cost=50,
               time="2026-08-27T10:00:00Z")]
    assert F.purchase_prices([r], tr, gw10.by_id)[r][0] == 81


def test_purchase_fallback_needs_the_opening_squad(gw10):
    squad = [p["element"] for p in gw10.picks["picks"]]
    with pytest.raises(ValueError):
        F.purchase_prices(squad, [], gw10.by_id, opening=[])


def test_start_share_follows_this_seasons_games():
    """A fit backup with 0 starts in 9 GWs must not inherit last season's
    start share (Dubravka 84 xMins over Spurs' starter, 2026-11-03)."""
    import baseline
    base = {"games": 38, "players": {"1": dict(minutes=3420, starts=38, expected_goals=0,
                                                expected_assists=0, bonus=0, saves=100,
                                                clearances_blocks_interceptions=0, tackles=0,
                                                recoveries=0)}}
    backup = dict(code=1, minutes=0, starts=0)
    starter = dict(code=1, minutes=810, starts=9)
    sb = baseline.blended_stats(backup, base, 9, 900)
    ss = baseline.blended_stats(starter, base, 9, 900)
    assert sb["start_share"] <= 0.25 + 1e-9
    assert ss["start_share"] >= 0.99
