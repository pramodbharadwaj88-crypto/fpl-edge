"""Name matching, Fix board parsing (by FPL code) and Kalshi price fields."""
import pytest

import fix_data
import fpl_api
from odds import match_player
from prediction_markets import _kalshi_leg_prob, _kalshi_price


@pytest.mark.parametrize("name,want", [
    ("Cole Palmer", 154), ("Alex Palmer", 301),       # Palmer collision (2026-10)
    ("Bobby Thomas", 173), ("Sorba Thomas", 642),
    ("Tom King", 228), ("Josh King", 268),
    ("Daniel Muñoz", 201), ("Martin Ødegaard", 15), ("Erling Haaland", 411),
])
def test_odds_name_matching(gw10, name, want):
    assert match_player(name, gw10.bootstrap["elements"]) == want


def _card(players):
    """Minimal Fix flip-card: 15 player links with /player_modal/<code>/."""
    links = "".join(
        f'<a class="open-playermodal" data-name="{n}" href="/player_modal/{code}/">i</a>'
        + ('<a title="Captain">C</a>' if k == 9 else "")
        + ('<a title="Vice-captain">V</a>' if k == 5 else "")
        for k, (n, code) in enumerate(players))
    return f'<div class="flip-card-wrap"><div><h3>Elite {len(players)}</h3></div>{links}</div>'


def test_fix_parse_maps_by_fpl_code_not_name(gw10, monkeypatch):
    """A bare "Muñoz" on a Fix board must resolve through the player_modal
    FPL code to Daniel Muñoz (DEF), never Victor Munoz (MID)."""
    monkeypatch.setattr(fpl_api, "bootstrap", lambda: gw10.bootstrap)
    by = gw10.by_id
    board = next(t for t in gw10.elite["teams"] if 201 in t["player_ids"])
    players = [("Muñoz" if p == 201 else f"{by[p]['first_name']} {by[p]['second_name']}",
                by[p]["code"]) for p in board["player_ids"]]
    html = "<html>Gameweek 10 " + "".join(_card(players) for _ in range(10)) + "</html>"
    data, _ = fix_data.parse(html)
    assert data["gw"] == 10 and data["n_teams"] == 10
    assert data["teams"][0]["player_ids"] == board["player_ids"]
    assert 377 not in data["ownership_ids"]


def test_kalshi_dollar_fields_and_legacy_cents():
    assert _kalshi_price({"yes_bid_dollars": "0.4300"}, "yes_bid") == pytest.approx(0.43)
    assert _kalshi_price({"yes_bid": 43}, "yes_bid") == pytest.approx(0.43)
    assert _kalshi_price({"yes_bid_dollars": "0"}, "yes_bid") is None
    m = {"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44"}
    assert _kalshi_leg_prob(m) == pytest.approx(0.42)
    assert _kalshi_leg_prob({"last_price_dollars": "0.3"}) == pytest.approx(0.3)


def test_secret_scan_catches_credentials(tmp_path):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "secret_scan", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "secret_scan.py")
    ss = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ss)
    bad = tmp_path / "leak.py"
    fake = "ab12cd34" * 4                     # built at runtime: the source stays clean
    bad.write_text('cfg = {"fix_' + 'sessionid": "' + fake + '"}\n'
                   'tok = "ghp_' + "A1" * 18 + '"\n')
    ok = tmp_path / "ok.json"
    ok.write_text('{"code": 12345678901234567890123456789012, "name": "abcdefghijklmnopqrstuvwxyzabcdef"}')
    assert len(ss.scan([bad])) >= 2
    assert ss.scan([ok]) == []
