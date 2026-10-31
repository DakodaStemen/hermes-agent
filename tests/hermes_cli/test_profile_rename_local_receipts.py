"""A renamed profile's LOCAL creation receipts stay restorable (t_4813d387).

``hermes profile rename`` rekeys routing/session identity, but a local receipt's ``profile_id`` is
the creating home and its session id derives from it (``local_identity``), so ``owned_receipt``
refused every pre-rename local chat with ``profile_mismatch`` forever. The rename now re-mints those
receipts with a ``legacy_session_id`` binding (same chat id, nothing new minted).
"""
import json
import os
import time

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner

POLICY = 'gateway.local_policy.v1:'
BACKUP = 'gateway.local_policy.backup.v1:alpha:beta:'  # one backup per rename: <old>:<new>:<sid>
ARCHIVED = 'gateway.local_policy.archived.v1:'
LEGACY = 'gateway.local_legacy.v1:'


async def _create_alpha_session(tmp_path, monkeypatch):
    """A local session created by served profile ``alpha``; every store closed afterwards."""
    from gateway import run
    from gateway.run import _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=('alpha',))
    process_ownership.reserve([home for _, home in homes])
    runner = _runner(root, homes)
    try:
        await initialize_gateway_runtime(runner)
        home = homes[1][1]
        authority = runner.session_authorities.for_home(home)
        actor = Principal('owner', authority.profile_id,
                          frozenset({'session:create', 'session:read', 'session:control'}), 'socket')
        with _profile_runtime_scope(home, {}):
            ref = create_local_session(authority, actor, {'request_id': 'pre-rename', 'source': 'gui',
                'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
        alpha_id = authority.profile_id
    finally:
        for owner in runner.session_authorities:
            owner.db.close()
        for _, home in homes:
            process_ownership.release(home)
    return root, ref.session_id, alpha_id


def _rename_dir(root):
    """Step 1 of ``rename_profile``: the directory (and its state.db) moves with the profile."""
    os.rename(root / 'profiles' / 'alpha', root / 'profiles' / 'beta')
    return root / 'profiles' / 'beta'


def _meta(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute('SELECT key, value FROM state_meta ORDER BY key').fetchall())
    finally:
        conn.close()


def _db(path):
    from hermes_state_registry import acquire
    return acquire(path)


def _release(db):
    from hermes_state_registry import release_or_close
    release_or_close(db)


def _rekey(old='alpha', new='beta', **kw):
    from hermes_cli.profile_identity import rekey_profile_receipts
    from hermes_cli.profiles import get_profile_dir
    db = _db(get_profile_dir(new) / 'state.db')
    try:
        return rekey_profile_receipts(db, old, new, **kw)
    finally:
        _release(db)


async def _restore_under(root, name, sid):
    """Boot a fresh multiplexer serving *name* and restore *sid* with its authority."""
    from gateway.run import _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_local_recovery import owned_receipt, restore_local_session
    homes = [('default', root), (name, root / 'profiles' / name)]
    process_ownership.reserve([home for _, home in homes])
    runner = _runner(root, homes)
    try:
        await initialize_gateway_runtime(runner)
        authority = runner.session_authorities.for_home(homes[1][1])
        recovered = sid in authority.sessions
        with _profile_runtime_scope(homes[1][1], {}):
            receipt, source, route, entry = owned_receipt(authority, sid)
            ref = restore_local_session(authority, sid)
        return recovered, authority.profile_id, receipt, source, route, entry, ref
    finally:
        for owner in runner.session_authorities:
            owner.db.close()
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
async def test_rename_keeps_local_chat_restorable(tmp_path, monkeypatch):
    """Red on base: owned_receipt raised profile_mismatch for every pre-rename local chat."""
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, alpha_id = await _create_alpha_session(tmp_path, monkeypatch)
    _rename_dir(root)
    _migrate_profile_identity('alpha', 'beta', live_mux=False)

    recovered, beta_id, receipt, source, route, entry, ref = await _restore_under(root, 'beta', sid)
    assert recovered, 'renamed profile did not recover its local chat on boot'
    assert beta_id != alpha_id
    assert receipt['profile_id'] == beta_id
    assert receipt['session_id'] == sid and receipt['legacy_session_id'] == sid
    assert source.chat_id == sid and source.profile == 'beta'
    assert route == f'agent:beta:local:dm:{sid}' == receipt['route']
    assert entry.session_id == sid
    assert ref.session_id == sid and ref.profile_id == beta_id


@pytest.mark.asyncio
async def test_live_gateway_verb_remints_receipts(tmp_path, monkeypatch):
    """The ``migrate-profile-identity`` control verb (a live multiplexer owns the rename) re-mints too."""
    from gateway.run_profile_reconcile import migrate_profile_identity_verb
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    _rename_dir(root)

    class Store:
        _routing_db = None
        _routing_home = root

        def rekey_profile_routing(self, old, new):
            return 0

    class Runner:
        session_store = Store()

    dry = migrate_profile_identity_verb(Runner())({'old': 'alpha', 'new': 'beta', 'receipts': 'dry-run'})
    assert dry['ok'] is True and dry['receipts']['remintable'] == 1 and 'sids' not in dry['receipts']
    answer = migrate_profile_identity_verb(Runner())({'old': 'alpha', 'new': 'beta'})  # rename default
    assert answer['ok'] is True, answer
    assert (answer['receipts']['reminted'], answer['receipts']['refused']) == (1, 0)
    recovered, beta_id, receipt, *_ = await _restore_under(root, 'beta', sid)
    assert recovered and receipt['profile_id'] == beta_id


@pytest.mark.asyncio
async def test_rerun_is_idempotent_and_dry_run_writes_nothing(tmp_path, monkeypatch):
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    _migrate_profile_identity('alpha', 'beta', live_mux=False, receipts='dry-run')
    before = _meta(beta / 'state.db')
    assert json.loads(before[POLICY + sid])['profile_id'].endswith('/alpha')
    assert BACKUP + sid not in before and LEGACY + sid not in before
    assert _rekey(apply=False)['remintable'] == 1
    assert _meta(beta / 'state.db') == before, 'dry-run wrote'

    first = _rekey(apply=True)
    assert (first['remintable'], first['reminted'], first['refused']) == (1, 1, 0)
    after = _meta(beta / 'state.db')
    second = _rekey(apply=True)
    assert (second['remintable'], second['reminted'], second['refused'], second['owned']) == (0, 0, 0, 1)
    assert _meta(beta / 'state.db') == after, 're-run rewrote a re-minted receipt'
    assert _migrate_profile_identity('alpha', 'beta', live_mux=False) is True
    assert _meta(beta / 'state.db') == after


@pytest.mark.asyncio
async def test_live_turn_lease_refuses_remint(tmp_path, monkeypatch):
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    _migrate_profile_identity('alpha', 'beta', live_mux=False, receipts='dry-run')  # routing rekey only
    db = _db(beta / 'state.db')
    try:
        db._execute_write(lambda conn: conn.execute(
            'INSERT INTO session_turn_leases(conversation_id,holder,acquired_at,expires_at) VALUES(?,?,?,?)',
            (db._session_turn_lease_key_on_conn(conn, sid), 'other-process', time.time(), time.time() + 600)))
    finally:
        _release(db)
    before = _meta(beta / 'state.db')
    report = _rekey(apply=True)
    assert (report['reminted'], report['refused']) == (0, 1)
    assert report['refused_reasons'] == {'runtime_coordination_required': 1}
    assert _meta(beta / 'state.db') == before, 'a leased receipt was rewritten'


def _plant_foreign(beta_db_path, sid, home='/elsewhere/.hermes/profiles/edge', suffix='f', route=None):
    """A copy of *sid*'s receipt claiming another home under a fresh key (host/test leakage)."""
    import sqlite3
    conn = sqlite3.connect(beta_db_path)
    try:
        receipt = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY + sid,)).fetchone()[0])
        foreign_sid = 'local-' + suffix * 64
        receipt.update(profile_id=home, session_id=foreign_sid)
        if route is not None:
            receipt['route'] = route
        raw = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
        with conn:
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (POLICY + foreign_sid, raw))
        return foreign_sid, raw
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_unprovable_receipt_untouched_without_flag_archived_with_flag(tmp_path, monkeypatch):
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    foreign_sid, raw = _plant_foreign(beta / 'state.db', sid)
    _migrate_profile_identity('alpha', 'beta', live_mux=False)  # a rename never archives
    meta = _meta(beta / 'state.db')
    assert meta[POLICY + foreign_sid] == raw and ARCHIVED + foreign_sid not in meta
    assert json.loads(meta[POLICY + sid])['legacy_session_id'] == sid

    report = _rekey(apply=False, archive_foreign=True)
    assert (report['archivable'], report['archived']) == (1, 1)  # would archive; rolled back
    assert _meta(beta / 'state.db') == meta, 'dry-run --archive-foreign wrote'
    report = _rekey(apply=True)
    assert (report['archivable'], report['archived']) == (1, 0)
    assert _meta(beta / 'state.db') == meta

    report = _rekey(apply=True, archive_foreign=True)
    assert (report['archivable'], report['archived'], report['refused']) == (1, 1, 0)
    meta = _meta(beta / 'state.db')
    assert POLICY + foreign_sid not in meta
    archived = json.loads(meta[ARCHIVED + foreign_sid])
    assert archived.pop('archived_reason').startswith("foreign profile_id '/elsewhere/.hermes/profiles/edge'")
    assert archived.pop('archived_at')
    assert archived == json.loads(raw)
    assert json.loads(meta[BACKUP + foreign_sid])['rows'][POLICY + foreign_sid] == raw


