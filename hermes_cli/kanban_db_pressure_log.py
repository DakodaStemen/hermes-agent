"""Edge-triggered log lines for the dispatcher's memory-pressure guard.

The guard runs every tick. On a host that sits at ``elevated`` for hours, a WARNING per tick was the
largest repeated source in ``errors.log`` (hundreds a day) while carrying no new information. The
level is still on every ``DispatchResult``; the log now records changes, plus a reminder while a
restricting level persists so a long stretch stays visible in the retained window.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

_log = logging.getLogger("hermes_cli.kanban_db")

# A level that keeps restricting spawns is re-announced at most this often.
REMINDER_INTERVAL_S = 30 * 60

_MESSAGES = {
    "critical": "kanban dispatch: system memory pressure is critical; "
                "spawning no new workers (deferred, not dropped)",
    "elevated": "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker per tick",
}

_lock = threading.Lock()
_state: dict = {"level": None, "announced_at": 0.0}


def note_pressure(level: str, *, clock: Callable[[], float] = time.monotonic) -> Optional[str]:
    """Record this tick's restricting *level* (``critical``/``elevated``) or ``ok``; return the
    message logged at WARNING, else None. Repeats inside ``REMINDER_INTERVAL_S`` log at DEBUG."""
    now = clock()
    with _lock:
        previous, announced_at = _state["level"], _state["announced_at"]
        if level not in _MESSAGES:
            _state["level"] = None
            if previous is not None:
                _log.info("kanban dispatch: system memory pressure cleared (was %s)", previous)
            return None
        if level == previous and now - announced_at < REMINDER_INTERVAL_S:
            _log.debug(_MESSAGES[level])
            return None
        _state["level"], _state["announced_at"] = level, now
    message = _MESSAGES[level] if level != previous else f"{_MESSAGES[level]} (still, since last notice)"
    _log.warning(message)
    return message


def _reset_for_tests() -> None:
    with _lock:
        _state["level"], _state["announced_at"] = None, 0.0
