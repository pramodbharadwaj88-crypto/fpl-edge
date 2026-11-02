"""FPL Edge — the validator. No recommendation reaches Pramod without it.

A plan is JSON keyed by element id:

  {"gw": 10, "team_id": 3314291, "phase": "finalize",
   "transfers": [{"out": 497, "in": 496}],
   "xi": [11 ids], "bench": [4 ids, substitute GK first],
   "captain": 411, "vice": 154,
   "chip": null}

Hits (-4s) are allowed only up to "approved_hits" in Pramod's pending-moves
note (data/pending_moves.json) — never from the plan itself.

`check(plan, bundle)` runs every rule below against code-computed facts
(squad_state + facts) and returns a Report. Only a report with zero FAILs
renders a message, and that message ends with the stamp
"Checks passed N/N · plan <hash>". Anything else renders
"VALIDATION FAILED" with the reasons, and nothing may be sent in its place.

CLI (run from src/):
  python3 -m guards check plan.json --live [--pending FILE] [--json]
  python3 -m guards check plan.json --bundle DIR
  python3 -m guards facts --live            # the fact table plans are built from
Exit codes: 0 passed, 2 validation failed, 3 inputs missing/stale, 4 internal error.
"""
from __future__ import annotations
import dataclasses as dc
import datetime as dt
import hashlib, json, re, sys
from collections import Counter

import facts as F
import fpl_rules as R
from bundle import Bundle, MissingInput
from squad_state import SquadState, build_state

# ---- thresholds (all in one place) ---------------------------------------
MAX_AGE = {"bootstrap": 2 * 3600, "fixtures": 6 * 3600, "entry_history": 6 * 3600,
           "transfers": 6 * 3600, "picks": 6 * 3600, "elite": 12 * 3600}
XP_MAX_AGE = 48 * 3600
MIN_ELITE_BOARDS = 10
EXPECTED_ELITE_BOARDS = 12
IN_MIN_MINUTES = 120            # over the club's last 3 matches
IN_MIN_STARTS = 2               # of the club's last 3 matches
IN_MIN_ELITE_STARTS = 3         # elite boards that START the incoming player
MIN_START_GAP = 2               # a gap of exactly 1 needs the odds tie-break
ENGINE_SLACK = 1                # plan may trail the engine by at most 1 elite start
XP_TIE_MARGIN = 0.3             # xP edge that breaks a 1-board tie (odds rule)
MIN_STAMP_CHECKS = 40           # a stamp claiming fewer checks is not ours
MIN_PER_MATCH = 40              # incoming player: 40 min per club match (120 over 3)
DEADLINE_BUFFER = dt.timedelta(minutes=15)

FAIL, WARN = "FAIL", "WARN"


@dc.dataclass
class Result:
    code: str
    ok: bool
    level: str = FAIL
    detail: str = ""


@dc.dataclass
class Report:
    plan: dict
    results: list[Result]
    message: str
    passed: bool
    state: SquadState | None = None

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if not r.ok and r.level == FAIL]

    @property
    def warnings(self) -> list[Result]:
        return [r for r in self.results if not r.ok and r.level == WARN]

    def to_json(self) -> dict:
        return dict(passed=self.passed, message=self.message,
                    results=[dc.asdict(r) for r in self.results])


_PLAN_KEYS = ("gw", "team_id", "transfers", "xi", "bench", "captain", "vice", "chip")


def plan_hash(plan: dict, body: str = "") -> str:
    """Hash of the plan AND the exact message body it rendered (which states
    the bank, free transfers and moves already made), so any edit to the
    text breaks the stamp."""
    canon = json.dumps({k: plan.get(k) for k in _PLAN_KEYS}, sort_keys=True,
                       separators=(",", ":"))
    return hashlib.sha256((canon + "\n" + body.strip()).encode()).hexdigest()[:12]