@pytest.mark.asyncio
async def test_archive_never_takes_a_receipt_that_may_still_be_owned(tmp_path, monkeypatch):
    """A relocated root makes every receipt look foreign: archiving must not take this profile's
    own (route in its namespace) or a home that still exists."""
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    own_sid, _ = _plant_foreign(beta / 'state.db', sid, suffix='a', route='agent:beta:local:dm:x')
    live_sid, _ = _plant_foreign(beta / 'state.db', sid, home=str(root), suffix='b')
    _migrate_profile_identity('alpha', 'beta', live_mux=False)
    before = _meta(beta / 'state.db')
    report = _rekey(apply=True, archive_foreign=True)
    assert report['archived'] == 0
    assert report['refused_reasons'] == {'claims_this_profile': 1, 'home_exists': 1}
    assert _meta(beta / 'state.db') == before


@pytest.mark.asyncio
async def test_rollback_restores_byte_identical_rows(tmp_path, monkeypatch):
    from hermes_cli.profile_identity import _migrate_profile_identity
    from hermes_state_local_rekey import rollback_local_receipts
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    foreign_sid, _raw = _plant_foreign(beta / 'state.db', sid)
    _migrate_profile_identity('alpha', 'beta', live_mux=False, receipts='dry-run')
    pristine = _meta(beta / 'state.db')
    _rekey(apply=True, archive_foreign=True)
    changed = _meta(beta / 'state.db')
    assert changed != pristine
    db = _db(beta / 'state.db')
    try:
        assert rollback_local_receipts(db, old_name='alpha', new_name='beta')['restorable'] == 2
        assert _meta(beta / 'state.db') == changed, 'rollback dry-run wrote'
        report = rollback_local_receipts(db, old_name='alpha', new_name='beta', apply=True)
    finally:
        _release(db)
    assert (report['restored'], report['refused']) == (2, 0)
    assert _meta(beta / 'state.db') == pristine, 'rollback did not restore byte-identical rows'


