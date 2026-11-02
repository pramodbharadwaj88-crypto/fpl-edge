"""FPL rules: selling price, hits, free transfers, legality, auto-subs, drift."""
import copy

import pytest

import fpl_rules as R
from conftest import pid


@pytest.mark.parametrize("now,bought,sell", [
    (60, 60, 60),      # no change
    (61, 60, 60),      # +0.1 profit rounds down to 0
    (62, 60, 61),      # +0.2 -> keep 0.1
    (63, 60, 61),      # +0.3 -> keep 0.1 (rounded down)
    (58, 60, 58),      # losses pass through in full
    (158, 155, 156),   # Haaland GW10: bought 15.5, now 15.8 -> 15.6
])
def test_sell_price(now, bought, sell):
    assert R.sell_price(now, bought) == sell


@pytest.mark.parametrize("n,ft,hit", [(0, 1, 0), (1, 1, 0), (2, 1, 4), (3, 1, 8), (3, 2, 4), (1, 0, 4)])
def test_hits(n, ft, hit):
    assert R.hits_for(n, ft) == hit


def test_ft_gw9_is_two_after_wildcard(gw9):
    # GW4 wildcard earns no +1; app showed 2 for GW9, not 3 (2026-10-30)
    ft, gw = R.ft_replay(gw9.entry_history["current"], gw9.transfers, gw9.entry_history["chips"])
    assert (ft, gw) == (2, 9)


def test_ft_gw10_is_one(gw10):
    ft, gw = R.ft_replay(gw10.entry_history["current"], gw10.transfers, gw10.entry_history["chips"])
    assert (ft, gw) == (1, 10)


def test_ft_caps_at_five():
    rows = [dict(event=g, event_transfers=0) for g in range(1, 12)]
    assert R.ft_replay(rows, [], [])[0] == 5


def test_ft_hit_floors_at_zero():
    rows = [dict(event=1, event_transfers=0), dict(event=2, event_transfers=3)]
    assert R.ft_replay(rows, [], [])[0] == 1          # 1 - 3 -> 0, then +1


def test_squad_and_xi_legality(gw10):
    by = gw10.by_id
    squad = [p["element"] for p in sorted(gw10.picks["picks"], key=lambda p: p["position"])]
    assert R.squad_violations(squad, by) == []
    assert R.xi_violations(squad[:11], by) == []
    two_gk_xi = squad[:10] + [squad[11]]                 # bench GK in for a forward
    assert R.xi_violations(two_gk_xi, by)
    fourth_arsenal = [p for p in squad if p != pid(gw10, "Dubravka")] + [pid(gw10, "Raya")]
    assert any("max 3" in v for v in R.squad_violations(fourth_arsenal, by))


def test_autosub_gk_only_for_gk_and_formation_kept(gw10):
    by = gw10.by_id
    squad = [p["element"] for p in sorted(gw10.picks["picks"], key=lambda p: p["position"])]
    xi, bench = squad[:11], squad[11:]
    gk, sub_gk = xi[0], bench[0]
    played = set(squad) - {gk}
    final, swaps = R.autosub(xi, bench, played, by)
    assert swaps == [(gk, sub_gk)]
    # a non-playing GK with a non-playing bench GK stays a zero (no outfield cover)
    final, swaps = R.autosub(xi, bench, set(squad) - {gk, sub_gk}, by)
    assert swaps == [] and gk in final


def test_autosub_respects_minimum_defenders(gw10):
    by = gw10.by_id
    d = [pid(gw10, n) for n in ("Muharemović", "Hall", "Calafiori")]
    m = [pid(gw10, n) for n in ("Ødegaard", "Cole Palmer", "Gibbs-White", "Rogers")]
    f = [pid(gw10, n) for n in ("N.Jackson", "Haaland", "Barry")]
    xi = [pid(gw10, "Dubravka")] + d + m + f                         # 3-4-3
    bench = [pid(gw10, "Bentley"), pid(gw10, "Groß"), pid(gw10, "Konsa"), pid(gw10, "De Cuyper")]
    played = set(xi + bench) - {d[0]}
    final, swaps = R.autosub(xi, bench, played, by)
    assert swaps == [(d[0], pid(gw10, "Konsa"))]       # Groß skipped: 2 DEF is illegal


def test_rules_drift(gw10):
    assert R.rules_drift(gw10.bootstrap) == []
    bs = copy.deepcopy(gw10.bootstrap)
    bs["game_settings"]["squad_team_limit"] = 4
    bs["element_types"][1]["squad_min_play"] = 2
    drift = R.rules_drift(bs)
    assert any("squad_team_limit" in d for d in drift)
    assert any("DEF squad_min_play" in d for d in drift)
