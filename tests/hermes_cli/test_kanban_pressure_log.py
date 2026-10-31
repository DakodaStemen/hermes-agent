"""The dispatcher's memory-pressure guard logs level changes, not every tick.

A host sitting at ``elevated`` for hours logged one WARNING per dispatcher tick, the largest repeated
non-recovery source in a root ``errors.log`` (805 lines in ~6 days of retained logs).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_pressure_log as plog

GIB = 1024 * 1024  # KiB per GiB
_ELEVATED = {"mem_available_kib": 100 * 1024, "mem_total_kib": GIB}
_OK = {"mem_available_kib": GIB // 2, "mem_total_kib": GIB}


@pytest.fixture(autouse=True)
def _fresh_state():
    plog._reset_for_tests()
    yield
    plog._reset_for_tests()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "memory pressure" in r.getMessage()]


def test_a_persisting_level_warns_once_then_reminds_after_the_interval():
    now = [1000.0]
    clock = lambda: now[0]
    assert plog.note_pressure("elevated", clock=clock) is not None
    now[0] += 60
    assert plog.note_pressure("elevated", clock=clock) is None
    now[0] += plog.REMINDER_INTERVAL_S
    assert "still" in (plog.note_pressure("elevated", clock=clock) or "")
    assert plog.note_pressure("critical", clock=clock) is not None  # an escalation is news
    assert plog.note_pressure("ok", clock=clock) is None
    assert plog.note_pressure("elevated", clock=clock) is not None  # re-entry after clearing is news


def test_consecutive_elevated_ticks_log_one_warning(kanban_home, all_assignees_spawnable, monkeypatch, caplog):
    sample = {"value": _ELEVATED}
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: sample["value"])
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with caplog.at_level(logging.DEBUG, logger="hermes_cli.kanban_db"), kbc.connect() as conn:
        for title in ("a", "b", "c", "d", "e", "f"):
            kb.create_task(conn, title=title, assignee="alice")
        results = [kbd.dispatch_once(conn, spawn_fn=fake_spawn) for _ in range(3)]
        assert [r.memory_pressure for r in results] == ["elevated"] * 3  # the guard still applies
        assert len(_warnings(caplog)) == 1, _warnings(caplog)

        sample["value"] = _OK
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        sample["value"] = _ELEVATED
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
    assert len(_warnings(caplog)) == 2, _warnings(caplog)
    assert any("cleared" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
