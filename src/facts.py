"""FPL Edge — derived facts, computed by code (never by an LLM from memory).

Pure functions over raw API / Fix data. Everything is keyed by FPL element
id; names are for display only. These are the facts the validator
(guards.py) checks every recommendation against.
"""
from __future__ import annotations
import datetime as dt
from collections import Counter
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import fpl_rules as R

UK = ZoneInfo("Europe/London")
ET = ZoneInfo("America/New_York")


def index(bootstrap: dict) -> dict[int, dict]:
    return {e["id"]: e for e in bootstrap["elements"]}


def club_short(bootstrap: dict) -> dict[int, str]:
    return {t["id"]: t["short_name"] for t in bootstrap["teams"]}


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------
def purchase_prices(squad_ids: Iterable[int], transfers: list[dict],
                    by_id: Mapping[int, dict], chips: list[dict] | None = None,
                    pending_moves: list[dict] | None = None,
                    opening: Iterable[int] | None = None) -> dict[int, tuple[int, str]]:
    """{element id: (purchase price in tenths, source)} for the current squad.

    Source of truth is the API's transfer record: the most recent transfer
    IN of that player (Free Hit GWs excluded — the squad reverts). Players in
    the entry's opening squad have never been transferred in; they cost the
    season start price (prices are frozen until the first deadline).
    Pending (pre-deadline, user-reported) buys use the reported cost.
    With `opening` (the entry's first-GW squad), a player with no transfer
    record who was NOT in that squad raises: a record is missing.
    """
    fh = {c["event"] for c in (chips or []) if c["name"] == "freehit"}
    latest: dict[int, tuple[str, int]] = {}
    for t in transfers:
        if t["event"] in fh:
            continue
        key = (t["event"], t.get("time") or "")
        pid = t["element_in"]
        if pid not in latest or key > latest[pid][0]:
            latest[pid] = (key, t["element_in_cost"])
    pend = {m["in"]: m for m in (pending_moves or [])}
    out = {}
    for pid in squad_ids:
        if pid in pend:
            m = pend[pid]
            out[pid] = (int(m.get("in_cost") or by_id[pid]["now_cost"]), "pending move")
        elif pid in latest:
            out[pid] = (int(latest[pid][1]), "transfer record")
        else:
            if opening is not None and pid not in set(opening):
                raise ValueError(f"no transfer record for {by_id[pid]['web_name']} ({pid}) "
                                 "and he was not in the opening squad")
            e = by_id[pid]
            out[pid] = (int(e["now_cost"] - e["cost_change_start"]), "season start price")
    return out


def squad_replay(opening: Iterable[int], transfers: list[dict],
                 chips: list[dict] | None = None) -> set[int]:
    """The squad implied by the opening squad plus every transfer (Free Hit
    weeks excluded: the squad reverts). Must equal the API's current squad."""
    fh = {c["event"] for c in (chips or []) if c["name"] == "freehit"}
    squad = set(opening)
    for t in sorted(transfers, key=lambda t: (t["event"], t.get("time") or "")):
        if t["event"] in fh:
            continue
        squad.discard(t["element_out"])
        squad.add(t["element_in"])
    return squad


def display_names(bootstrap: dict) -> dict[int, str]:
    """web_name, plus the club when two players share it ("Palmer (CHE)")."""
    from collections import Counter as _C
    short = club_short(bootstrap)
    n = _C(e["web_name"] for e in bootstrap["elements"])
    return {e["id"]: (e["web_name"] if n[e["web_name"]] == 1
                      else f"{e['web_name']} ({short[e['team']]})") for e in bootstrap["elements"]}


def selling_prices(purchase: Mapping[int, tuple[int, str]],
                   by_id: Mapping[int, dict]) -> dict[int, int]:
    return {pid: R.sell_price(by_id[pid]["now_cost"], p[0]) for pid, p in purchase.items()}


