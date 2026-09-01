"""FPL Edge — canonical squad state (council v4: kill the squad-state fiction).

Single authority for "what team does Pramod actually own": the FPL API's own
record of his picks, falling back to data/actual_entry_gw1.json only while the
API is in post-deadline maintenance. Everything downstream (transfer planning,
briefings, ledger counterfactual) MUST source the squad from here — never from
chip_plan.json, solver output, or memory.

Also maintains data/purchases.json {element_id: purchase_price_tenths} so the
50%-sell-on rule uses real purchase prices from the moment of first sight.

API REALITY (lesson 2026-08-31): the FPL API HIDES transfers made for the
upcoming GW until its deadline passes (anti-copy privacy). /entry/{id}/transfers/
and picks reflect only the LAST deadline. User-made pre-deadline moves therefore
arrive via data/pending_moves.json (written from user reports / state store) and
are overlaid here until the API confirms them post-deadline, at which point the
overlay self-expires and is reconciled against /transfers/.
"""
from __future__ import annotations
import json, pathlib
import fpl_api

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _fallback_manual() -> dict | None:
    f = DATA / "actual_entry_gw1.json"
    if not f.exists():
        return None
    manual = json.loads(f.read_text())
    bs = fpl_api.bootstrap()
    short = {t["id"]: t["short_name"] for t in bs["teams"]}
    ids, unresolved = [], []
    for name in manual["squad15"]:
        cands = [e for e in bs["elements"] if e["web_name"] == name]
        if len(cands) == 1:
            ids.append(cands[0]["id"])
        elif len(cands) > 1:
            # disambiguate by highest ownership (the famous player)
            cands.sort(key=lambda e: -float(e["selected_by_percent"]))
            ids.append(cands[0]["id"])
        else:
            unresolved.append(name)
    cap = next((e["id"] for e in bs["elements"]
                if e["web_name"] == manual.get("captain")), None)
    return dict(source="manual_fallback", gw=manual["gw"], element_ids=ids,
                captain=cap, chip=manual.get("chip"), bank=0.0,
                free_transfers=1, unresolved=unresolved)


def _resolve_name(name: str, bs) -> int | None:
    """web_name -> element id; ties broken by ownership (the famous one)."""
    cands = [e for e in bs["elements"] if e["web_name"] == name]
    if not cands:
        return None
    cands.sort(key=lambda e: -float(e["selected_by_percent"]))
    return cands[0]["id"]


def _apply_pending(state: dict, bs) -> dict:
    """Overlay user-reported pre-deadline moves the API cannot show yet.

    pending_moves.json: {"gw": N, "moves": [{"out": "Mbeumo", "in": "Cherki"}],
                         "free_transfers_left": 1}
    Applies only while the move's GW is still upcoming (API picks gw < N);
    once the API has caught up post-deadline the file is reconciled against
    /transfers/ and deleted (self-expiring — never a stale second truth).
    """
    f = DATA / "pending_moves.json"
    if not f.exists():
        return state
    try:
        pend = json.loads(f.read_text())
    except Exception:
        return state
    if state["gw"] >= pend.get("gw", 0):
        # API has caught up — reconcile then retire the overlay
        try:
            import requests
            tr = requests.get(
                f"https://fantasy.premierleague.com/api/entry/{pend['team_id']}/transfers/",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=20).json()
            confirmed = {(t["element_out"], t["element_in"]) for t in tr
                         if t["event"] == pend.get("gw")}
            true_cost = {t["element_in"]: t["element_in_cost"] for t in tr
                         if t["event"] == pend.get("gw")}
            for m in pend.get("moves", []):
                pair = (_resolve_name(m["out"], bs), _resolve_name(m["in"], bs))
                if pair not in confirmed:
                    state.setdefault("warnings", []).append(
                        f"pending move {m['out']}->{m['in']} NOT in API post-deadline")
                elif pair[1] in true_cost:
                    # correct the purchase book with the true buy price
                    book_f = DATA / "purchases.json"
                    book = json.loads(book_f.read_text()) if book_f.exists() else {}
                    book[str(pair[1])] = true_cost[pair[1]]
                    book_f.write_text(json.dumps(book))
        except Exception:
            pass
        f.unlink(missing_ok=True)
        return state
    applied = []
    for m in pend.get("moves", []):
        out_id, in_id = _resolve_name(m["out"], bs), _resolve_name(m["in"], bs)
        if out_id in state["element_ids"] and in_id and in_id not in state["element_ids"]:
            sell = selling_price(out_id, bs)
            book_f = DATA / "purchases.json"
            book = json.loads(book_f.read_text()) if book_f.exists() else {}
            # buy price = user-stated, else first-seen booked price, else today's
            now_in = next(e["now_cost"] for e in bs["elements"] if e["id"] == in_id)
            buy_tenths = int(m.get("in_cost_tenths") or book.get(str(in_id)) or now_in)
            buy = buy_tenths / 10.0
            state["element_ids"] = [in_id if x == out_id else x
                                    for x in state["element_ids"]]
            state["bank"] = round(state["bank"] + sell - buy, 1)
            book.pop(str(out_id), None)
            book[str(in_id)] = buy_tenths
            book_f.write_text(json.dumps(book))
            if state.get("captain") == out_id:
                state["captain"] = None
            if state.get("vice") == out_id:
                state["vice"] = None
            applied.append(f"{m['out']}->{m['in']}")
    if applied:
        state["source"] += "+pending_user_moves"
        state["pending_applied"] = applied
        if "free_transfers_left" in pend:
            state["free_transfers"] = pend["free_transfers_left"]
    return state