# ==========================================================================
# the checks
# ==========================================================================
class _Ctx:
    """Facts shared by the checks, computed once."""

    def __init__(self, plan: dict, b: Bundle, st: SquadState):
        self.plan, self.b, self.st = plan, b, st
        self.by = b.by_id
        self.event = b.next_event()
        self.gw = self.event["id"]
        self.deadline = F.parse_utc(self.event["deadline_time"])
        self.moves = [(int(m["out"]), int(m["in"])) for m in plan.get("transfers") or []]
        self.xi = [int(x) for x in plan.get("xi") or []]
        self.bench = [int(x) for x in plan.get("bench") or []]
        self.cap = plan.get("captain")
        self.vice = plan.get("vice")
        self.squad_after = None
        try:
            self.squad_after = F.predicted_app_slots(st.slots, self.moves)
        except ValueError:
            pass
        # elite boards: legal, distinct, not a one-week Free Hit squad
        self.elite_ok, self.elite_bad = [], []
        seen = set()
        e = b.elite or {}
        for t in e.get("teams") or []:
            v = F.board_violations(t, self.by)
            mgr = t.get("manager")
            key = mgr if mgr not in (None, "", "unknown") else (
                mgr, tuple(t.get("player_ids") or []), t.get("captain_id"), t.get("vice_id"))
            if key in seen:
                v = v + ["duplicate board"]
            seen.add(key)
            if "chip" not in t or F.parse_fix_time(t.get("last_updated")) is None:
                v = v + ["no chip / last-updated data (a Free Hit board could slip in)"]
            elif (t.get("chip") or "").upper().startswith("FH"):
                v = v + ["Free Hit board (one-week squad)"]
            (self.elite_bad if v else self.elite_ok).append((t, v))
        self.missing_boards = max(0, EXPECTED_ELITE_BOARDS - len(self.elite_ok))
        self.ec = F.elite_counts(dict(teams=[t for t, _ in self.elite_ok]))
        self.fx_by_id = {f["id"]: f for f in b.fixtures if "id" in f}
        self.stale_history: set[int] = set()
        self.xp_fresh = bool(b.xp) and "xp" in b.fetched_at and (
            (b.now - b.fetched_at["xp"]).total_seconds() <= XP_MAX_AGE)
        self.names = F.display_names(b.bootstrap)

    def xp(self, pid) -> float | None:
        """Model xP for the target GW, only when fresh."""
        return self.b.xp.get(pid) if self.xp_fresh else None

    def name(self, pid) -> str:
        return self.names.get(pid, f"#{pid}")

    def _summary(self, pid) -> dict:
        s = self.b.summary(pid)
        if not F.history_is_current(s, self.by[pid]["team"], self.b.fixtures, self.b.now):
            self.stale_history.add(pid)
        return s

    def recent(self, pid) -> dict:
        return F.recent(self._summary(pid), self.gw, now=self.b.now,
                        team=self.by[pid]["team"], fixtures_by_id=self.fx_by_id)

    def starter_gk(self, club) -> dict:
        sums = {p: self._summary(p) for p in self.b.club_gk_summaries(club)}
        return F.club_starter_gk(club, self.by, sums, self.gw, now=self.b.now,
                                 fixtures_by_id=self.fx_by_id, elite_starts=self.ec["start"])

    def has_fixture(self, pid) -> bool:
        return F.fixture_count(self.by[pid]["team"], self.gw, self.b.fixtures) > 0

    def will_play(self, pid) -> bool:
        """Not out, and started at least once in his club's last 3 matches (or
        is started by half the elites: back from injury). Says nothing about
        this GW's fixture — a blank is not an injury."""
        fit, ch = F.fitness(self.by[pid])
        if fit == "out" or (fit == "doubt" and (ch or 0) <= 50):
            return False
        if self.recent(pid)["starts"] >= 1:
            return True
        return self.ec["start"][pid] * 2 >= max(self.ec["n"], 1)

    def playing(self, pid) -> bool:
        """Will play AND has a fixture this gameweek."""
        return self.has_fixture(pid) and self.will_play(pid)

    def eff_starts(self, pid) -> int:
        """Elite starts, counted as 0 for a player who will not play this GW."""
        return self.ec["start"][pid] if self.playing(pid) else 0

    def out_starts(self, pid) -> int:
        """Elite starts of a player being SOLD: 0 only if he is out or not
        playing for his club (a one-week blank does not count)."""
        return self.ec["start"][pid] if self.will_play(pid) else 0


