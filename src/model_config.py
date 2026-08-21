"""FPL Edge — central config access. All tuning constants live in config.json
under "model" (council: no scattered literals)."""
from __future__ import annotations
import json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

_DEFAULTS = {
    "odds_blend": 0.70, "pm_blend": 0.20, "scorer_prop_blend": 0.50,
    "ep_next_weight_reliable": 0.10, "ep_next_weight_thin": 0.50,
    "decay": 0.85, "horizon": 6, "future_weight": 0.45, "bench_weight": 0.15,
    "sim_sd_scale": 1.6, "defcon_dispersion_k": 6.0, "shrinkage_mins": 900,
    "finishing_cap": 0.15, "differential_on_after_gw": 8,
    "lineup_blend": 0.5, "lineup_start_mins": 82, "lineup_bench_mins": 25,
    "fix_drift_threshold": 0.80,
}


def full() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text())
    except Exception:
        return {}


def model(key: str):
    return full().get("model", {}).get(key, _DEFAULTS.get(key))
