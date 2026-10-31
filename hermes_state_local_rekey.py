"""Re-mint a renamed profile's LOCAL creation receipts; archive receipts no rename can prove.

A local session id is content-addressed by its creating home (``local_identity(profile_id,
principal, request_id)``), so ``hermes profile rename`` cannot simply rewrite a receipt's
``profile_id``: the id would stop matching its own derivation and ``owned_receipt`` would refuse
it with ``identity mismatch`` instead of ``profile_mismatch``. The receipt is therefore re-minted
the way pre-authority local history is adopted (``hermes_state_local_migration.bind_legacy_target``):
``profile_id`` becomes the new home, ``legacy_session_id`` names the unchanged session id, and a
``gateway.local_legacy.v1:<sid>`` binding records the claim, so ``owned_receipt`` takes its
existing ``legacy_session_id`` branch (chat id = the old id; no new id is minted).

A receipt is re-minted only when it is PROVABLY a former identity of this profile:
* its ``profile_id`` is exactly the old home of the rename being migrated, and the old profile
  directory no longer exists;
* its id derives from that old home (or it already carries a matching legacy binding);
* its route/entry are the old profile's ``agent:<old>:`` namespace, and the physical row its entry
  names has already been rekeyed by ``rekey_profile_state`` to ``agent:<new>:`` with the same chat
  and principal (what ``_checked_entry`` verifies under the new profile);
* its lineage validates, nothing executes on it and no live turn lease holds it, and none of the
  keys the re-mint writes already exists.

Anything else stays untouched. A receipt naming another home entirely is "foreign": it is moved to
``gateway.local_policy.archived.v1:<sid>`` only when the caller passes ``archive_foreign``; it is
never deleted.

Every write happens in one transaction per state.db and first stores the original row(s) under
``gateway.local_policy.backup.v1:<old>:<new>:<sid>`` (one backup per rename, so a chained rename
alpha→beta→gamma keeps both) together with a digest of what was written and the backups of the
same sid it was layered on, so :func:`rollback_local_receipts` restores byte-identical rows of the
most recent rename only, and refuses one that changed or was superseded since.
``apply=False`` (the default) classifies without writing.
"""
import copy
import hashlib
import json
import os
import time

from hermes_state_local import POLICY_PREFIX
from hermes_state_local_migration import LEGACY_PREFIX
from hermes_state_runtime import RuntimeStoreError, _json

BACKUP_PREFIX = 'gateway.local_policy.backup.v1:'
ARCHIVE_PREFIX = 'gateway.local_policy.archived.v1:'

REMINT = 'remintable'
ARCHIVE = 'archivable'
REFUSED = 'refused'


class _DryRun(Exception):
    """Carries a dry-run's report out of the write transaction it rolls back."""
    def __init__(self, report):
        super().__init__('dry-run')
        self.report = report


class _Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def backup_key(old_name, new_name, sid):
    """The backup of one receipt's rows for the rename *old_name* → *new_name* (profile names never
    contain ``:``; a sid is ``local-<hex>``)."""
    return f'{BACKUP_PREFIX}{old_name}:{new_name}:{sid}'


def _backups_of(conn, sid):
    """Every backup key (any rename) holding rows of *sid*."""
    tail = ':' + sid
    return [k for (k,) in conn.execute('SELECT key FROM state_meta WHERE substr(key,1,?)=?',
                                       (len(BACKUP_PREFIX), BACKUP_PREFIX)).fetchall() if k.endswith(tail)]


def _digest(value):
    return None if value is None else hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()


def _meta(conn, key):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return None if row is None else row[0]


def _put(conn, key, value):
    if value is None:
        conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
    else:
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def _guard_idle(db, conn, session_ids):
    """``bind_legacy_target``'s guards: nothing queued/running/unknown and no live turn lease."""
    from hermes_state_mutation_guards import require_idle
    try:
        require_idle(db, conn, session_ids)
    except RuntimeStoreError as exc:
        raise _Refused(exc.reason) from exc
    except Exception as exc:  # the transcript guards raise their own types
        name = type(exc).__name__
        raise _Refused('runtime_coordination_required' if 'Lease' in name else f'busy:{name}') from exc
    now = time.time()
    for sid in session_ids:
        root = db._session_turn_lease_key_on_conn(conn, sid)
        if conn.execute('SELECT 1 FROM session_turn_leases WHERE conversation_id=? AND expires_at>?',
                        (root, now)).fetchone():
            raise _Refused('runtime_coordination_required')