def _checks_data(c: _Ctx) -> list[Result]:
    out = []
    stale = []
    for src, limit in MAX_AGE.items():
        t = c.b.fetched_at.get(src)
        if src == "elite" and c.b.elite is None:
            continue
        if t is None:
            stale.append(f"{src}: no fetch time")
        elif (c.b.now - t).total_seconds() > limit:
            stale.append(f"{src}: {int((c.b.now - t).total_seconds() // 60)} min old")
    bt, lw = c.b.fetched_at.get("bootstrap"), F.last_price_window(c.b.now)
    if bt is not None and bt < lw:
        stale.append(f"bootstrap fetched before the {F.fmt_et(lw)} price change")
    out.append(Result("DATA_FRESH", not stale, FAIL, "; ".join(stale)))
    out.append(Result("GW_TARGET", c.plan.get("gw") == c.gw, FAIL,
                      f"plan GW{c.plan.get('gw')}, next deadline is GW{c.gw}"))
    out.append(Result("TEAM_ID", c.plan.get("team_id") in (None, c.b.team_id), FAIL,
                      f"plan team {c.plan.get('team_id')}, bundle team {c.b.team_id}"))
    drift = R.rules_drift(c.b.bootstrap)
    out.append(Result("RULES_DRIFT", not drift, FAIL, "; ".join(drift)))
    ids = [x for m in c.moves for x in m] + c.xi + c.bench + [c.cap, c.vice]
    unknown = [x for x in ids if x not in c.by]
    out.append(Result("IDS_KNOWN", not unknown, FAIL, f"unknown ids {unknown}"))
    e = c.b.elite
    out.append(Result("ELITE_PRESENT", bool(e) and e.get("gw") == c.gw, FAIL,
                      "no elite boards" if not e else
                      f"elite boards are for GW{e.get('gw')}, advice is for GW{c.gw}"))
    n_ok = len(c.elite_ok)
    out.append(Result("ELITE_BOARDS", n_ok >= MIN_ELITE_BOARDS, FAIL,
                      f"{n_ok} legal boards (need {MIN_ELITE_BOARDS})"))
    out.append(Result("ELITE_BOARDS_MAX", n_ok <= EXPECTED_ELITE_BOARDS, FAIL,
                      f"{n_ok} boards parsed, expected {EXPECTED_ELITE_BOARDS}: an extra card was read as a board"))
    out.append(Result("ELITE_BOARDS_ALL", n_ok >= EXPECTED_ELITE_BOARDS and not c.elite_bad,
                      WARN, f"{n_ok} legal elite boards; dropped: " + "; ".join(
                          f"board {k}: {', '.join(v)}" for k, (t, v) in enumerate(c.elite_bad, 1))))
    out.append(Result("XP_FRESH", c.xp_fresh, WARN, "no fresh model xP (odds tie-breaks unavailable)"))
    prev = next((ev for ev in c.b.bootstrap["events"] if ev["id"] == c.gw - 1), None)
    if prev:
        prev_dl = F.parse_utc(prev["deadline_time"])
        old_boards = [k for k, (t, _) in enumerate(c.elite_ok, 1)
                      if F.parse_fix_time(t.get("last_updated")) < prev_dl]
        current = len(c.elite_ok) - len(old_boards)
        out.append(Result("ELITE_BOARDS_CURRENT", current >= MIN_ELITE_BOARDS, FAIL,
                          f"only {current} elite boards updated since the GW{c.gw - 1} deadline"))
        out.append(Result("ELITE_BOARDS_STALE", not old_boards, WARN,
                          f"{len(old_boards)} elite board(s) not updated since the GW{c.gw - 1} deadline"))
    st = c.st
    fh_gws = {ch["event"] for ch in c.b.entry_history.get("chips", []) if ch["name"] == "freehit"}
    aligned = st.hist_gw == st.api_gw or (st.hist_gw == st.api_gw + 1 and st.hist_gw in fh_gws)
    ft_ok = aligned and (st.ft_source.startswith("FPL app") or st.ft_gw == c.gw)
    out.append(Result("FT_GW", ft_ok, FAIL,
                      f"free-transfer count is for GW{st.ft_gw} (history to GW{st.hist_gw}, "
                      f"picks GW{st.api_gw}) but the advice is for GW{c.gw}: inputs out of step"))
    out.append(Result("PENDING_PRICED", st.pending_priced, FAIL,
                      "a move you already made has no price: tell me what you paid, or the bank your app shows"))
    if c.b.opening is not None:
        implied = F.squad_replay(c.b.opening, c.b.transfers, c.b.entry_history.get("chips"))
        out.append(Result("SQUAD_REPLAY", implied == set(st.api_slots), FAIL,
                          "opening squad + transfer record does not give your current squad "
                          "(a transfer record is missing, so selling prices can't be trusted)"))
    else:
        out.append(Result("SQUAD_REPLAY", False, WARN,
                          "purchase prices not cross-checked against your opening squad"))
    return out


def _checks_squad(c: _Ctx) -> list[Result]:
    out, st, by = [], c.st, c.by
    ins = [i for _, i in c.moves]
    outs = [o for o, _ in c.moves]
    not_owned = [c.name(o) for o in outs if o not in st.slots]
    out.append(Result("MOVE_OUT_OWNED", not not_owned, FAIL, f"not in your squad: {not_owned}"))
    owned = [c.name(i) for i in ins if i in st.slots]
    out.append(Result("MOVE_IN_NEW", not owned, FAIL, f"already in your squad: {owned}"))
    dup = len(set(ins)) != len(ins) or len(set(outs)) != len(outs) or set(ins) & set(outs)
    out.append(Result("MOVE_UNIQUE", not dup, FAIL, "a player appears in two moves"))
    pos = [f"{c.name(o)}→{c.name(i)}" for o, i in c.moves
           if o in by and i in by and by[o]["element_type"] != by[i]["element_type"]]
    out.append(Result("MOVE_SAME_POS", not pos, FAIL, f"position mismatch: {pos}"))
    buyback = [c.name(i) for i in ins if i in st.sold_this_gw]
    out.append(Result("NO_BUYBACK", not buyback, FAIL,
                      f"sold earlier this gameweek: {buyback}"))
    if c.squad_after is None:
        out.append(Result("SQUAD_LEGAL", False, FAIL, "moves do not apply to your squad"))
        return out
    v = R.squad_violations(c.squad_after, by)
    out.append(Result("SQUAD_LEGAL", not v, FAIL, "; ".join(v)))
    part_ok = (len(c.xi) == 11 and len(c.bench) == 4
               and sorted(c.xi + c.bench) == sorted(c.squad_after))
    out.append(Result("LINEUP_PARTITION", part_ok, FAIL,
                      "XI + bench must be exactly the 15 players you will own"))
    out.append(Result("BENCH_GK_FIRST", bool(c.bench) and c.bench[0] in by
                      and by[c.bench[0]]["element_type"] == R.GK, FAIL,
                      "bench slot 1 must be the substitute goalkeeper"))
    return out