# --------------------------------------------------------------------------
# app lineup slots (signings inherit the sold player's slot)
# --------------------------------------------------------------------------
def predicted_app_slots(slots: list[int], moves: list[tuple[int, int]]) -> list[int]:
    """The 15 ids in app slot order (1-11 XI, 12-15 bench) after the moves.

    The FPL app puts an incoming player into the slot of the player he
    replaced (GW8 lesson: Wissa's slot 11 went to Barry, Egan's bench slot
    15 went to Muharemović). The user must then re-arrange the lineup."""
    out = list(slots)
    for o, i in moves:
        if o not in out:
            raise ValueError(f"cannot sell {o}: not in squad")
        out[out.index(o)] = i
    return out


def lineup_actions(app_slots: list[int], xi: list[int], bench: list[int],
                   by_id: Mapping[int, dict]) -> list[tuple[str, int, int]]:
    """The lineup edits needed to turn the app's predicted lineup into the
    planned one: [("swap", bench_player, starter), ...] then, if the bench
    order still differs, ("bench_order", 0, 0). Same-position swaps are
    paired first so every intermediate formation stays legal."""
    want_xi = set(xi)
    up = [p for p in app_slots[11:] if p in want_xi]          # bench -> XI
    down = [p for p in app_slots[:11] if p not in want_xi]    # XI -> bench
    pairs = []
    for a in list(up):
        same = next((b for b in down if by_id[b]["element_type"] == by_id[a]["element_type"]), None)
        if same is not None:
            pairs.append((a, same)); up.remove(a); down.remove(same)
    pairs += list(zip(up, down))
    # the app refuses a swap that leaves an illegal formation, so find an
    # order in which every intermediate XI is legal
    import itertools
    for order in itertools.permutations(pairs):
        cur, ok = list(app_slots[:11]), True
        for a, b in order:
            cur = [a if p == b else p for p in cur]
            if R.xi_violations(cur, by_id):
                ok = False
                break
        if ok:
            pairs = list(order)
            break
    acts: list[tuple[str, int, int]] = [("swap", a, b) for a, b in pairs]
    after_bench = list(app_slots[11:])
    for a, b in pairs:
        after_bench[after_bench.index(a)] = b
    if after_bench != list(bench):
        acts.append(("bench_order", 0, 0))
    return acts


# --------------------------------------------------------------------------
# elite boards
# --------------------------------------------------------------------------
def board_violations(team: dict, by_id: Mapping[int, dict]) -> list[str]:
    """A parsed elite board must be a legal squad with a legal XI, or the
    XI/bench split (first 11 / last 4) cannot be trusted."""
    ids = team.get("player_ids") or []
    out = []
    if len(ids) != R.SQUAD_SIZE or any(i is None for i in ids):
        out.append(f"{sum(i is not None for i in ids)} of 15 players resolved")
        return out
    out += R.squad_violations(ids, by_id)
    out += [f"XI: {v}" for v in R.xi_violations(ids[:11], by_id)]
    if ids and by_id.get(ids[11], {}).get("element_type") != R.GK:
        out.append("bench slot 1 is not a goalkeeper")
    if team.get("captain_id") not in ids[:11]:
        out.append("captain not in XI")
    if team.get("vice_id") is not None and team.get("vice_id") not in ids[:11]:
        out.append("vice-captain not in XI")
    return out


def elite_counts(elite: dict) -> dict:
    """Per-element counts over the elite boards: own / start / bench /
    captain / vice, plus the number of boards. Board order is XI (11) then
    bench (4)."""
    teams = elite.get("teams") or []
    own, start, bench, cap, vice = Counter(), Counter(), Counter(), Counter(), Counter()
    for t in teams:
        ids = [i for i in (t.get("player_ids") or []) if i is not None]
        for i in ids:
            own[i] += 1
        for i in (t.get("player_ids") or [])[:11]:
            if i is not None:
                start[i] += 1
        for i in (t.get("player_ids") or [])[11:]:
            if i is not None:
                bench[i] += 1
        if t.get("captain_id"):
            cap[t["captain_id"]] += 1
        if t.get("vice_id"):
            vice[t["vice_id"]] += 1
    return dict(n=len(teams), own=own, start=start, bench=bench, cap=cap, vice=vice)


