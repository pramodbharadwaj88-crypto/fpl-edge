"""FPL Edge — the FPL game rules, in ONE place.

Every module that needs a rule (selling price, squad/XI legality, club cap,
auto-subs, hits, free-transfer accrual) imports it from here. The constants are
checked against the live game's own settings by `rules_drift()`, so a rule
change by the game shows up as a hard validation failure instead of a silent
wrong answer.

Pure functions only: no network, no file I/O.
"""
from __future__ import annotations
from collections import Counter
from typing import Iterable, Mapping

GK, DEF, MID, FWD = 1, 2, 3, 4
POS_NAME = {GK: "GKP", DEF: "DEF", MID: "MID", FWD: "FWD"}

SQUAD_SIZE = 15
XI_SIZE = 11
POS_SQUAD = {GK: 2, DEF: 5, MID: 5, FWD: 3}
POS_MIN_XI = {GK: 1, DEF: 3, MID: 2, FWD: 1}
POS_MAX_XI = {GK: 1, DEF: 5, MID: 5, FWD: 3}
MAX_PER_CLUB = 3
BUDGET = 1000                 # tenths of £m
SELL_ON_FEE = 0.5             # half of any profit is kept
HIT_COST = 4                  # points per transfer beyond the free ones
FT_CAP = 5                    # 1 + max_extra_free_transfers (4)
FT_FREE_CHIPS = frozenset({"wildcard", "freehit"})


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------
def sell_price(now_tenths: int, purchase_tenths: int) -> int:
    """Selling price in tenths: losses pass through in full, profit is
    banked at 50% rounded down to the nearest £0.1m."""
    if now_tenths <= purchase_tenths:
        return int(now_tenths)
    return int(purchase_tenths + (now_tenths - purchase_tenths) // 2)


def hits_for(n_transfers: int, free_transfers: int) -> int:
    """Points deducted for n transfers given the free transfers available."""
    return HIT_COST * max(0, int(n_transfers) - int(free_transfers))


# --------------------------------------------------------------------------
# free transfers
# --------------------------------------------------------------------------
def ft_replay(history_rows: list[dict], transfers: list[dict],
              chips: list[dict]) -> tuple[int, int]:
    """(free transfers available, the GW they apply to) from public API data.

    * The entry's first GW squad is free and earns 1 FT for the next GW.
    * Each completed GW adds +1, capped at FT_CAP; transfers consume FTs
      (extras are hits; the bank floors at 0).
    * Wildcard / Free Hit GWs consume nothing and add nothing: banked FTs
      carry over unchanged (verified against the app, GW9 2026-10-30).
    """
    rows = sorted(history_rows, key=lambda r: r["event"])
    if not rows:
        return 1, 1
    made: Counter = Counter(t["event"] for t in transfers)
    chip_by_gw = {c["event"]: c["name"] for c in chips}
    ft = 1
    for r in rows[1:]:
        gw = r["event"]
        if chip_by_gw.get(gw) in FT_FREE_CHIPS:
            continue
        n = max(made.get(gw, 0), r.get("event_transfers") or 0)
        ft = min(FT_CAP, max(0, ft - n) + 1)
    return ft, rows[-1]["event"] + 1


# --------------------------------------------------------------------------
# legality
# --------------------------------------------------------------------------
def squad_violations(ids: Iterable[int], by_id: Mapping[int, dict]) -> list[str]:
    """Reasons a 15-man squad is illegal (empty list = legal)."""
    ids = list(ids)
    out = []
    if len(ids) != SQUAD_SIZE:
        out.append(f"squad has {len(ids)} players, needs {SQUAD_SIZE}")
    if len(set(ids)) != len(ids):
        out.append("duplicate player in squad")
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        out.append(f"unknown element ids {unknown}")
        return out
    pos = Counter(by_id[i]["element_type"] for i in ids)
    for et, need in POS_SQUAD.items():
        if pos.get(et, 0) != need:
            out.append(f"{POS_NAME[et]} count {pos.get(et, 0)}, needs {need}")
    clubs = Counter(by_id[i]["team"] for i in ids)
    for club, n in clubs.items():
        if n > MAX_PER_CLUB:
            out.append(f"{n} players from club {club} (max {MAX_PER_CLUB})")
    return out


def xi_violations(xi: Iterable[int], by_id: Mapping[int, dict]) -> list[str]:
    """Reasons a starting XI formation is illegal (empty list = legal)."""
    xi = list(xi)
    out = []
    if len(xi) != XI_SIZE:
        out.append(f"XI has {len(xi)} players, needs {XI_SIZE}")
    if len(set(xi)) != len(xi):
        out.append("duplicate player in XI")
    if any(i not in by_id for i in xi):
        out.append("unknown element id in XI")
        return out
    pos = Counter(by_id[i]["element_type"] for i in xi)
    for et in POS_SQUAD:
        n = pos.get(et, 0)
        if n < POS_MIN_XI[et] or n > POS_MAX_XI[et]:
            out.append(f"{POS_NAME[et]} in XI {n}, allowed "
                       f"{POS_MIN_XI[et]}-{POS_MAX_XI[et]}")
    return out


def formation(xi: Iterable[int], by_id: Mapping[int, dict]) -> str:
    pos = Counter(by_id[i]["element_type"] for i in xi)
    return f"{pos.get(DEF, 0)}-{pos.get(MID, 0)}-{pos.get(FWD, 0)}"


# --------------------------------------------------------------------------
# auto-subs
# --------------------------------------------------------------------------
def autosub(xi: list[int], bench: list[int], played: set[int],
            by_id: Mapping[int, dict]) -> tuple[list[int], list[tuple[int, int]]]:
    """Simulate FPL automatic substitutions.

    bench is in app order (bench[0] is the substitute GK). A non-playing
    starter is replaced by the first bench player (in order) who played and
    keeps the formation legal; the GK can only be replaced by the bench GK.
    Returns (final XI, [(out, in), ...])."""
    final = list(xi)
    used: set[int] = set()
    swaps = []
    for starter in xi:
        if starter in played:
            continue
        is_gk = by_id[starter]["element_type"] == GK
        for sub in bench:
            if sub in used or sub not in played:
                continue
            if (by_id[sub]["element_type"] == GK) != is_gk:
                continue
            trial = [sub if p == starter else p for p in final]
            if xi_violations(trial, by_id):
                continue
            final = trial
            used.add(sub)
            swaps.append((starter, sub))
            break
    return final, swaps


# --------------------------------------------------------------------------
# drift against the live game
# --------------------------------------------------------------------------
def rules_drift(bootstrap: dict) -> list[str]:
    """Differences between these constants and the game's own settings."""
    gs = bootstrap.get("game_settings") or {}
    out = []

    def cmp(label, live, ours):
        if live is not None and live != ours:
            out.append(f"{label}: game says {live}, code assumes {ours}")

    cmp("squad_squadsize", gs.get("squad_squadsize"), SQUAD_SIZE)
    cmp("squad_squadplay", gs.get("squad_squadplay"), XI_SIZE)
    cmp("squad_team_limit", gs.get("squad_team_limit"), MAX_PER_CLUB)
    cmp("squad_total_spend", gs.get("squad_total_spend"), BUDGET)
    cmp("transfers_sell_on_fee", gs.get("transfers_sell_on_fee"), SELL_ON_FEE)
    if gs.get("max_extra_free_transfers") is not None:
        cmp("max free transfers", gs["max_extra_free_transfers"] + 1, FT_CAP)
    if gs.get("element_sell_at_purchase_price"):
        out.append("element_sell_at_purchase_price is ON (code assumes 50% sell-on)")
    for et in bootstrap.get("element_types") or []:
        i = et["id"]
        if i not in POS_SQUAD:
            out.append(f"unknown element type {i}")
            continue
        cmp(f"{POS_NAME[i]} squad_select", et.get("squad_select"), POS_SQUAD[i])
        cmp(f"{POS_NAME[i]} squad_min_play", et.get("squad_min_play"), POS_MIN_XI[i])
        cmp(f"{POS_NAME[i]} squad_max_play", et.get("squad_max_play"), POS_MAX_XI[i])
    return out