def _checks_money(c: _Ctx) -> list[Result]:
    out, st, by = [], c.st, c.by
    if any(o not in st.sell or i not in by for o, i in c.moves):
        out.append(Result("BUDGET", False, FAIL, "cannot price these moves"))
        return out
    bank_after = st.bank + sum(st.sell[o] for o, _ in c.moves) - sum(by[i]["now_cost"] for _, i in c.moves)
    c.bank_after = bank_after
    out.append(Result("BUDGET", bank_after >= 0, FAIL,
                      f"bank after moves £{bank_after / 10:.1f}m"))
    # after tonight's price change (the game's own projections)
    worst = st.bank
    for o, i in c.moves:
        drop = 1 if F.price_direction_tonight(by[o]) < 0 else 0
        now_o = by[o]["now_cost"] - drop
        worst += R.sell_price(now_o, st.purchase[o][0]) - by[i]["now_cost"] \
            - (1 if F.price_direction_tonight(by[i]) > 0 else 0)
    window = F.next_price_window(c.b.now)
    c.window_matters = bool(c.moves) and window < c.deadline and worst != bank_after
    c.window = window
    out.append(Result("BUDGET_AFTER_PRICE_CHANGE", worst >= 0 or window >= c.deadline, WARN,
                      f"after the {F.fmt_et(window)} price change the bank would be "
                      f"£{worst / 10:.1f}m — make the moves before then"))
    hits = R.hits_for(len(c.moves), st.ft)
    c.hits = hits
    left = st.approved_hits - st.pending_hits
    out.append(Result("NO_UNAPPROVED_HITS", hits == 0 or hits <= left, FAIL,
                      f"{len(c.moves)} transfers with {st.ft} free = -{hits} points; Pramod approved "
                      f"{st.approved_hits}, {st.pending_hits} already used (only his pending-moves note "
                      "can approve a hit)"))
    out.append(Result("FT_NOT_WASTED", bool(c.moves) or st.ft < R.FT_CAP, WARN,
                      f"rolling at {st.ft} free transfers wastes one"))
    out.append(Result("FT_FROM_APP", st.ft_source.startswith("FPL app"), WARN,
                      f"free transfers ({st.ft}) computed from the API, not confirmed in the app"))
    out.append(Result("NO_CHIP", c.plan.get("chip") in (None, ""), FAIL,
                      "chip plans are not validated yet — decide chips with Pramod directly"))
    for w in st.warnings:
        out.append(Result("STATE_WARNING", False, WARN, w))
    return out


def must_fix(c: _Ctx, o: int) -> bool:
    """o will not play AND without him you cannot field a playing XI at his
    position (e.g. both keepers are backups). Then the best available
    starter is needed even if fewer than IN_MIN_ELITE_STARTS elites start him."""
    if c.will_play(o):
        return False
    by, squad = c.by, c.st.slots
    pos = by[o]["element_type"]
    playing = [p for p in squad if p != o and c.will_play(p)]
    return (sum(1 for p in playing if by[p]["element_type"] == pos) < R.POS_MIN_XI[pos]
            or len(playing) < R.XI_SIZE)


def best_alternative(c: _Ctx, o: int, exclude: int) -> tuple[int, int | None]:
    """(elite starts, id) of the best other replacement for o: same position,
    not owned, fit, a fixture, affordable on its own, inside the club cap,
    playing (and his club's No.1 if a keeper)."""
    by, st = c.by, c.st
    pos = by[o]["element_type"]
    clubs = {}
    for p in st.slots:
        if p != o:
            clubs[by[p]["team"]] = clubs.get(by[p]["team"], 0) + 1
    best = (0, None)
    for p, n in c.ec["start"].most_common():
        if n <= best[0]:
            break
        e = by[p]
        if (p == exclude or p in st.slots or e["element_type"] != pos
                or F.fitness(e)[0] != "fit" or not c.has_fixture(p)
                or clubs.get(e["team"], 0) >= R.MAX_PER_CLUB
                or st.bank + st.sell[o] - e["now_cost"] < 0 or not c.playing(p)):
            continue
        if pos == R.GK and c.starter_gk(e["team"])["starter"] != p:
            continue
        best = (n, p)
    return best