# --------------------------------------------------------------------------
# minutes, starts, fixtures, fitness
# --------------------------------------------------------------------------
def _club_rows(summary: dict, team: int | None, fixtures_by_id: Mapping[int, dict] | None):
    """History rows for the player's CURRENT club (a mid-season mover's old
    club's matches say nothing about who starts for the new one)."""
    rows = summary.get("history", [])
    if team is None or not fixtures_by_id:
        return list(rows)
    out = []
    for r in rows:
        f = fixtures_by_id.get(r.get("fixture"))
        if f is None:
            out.append(r)
        elif "was_home" in r:
            if (f["team_h"] if r["was_home"] else f["team_a"]) == team:
                out.append(r)
        elif team in (f["team_h"], f["team_a"]):
            out.append(r)
    return out


def recent(summary: dict, before_gw: int, n: int = 3, now: dt.datetime | None = None,
           team: int | None = None, fixtures_by_id: Mapping[int, dict] | None = None) -> dict:
    """Minutes and starts over the player's last n club matches. With `now`,
    every match kicked off before now counts (a rescheduled later-round game
    included); without it, rounds before `before_gw`. A double GW counts as
    two matches."""
    rows = _club_rows(summary, team, fixtures_by_id)
    if now is not None:
        rows = [r for r in rows if r.get("kickoff_time") and parse_utc(r["kickoff_time"]) < now]
    else:
        rows = [r for r in rows if r["round"] < before_gw]
    rows.sort(key=lambda r: (r.get("kickoff_time") or "", r["round"]))
    last = rows[-n:]
    if now is not None and team is not None and fixtures_by_id:
        # the club's actual last n matches: a match missing from the player's
        # rows counts as 0 minutes instead of letting an older start slide in
        over = now - dt.timedelta(hours=3)
        club = sorted((f for f in fixtures_by_id.values()
                       if f.get("event") and f.get("kickoff_time") and team in (f["team_h"], f["team_a"])
                       and parse_utc(f["kickoff_time"]) < over),
                      key=lambda f: f["kickoff_time"])[-n:]
        oldest_row = rows[0]["kickoff_time"] if rows else None
        if len(club) == n and oldest_row and club[0]["kickoff_time"] >= oldest_row:
            by_fix = {r.get("fixture"): r for r in rows}
            last = [by_fix.get(f["id"], dict(round=f["event"], fixture=f["id"], minutes=0, starts=0,
                                              kickoff_time=f["kickoff_time"])) for f in club]
    return dict(matches=len(last),
                minutes=sum(int(r.get("minutes") or 0) for r in last),
                starts=sum(int(r.get("starts") or 0) for r in last),
                last_started=bool(last and last[-1].get("starts")),
                last_kickoff=last[-1].get("kickoff_time") if last else None,
                rounds=[r["round"] for r in last])


def history_is_current(summary: dict, team: int, fixtures: list[dict], now: dt.datetime) -> bool:
    """False when the club has a match that kicked off more than 3 hours ago
    (so it is over) that is newer than the player's latest history row: a
    stale element-summary would hide a keeper change or a dropped player.
    (The API's `finished` flag lags by up to a day, so kickoff time is used.)"""
    over = now - dt.timedelta(hours=3)
    played = [parse_utc(f["kickoff_time"]) for f in fixtures
              if f.get("event") and f.get("kickoff_time") and team in (f["team_h"], f["team_a"])
              and parse_utc(f["kickoff_time"]) < over]
    if not played:
        return True
    rows = [parse_utc(r["kickoff_time"]) for r in summary.get("history", []) if r.get("kickoff_time")]
    if not rows:
        return True            # no history at all: the minutes rules fail him anyway
    return max(rows) >= max(played)


def fitness(e: dict) -> tuple[str, int | None]:
    """('fit' | 'doubt' | 'out', chance of playing next round)."""
    ch = e.get("chance_of_playing_next_round")
    st = e.get("status")
    if st == "a" and ch in (None, 100):
        return "fit", ch
    if st in ("i", "s", "u", "n") or ch == 0:
        return "out", ch
    return "doubt", ch


def fixture_count(club: int, gw: int, fixtures: list[dict]) -> int:
    return sum(1 for f in fixtures if f.get("event") == gw and club in (f["team_h"], f["team_a"]))


