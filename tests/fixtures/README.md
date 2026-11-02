# Frozen regression fixtures

Each folder is a `bundle.from_dir()` snapshot built by `scripts/freeze_fixture.py`.
Public FPL API data is trimmed to the fields the code uses.

**Elite boards are synthetic.** The real boards come from Pramod's paid
Fantasy Football Fix account and this repository is public, so `elite.json`
holds shuffled boards (`scripts/synthesize_boards.py`) that keep every
player's elite own / start / bench / captain / vice counts and keep every
board legal. The advisor and validator only use those counts, so replays
behave exactly as they did on the real boards. No real manager's squad is here.

| Fixture | Moment | Used for |
|---|---|---|
| `gw9_pre_deadline` | Fri Oct 30 2026, 4:10 PM ET (GW9 deadline Sat 7:00 AM ET) | The Dubravka error replay; FT = 2 after the GW4 wildcard |
| `gw10_nov2` | Mon Nov 2 2026, 8:30 AM ET | GW10 dry run: both keepers are backups, Kinsky fix, real selling prices (993) |

The GW9 boards were synthesized after correcting one real board where the
old name matcher had mapped Fix's bare "Muñoz" to Victor Munoz (MID, id 377)
instead of Daniel Muñoz (DEF, id 201). Fix boards are now mapped by FPL code.
