"""Synthetic elite boards for the public test fixtures.

The real boards come from Pramod's paid Fantasy Football Fix account, and this
repository is public, so fixtures never contain them. Instead each fixture
holds shuffled boards that keep, for every player, exactly the same number of
elite OWNS, STARTS (first 11), bench spots, captaincies and vice-captaincies,
and keep every board legal (2/5/5/3, legal XI, club cap 3, GK first on the
bench). Everything the advisor and validator use is a count, so replays behave
the same; no real manager's squad is published.

  python3 scripts/synthesize_boards.py REAL_ELITE.json BOOTSTRAP.json OUT.json [--seed N]
"""
from __future__ import annotations
import argparse, json, pathlib, random, sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _violations(boards, club):
    v = 0
    for b in boards:
        ids = b["player_ids"]
        v += len(ids) - len(set(ids))
        for n in Counter(club[p] for p in ids).values():
            v += max(0, n - 3)
    return v


def synthesize(elite: dict, by_id: dict, seed: int = 7, tries: int = 50) -> dict:
    import facts as F
    rng = random.Random(seed)
    real = [t for t in elite["teams"] if not F.board_violations(t, by_id)]
    club = {p: by_id[p]["team"] for t in real for p in t["player_ids"]}
    etype = {p: by_id[p]["element_type"] for t in real for p in t["player_ids"]}
    for _ in range(tries):
        boards = [dict(player_ids=list(t["player_ids"])) for t in real]
        # slots grouped by (position, XI or bench); shuffle players within groups
        groups: dict = {}
        for bi, t in enumerate(boards):
            for si, p in enumerate(t["player_ids"]):
                groups.setdefault((etype[p], si < 11), []).append((bi, si))
        for slots in groups.values():
            vals = [boards[bi]["player_ids"][si] for bi, si in slots]
            rng.shuffle(vals)
            for (bi, si), p in zip(slots, vals):
                boards[bi]["player_ids"][si] = p
        # repair duplicates / club caps by swapping within the same group
        keys = list(groups)
        cur = _violations(boards, club)
        for _ in range(20000):
            if cur == 0:
                break
            g = groups[rng.choice(keys)]
            (b1, s1), (b2, s2) = rng.sample(g, 2)
            if b1 == b2:
                continue
            a, b = boards[b1]["player_ids"][s1], boards[b2]["player_ids"][s2]
            boards[b1]["player_ids"][s1], boards[b2]["player_ids"][s2] = b, a
            new = _violations(boards, club)
            if new <= cur:
                cur = new
            else:
                boards[b1]["player_ids"][s1], boards[b2]["player_ids"][s2] = a, b
        if cur:
            continue
        # captains and vices: same counts, each on a board that starts him
        ok = True
        for role in ("captain_id", "vice_id"):
            need = [t.get(role) for t in real if t.get(role)]
            rng.shuffle(need)
            free = list(range(len(boards)))
            for p in sorted(need, key=lambda p: sum(p in b["player_ids"][:11] for b in boards)):
                cands = [bi for bi in free if p in boards[bi]["player_ids"][:11]
                         and boards[bi].get("captain_id") != p]
                if not cands:
                    ok = False
                    break
                bi = rng.choice(cands)
                boards[bi][role] = p
                free.remove(bi)
            if not ok:
                break
        if not ok:
            continue
        if any(F.board_violations(b, by_id) for b in boards):
            continue
        real_sets = [set(t["player_ids"]) for t in real]
        if any(set(b["player_ids"]) in real_sets for b in boards):
            continue
        for b, t in zip(boards, real):           # board metadata, not tied to any squad
            for k in ("chip", "last_updated"):
                if k in t:
                    b[k] = t[k]
        out = dict(gw=elite["gw"], updated=elite.get("updated"), n_teams=len(boards),
                   source="synthetic test boards (counts preserved; no real squads)",
                   teams=[dict(manager=f"S{k:02d}", **b) for k, b in enumerate(boards, 1)])
        return out
    raise RuntimeError("could not build legal synthetic boards")


def same_counts(a: dict, b: dict) -> bool:
    import facts as F
    ca, cb = F.elite_counts(a), F.elite_counts(b)
    return all(ca[k] == cb[k] for k in ("n", "own", "start", "bench", "cap", "vice"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("elite")
    ap.add_argument("bootstrap")
    ap.add_argument("out")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    elite = json.loads(pathlib.Path(a.elite).read_text())
    bs = json.loads(pathlib.Path(a.bootstrap).read_text())
    by = {e["id"]: e for e in bs["elements"]}
    syn = synthesize(elite, by, a.seed)
    import facts as F
    legal = [t for t in elite["teams"] if not F.board_violations(t, by)]
    if not same_counts(dict(teams=legal), syn):
        raise SystemExit("synthetic boards changed a count — not written")
    pathlib.Path(a.out).write_text(json.dumps(syn, ensure_ascii=False, separators=(",", ":")))
    real = [set(t["player_ids"]) for t in elite["teams"]]
    overlap = max(len(set(t["player_ids"]) & r) for t in syn["teams"] for r in real)
    print(f"wrote {a.out}: {syn['n_teams']} boards; max overlap with any real board {overlap}/15")


if __name__ == "__main__":
    main()