def incoming_results(c: _Ctx, o: int, i: int) -> list[Result]:
    """Every rule an incoming player must pass (shared with the engine)."""
    out, by = [], c.by
    ni, no = c.name(i), c.name(o)
    fit, ch = F.fitness(by[i])
    out.append(Result("IN_FIT", fit == "fit", FAIL, f"{ni}: {fit} ({ch}%) {by[i].get('news', '')}"))
    out.append(Result("IN_FIXTURE", c.has_fixture(i), FAIL, f"{ni} has no GW{c.gw} fixture"))
    rc = c.recent(i)
    need_min = MIN_PER_MATCH * rc["matches"]                 # 120 over 3; scaled early season
    need_st = max(1, rc["matches"] - 1)                      # 2 of 3
    out.append(Result("IN_RECENT_MINUTES",
                      rc["matches"] >= 1 and rc["minutes"] >= need_min and rc["starts"] >= need_st, FAIL,
                      f"{ni}: {rc['minutes']} min, {rc['starts']} starts in the club's last "
                      f"{rc['matches']} (need {need_min}+ min and {need_st}+ starts)"))
    if by[i]["element_type"] == R.GK:
        sg = c.starter_gk(by[i]["team"])
        out.append(Result("IN_GK_STARTER", sg["starter"] == i, FAIL,
                          f"{ni} is not his club's starting keeper "
                          f"(starter: {c.name(sg['starter']) if sg['starter'] else 'unclear'})"))
    s_in = c.ec["start"][i]
    s_out = c.out_starts(o)
    gap = s_in - s_out
    fix = must_fix(c, o)
    # a one-week blank empties a player's elite STARTS but not his ownership:
    # in a blank, the elites still holding him means "keep", not "sell"
    keep = (not c.has_fixture(o) and c.will_play(o)
            and c.ec["own"][o] * 2 >= max(c.ec["n"], 1))
    out.append(Result("SELL_ELITE_OWNED", not keep, FAIL,
                      f"{no} blanks this week but {c.ec['own'][o]}/{c.ec['n']} elites keep him"))
    if s_in >= IN_MIN_ELITE_STARTS:
        out.append(Result("IN_ELITE_STARTS", True, FAIL, ""))
    elif fix:
        alt_n, alt = best_alternative(c, o, exclude=i)
        ok = s_in >= 1 and s_in >= alt_n
        out.append(Result("IN_ELITE_STARTS", ok, FAIL,
                          f"{no} must be replaced (no playing cover); {ni} started by {s_in}/"
                          f"{c.ec['n']} elites" + (f" but {c.name(alt)} has {alt_n}" if alt_n > s_in
                                                   else "" if s_in else " — no elite-backed option")))
    else:
        out.append(Result("IN_ELITE_STARTS", False, FAIL,
                          f"{ni} started by {s_in}/{c.ec['n']} elites (need {IN_MIN_ELITE_STARTS})"))
    # missing boards could reverse a narrow call: the margin grows with them
    need = MIN_START_GAP + c.missing_boards
    ok, why = gap >= need or (fix and gap >= 1), ""
    if c.missing_boards and not fix and gap < need:
        why = f"; {c.missing_boards} board(s) missing, need a gap of {need}"
    if gap == 1 and not fix and not c.missing_boards:
        xi_, xo = c.xp(i), c.xp(o)
        ok = xi_ is not None and xo is not None and xi_ > xo
        why = (f"; odds tie-break xP {xi_:.2f} v {xo:.2f}" if xi_ is not None and xo is not None
               else "; no fresh odds tie-break")
    out.append(Result("START_GAP", ok, FAIL,
                      f"{no} → {ni}: elite starts {s_out} → {s_in} (gap {gap}){why}"))
    return out


def _checks_incoming(c: _Ctx) -> list[Result]:
    out = []
    for o, i in c.moves:
        if o in c.by and i in c.by and o in c.st.sell:
            out += incoming_results(c, o, i)
    return out


