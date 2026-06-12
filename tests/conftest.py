"""Test harness: every test runs against a throwaway database with the
deterministic MockProvider — fully offline, zero API tokens, deterministic."""
from __future__ import annotations

import shutil

import pytest

from cardinal.core import config as cardinal_config
from cardinal.core import db as cardinal_db


@pytest.fixture(autouse=True)
def cardinal_env(tmp_path, monkeypatch):
    """Isolated DB + MockProvider for every test."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CARDINAL_USE_MOCK", "true")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    cfg = cardinal_config.get_config(refresh=True)
    monkeypatch.setattr(cfg, "db_path", tmp_path / "cardinal_test.db")
    monkeypatch.setattr(cfg, "backups_dir", tmp_path / "backups")
    cfg.backups_dir.mkdir(exist_ok=True)
    cardinal_db.init_db()
    yield cfg
    cardinal_config.get_config(refresh=True)


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch, cardinal_env):
    """Copy data/ into tmp so tests can mutate items.json etc. safely."""
    src = cardinal_config.PROJECT_ROOT / "data"
    dst = tmp_path / "data"
    shutil.copytree(src, dst)
    monkeypatch.setattr(cardinal_env, "data_dir", dst)
    from cardinal.modules.taboo_index import get_taboo_index

    get_taboo_index(refresh=True)
    yield dst
    get_taboo_index(refresh=True)


@pytest.fixture
def fake_test_pass(monkeypatch):
    """Simulate the gate's post-patch regression run PASSING (avoids nested
    pytest processes inside the suite)."""
    import subprocess as _subprocess

    from cardinal import sub_process

    class _OK:
        returncode = 0
        stdout = "simulated: all tests passed"
        stderr = ""

    monkeypatch.setattr(sub_process.subprocess, "run", lambda *a, **k: _OK())
    return _OK


@pytest.fixture
def fake_test_fail(monkeypatch):
    """Simulate the gate's post-patch regression run FAILING."""
    from cardinal import sub_process

    class _Fail:
        returncode = 1
        stdout = "simulated: 1 failed"
        stderr = ""

    monkeypatch.setattr(sub_process.subprocess, "run", lambda *a, **k: _Fail())
    return _Fail


class ForcedRandom:
    """Deterministic rng stub: random() yields the scripted sequence."""

    def __init__(self, sequence, uniform_value=1.0):
        self._seq = list(sequence)
        self._uniform = uniform_value

    def random(self):
        return self._seq.pop(0) if self._seq else 0.99

    def uniform(self, a, b):
        return self._uniform if a <= self._uniform <= b else (a + b) / 2

    def choice(self, seq):
        return seq[0]

    def choices(self, population, weights=None, k=1):
        return [population[0]]

    def randrange(self, n):
        return 0

    def gauss(self, mu, sigma):
        return mu

    def shuffle(self, x):
        pass
