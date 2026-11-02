"""FPL Edge — deterministic advisor. Code decides; guards.py checks.

  python3 -m advisor run --live [--pending FILE] [--out plan.json] [--msg-out message.txt]
  python3 -m advisor run --bundle DIR
  python3 -m advisor facts --live

Decision rules (Pramod's, made executable):
  * Follow the 12 elite boards. Rank by elite STARTS (XI on their boards),
    not ownership. A player who will not play counts as 0 starts.
  * A transfer needs the incoming player started by >= 3 elites (or, when
    nobody you own at that position will play, the best-backed option), a start
    gap >= 2 over the player sold (a gap of 1 only with the odds tie-break),
    fit, a fixture, 120+ min and 2+ starts in his club's last 3, and for a
    keeper: his club's current No.1.
  * Never a hit (no -4) unless Pramod approves it himself.
  * XI and bench maximise elite starts (odds xP breaks ties); captain is the
    elites' modal captain.
Exit codes: 0 plan passed validation, 2 validation failed, 3 inputs missing,
4 internal error.
"""
from __future__ import annotations
import itertools, json, sys

import facts as F
import fpl_rules as R
import guards as G
from bundle import Bundle, MissingInput
from squad_state import build_state

TOP_PAIRS = 25          # candidate transfers kept for the combination search
MAX_MOVES_SEARCHED = 3  # combinations searched exhaustively up to this size


def _ctx(b: Bundle):
    st = build_state(b)
    gw = b.next_event()["id"]
    return G._Ctx(dict(gw=gw, transfers=[], xi=[], bench=[]), b, st)


def _xp(b: Bundle, pid: int) -> float:
    return float(b.xp.get(pid) or 0.0)


# --------------------------------------------------------------------------
# lineup
# --------------------------------------------------------------------------
FORMATIONS = [(d, m, 10 - d - m) for d in range(3, 6) for m in range(2, 6)
              if 1 <= 10 - d - m <= 3]


def _player_score(c, pid: int, incoming: set[int]) -> float:
    """Elite starts dominate (x100); model xP breaks ties (x10). A player who
    will not play, or a keeper who is not his club's No.1, is unpickable."""
    by = c.by
    fit, ch = F.fitness(by[pid])
    if fit == "out" or not c.has_fixture(pid):
        return -1e6
    if not (pid in incoming or c.playing(pid)):
        return -1e5
    if by[pid]["element_type"] == R.GK and c.starter_gk(by[pid]["team"])["starter"] != pid:
        return -1e5
    s = c.ec["start"][pid] * 100 + _xp(c.b, pid) * 10
    if fit == "doubt":
        s -= 150 if (ch or 0) >= 75 else 1e4
    return s


def best_lineup(c, squad: list[int], incoming: set[int]):
    """Exact best XI: score is additive, so the best XI for a formation is
    the top players per position; try every legal formation."""
    by = c.by
    score = {p: _player_score(c, p, incoming) for p in squad}
    pos = {et: sorted((p for p in squad if by[p]["element_type"] == et),
                      key=lambda p: (-score[p], squad.index(p))) for et in R.POS_SQUAD}
    best, best_s = None, -1e18
    for d, m, f in FORMATIONS:
        xi = pos[R.GK][:1] + pos[R.DEF][:d] + pos[R.MID][:m] + pos[R.FWD][:f]
        s = sum(score[p] for p in xi)
        if s > best_s:
            best, best_s = xi, s
    xi = best
    sub_gk = pos[R.GK][1]
    rest = sorted((p for p in squad if p not in xi and by[p]["element_type"] != R.GK),
                  key=lambda p: (-score[p], squad.index(p)))
    bench = [sub_gk, *rest]
    caps = c.ec["cap"]
    opts = G.captain_options(c, xi) or xi
    captain = sorted(opts, key=lambda p: (-caps.get(p, 0), -score[p]))[0]
    fit_xi = sorted((p for p in xi if p != captain and F.fitness(by[p])[0] == "fit"),
                    key=lambda p: (-caps.get(p, 0), -score[p]))
    return xi, bench, captain, fit_xi[0], best_s


