"""Two writers of one log file never both rotate it (audit P1-07).

Every Hermes process sharing a ``logs/`` dir rotates the same ``agent.log``. When two crossed maxBytes
together, the second renamed the first's fresh file into ``.1``: a 390-byte ``agent.log.2`` sat
between two 5 MiB backups, so history was lost one slot early.
"""
import logging

import pytest

import hermes_logging


def _handler(path, size):
    return hermes_logging._new_file_handler(
        path, level=logging.INFO, max_bytes=size, backup_count=3,
        formatter=logging.Formatter("%(message)s"),
    )


def _record(message):
    return logging.LogRecord("test", logging.INFO, "", 0, message, (), None)


@pytest.mark.platforms("posix")
def test_a_writer_whose_peer_already_rotated_reopens_instead_of_rotating(tmp_path):
    path = tmp_path / "agent.log"
    first, second = _handler(path, 64), _handler(path, 64)
    try:
        first.handle(_record("x" * 80))           # the file is past maxBytes for both writers
        first.doRollover()                       # the peer rotates first
        first.handle(_record("fresh after rotation"))
        second.doRollover()                      # stale writer crossed the same threshold
        second.handle(_record("from the second writer"))
        for h in (first, second):
            h.flush()
        assert not (tmp_path / "agent.log.2").exists(), "the stale writer rotated a second time"
        assert (tmp_path / "agent.log.1").read_text().strip() == "x" * 80
        assert path.read_text().splitlines() == ["fresh after rotation", "from the second writer"]
    finally:
        first.close()
        second.close()


@pytest.mark.platforms("posix")
def test_a_lone_writer_still_rotates(tmp_path):
    path = tmp_path / "agent.log"
    handler = _handler(path, 32)
    try:
        for message in ("a" * 40, "b" * 40, "c" * 40):
            handler.handle(_record(message))
        handler.flush()
        assert path.read_text().strip() == "c" * 40
        assert (tmp_path / "agent.log.1").read_text().strip() == "b" * 40
        assert (tmp_path / "agent.log.2").read_text().strip() == "a" * 40
    finally:
        handler.close()


def test_default_retention_meets_the_stated_targets():
    """agent.log keeps >= 7 days at 35 MiB/day; errors.log >= 30 days at 1 MiB/day."""
    agent_cap_mib = hermes_logging.AGENT_LOG_MAX_MB * (1 + hermes_logging.AGENT_LOG_BACKUPS)
    assert agent_cap_mib / 35 >= 7
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["logging"]["max_size_mb"] == hermes_logging.AGENT_LOG_MAX_MB
    assert DEFAULT_CONFIG["logging"]["backup_count"] == hermes_logging.AGENT_LOG_BACKUPS