def _remint(db, conn, sid, receipt, ctx):
    """The re-minted receipt and legacy binding of *receipt*, or ``_Refused`` naming the failing check."""
    from hermes_state_keys import local_identity
    from hermes_state_local_lineage import validate_local_lineage
    old_name, new_name = ctx['old_name'], ctx['new_name']
    if ctx['old_live']:
        raise _Refused('old_profile_exists')
    principal = receipt.get('principal_id')
    if receipt.get('session_id') != sid or not isinstance(principal, str):
        raise _Refused('identity_mismatch')
    legacy_key = LEGACY_PREFIX + sid
    legacy_raw = _meta(conn, legacy_key)
    if 'legacy_session_id' in receipt:
        # An already-bound receipt (a chained rename, or adopted pre-authority history): its
        # binding must name exactly this receipt before it is carried over to the new home.
        expected = {k: receipt.get(k) for k in ('session_id', 'profile_id', 'principal_id', 'legacy_session_id')}
        if receipt['legacy_session_id'] != sid or legacy_raw is None or json.loads(legacy_raw) != expected:
            raise _Refused('legacy_binding_mismatch')
    else:
        if sid != local_identity(receipt['profile_id'], principal, receipt.get('request_id')):
            raise _Refused('identity_mismatch')
        if legacy_raw is not None:
            raise _Refused('legacy_key_exists')
    old_ns, new_ns = f'agent:{old_name}:', f'agent:{new_name}:'
    route = receipt.get('route')
    entry = receipt.get('entry')
    if not isinstance(route, str) or not route.startswith(old_ns) or not isinstance(entry, dict):
        raise _Refused('route_not_old_namespace')
    origin = entry.get('origin')
    if (entry.get('session_key') != route or not isinstance(origin, dict)
            or origin.get('chat_id') != sid or origin.get('user_id') != principal
            or origin.get('profile', old_name) not in (old_name, None)):
        raise _Refused('entry_mismatch')
    new_route = new_ns + route[len(old_ns):]
    tip = entry.get('session_id')
    row = conn.execute('SELECT session_key,chat_id,user_id FROM sessions WHERE id=?', (tip,)).fetchone()
    if row is None:
        raise _Refused('stored_row_missing')
    if row[0] == route:
        raise _Refused('routing_not_rekeyed')
    if (row[0], row[1], row[2]) != (new_route, sid, principal):
        raise _Refused('stored_identity_mismatch')
    try:
        validate_local_lineage(conn, receipt)
    except (RuntimeStoreError, KeyError, TypeError, ValueError) as exc:
        raise _Refused('lineage_invalid') from exc
    _guard_idle(db, conn, list(dict.fromkeys([sid, tip])))
    minted = copy.deepcopy(receipt)
    minted['profile_id'] = ctx['new_home']
    minted['legacy_session_id'] = sid
    minted['route'] = new_route
    minted['entry']['session_key'] = new_route
    if 'profile' in origin and origin['profile'] is not None:
        minted['entry']['origin']['profile'] = new_name
    binding = {'session_id': sid, 'profile_id': ctx['new_home'],
               'principal_id': principal, 'legacy_session_id': sid}
    return minted, binding


def _plan(db, conn, sid, raw, ctx):
    """``(class, reason, writes)`` of one stored receipt; *writes* maps key → new value (None deletes)."""
    try:
        receipt = json.loads(raw)
        owner = receipt.get('profile_id')
    except (ValueError, TypeError, AttributeError):
        return REFUSED, 'undecodable', None
    if owner == ctx['new_home']:
        return None, 'owned', None
    if _meta(conn, backup_key(ctx['old_name'], ctx['new_name'], sid)) is not None:
        # Only this very rename's backup blocks it: an earlier rename's (alpha→beta when migrating
        # beta→gamma) is history the chained re-mint layers on.
        return REFUSED, 'backup_exists', None
    if owner == ctx['old_home']:
        try:
            minted, binding = _remint(db, conn, sid, receipt, ctx)
        except _Refused as exc:
            return REFUSED, exc.reason, None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return REFUSED, f'malformed:{type(exc).__name__}', None
        return REMINT, 'renamed_profile', {POLICY_PREFIX + sid: _json(minted), LEGACY_PREFIX + sid: _json(binding)}
    if _meta(conn, ARCHIVE_PREFIX + sid) is not None:
        return REFUSED, 'archive_exists', None
    # Archive only what is plainly not this profile's: a receipt routed in this profile's own
    # namespace, or naming a home that still exists (another live profile, or this one under a
    # relocated root), needs a human, not an archive.
    if str(receipt.get('route', '')).startswith(f"agent:{ctx['new_name']}:"):
        return REFUSED, 'claims_this_profile', None
    if isinstance(owner, str) and owner and os.path.isdir(owner):
        return REFUSED, 'home_exists', None
    try:
        tip = (receipt.get('entry') or {}).get('session_id') or sid
        _guard_idle(db, conn, list(dict.fromkeys([sid, tip])))
    except _Refused as exc:
        return REFUSED, exc.reason, None
    archived = dict(receipt)
    archived['archived_reason'] = (
        f"foreign profile_id {owner!r} is not provably a former identity of {ctx['new_home']!r} "
        f"(migrate-identity {ctx['old_name']} -> {ctx['new_name']})")
    archived['archived_at'] = ctx['now']
    return ARCHIVE, 'foreign_profile', {POLICY_PREFIX + sid: None, ARCHIVE_PREFIX + sid: _json(archived)}


