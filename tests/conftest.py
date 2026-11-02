"""Test harness: no network, no writes into data/, frozen fixtures only."""
from __future__ import annotations
import copy, pathlib, socket

import pytest

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def deny(*a, **k):
        raise RuntimeError("network access is blocked in tests")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture(autouse=True)
def _tmp_data(monkeypatch, tmp_path):
    import bundle, fpl_api, fix_data
    for mod, attr in ((bundle, "DATA"), (fpl_api, "CACHE"), (fix_data, "DATA")):
        monkeypatch.setattr(mod, attr, tmp_path)


_cache: dict = {}


def load(name: str):
    """A fresh (deep-copied) frozen Bundle, safe to mutate in a test."""
    import bundle
    if name not in _cache:
        _cache[name] = bundle.from_dir(FIX / name)
    b = copy.deepcopy(_cache[name])
    b.__dict__.pop("_by_id", None)
    return b


@pytest.fixture
def gw9():
    return load("gw9_pre_deadline")


@pytest.fixture
def gw10():
    return load("gw10_nov2")


def pid(b, name: str) -> int:
    """Element id by unique web_name or "First Second" (ambiguity fails)."""
    hits = [e["id"] for e in b.bootstrap["elements"] if e["web_name"] == name] or \
        [e["id"] for e in b.bootstrap["elements"] if f"{e['first_name']} {e['second_name']}" == name]
    assert len(hits) == 1, f"{name!r} is ambiguous or unknown: {hits}"
    return hits[0]
