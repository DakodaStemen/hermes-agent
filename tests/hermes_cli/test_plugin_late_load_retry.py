"""A plugin load that overruns ``plugins.load_timeout_seconds`` but then finishes was slow, not hung.

Before the fix its registrations were refused and the plugin stayed disabled until a restart (seen live:
hermes_otel, toolaria, plan-mode, web-search-plus finishing 1-23 s after a 10 s deadline while the host
was swapping at gateway start), and every refused ``register_*`` call logged its own WARNING.
Separately, ``on_session_end`` had no call identity, so sessions closing together shared one gate key.
"""

import logging
import sys
import threading

import pytest
import hermes_yaml as yaml

from hermes_cli.plugins import PluginManager
from hermes_cli.plugins_dispatch import _hook_call_identity
from tests.waiting import wait_until


def _write_plugin(base, name, body):
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump({"name": name, "version": "0.1.0"}))
    (plugin_dir / "__init__.py").write_text(body)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    (tmp_path / "empty-bundled").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "empty-bundled"))
    return home


@pytest.fixture
def probe():
    state = {"gates": [threading.Event()], "imports": 0, "registers": 0, "finished": 0}
    setattr(sys, "_late_probe", state)
    yield state
    for gate in state["gates"]:
        gate.set()
    delattr(sys, "_late_probe")


# Module body counts imports; register() call N blocks on gates[N-1] when that gate exists (a slow load).
_SLOW_PLUGIN = """
import sys
probe = sys._late_probe
probe["imports"] += 1

def register(ctx):
    probe["registers"] += 1
    if probe["registers"] <= len(probe["gates"]):
        probe["gates"][probe["registers"] - 1].wait(10)
    for hook in ("pre_llm_call", "post_llm_call", "on_session_end"):
        ctx.register_hook(hook, lambda **kw: None)
    probe["finished"] += 1
"""


def _configure(home, names, timeout):
    (home / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": list(names), "load_timeout_seconds": timeout}}))


def _loader_threads_idle(key):
    """No load worker or late-retry thread for ``key`` is alive (late callbacks run before exit)."""
    names = {f"plugin-load:{key}", f"plugin-late-retry:{key}"}
    return not any(t.name in names for t in threading.enumerate())


def test_slow_load_is_reloaded_once_it_finishes(hermes_home, probe, caplog):
    _write_plugin(hermes_home / "plugins", "slow_otel", _SLOW_PLUGIN)
    _configure(hermes_home, ["slow_otel"], 0.3)
    mgr = PluginManager()
    events = []
    mgr.on_plugin_loaded(events.append)
    with caplog.at_level(logging.DEBUG, logger="hermes_cli.plugins"):
        mgr.discover_and_load()
        assert not mgr._plugins["slow_otel"].enabled
        assert "load timed out" in (mgr._plugins["slow_otel"].error or "")

        probe["gates"][0].set()  # the load was only slow: let it finish
        wait_until(lambda: mgr._plugins["slow_otel"].enabled, what="late re-load enabled the plugin")

    assert len(mgr._hooks.get("on_session_end", [])) == 1
    assert len(mgr._hooks.get("pre_llm_call", [])) == 1
    assert probe["imports"] == 1  # module body reused, not re-executed
    assert probe["registers"] == 2
    assert any(s["key"] == "slow_otel" for batch in events for s in batch)  # gateway re-wire listener fired
    late = [r for r in caplog.records if "after its load timed out" in r.getMessage()]
    assert [r.levelno for r in late] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    assert any("re-loaded and enabled" in r.getMessage() for r in caplog.records)


def test_reload_is_single_shot(hermes_home, probe):
    """A re-load that overruns again is not retried a second time (no retry loop under sustained pressure)."""
    probe["gates"].append(threading.Event())  # register() is slow on the re-load as well
    _write_plugin(hermes_home / "plugins", "always_slow", _SLOW_PLUGIN)
    _configure(hermes_home, ["always_slow"], 0.3)
    mgr = PluginManager()
    mgr.discover_and_load()
    first = mgr._plugins["always_slow"]
    probe["gates"][0].set()
    second = wait_until(lambda: mgr._plugins["always_slow"] is not first and mgr._plugins["always_slow"],
                        what="the single re-load recorded its own timeout")
    probe["gates"][1].set()  # the re-load's abandoned worker finishes late too
    wait_until(lambda: probe["finished"] == 2 and _loader_threads_idle("always_slow"), what="workers done")
    assert probe["registers"] == 2
    assert mgr._plugins["always_slow"] is second and not second.enabled
    assert "retried once" not in (second.error or "")


def test_superseded_timeout_record_is_not_reloaded(hermes_home, probe):
    """If force re-discovery replaced the timed-out record before the late finish, nothing is re-loaded."""
    _write_plugin(hermes_home / "plugins", "slow_otel", _SLOW_PLUGIN)
    _configure(hermes_home, ["slow_otel"], 0.3)
    mgr = PluginManager()
    mgr.discover_and_load()
    stale = mgr._plugins["slow_otel"]
    _configure(hermes_home, [], 0.3)
    mgr.discover_and_load(force=True)  # plugin disabled meanwhile
    probe["gates"][0].set()
    wait_until(lambda: probe["finished"] == 1 and _loader_threads_idle("slow_otel"), what="late worker done")
    assert mgr._plugins.get("slow_otel") is not stale
    assert mgr._hooks.get("on_session_end", []) == []
    assert probe["registers"] == 1


def test_on_session_end_identity_uses_session_id():
    assert _hook_call_identity({"session_id": "s1", "completed": False}, "on_session_end") == "s1"
    assert _hook_call_identity({"session_id": "s1", "turn_id": "t9"}, "on_session_end") == "t9"


def _end_two(monkeypatch, sid_a, sid_b):
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 5.0)
    hold, started, calls = threading.Event(), threading.Event(), []

    def recorder(**kwargs):
        calls.append(kwargs["session_id"])
        started.set()
        hold.wait(timeout=10.0)

    mgr = PluginManager()
    mgr._hooks["on_session_end"] = [recorder]
    fire = lambda sid: mgr.invoke_hook("on_session_end", session_id=sid, completed=False, interrupted=True)
    a = threading.Thread(target=fire, args=(sid_a,), daemon=True)
    a.start()
    assert started.wait(5.0)
    b = threading.Thread(target=fire, args=(sid_b,), daemon=True)
    b.start()
    wait_until(lambda: len(calls) == 2 or not b.is_alive(), what="second close ran or was skipped")
    hold.set()
    a.join(5.0)
    b.join(5.0)
    return calls


def test_sessions_closing_together_each_run_on_session_end(monkeypatch):
    assert sorted(_end_two(monkeypatch, "sess-a", "sess-b")) == ["sess-a", "sess-b"]


def test_same_session_end_still_deduplicated(monkeypatch):
    assert _end_two(monkeypatch, "sess-a", "sess-a") == ["sess-a"]