# --------------------------------------------------------------------------
# transfers
# --------------------------------------------------------------------------
def candidate_moves(c) -> list[tuple[int, int, int]]:
    """[(out, in, gap)] single transfers that pass every incoming-player rule
    and are affordable on their own, best first."""
    st, by = c.st, c.by
    owned = set(st.slots)
    clubs: dict[int, int] = {}
    for p in st.slots:
        clubs[by[p]["team"]] = clubs.get(by[p]["team"], 0) + 1
    pool = [pid for pid, n in c.ec["start"].items()
            if n >= 1 and pid not in owned and pid not in st.sold_this_gw]
    pairs = []
    for o in st.slots:
        if o in st.bought_this_gw:
            continue
        for i in pool:
            if by[i]["element_type"] != by[o]["element_type"]:
                continue
            if st.bank + st.sell[o] - by[i]["now_cost"] < 0:
                continue
            same_club = by[i]["team"] == by[o]["team"]
            if not same_club and clubs.get(by[i]["team"], 0) >= R.MAX_PER_CLUB:
                continue
            gap = c.ec["start"][i] - c.out_starts(o)
            if gap < 1:
                continue
            if all(r.ok for r in G.incoming_results(c, o, i)):
                pairs.append((o, i, gap))
    pairs.sort(key=lambda p: (-p[2], -(_xp(c.b, p[1]) - _xp(c.b, p[0])), by[p[1]]["now_cost"]))
    return pairs[:TOP_PAIRS]


def best_transfers(c) -> list[tuple[int, int]]:
    """The move set (within the free transfers — never a hit) that gives the
    best XI; ties go to the bigger total start gap, then xP, then bank."""
    st, by = c.st, c.by
    pairs = candidate_moves(c)
    base = best_lineup(c, st.slots, set())[4]
    best, best_key = [], (base, 0, 0.0, st.bank)
    for k in range(1, min(st.ft, MAX_MOVES_SEARCHED) + 1):
        for combo in itertools.combinations(pairs, k):
            outs = [p[0] for p in combo]
            ins = [p[1] for p in combo]
            if len(set(outs)) < k or len(set(ins)) < k:
                continue
            moves = [(p[0], p[1]) for p in combo]
            bank = st.bank + sum(st.sell[o] for o in outs) - sum(by[i]["now_cost"] for i in ins)
            if bank < 0:
                continue
            squad = F.predicted_app_slots(st.slots, moves)
            if R.squad_violations(squad, by):
                continue
            lineup = best_lineup(c, squad, set(ins))[4]
            key = (lineup, sum(p[2] for p in combo),
                   round(sum(_xp(c.b, i) - _xp(c.b, o) for o, i, _ in combo), 3), bank)
            if key > best_key:
                best, best_key = moves, key
    return best


def propose(b: Bundle) -> dict:
    c = _ctx(b)
    moves = best_transfers(c)
    squad = F.predicted_app_slots(c.st.slots, moves)
    xi, bench, cap, vice, _ = best_lineup(c, squad, {i for _, i in moves})
    return dict(gw=c.gw, team_id=b.team_id, phase="engine",
                transfers=[{"out": o, "in": i} for o, i in moves],
                xi=xi, bench=bench, captain=cap, vice=vice, chip=None)