@pytest.mark.asyncio
async def test_rollback_refuses_a_receipt_changed_since(tmp_path, monkeypatch):
    from hermes_state_local_rekey import rollback_local_receipts
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    _migrate_profile_identity('alpha', 'beta', live_mux=False)
    db = _db(beta / 'state.db')
    try:
        def touch(conn):
            receipt = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY + sid,)).fetchone()[0])
            receipt['entry']['display_name'] = 'advanced since'
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY + sid))
        db._execute_write(touch)
        before = _meta(beta / 'state.db')
        report = rollback_local_receipts(db, old_name='alpha', new_name='beta', apply=True)
    finally:
        _release(db)
    assert (report['restored'], report['refused'], report['refused_reasons']) == (0, 1, {'changed_since': 1})
    assert _meta(beta / 'state.db') == before


@pytest.mark.asyncio
async def test_old_profile_still_present_is_not_provable(tmp_path, monkeypatch):
    """A receipt naming a home that is still a live profile is that profile's, not a rename's."""
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    (root / 'profiles' / 'alpha').mkdir()
    _migrate_profile_identity('alpha', 'beta', live_mux=False, receipts='dry-run')
    before = _meta(beta / 'state.db')
    report = _rekey(apply=True)
    assert report['refused_reasons'] == {'old_profile_exists': 1}
    assert _meta(beta / 'state.db') == before


