"""FPL Edge — price-change window + official predictor (2026/27).

RULE (verified 2026-09-07 from premierleague.com news/4680462 and FFS
2026/08/22): prices change ONCE A DAY AT 00:00 UK TIME (Europe/London), not
the old 01:30 GMT / 02:30 BST. In ET that is 7:00 PM while both zones are on
summer time, 8:00 PM between the UK clock change (last Sun of Oct) and the US
one (first Sun of Nov), then 7:00 PM EST. Never hard-code an ET hour — call
next_window().

The official FPL API now carries the predictor on every element:
  price_change_percent      progress now (>=100 => change expected)
  price_change_hourly_rate  current drift per hour (managers net in/out)
  price_change_projections  [{offset:0 (next window), projected_percent, likelihood 1-5}, ...]
  price_change_locked_until / price_change_calibrating
Positive percent = toward a RISE, negative = toward a FALL.
"""
from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
ET = ZoneInfo("America/New_York")


def next_window(now: dt.datetime | None = None) -> dict:
    """Next 00:00 UK price-change moment, expressed in UK, ET and UTC."""
    now = now or dt.datetime.now(dt.timezone.utc)
    now_uk = now.astimezone(UK)
    nxt = (now_uk + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if now_uk.hour == 0 and now_uk.minute == 0:
        nxt = now_uk.replace(second=0, microsecond=0)
    return dict(uk=nxt.isoformat(), et=nxt.astimezone(ET).strftime("%a %b %d %-I:%M %p ET"),
                utc=nxt.astimezone(dt.timezone.utc).isoformat(),
                hours_away=round((nxt - now_uk).total_seconds() / 3600, 2))


def predictor(bs: dict, names: list[str] | None = None, threshold: float = 60.0) -> dict:
    """Rise/fall candidates for the NEXT window from the official predictor.

    Returns {'window': next_window(), 'rising': [...], 'falling': [...]} where
    each row = name, team, price, pct_now, proj_next (offset 0), proj_next2,
    likelihood (1-5), hourly_rate. If `names` is given, only those players;
    otherwise everyone whose |proj_next| >= threshold.
    """
    short = {t["id"]: t["short_name"] for t in bs["teams"]}
    rows = []
    for e in bs["elements"]:
        if names and e["web_name"] not in names:
            continue
        try:
            pct = float(e.get("price_change_percent") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        proj = e.get("price_change_projections") or []
        p0 = next((p for p in proj if p.get("offset") == 0), {})
        p1 = next((p for p in proj if p.get("offset") == 1), {})
        try:
            proj0 = float(p0.get("projected_percent") or pct)
            proj1 = float(p1.get("projected_percent") or proj0)
        except (TypeError, ValueError):
            proj0, proj1 = pct, pct
        if not names and abs(proj0) < threshold and abs(proj1) < 100:
            continue
        rows.append(dict(name=e["web_name"], team=short[e["team"]], price=e["now_cost"] / 10,
                         pct_now=pct, proj_next=proj0, proj_next2=proj1,
                         likelihood=p0.get("likelihood"), hourly_rate=e.get("price_change_hourly_rate"),
                         locked_until=e.get("price_change_locked_until"),
                         calibrating=e.get("price_change_calibrating"),
                         sel=float(e.get("selected_by_percent") or 0)))
    rising = sorted([r for r in rows if r["proj_next"] > 0], key=lambda r: -r["proj_next"])
    falling = sorted([r for r in rows if r["proj_next"] < 0], key=lambda r: r["proj_next"])
    return dict(window=next_window(), rising=rising, falling=falling)


def fmt(row: dict) -> str:
    tag = "RISE" if row["proj_next"] > 0 else "FALL"
    return (f"{row['name']} ({row['team']} £{row['price']}) {tag} — now {row['pct_now']:+.0f}%, "
            f"next window {row['proj_next']:+.0f}% (L{row['likelihood']}), "
            f"following {row['proj_next2']:+.0f}%")


if __name__ == "__main__":
    import fpl_api
    bs = fpl_api.bootstrap()
    out = predictor(bs)
    w = out["window"]
    print(f"next price window: {w['et']} ({w['uk'][:16]} UK, {w['hours_away']}h away)")
    print("RISING:"); [print("  " + fmt(r)) for r in out["rising"][:15]]
    print("FALLING:"); [print("  " + fmt(r)) for r in out["falling"][:15]]