def _write_one(conn, sid, cls, writes, meta):
    """Back up then write one receipt's keys inside a savepoint; None on success, else why it was
    rolled back (the receipt is then left exactly as it was)."""
    conn.execute('SAVEPOINT receipt_rekey')
    try:
        key = backup_key(meta['old_name'], meta['new_name'], sid)
        backup = {'sid': sid, 'action': cls, **meta, 'rows': {k: _meta(conn, k) for k in writes},
                  'written': {k: _digest(v) for k, v in writes.items()},
                  'supersedes': sorted(_backups_of(conn, sid))}
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, _json(backup)))
        for key, value in writes.items():
            _put(conn, key, value)
        if cls == REMINT:
            from hermes_state_local_lineage import validate_local_lineage
            validate_local_lineage(conn, json.loads(writes[POLICY_PREFIX + sid]))
    except (RuntimeStoreError, KeyError, TypeError, ValueError) as exc:
        conn.execute('ROLLBACK TO receipt_rekey')
        conn.execute('RELEASE receipt_rekey')
        return f'post_write_validation:{getattr(exc, "reason", type(exc).__name__)}'
    except BaseException:
        conn.execute('ROLLBACK TO receipt_rekey')
        conn.execute('RELEASE receipt_rekey')
        raise
    conn.execute('RELEASE receipt_rekey')
    return None


def _empty_report(db_path, mode):
    return {'db': str(db_path), 'mode': mode, REMINT: 0, ARCHIVE: 0, REFUSED: 0, 'owned': 0,
            'reminted': 0, 'archived': 0, 'refused_reasons': {}, 'foreign_homes': {},
            # Receipts of the OLD home that stayed un-reminted: those chats are unrestorable
            # under the new name until fixed, so callers must not report success.
            'refused_own': 0, 'refused_own_reasons': {},
            'sids': {REMINT: [], ARCHIVE: [], REFUSED: []}}


