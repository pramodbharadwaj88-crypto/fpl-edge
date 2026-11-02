"""FPL Edge — canonical squad state.

Single authority for "what does Pramod own going into the next deadline".
`build_state(bundle)` is a pure function over the API snapshot plus the
user-reported pending moves; nothing is written to disk.

API reality (2026-08-31): the FPL API hides transfers made for the upcoming
GW until its deadline passes, so pre-deadline moves arrive via the pending
file, keyed by element id:

  data/pending_moves.json
  {"gw": 10, "team_id": 3314291,
   "moves": [{"out": 497, "in": 496, "in_cost": 44}],
   "app_ft_left": 0, "app_bank_tenths": 15,          # as the FPL app shows
   "approved_hits": 0,                               # -4s Pramod said yes to
   "reported_at": "2026-11-05T14:00:00Z"}

The app is the truth for free transfers and bank: when the user reports
them they override the computed values (a disagreement is a warning).
A pending file for an earlier GW is ignored (the API has caught up).
"""
from __future__ import annotations
import dataclasses as dc

import facts
import fpl_rules as R
from bundle import Bundle


class AmbiguousName(ValueError):
    pass


def resolve(ref, by_id: dict[int, dict]) -> int:
    """Element id from an id or a name. Names must match exactly one player
    (web_name, then "first second"); anything else raises."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        pid = int(ref)
        if pid not in by_id:
            raise ValueError(f"unknown element id {pid}")
        return pid
    name = str(ref).strip().lower()
    hits = [e for e in by_id.values() if e["web_name"].lower() == name]
    if not hits:
        hits = [e for e in by_id.values()
                if f"{e['first_name']} {e['second_name']}".lower() == name]
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        raise ValueError(f"no player named {ref!r}")
    raise AmbiguousName(f"{ref!r} matches {len(hits)} players: " + ", ".join(
        f"{e['id']} ({e['first_name']} {e['second_name']}, team {e['team']})" for e in hits))


@dc.dataclass
class SquadState:
    gw: int                              # the GW this advice is for
    api_gw: int                          # GW of the API picks it starts from
    api_slots: list[int]                 # 15 ids in app order, per the API
    slots: list[int]                     # after pending moves (slot inheritance)
    captain: int | None
    vice: int | None
    bank: int                            # tenths, after pending moves
    ft: int                              # free transfers left for `gw`
    ft_computed: int                     # from the API replay, before overrides
    ft_source: str
    ft_gw: int                           # the GW the FT replay applies to
    hist_gw: int                         # latest GW in the entry history
    approved_hits: int                   # points of hits Pramod approved (pending file)
    pending_hits: int                    # points already spent by the pending moves
    pending_priced: bool                 # every pending buy has a cost, or the app bank is known
    purchase: dict[int, tuple[int, str]]
    sell: dict[int, int]
    pending_moves: list[tuple[int, int]]
    chip_used_api_gw: str | None
    warnings: list[str]

    @property
    def sold_this_gw(self) -> set[int]:
        return {o for o, _ in self.pending_moves}

    @property
    def bought_this_gw(self) -> set[int]:
        return {i for _, i in self.pending_moves}


def _validate_pending(pend: dict) -> None:
    """Reject malformed user notes before they can shape a plan."""
    def intlike(v):
        return isinstance(v, int) and not isinstance(v, bool)
    if pend.get("app_ft_left") is not None and not (intlike(pend["app_ft_left"])
                                                   and 0 <= pend["app_ft_left"] <= R.FT_CAP):
        raise ValueError(f"app_ft_left must be 0-{R.FT_CAP}, got {pend['app_ft_left']!r}")
    if pend.get("app_bank_tenths") is not None and not (intlike(pend["app_bank_tenths"])
                                                       and 0 <= pend["app_bank_tenths"] <= 1000):
        raise ValueError(f"app_bank_tenths must be 0-1000 tenths, got {pend['app_bank_tenths']!r}")
    ah = pend.get("approved_hits")
    if ah is not None and not (intlike(ah) and ah >= 0 and ah % R.HIT_COST == 0):
        raise ValueError(f"approved_hits must be a multiple of {R.HIT_COST}, got {ah!r}")
    for m in pend.get("moves", []):
        c = m.get("in_cost")
        if c is not None and not (intlike(c) and 35 <= c <= 160):
            raise ValueError(f"in_cost must be in tenths (35-160), got {c!r}")


def build_state(b: Bundle) -> SquadState:
    by = b.by_id
    gw = b.next_event()["id"]
    if b.picks.get("active_chip") == "freehit":
        raise ValueError(f"GW{b.picks_gw} was a Free Hit: load GW{b.picks_gw - 1} picks")
    picks = sorted(b.picks["picks"], key=lambda p: p["position"])
    api_slots = [p["element"] for p in picks]
    cap = next((p["element"] for p in picks if p["is_captain"]), None)
    vice = next((p["element"] for p in picks if p["is_vice_captain"]), None)
    bank = int(b.picks["entry_history"]["bank"])
    warnings: list[str] = []

    rows = b.entry_history.get("current", [])
    hist_gw = max((r["event"] for r in rows), default=0)
    ft, ft_gw = R.ft_replay(rows, b.transfers, b.entry_history.get("chips", []))
    ft_computed = ft
    ft_source = "API replay"

    purchase = facts.purchase_prices(api_slots, b.transfers, by, b.entry_history.get("chips"),
                                     opening=b.opening)

    moves: list[tuple[int, int]] = []
    approved_hits = pending_hits = 0
    pending_priced = True
    pend = b.pending
    if pend:
        pgw = int(pend.get("gw", 0))
        if pend.get("team_id") not in (None, b.team_id):
            raise ValueError("pending moves belong to another team")
        if pgw > gw:
            raise ValueError(f"pending moves are for GW{pgw}, next deadline is GW{gw}")
        if pgw < gw:
            warnings.append(f"pending moves for GW{pgw} ignored (that deadline has passed)")
            pend = None
    slots = list(api_slots)
    if pend:
        _validate_pending(pend)
        approved_hits = int(pend.get("approved_hits") or 0)
        pending_hits = R.hits_for(len(pend.get("moves", [])), ft)
        pending_priced = (pend.get("app_bank_tenths") is not None
                          or all(m.get("in_cost") for m in pend.get("moves", [])))
        book = dict(purchase)                      # running book: chains A->B->C work
        for m in pend.get("moves", []):
            o, i = resolve(m["out"], by), resolve(m["in"], by)
            if o not in book:
                raise ValueError(f"pending move sells {by[o]['web_name']}, who is not in the squad")
            cost = int(m.get("in_cost") or by[i]["now_cost"])
            bank += R.sell_price(by[o]["now_cost"], book.pop(o)[0]) - cost
            book[i] = (cost, "pending move")
            slots = facts.predicted_app_slots(slots, [(o, i)])
            moves.append((o, i))
            if cap == o:
                cap = None
            if vice == o:
                vice = None
        purchase = book
        ft = max(0, ft - len(moves))
        if pend.get("app_ft_left") is not None:
            if int(pend["app_ft_left"]) != ft:
                warnings.append(f"app shows {pend['app_ft_left']} FT left, "
                                f"replay says {ft}; using the app")
            ft, ft_source = int(pend["app_ft_left"]), "FPL app (reported)"
        if pend.get("app_bank_tenths") is not None:
            if int(pend["app_bank_tenths"]) != bank:
                warnings.append(f"app shows £{pend['app_bank_tenths'] / 10:.1f}m bank, "
                                f"computed £{bank / 10:.1f}m; using the app")
            bank = int(pend["app_bank_tenths"])
    sell = facts.selling_prices(purchase, by)

    return SquadState(gw=gw, api_gw=b.picks_gw, api_slots=api_slots, slots=slots,
                      captain=cap, vice=vice, bank=bank, ft=ft, ft_computed=ft_computed,
                      ft_source=ft_source, ft_gw=ft_gw, hist_gw=hist_gw,
                      approved_hits=approved_hits, pending_hits=pending_hits,
                      pending_priced=pending_priced, purchase=purchase, sell=sell,
                      pending_moves=moves, chip_used_api_gw=b.picks.get("active_chip"),
                      warnings=warnings)


def free_transfers(team_id: int, hist: dict | None = None,
                   transfers: list | None = None) -> tuple[int, int]:
    """Back-compat wrapper: (free transfers, GW they apply to)."""
    import fpl_api
    hist = hist if hist is not None else fpl_api.entry_history(team_id)
    if transfers is None:
        transfers = fpl_api.entry_transfers(team_id)
    return R.ft_replay(hist.get("current", []), transfers, hist.get("chips", []))


def current(team_id: int) -> dict:
    """Legacy dict view for report.py (live API, no elite fetch)."""
    import bundle
    try:
        b = bundle.live(team_id, with_elite=False)
        st = build_state(b)
    except Exception as ex:
        return dict(source="none", gw=None, element_ids=[], captain=None, chip=None,
                    bank=0.0, free_transfers=1, sell={},
                    unresolved=[f"NO SQUAD STATE: {type(ex).__name__}: {ex}"])
    return dict(source="fpl_api" + ("+pending_user_moves" if st.pending_moves else ""),
                gw=st.api_gw, target_gw=st.gw, element_ids=st.slots,
                captain=st.captain, vice=st.vice, chip=st.chip_used_api_gw,
                bank=st.bank / 10.0, free_transfers=st.ft, ft_gw=st.gw,
                sell=st.sell, unresolved=[], warnings=st.warnings)


if __name__ == "__main__":
    import model_config as mc, bundle
    b = bundle.live(mc.full().get("team_id"), with_elite=False)
    st = build_state(b)
    by = b.by_id
    print(f"GW{st.gw} | bank £{st.bank / 10:.1f}m | FT {st.ft} ({st.ft_source})")
    for k, pid in enumerate(st.slots, 1):
        e = by[pid]
        p, src = st.purchase[pid]
        tag = " (C)" if pid == st.captain else (" (V)" if pid == st.vice else "")
        print(f"{k:2} {e['web_name']:16} now {e['now_cost']/10:.1f} bought {p/10:.1f} "
              f"sell {st.sell[pid]/10:.1f} [{src}]{tag}")
    for w in st.warnings:
        print("WARNING:", w)
