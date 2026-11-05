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

TOP_PAIRS = 40          # candidate transfers kept for the combination search
MAX_MOVES_SEARCHED = 3  # combinations searched exhaustively up to this size


def _ctx(b: Bundle):
    st = build_state(b)
    gw = b.next_event()["id"]
    return G._Ctx(dict(gw=gw, transfers=[], xi=[], bench=[]), b, st)


def _xp(c, pid: int) -> float:
    """Model xP for the target GW, only when fresh (stale odds decide nothing)."""
    v = c.xp(pid)
    return float(v) if v is not None else 0.0


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
    s = c.ec["start"][pid] * 100 + _xp(c, pid) * 10
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
    xi = _tie_pass(c, list(best), squad, incoming)
    best_s = sum(score[p] for p in xi)
    sub_gk = next(p for p in pos[R.GK] if p not in xi)
    rest = sorted((p for p in squad if p not in xi and by[p]["element_type"] != R.GK),
                  key=lambda p: (-score[p], squad.index(p)))
    bench = [sub_gk, *rest]
    caps = c.ec["cap"]
    opts = G.captain_options(c, xi) or xi
    captain = sorted(opts, key=lambda p: (-caps.get(p, 0), -score[p]))[0]
    vcs = c.ec["vice"]
    vices = sorted(G.vice_options(c, xi, captain) or [p for p in xi if p != captain],
                   key=lambda p: (-vcs.get(p, 0), -caps.get(p, 0), -score[p]))
    return xi, bench, captain, vices[0], best_s


def _tie_pass(c, xi: list[int], squad: list[int], incoming: set[int]) -> list[int]:
    """Pramod's rule: a 1-board elite-start gap is a tie that the odds break.
    Swap a benched player in when he is within one board of a starter and his
    fresh xP is clearly higher, as long as no 2-board order is broken."""
    by = c.by
    def eff(p):
        return c.ec["start"][p] if (p in incoming or c.playing(p)) else -99
    changed = True
    while changed:
        changed = False
        for b_ in [p for p in squad if p not in xi and by[p]["element_type"] != R.GK]:
            if eff(b_) < 0 or c.xp(b_) is None:
                continue
            for x in [p for p in xi if by[p]["element_type"] != R.GK]:
                if c.xp(x) is None or abs(eff(b_) - eff(x)) > 1:
                    continue
                if c.xp(b_) <= c.xp(x) + G.XP_TIE_MARGIN:
                    continue
                trial = [b_ if p == x else p for p in xi]
                if R.xi_violations(trial, by):
                    continue
                bench = [p for p in squad if p not in trial and by[p]["element_type"] != R.GK]
                if any(eff(q) - eff(y) >= 2 for q in bench for y in trial
                       if by[y]["element_type"] != R.GK
                       and not R.xi_violations([q if p == y else p for p in trial], by)):
                    continue
                xi, changed = trial, True
                break
            if changed:
                break
    return xi


# --------------------------------------------------------------------------
# transfers
# --------------------------------------------------------------------------
def candidate_moves(c, single: bool = True) -> list[tuple[int, int, int]]:
    """[(out, in, gap)] transfers that pass every incoming-player rule, best
    first. single=True also requires each to be affordable and inside the club
    cap on its own (the fact table); single=False leaves money and club caps
    to the combination search, so a sale can fund or make room for a buy."""
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
            if single and st.bank + st.sell[o] - by[i]["now_cost"] < 0:
                continue
            same_club = by[i]["team"] == by[o]["team"]
            if single and not same_club and clubs.get(by[i]["team"], 0) >= R.MAX_PER_CLUB:
                continue
            gap = c.ec["start"][i] - c.out_starts(o)
            if gap < 1:
                continue
            if all(r.ok for r in G.incoming_results(c, o, i)):
                pairs.append((o, i, gap))
    pairs.sort(key=lambda p: (-p[2], -(_xp(c, p[1]) - _xp(c, p[0])), by[p[1]]["now_cost"]))
    return pairs[:TOP_PAIRS]


def best_transfers(c) -> list[tuple[int, int]]:
    """The move set (within the free transfers — never a hit) that gives the
    best XI; ties go to the bigger total start gap, then xP, then bank."""
    st, by = c.st, c.by
    k_max = min(st.ft + max(0, st.approved_hits - st.pending_hits) // R.HIT_COST,
                MAX_MOVES_SEARCHED)
    pairs = candidate_moves(c, single=k_max < 2)
    base = best_lineup(c, st.slots, set())[4]
    best, best_key = [], (base, 0, 0.0, st.bank)
    for k in range(1, k_max + 1):
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
            # spending a free transfer on the bench alone isn't worth losing
            # the rolled transfer, unless they're about to be wasted (cap)
            if lineup <= base and st.ft < R.FT_CAP:
                continue
            if k > st.ft and lineup <= best_key[0]:
                continue                  # an approved hit must improve the XI
            key = (lineup, sum(p[2] for p in combo),
                   round(sum(_xp(c, i) - _xp(c, o) for o, i, _ in combo), 3), bank)
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
                 f"          {_xp(c, p):.2f}")
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
    if getattr(args, "msg_out", None):
        pathlib.Path(args.msg_out).unlink(missing_ok=True)   # never leave an old stamped message
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
        banner = (f"REPLAY of a saved snapshot ({F.fmt_et(b.now)}) — not for sending.\n"
                  if args.bundle else "")
        if args.msg_out:
            pathlib.Path(args.msg_out).write_text(banner + rep.message + "\n")
        print(banner + rep.message)
        return 0 if rep.passed else 2
    except MissingInput as ex:
        print(f"INPUTS MISSING: {ex}")
        return 3
    except Exception as ex:
        print(f"INTERNAL ERROR: {type(ex).__name__}: {ex}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