# --------------------------------------------------------------------------
# fact table (what every plan is built from)
# --------------------------------------------------------------------------
def fact_table(b: Bundle) -> str:
    c = _ctx(b)
    by, st = c.by, c.st
    short = F.club_short(b.bootstrap)
    n = c.ec["n"]
    L = [f"GW{c.gw} · deadline {F.fmt_et(c.deadline)} · next price change "
         f"{F.fmt_et(F.next_price_window(b.now))}",
         f"Free transfers {st.ft} ({st.ft_source}) · bank £{st.bank / 10:.1f}m · "
         f"elite boards {n} legal" + (f", {len(c.elite_bad)} dropped" if c.elite_bad else ""),
         "", "slot player            club pos  now  sell fit    last3(min/st) elite own/start/cap  xP"]
    for k, p in enumerate(st.slots, 1):
        e = by[p]
        fit, ch = F.fitness(e)
        rc = c.recent(p)
        L.append(f"{k:>4} {e['web_name'][:17]:17} {short[e['team']]:4} {R.POS_NAME[e['element_type']]} "
                 f"{e['now_cost'] / 10:5.1f} {st.sell[p] / 10:5.1f} {fit:5}{'' if ch is None else f'{ch:>3}%':4} "
                 f"{rc['minutes']:>4}/{rc['starts']}         {c.ec['own'][p]:>2}/{c.ec['start'][p]:>2}/{c.ec['cap'][p]:<2}"
                 f"          {_xp(b, p):.2f}")
    L += ["", "Starting keepers (club's last match):"]
    for gk in [p for p in st.slots if by[p]["element_type"] == R.GK]:
        sg = c.starter_gk(by[gk]["team"])
        L.append(f"  {short[by[gk]['team']]}: {c.name(sg['starter']) if sg['starter'] else 'unclear'}"
                 + (f" ({sg['note']})" if sg["note"] else ""))
    L += ["", "Elite starters you don't own (starts>=3):"]
    owned = set(st.slots)
    for pid, s in c.ec["start"].most_common():
        if s < G.IN_MIN_ELITE_STARTS or pid in owned:
            continue
        e = by[pid]
        clubs = sum(1 for q in owned if by[q]["team"] == e["team"])
        fit, _ = F.fitness(e)
        L.append(f"  {e['web_name']:17} {short[e['team']]:4} {R.POS_NAME[e['element_type']]} "
                 f"£{e['now_cost'] / 10:.1f} start {s}/{n} own {c.ec['own'][pid]}/{n} {fit}"
                 + (" · CLUB CAP (you have 3)" if clubs >= R.MAX_PER_CLUB else ""))
    L += ["", "Elite captains: " + ", ".join(f"{c.name(k)} {v}" for k, v in c.ec["cap"].most_common(4))]
    pairs = candidate_moves(c)
    L += ["", "Transfers that pass every rule (out → in, start gap):"]
    L += [f"  {c.name(o)} → {c.name(i)} (+{g})" for o, i, g in pairs[:10]] or ["  none"]
    for w in st.warnings:
        L.append(f"WARNING: {w}")
    return "\n".join(L)


def main(argv=None) -> int:
    import argparse, pathlib
    ap = argparse.ArgumentParser(prog="advisor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "facts"):
        p = sub.add_parser(name)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--live", action="store_true")
        g.add_argument("--bundle")
        p.add_argument("--pending")
        if name == "run":
            p.add_argument("--out", default=None)
            p.add_argument("--msg-out", default=None)
    args = ap.parse_args(argv)
    try:
        b = G._bundle_from_args(args)
    except Exception as ex:
        print(f"INPUTS MISSING: {type(ex).__name__}: {ex}")
        return 3
    try:
        if args.cmd == "facts":
            print(fact_table(b))
            return 0
        plan = propose(b)
        if args.out:
            pathlib.Path(args.out).write_text(json.dumps(plan, indent=1))
        rep = G.check(plan, b)
        if args.msg_out:
            pathlib.Path(args.msg_out).write_text(rep.message + "\n")
        if args.bundle:
            print(f"REPLAY of a saved snapshot ({F.fmt_et(b.now)}) — not for sending.")
        print(rep.message)
        return 0 if rep.passed else 2
    except MissingInput as ex:
        print(f"INPUTS MISSING: {ex}")
        return 3
    except Exception as ex:
        print(f"INTERNAL ERROR: {type(ex).__name__}: {ex}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