def _checks_lineup(c: _Ctx) -> list[Result]:
    out, by = [], c.by
    if not c.xi or any(x not in by for x in c.xi + c.bench):
        out.append(Result("XI_LEGAL", False, FAIL, "XI/bench missing or unknown ids"))
        return out
    v = R.xi_violations(c.xi, by)
    out.append(Result("XI_LEGAL", not v, FAIL, "; ".join(v)))
    out_ = [c.name(p) for p in c.xi if F.fitness(by[p])[0] == "out"]
    out.append(Result("XI_FIT", not out_, FAIL, f"unavailable starters: {out_}"))
    doubt = [(c.name(p), F.fitness(by[p])[1]) for p in c.xi if F.fitness(by[p])[0] == "doubt"]
    bad_doubt = [d for d in doubt if (d[1] or 0) < 75]
    fmt = lambda ds: ", ".join(f"{n} {'?' if ch is None else ch}%" for n, ch in ds)
    out.append(Result("XI_NO_LONG_SHOTS", not bad_doubt, FAIL,
                      f"starters at 50% or less: {fmt(bad_doubt)}"))
    out.append(Result("XI_DOUBTS", not doubt, WARN, f"doubtful starters: {fmt(doubt)}"))
    nofix = [c.name(p) for p in c.xi if not c.has_fixture(p)]
    out.append(Result("XI_HAS_FIXTURE", not nofix, FAIL, f"no GW{c.gw} fixture: {nofix}"))
    idle = [c.name(p) for p in c.xi if p not in {i for _, i in c.moves} and not c.playing(p)]
    out.append(Result("XI_PLAYING", not idle, FAIL,
                      f"not starting for their clubs lately: {idle}"))
    gk = next((p for p in c.xi if by[p]["element_type"] == R.GK), None)
    if gk is not None:
        sg = c.starter_gk(by[gk]["team"])
        out.append(Result("XI_GK_STARTER", sg["starter"] == gk, FAIL,
                          f"{c.name(gk)} is not his club's starting keeper "
                          f"(starter: {c.name(sg['starter']) if sg['starter'] else 'unclear'})"))
    # elite-preferred player left on the bench
    bad, close = [], []
    for b_ in c.bench[1:]:
        if not c.playing(b_):
            continue
        for x in c.xi:
            if by[x]["element_type"] == R.GK:
                continue
            trial = [b_ if p == x else p for p in c.xi]
            if R.xi_violations(trial, by):
                continue
            gap = c.eff_starts(b_) - c.eff_starts(x)
            if gap >= 2:
                bad.append(f"{c.name(b_)} ({c.eff_starts(b_)}) over {c.name(x)} ({c.eff_starts(x)})")
            elif gap == 1:
                close.append(f"{c.name(b_)} v {c.name(x)}")
    if gk is not None and c.bench and by[c.bench[0]]["element_type"] == R.GK:
        sub = c.bench[0]
        if (c.playing(sub) and c.starter_gk(by[sub]["team"])["starter"] == sub
                and c.eff_starts(sub) - c.eff_starts(gk) >= 2):
            bad.append(f"{c.name(sub)} ({c.eff_starts(sub)}) over {c.name(gk)} ({c.eff_starts(gk)})")
    out.append(Result("BENCH_ELITE_ORDER", not bad, FAIL,
                      "elites start your bench player instead: " + "; ".join(bad)))
    order_bad = []
    outfield_bench = c.bench[1:]
    for k, early in enumerate(outfield_bench):
        for late in outfield_bench[k + 1:]:
            if c.playing(late) and (c.eff_starts(late) - c.eff_starts(early) >= 2
                                    or not c.playing(early)):
                order_bad.append(f"{c.name(late)} ({c.eff_starts(late)}) should come before "
                                 f"{c.name(early)} ({c.eff_starts(early)})")
    out.append(Result("BENCH_ORDER", not order_bad, FAIL,
                      "auto-sub order: " + "; ".join(order_bad)))
    out.append(Result("BENCH_CLOSE_CALLS", not close, WARN, "1-board calls: " + "; ".join(close)))
    # 1-board gaps are ties that the odds break (Pramod's rule)
    tie_bad = []
    for b_ in c.bench[1:]:
        if not c.playing(b_) or c.xp(b_) is None:
            continue
        for x in c.xi:
            if by[x]["element_type"] == R.GK or c.xp(x) is None:
                continue
            if abs(c.eff_starts(b_) - c.eff_starts(x)) > 1:
                continue
            if R.xi_violations([b_ if p == x else p for p in c.xi], by):
                continue
            if c.xp(b_) > c.xp(x) + XP_TIE_MARGIN:
                tie_bad.append(f"{c.name(b_)} (xP {c.xp(b_):.1f}) over {c.name(x)} (xP {c.xp(x):.1f})")
    out.append(Result("XI_TIE_ODDS", not tie_bad, FAIL,
                      "1-board ties the odds say to flip: " + "; ".join(tie_bad)))
    # captaincy
    cap_ok = c.cap in by and c.cap in c.xi and F.fitness(by[c.cap])[0] == "fit" and c.playing(c.cap)
    out.append(Result("CAPTAIN_VALID", cap_ok, FAIL, f"captain {c.name(c.cap)} must be a fit starter"))
    vice_ok = (c.vice in c.xi and c.vice != c.cap and c.vice in by
               and F.fitness(by[c.vice])[0] == "fit" and c.playing(c.vice))
    out.append(Result("VICE_VALID", vice_ok, FAIL,
                      f"vice {c.name(c.vice)} must be a different fit starter who plays this week"))
    caps = c.ec["cap"]
    opts = captain_options(c, c.xi)
    out.append(Result("CAPTAIN_ELITE", c.cap in opts, FAIL,
                      f"elites captain {', '.join(f'{c.name(k)} {v}' for k, v in caps.most_common(3))}; "
                      f"among your starters the pick is {', '.join(c.name(p) for p in opts) or 'none'}; "
                      f"plan captains {c.name(c.cap)} ({caps.get(c.cap, 0)})"))
    # auto-sub cover for every doubtful starter
    uncovered = []
    played = {p for p in c.xi + c.bench if c.playing(p) and F.fitness(by[p])[0] == "fit"}
    for p in c.xi:
        if F.fitness(by[p])[0] != "doubt":
            continue
        _, swaps = R.autosub(c.xi, c.bench, played - {p}, by)
        if not any(s[0] == p for s in swaps):
            uncovered.append(c.name(p))
    out.append(Result("AUTOSUB_COVER", not uncovered, FAIL,
                      f"no playing bench cover for doubtful {uncovered}"))
    return out


def captain_options(c: _Ctx, xi: list[int]) -> list[int]:
    """Who may wear the armband: among fit, playing starters, the most
    elite captaincies; a 1-board gap is a tie that fresh odds (xP) break.
    With no elite captain among them, any fit starter."""
    caps = c.ec["cap"]
    fit = [p for p in xi if p in c.by and F.fitness(c.by[p])[0] == "fit" and c.playing(p)]
    top = max((caps.get(p, 0) for p in fit), default=0)
    if top == 0:
        return fit
    tops = [p for p in fit if caps.get(p, 0) == top]
    band = [p for p in fit if caps.get(p, 0) >= max(1, top - 1)]
    if len(band) == len(tops) or any(c.xp(p) is None for p in band):
        return tops                      # no tie, or odds missing for someone: consensus
    return [max(band, key=lambda p: c.xp(p))]


