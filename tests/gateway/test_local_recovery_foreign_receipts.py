"""Recovery summarizes receipts another profile home owns instead of refusing each one (audit P1-10).

A local session id is content-addressed by its creating home, so a receipt left in a ``state.db``
under another home (a profile renamed before rename rekeyed receipts) can never be restored there.
Every boot and hot-profile add refused all of them again, one WARNING each: 140 such receipts on
one host produced ~15k of the ~22.7k WARNING lines in a retained root ``errors.log``.
"""
import json
import logging

import pytest

from tests.gateway.test_local_recovery_pre_profile_origin import _served_authority


def _rehome_receipt(db, sid, home):
    """Make *sid*'s stored receipt name *home* as its owner, as a pre-rename profile left it."""
    from hermes_state_local import POLICY_PREFIX
    with db._read_ctx() as conn:
        stored = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?',
                                         (POLICY_PREFIX + sid,)).fetchone()[0])
    stored['profile_id'] = home
    db._write_sql('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(stored), POLICY_PREFIX + sid))


@pytest.mark.asyncio
async def test_foreign_receipts_get_one_summary_per_recovery_pass(tmp_path, monkeypatch, caplog):
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import recover_local_sessions, restore_local_session
    from hermes_state_runtime import RuntimeStoreError

    served, db, secondary = await _served_authority(tmp_path, monkeypatch, 'first')
    actor = Principal('owner', str(secondary), frozenset({'session:create', 'session:read'}), 'socket')
    refs = [create_local_session(served, actor, {'request_id': f'r{i}', 'source': 'gui',
                                                 'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
            for i in range(3)]
    old_home = str(secondary.parent / 'renamed-away')
    for ref in refs[:2]:
        _rehome_receipt(db, ref.session_id, old_home)

    with caplog.at_level(logging.WARNING, logger='gateway.session_local_recovery'):
        cold, _, _ = await _served_authority(tmp_path, monkeypatch, 'second', db=db)
        recover_local_sessions(cold)  # a second pass, as a hot-profile add runs
    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if 'receipt refused' in m], messages
    summaries = [m for m in messages if 'owned by another profile home' in m]
    assert len(summaries) == 2, messages                  # one per pass, not one per receipt
    assert all(f'{old_home}=2' in m for m in summaries), summaries
    assert refs[2].session_id in cold.sessions             # the owned receipt still restores
    assert refs[0].session_id not in cold.sessions

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='gateway.session_local_recovery'):
        with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
            restore_local_session(cold, refs[0].session_id)  # an explicit resume still names it
    assert any('profile_mismatch' in r.getMessage() and refs[0].session_id in r.getMessage()
               for r in caplog.records)