def rekey_local_receipts(db, *, old_name, new_name, old_home, new_home, old_live=False,
                         apply=False, archive_foreign=False, now=None):
    """Classify (and with *apply*, re-mint / archive) every local receipt in *db* for one rename.

    Returns a JSON-able report: counts per class (re-mintable / archivable / refused + reasons /
    owned), what was written, and the foreign homes seen. Without *apply* nothing is written; with
    *apply* re-mintable receipts are re-minted and, only when *archive_foreign* is also set,
    archivable ones are archived. All writes share one transaction; each receipt runs in its own
    savepoint so one failing check leaves that receipt untouched without aborting the others.
    """
    ctx = {'old_name': old_name, 'new_name': new_name, 'old_home': str(old_home),
           'new_home': str(new_home), 'old_live': bool(old_live),
           'now': now if now is not None else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    mode = 'apply' if apply else 'dry-run'

    def classify(conn, write):  # *write*: perform the writes (a dry run rolls them back)
        report = _empty_report(db.db_path, mode + ('+archive-foreign' if archive_foreign else ''))
        rows = conn.execute('SELECT key, value FROM state_meta WHERE key LIKE ? ORDER BY key',
                            (POLICY_PREFIX + '%',)).fetchall()
        for key, raw in rows:
            sid = key[len(POLICY_PREFIX):]
            try:
                owner = json.loads(raw).get('profile_id')
            except (ValueError, TypeError, AttributeError):
                owner = '<undecodable>'
            if owner != ctx['new_home']:
                report['foreign_homes'][owner] = report['foreign_homes'].get(owner, 0) + 1
            cls, reason, writes = _plan(db, conn, sid, raw, ctx)  # reads only
            if cls is None:
                report['owned'] += 1
                continue
            if write and writes is not None and (cls == REMINT or archive_foreign):
                reason = _write_one(conn, sid, cls, writes, backup_meta)
                if reason is None:
                    report['reminted' if cls == REMINT else 'archived'] += 1
                else:
                    cls = REFUSED
            report[cls] += 1
            report['sids'][cls].append(sid)
            if cls == REFUSED:
                report['refused_reasons'][reason] = report['refused_reasons'].get(reason, 0) + 1
                if owner == ctx['old_home']:
                    report['refused_own'] += 1
                    own = report['refused_own_reasons']
                    own[reason] = own.get(reason, 0) + 1
        return report

    backup_meta = {'at': ctx['now'], 'old_name': old_name, 'new_name': new_name}

    if apply:
        return db._execute_write(lambda conn: classify(conn, True))
    # A dry run runs the very same checks and writes, then rolls the transaction back: the guards
    # it shares with ``bind_legacy_target`` reap stale leases in-transaction, and the post-write
    # lineage validation only runs against written rows. Nothing it does is ever committed.
    def dry(conn):
        raise _DryRun(classify(conn, True))
    try:
        db._execute_write(dry)
    except _DryRun as done:
        return done.report
    raise RuntimeError('dry-run transaction was not rolled back')  # unreachable: dry() always raises


def rollback_local_receipts(db, *, old_name, new_name, apply=False):
    """Restore every receipt this rename's re-mint/archive backed up in *db*, byte-identical.

    A backup is restored only while every key it wrote still holds exactly what it wrote (a receipt
    that advanced since, e.g. by a compression, is refused and kept) and no later rename's backup
    of the same sid was layered on it (roll the most recent rename back first: beta→gamma, then
    alpha→beta). Restored backups are removed. Without *apply* this only reports what would be
    restored.
    """
    prefix = backup_key(old_name, new_name, '')

    def run(conn, write):
        report = {'db': str(db.db_path), 'mode': 'apply' if write else 'dry-run',
                  'restorable': 0, 'restored': 0, 'refused': 0, 'refused_reasons': {}, 'sids': []}
        rows = conn.execute('SELECT key, value FROM state_meta WHERE substr(key,1,?)=? ORDER BY key',
                            (len(BACKUP_PREFIX), BACKUP_PREFIX)).fetchall()
        superseded = set()
        for _key, raw in rows:
            try:
                superseded.update(json.loads(raw).get('supersedes') or ())
            except (ValueError, TypeError, AttributeError):
                pass
        for key, raw in rows:
            if not key.startswith(prefix):
                continue
            reason = None
            try:
                backup = json.loads(raw)
                if (backup.get('old_name'), backup.get('new_name')) != (old_name, new_name) \
                        or backup.get('sid') != key[len(prefix):]:
                    raise ValueError('backup does not name this key')
                restore = backup['rows']
                if key in superseded:
                    reason = 'superseded'
                elif not all(_digest(_meta(conn, k)) == d for k, d in backup['written'].items()):
                    reason = 'changed_since'
            except (ValueError, TypeError, KeyError, AttributeError):
                reason = 'undecodable_backup'
            if reason is not None:
                report['refused'] += 1
                report['refused_reasons'][reason] = report['refused_reasons'].get(reason, 0) + 1
                continue
            report['restorable'] += 1
            report['sids'].append(key[len(prefix):])
            if write:
                for k, v in restore.items():
                    _put(conn, k, v)
                conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
                report['restored'] += 1
        return report

    if not apply:
        with db._read_ctx() as conn:
            return run(conn, False)
    return db._execute_write(lambda conn: run(conn, True))


def format_report(report):
    """One human line per DB: the counts the operator acts on."""
    if 'restorable' in report:
        line = (f"{report['db']}: rollback {report['mode']}: restorable={report['restorable']} "
                f"restored={report['restored']} refused={report['refused']}")
    else:
        done = 'would-remint/would-archive' if report['mode'].startswith('dry-run') else 'reminted/archived'
        line = (f"{report['db']}: receipts {report['mode']}: re-mintable={report[REMINT]} "
                f"archivable={report[ARCHIVE]} refused={report[REFUSED]} owned={report['owned']} "
                f"{done}={report['reminted']}/{report['archived']}")
    if report.get('refused_reasons'):
        line += ' (' + ', '.join(f'{k}={v}' for k, v in sorted(report['refused_reasons'].items())) + ')'
    if report.get('refused_own'):
        line += f" refused-own={report['refused_own']}"
    return line