def _checks_engine(c: _Ctx) -> list[Result]:
    """The plan may not be clearly worse than the engine's own plan on the
    same snapshot (a weaker but individually legal move, a worse XI)."""
    import advisor
    eng = advisor.propose(c.b)
    xi_s = lambda xi: sum(c.eff_starts(p) for p in xi)
    eng_squad = F.predicted_app_slots(c.st.slots, [(m["out"], m["in"]) for m in eng["transfers"]])
    sq_s = lambda sq: sum(c.out_starts(p) for p in sq)
    mine_xi, eng_xi = xi_s(c.xi), xi_s(eng["xi"])
    mine_sq, eng_sq = sq_s(c.squad_after or []), sq_s(eng_squad)
    ok = mine_xi >= eng_xi - ENGINE_SLACK and mine_sq >= eng_sq - ENGINE_SLACK
    return [Result("ENGINE_AGREES", ok, FAIL,
                   f"elite starts: your XI {mine_xi} v engine {eng_xi}; squad {mine_sq} v engine {eng_sq}")]


def _checks_timing(c: _Ctx) -> list[Result]:
    ok = c.b.now < c.deadline - DEADLINE_BUFFER
    return [Result("BEFORE_DEADLINE", ok, FAIL,
                   f"deadline {F.fmt_et(c.deadline)}, now {F.fmt_et(c.b.now)}"),
            Result("HISTORY_FRESH", not c.stale_history, FAIL,
                   "match history older than the club's last finished match for: "
                   + ", ".join(c.name(p) for p in sorted(c.stale_history)))]


SECRET_PAT = re.compile(r"\b[a-z0-9]{32}\b|sessionid|api[_-]?key", re.I)


def _checks_message(c: _Ctx, msg: str) -> list[Result]:
    leaked = any(s and s in msg for s in c.b.secrets) or bool(SECRET_PAT.search(msg))
    st = c.st
    confirm = (f"confirm your FPL app shows {st.ft} free transfer"
               f"{'' if st.ft == 1 else 's'} and £{st.bank / 10:.1f}m in the bank")
    out = [Result("NO_SECRETS", not leaked, FAIL, "message contains a credential-like string"),
           Result("CONFIRM_LINE", confirm in msg, FAIL, "missing the app confirmation line"),
           Result("ET_ONLY", not re.search(r"\b(UTC|GMT|BST|PST|PT|CET)\b", msg), FAIL,
                  "times must be shown in ET only")]
    if c.moves and getattr(c, "window_matters", False):
        out.append(Result("PRICE_WINDOW_LINE", F.fmt_et(c.window) in msg, FAIL,
                          "missing the 'before the price change' line"))
    return out


# ==========================================================================
# rendering
# ==========================================================================
def _money(t: int) -> str:
    return f"£{t / 10:.1f}m"


def render(c: _Ctx) -> str:
    by, st, nm = c.by, c.st, c.name
    short = F.club_short(c.b.bootstrap)
    pos = lambda p: R.POS_NAME[by[p]["element_type"]]
    ft_word = f"{st.ft} free transfer{'' if st.ft == 1 else 's'}"
    until = min(F.next_price_window(c.b.now), c.deadline)
    L = [f"FPL Edge — GW{c.gw} plan (deadline {F.fmt_et(c.deadline)})",
         f"Checked {F.fmt_et(c.b.now)} · valid until {F.fmt_et(until)}"
         + (" (price change)" if until < c.deadline else ""),
         f"First, confirm your FPL app shows {ft_word} and {_money(st.bank)} in the bank"
         + (" after the moves you already made" if st.pending_moves else "")
         + ". If it doesn't, stop and tell me."]
    if st.pending_moves:
        L.append("Already done this week: " + ", ".join(
            f"{nm(o)} → {nm(i)}" for o, i in st.pending_moves))
    L.append("")
    if c.moves:
        hit = f", -{c.hits} pts hit" if c.hits else ", no hit"
        L.append(f"Transfers ({ft_word}{hit}):")
        for k, (o, i) in enumerate(c.moves, 1):
            rc = c.recent(i)
            L.append(f"{k}. {nm(o)} ({short[by[o]['team']]} {pos(o)}, sells {_money(st.sell[o])}) → "
                     f"{nm(i)} ({short[by[i]['team']]} {pos(i)}, {_money(by[i]['now_cost'])})")
            L.append(f"   Elites start him {c.ec['start'][i]}/{c.ec['n']} (vs {c.out_starts(o)} for {nm(o)}); "
                     f"{rc['starts']} starts, {rc['minutes']} min in his club's last {rc['matches']}"
                     + ("; his club's No.1 keeper" if by[i]["element_type"] == R.GK else ""))
            if must_fix(c, o):
                L.append(f"   Needed: {nm(o)} isn't playing for his club and you have no "
                         f"playing {pos(o)} to cover him.")
        sells = " + ".join(_money(st.sell[o]) for o, _ in c.moves)
        buys = " − ".join(_money(by[i]["now_cost"]) for _, i in c.moves)
        L.append(f"Money: {_money(st.bank)} bank + {sells} − {buys} = {_money(c.bank_after)} left.")
        if c.window_matters:
            L.append(f"Make the transfers before {F.fmt_et(c.window)} (price change).")
    else:
        L.append(f"Transfers: none — roll the transfer ({ft_word} available).")
    L.append("")
    app = F.predicted_app_slots(st.slots, c.moves)
    acts = F.lineup_actions(app, c.xi, c.bench, by)
    if c.moves:
        L.append("The app puts each signing in the sold player's slot. Then:")
    else:
        L.append("Lineup changes in the app:")
    if not acts:
        L.append("- No lineup changes needed.")
    for kind, a, b in acts:
        if kind == "swap":
            L.append(f"- Start {nm(a)}, bench {nm(b)}")
        else:
            L.append("- Set the bench order: " + ", ".join(
                f"{k}. {nm(p)}" for k, p in enumerate(c.bench, 1)))
    xi_sorted = sorted(c.xi, key=lambda p: (by[p]["element_type"], app.index(p) if p in app else 99))
    L.append(f"Starting XI ({R.formation(c.xi, by)}): " + ", ".join(nm(p) for p in xi_sorted))
    L.append("Bench: " + ", ".join(f"{k}. {nm(p)}" for k, p in enumerate(c.bench, 1)))
    caps = c.ec["cap"]
    L.append(f"Captain: {nm(c.cap)} ({caps.get(c.cap, 0)}/{c.ec['n']} elites) · Vice: {nm(c.vice)}"
             " — set both in the app.")
    warns = [r for r in c.results_so_far if not r.ok and r.level == WARN
             and r.code not in ("FT_FROM_APP",)]
    if warns:
        L.append("")
        L.append("Heads-up:")
        L += [f"- {r.detail}" for r in warns]
    return "\n".join(L)