def club_starter_gk(club: int, by_id: Mapping[int, dict], summaries: Mapping[int, dict],
                    before_gw: int, now: dt.datetime | None = None,
                    fixtures_by_id: Mapping[int, dict] | None = None,
                    elite_starts: Mapping[int, int] | None = None) -> dict:
    """Who keeps goal for this club: the fit GK who started the club's most
    recent match. Returns {starter, detail}; starter is None when unclear
    (no history, or the last starter is now unfit)."""
    gks = [e for e in by_id.values() if e["team"] == club and e["element_type"] == R.GK]
    best, detail = None, {}
    latest_ko = ""
    for e in gks:
        s = summaries.get(e["id"])
        if s is None:
            continue
        rc = recent(s, before_gw, now=now, team=club, fixtures_by_id=fixtures_by_id)
        detail[e["id"]] = rc
        if rc["last_started"] and (rc["last_kickoff"] or "") >= latest_ko:
            latest_ko, best = rc["last_kickoff"] or "", e["id"]
    if best is not None and fitness(by_id[best])[0] != "fit":
        # the No.1 is injured: his stand-in is the fit keeper the elites start
        es = elite_starts or {}
        fit = [e["id"] for e in gks if fitness(e)[0] == "fit" and e["id"] != best]
        top = max((es.get(p, 0) for p in fit), default=0)
        picks = [p for p in fit if es.get(p, 0) == top]
        if top >= 1 and len(picks) == 1:
            return dict(starter=picks[0], detail=detail,
                        note=f"No.1 unfit; elites start {by_id[picks[0]]['web_name']}")
        return dict(starter=None, detail=detail, note="last starter is not fit")
    return dict(starter=best, detail=detail, note="")


# --------------------------------------------------------------------------
# prices and time
# --------------------------------------------------------------------------
def price_direction_tonight(e: dict) -> int:
    """+1 / -1 / 0: the game's own projection for the next price window."""
    for p in e.get("price_change_projections") or []:
        if p.get("offset") == 0:
            try:
                pct = float(p.get("projected_percent"))
            except (TypeError, ValueError):
                return 0
            if pct >= 100:
                return 1
            if pct <= -100:
                return -1
    return 0


def next_price_window(now_utc: dt.datetime) -> dt.datetime:
    """Next 00:00 UK time (when FPL prices change), as an aware UTC time."""
    uk = now_utc.astimezone(UK)
    midnight = dt.datetime.combine(uk.date() + dt.timedelta(days=1), dt.time(0), tzinfo=UK)
    return midnight.astimezone(dt.timezone.utc)


def last_price_window(now_utc: dt.datetime) -> dt.datetime:
    """Most recent 00:00 UK at or before now (prices may have changed then)."""
    uk = now_utc.astimezone(UK)
    midnight = dt.datetime.combine(uk.date(), dt.time(0), tzinfo=UK)
    return midnight.astimezone(dt.timezone.utc)


def fmt_et(t: dt.datetime) -> str:
    """'Thu Nov 5, 7:00 PM ET' — every time shown to the user is ET."""
    e = t.astimezone(ET)
    return e.strftime("%a %b ") + str(e.day) + ", " + \
        e.strftime("%I:%M %p").lstrip("0") + " ET"


def parse_utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def parse_fix_time(s: str | None) -> dt.datetime | None:
    """Fix's Django-style "Oct. 31, 2026, 11:59 a.m." / "noon" / "9 p.m."
    (UK local time) -> aware UTC; None when unparseable."""
    import re
    if not s:
        return None
    m = re.match(r"\s*([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4}),\s*(.+?)\s*$", s)
    if not m or m.group(1)[:3].lower() not in _MONTHS:
        return None
    t = m.group(4).lower().replace(".", "")
    if t == "noon":
        hh, mm = 12, 0
    elif t == "midnight":
        hh, mm = 0, 0
    else:
        tm = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", t)
        if not tm:
            return None
        hh, mm = int(tm.group(1)) % 12, int(tm.group(2) or 0)
        if tm.group(3) == "pm":
            hh += 12
    local = dt.datetime(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)),
                        hh, mm, tzinfo=UK)
    return local.astimezone(dt.timezone.utc)