def test_cli_migrate_identity_defaults_to_receipt_dry_run(monkeypatch):
    from hermes_cli import profile_identity
    from hermes_cli.profile_cmd import _profile_migrate_identity
    calls = []
    monkeypatch.setattr(profile_identity, 'migrate_profile_identity',
                        lambda old, new, **kw: calls.append((old, new, kw)) or True)

    class Args:
        old_name, new_name = 'alpha', 'beta'
    _profile_migrate_identity(Args())
    args = Args()
    args.apply, args.archive_foreign = True, True
    _profile_migrate_identity(args)
    assert calls == [
        ('alpha', 'beta', {'receipts': 'dry-run', 'archive_foreign': False, 'rollback': False}),
        ('alpha', 'beta', {'receipts': 'apply', 'archive_foreign': True, 'rollback': False}),
    ]


def _rows(meta, sid):
    """Every state_meta row a re-mint of *sid* touches (receipt + legacy binding)."""
    return {k: meta.get(k) for k in (POLICY + sid, LEGACY + sid)}


@pytest.mark.asyncio
async def test_chained_rename_remints_and_rolls_back_one_step(tmp_path, monkeypatch):
    """alpha→beta→gamma: the first rename's backup must not refuse the second (review t_21fccf57),
    the chat restores under gamma with the same id, and rolling back beta→gamma restores the
    beta-era rows byte-for-byte while alpha→beta stays superseded until then."""
    from hermes_cli.profile_identity import _migrate_profile_identity
    from hermes_state_local_rekey import rollback_local_receipts
    root, sid, alpha_id = await _create_alpha_session(tmp_path, monkeypatch)
    _rename_dir(root)
    assert _migrate_profile_identity('alpha', 'beta', live_mux=False) is True
    beta_era = _meta(root / 'profiles' / 'beta' / 'state.db')
    assert json.loads(beta_era[POLICY + sid])['legacy_session_id'] == sid

    os.rename(root / 'profiles' / 'beta', root / 'profiles' / 'gamma')
    gamma_db = root / 'profiles' / 'gamma' / 'state.db'
    assert _migrate_profile_identity('beta', 'gamma', live_mux=False) is True
    gamma_era = _meta(gamma_db)
    receipt = json.loads(gamma_era[POLICY + sid])
    assert receipt['profile_id'].endswith('/profiles/gamma')
    assert receipt['session_id'] == sid and receipt['legacy_session_id'] == sid
    assert receipt['route'] == f'agent:gamma:local:dm:{sid}'
    second = json.loads(gamma_era['gateway.local_policy.backup.v1:beta:gamma:' + sid])
    assert second['supersedes'] == [BACKUP + sid]
    assert BACKUP + sid in gamma_era  # the first rename's backup is kept, not overwritten

    recovered, gamma_id, got, source, route, entry, ref = await _restore_under(root, 'gamma', sid)
    assert recovered and gamma_id != alpha_id and got['profile_id'] == gamma_id
    assert got['session_id'] == sid and got['legacy_session_id'] == sid
    assert ref.session_id == sid and source.chat_id == sid and entry.session_id == sid
    assert route == f'agent:gamma:local:dm:{sid}'
    after_boot = _meta(gamma_db)

    db = _db(gamma_db)
    try:
        early = rollback_local_receipts(db, old_name='alpha', new_name='beta', apply=True)
        assert (early['restored'], early['refused_reasons']) == (0, {'superseded': 1})
        assert _meta(gamma_db) == after_boot, 'rolling back a superseded rename wrote'
        report = rollback_local_receipts(db, old_name='beta', new_name='gamma', apply=True)
    finally:
        _release(db)
    assert (report['restored'], report['refused']) == (1, 0), report
    rolled = _meta(gamma_db)
    assert _rows(rolled, sid) == _rows(beta_era, sid), 'beta-era rows not restored byte-for-byte'
    assert 'gateway.local_policy.backup.v1:beta:gamma:' + sid not in rolled
    assert rolled[BACKUP + sid] == beta_era[BACKUP + sid]