# ==========================================================================
# entry point
# ==========================================================================
def check(plan: dict, b: Bundle) -> Report:
    try:
        st = build_state(b)
    except Exception as ex:
        r = Result("STATE_BUILD", False, FAIL, f"{type(ex).__name__}: {ex}")
        return Report(plan, [r], _failed_text(plan, [r]), False)
    c = _Ctx(plan, b, st)
    results: list[Result] = []
    for fn in (_checks_data, _checks_squad, _checks_money, _checks_incoming,
               _checks_lineup, _checks_engine, _checks_timing):
        try:
            results += fn(c)
        except MissingInput as ex:
            results.append(Result("INPUT_MISSING", False, FAIL, str(ex)))
        except Exception as ex:      # a crashing check is a failed check
            results.append(Result(f"CHECK_ERROR_{fn.__name__.strip('_').upper()}", False, FAIL,
                                  f"{type(ex).__name__}: {ex}"))
    hard = [r for r in results if not r.ok and r.level == FAIL]
    if hard:
        return Report(plan, results, _failed_text(plan, results), False, st)
    c.results_so_far = results
    try:
        body = render(c)
    except Exception as ex:
        r = Result("RENDER_ERROR", False, FAIL, f"{type(ex).__name__}: {ex}")
        return Report(plan, results + [r], _failed_text(plan, results + [r]), False, st)
    results += _checks_message(c, body)
    hard = [r for r in results if not r.ok and r.level == FAIL]
    if hard:
        return Report(plan, results, _failed_text(plan, results), False, st)
    n_fail_level = sum(1 for r in results if r.level == FAIL)
    stamp = f"Checks passed {n_fail_level}/{n_fail_level} · plan {plan_hash(plan, body)}"
    return Report(plan, results, body + "\n\n" + stamp, True, st)


def _failed_text(plan: dict, results: list[Result]) -> str:
    bad = [r for r in results if not r.ok and r.level == FAIL]
    lines = [f"VALIDATION FAILED — GW{plan.get('gw')} plan {plan_hash(plan)} must NOT be sent.",
             "Reasons:"]
    lines += [f"- {r.code}: {r.detail}" for r in bad]
    return "\n".join(lines)


def verify_stamp(message: str, plan: dict) -> bool:
    """True only when the message is exactly what the validator rendered for
    this plan: the stamp's hash covers the plan and every line above it."""
    text = message.strip()
    body, _, last = text.rpartition("\n")
    m = re.fullmatch(r"Checks passed (\d+)/\1 · plan ([0-9a-f]{12})", last.strip())
    return bool(m) and int(m.group(1)) >= MIN_STAMP_CHECKS and m.group(2) == plan_hash(plan, body)


# ==========================================================================
# CLI
# ==========================================================================
def _bundle_from_args(args) -> Bundle:
    import bundle as B, pathlib
    if args.bundle:
        return B.from_dir(pathlib.Path(args.bundle))
    import model_config as mc
    team = mc.full().get("team_id")
    if not team:
        raise MissingInput("config.json has no team_id")
    return B.live(int(team), pending_path=pathlib.Path(args.pending) if args.pending else None)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="guards")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check")
    ck.add_argument("plan")
    for p in (ck, sub.add_parser("facts")):
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--live", action="store_true")
        g.add_argument("--bundle")
        p.add_argument("--pending")
    ck.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        b = _bundle_from_args(args)
    except Exception as ex:
        print(f"INPUTS MISSING: {type(ex).__name__}: {ex}")
        return 3
    try:
        if args.cmd == "facts":
            import advisor
            print(advisor.fact_table(b))
            return 0
        plan = json.loads(open(args.plan).read())
        rep = check(plan, b)
        if args.bundle:
            print(f"REPLAY of a saved snapshot ({F.fmt_et(b.now)}) — not for sending.")
        print(json.dumps(rep.to_json(), indent=1, ensure_ascii=False) if args.json else rep.message)
        return 0 if rep.passed else 2
    except Exception as ex:
        print(f"INTERNAL ERROR: {type(ex).__name__}: {ex}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
