"""Squad state: pending moves by id, app overrides, strict names, no writes."""
import pytest

import squad_state as S
from conftest import pid


def test_gw10_state_from_api(gw10, tmp_path):
    st = S.build_state(gw10)
    assert st.gw == 10 and st.api_gw == 9
    assert st.ft == 1 and st.bank == 19
    assert sum(st.sell.values()) == 993
    assert list(tmp_path.iterdir()) == []            # pure: nothing written


def test_pending_moves_apply_by_id(gw10):
    d, k = pid(gw10, "Dubravka"), pid(gw10, "Kinsky")
    gw10.pending = dict(gw=10, team_id=3314291, moves=[{"out": d, "in": k, "in_cost": 44}])
    st = S.build_state(gw10)
    assert st.slots[0] == k and d not in st.slots      # Kinsky inherits slot 1
    assert st.bank == 19 + 40 - 44
    assert st.ft == 0
    assert st.sold_this_gw == {d} and st.bought_this_gw == {k}
    assert st.purchase[k] == (44, "pending move")


def test_app_values_override_with_warning(gw10):
    d, k = pid(gw10, "Dubravka"), pid(gw10, "Kinsky")
    gw10.pending = dict(gw=10, moves=[{"out": d, "in": k}], app_ft_left=1, app_bank_tenths=16)
    st = S.build_state(gw10)
    assert (st.ft, st.bank, st.ft_source) == (1, 16, "FPL app (reported)")
    assert len(st.warnings) == 2


def test_expired_pending_is_ignored(gw10):
    gw10.pending = dict(gw=9, moves=[{"out": pid(gw10, "Dubravka"), "in": pid(gw10, "Kinsky")}])
    st = S.build_state(gw10)
    assert st.pending_moves == [] and any("ignored" in w for w in st.warnings)


def test_future_or_foreign_pending_raises(gw10):
    gw10.pending = dict(gw=11, moves=[])
    with pytest.raises(ValueError):
        S.build_state(gw10)
    gw10.pending = dict(gw=10, team_id=1, moves=[])
    with pytest.raises(ValueError):
        S.build_state(gw10)


@pytest.mark.parametrize("name", ["Palmer", "Thomas", "King"])
def test_ambiguous_names_raise(gw10, name):
    with pytest.raises(S.AmbiguousName):
        S.resolve(name, gw10.by_id)


def test_full_name_resolves(gw10):
    assert S.resolve("Cole Palmer", gw10.by_id) == 154
    assert S.resolve("154", gw10.by_id) == 154
    with pytest.raises(ValueError):
        S.resolve(999999, gw10.by_id)


def test_chained_pending_moves(gw10):
    """A -> B then B -> C before the deadline used to crash on B's price."""
    d, k, v = (pid(gw10, n) for n in ("Dubravka", "Kinsky", "Verbruggen"))
    gw10.pending = dict(gw=10, moves=[{"out": d, "in": k, "in_cost": 44},
                                      {"out": k, "in": v}])
    st = S.build_state(gw10)
    assert st.slots[0] == v and k not in st.slots
    assert st.bank == 19 + 40 - 44 + 44 - gw10.by_id[v]["now_cost"]
    assert st.ft == 0