@pytest.mark.asyncio
async def test_refused_own_receipt_fails_migration_and_warns(tmp_path, monkeypatch, capsys):
    """A refused receipt of the OLD home is a chat left unrestorable: never a silent success."""
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    _migrate_profile_identity('alpha', 'beta', live_mux=False, receipts='dry-run')
    db = _db(beta / 'state.db')
    try:
        db._execute_write(lambda conn: conn.execute(
            'INSERT INTO session_turn_leases(conversation_id,holder,acquired_at,expires_at) VALUES(?,?,?,?)',
            (db._session_turn_lease_key_on_conn(conn, sid), 'other-process', time.time(), time.time() + 600)))
    finally:
        _release(db)
    capsys.readouterr()
    assert _migrate_profile_identity('alpha', 'beta', live_mux=False) is False
    err = capsys.readouterr().err
    assert "1 local chat(s) created as 'alpha' were NOT re-minted (runtime_coordination_required=1)" in err


@pytest.mark.asyncio
async def test_refused_foreign_receipt_stays_non_fatal(tmp_path, monkeypatch, capsys):
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, sid, _ = await _create_alpha_session(tmp_path, monkeypatch)
    beta = _rename_dir(root)
    _plant_foreign(beta / 'state.db', sid, home=str(root), suffix='b')  # refused: home_exists
    assert _migrate_profile_identity('alpha', 'beta', live_mux=False) is True
    assert 'NOT re-minted' not in capsys.readouterr().err


def test_live_verb_refused_own_receipt_fails_cli(monkeypatch, capsys):
    """The live path: a gateway answer whose receipt report refused an own receipt is not success."""
    from hermes_cli import profile_identity
    import gateway.control_socket as control_socket
    monkeypatch.setattr(profile_identity, '_migrate_checkpoint_identity', lambda old, new: True)
    report = {'db': '/x/state.db', 'mode': 'apply', 'remintable': 0, 'archivable': 0, 'refused': 1,
              'owned': 0, 'reminted': 0, 'archived': 0, 'refused_reasons': {'lineage_invalid': 1},
              'foreign_homes': {}, 'refused_own': 1, 'refused_own_reasons': {'lineage_invalid': 1}}
    monkeypatch.setattr(control_socket, 'migrate_gateway_profile_identity',
                        lambda *a, **kw: {'ok': True, 'rekeyed': 1, 'db': {}, 'receipts': report})
    assert profile_identity._migrate_profile_identity('alpha', 'beta', live_mux=True) is False
    assert 'were NOT re-minted (lineage_invalid=1)' in capsys.readouterr().err
    report.update(refused=1, refused_own=0, refused_own_reasons={}, refused_reasons={'home_exists': 1})
    assert profile_identity._migrate_profile_identity('alpha', 'beta', live_mux=True) is True