def current(team_id: int) -> dict:
    """Canonical squad state. API-first; manual fallback; loud about which."""
    bs = fpl_api.bootstrap()
    ev = next((e for e in bs["events"] if e.get("is_current")), None) \
        or next((e for e in bs["events"] if e.get("is_next")), None)
    gw = ev["id"] if ev else 1
    for try_gw in (gw, gw - 1):
        if try_gw < 1:
            continue
        try:
            p = fpl_api.entry_picks(team_id, try_gw)
            ent = fpl_api.entry(team_id)
            state = dict(
                source="fpl_api", gw=try_gw,
                element_ids=[x["element"] for x in p["picks"]],
                captain=next((x["element"] for x in p["picks"] if x["is_captain"]), None),
                vice=next((x["element"] for x in p["picks"] if x["is_vice_captain"]), None),
                chip=p.get("active_chip"),
                bank=(ent.get("last_deadline_bank") or 0) / 10.0,
                value=(ent.get("last_deadline_value") or 0) / 10.0,
                free_transfers=1, unresolved=[])
            _update_purchases(state["element_ids"], bs)
            return _apply_pending(state, bs)
        except Exception:
            continue
    fb = _fallback_manual()
    if fb:
        _update_purchases(fb["element_ids"], bs)
        return fb
    return dict(source="none", gw=gw, element_ids=[], captain=None,
                chip=None, bank=0.0, free_transfers=1,
                unresolved=["NO SQUAD STATE AVAILABLE"])


def _update_purchases(ids: list, bs) -> dict:
    f = DATA / "purchases.json"
    book = json.loads(f.read_text()) if f.exists() else {}
    now = {e["id"]: e["now_cost"] for e in bs["elements"]}
    for pid in ids:
        book.setdefault(str(pid), now.get(pid))
    # drop players no longer owned (sold) so re-buys re-book at new price
    book = {k: v for k, v in book.items() if int(k) in set(ids)}
    f.write_text(json.dumps(book))
    return book


def selling_price(pid: int, bs) -> float:
    """FPL sell rule: purchase + floor(profit/2), full loss passed through."""
    f = DATA / "purchases.json"
    book = json.loads(f.read_text()) if f.exists() else {}
    now = next((e["now_cost"] for e in bs["elements"] if e["id"] == pid), None)
    buy = book.get(str(pid), now)
    if now is None or buy is None:
        return 0.0
    sell = buy + max(0, (now - buy) // 2) if now > buy else now
    return sell / 10.0


if __name__ == "__main__":
    import model_config as mc
    st = current(mc.full().get("team_id"))
    bs = fpl_api.bootstrap()
    byid = {e["id"]: e for e in bs["elements"]}
    short = {t["id"]: t["short_name"] for t in bs["teams"]}
    print(f"source: {st['source']} | gw: {st['gw']} | chip: {st.get('chip')} | "
          f"bank £{st['bank']:.1f}m | FTs {st['free_transfers']}")
    for pid in st["element_ids"]:
        e = byid.get(pid)
        tag = " (C)" if pid == st.get("captain") else (" (V)" if pid == st.get("vice") else "")
        print(f"  {e['web_name']:16} {short[e['team']]}{tag}" if e else f"  UNKNOWN {pid}")
    if st["unresolved"]:
        print("UNRESOLVED:", st["unresolved"])
