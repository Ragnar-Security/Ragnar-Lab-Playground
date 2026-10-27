"""Durable tracking for failed-deploy survivors when the database is down.

A failed deploy whose cleanup cannot confirm a guest's deletion commits a
promptly-expiring VMRecord, so the expiry loop destroys the guest. If that
commit fails too, the in-process admission lease was the only trace: once it
expired, or the process restarted, the guest was untracked and kept running
unaccounted.

The fallback is an append-only JSON-lines file on persistent storage
(`config.untracked_survivors_file()`, mode 0600). Each line describes one
guest: vmid, node, name, type, owner, and the reason. It holds no
credentials. `recover_untracked_survivors()` runs at startup and on every
expiry pass. It imports the lines as already-expired VMRecords once the
database answers, and removes the file only after that commit.
"""

import json
import logging
import os
import threading
from datetime import datetime, timedelta

from .config import untracked_survivors_file
from .models import KothGame, StudentGroup, User, VMRecord, db

logger = logging.getLogger(__name__)

# Serializes appends against recovery's hand-off of the file. Never held
# across a database call, so a hung database cannot stall a failing deploy.
_file_lock = threading.Lock()

_OPTIONAL_INTS = ('created_by_user_id', 'owner_group_id', 'koth_game_id',
                  'memory', 'cores', 'storage')
_REFERENCES = (('created_by_user_id', User), ('owner_group_id', StudentGroup),
               ('koth_game_id', KothGame))


def track_survivor(entry, expires_at, record=None):
    """Stage a VMRecord for a surviving guest in the open transaction.

    `record` is the existing row for the vmid, if any; it is rewritten to
    describe the survivor. The caller commits.
    """
    if record is None:
        record = VMRecord(vmid=entry['vmid'], created_at=datetime.now(),
                          ip_address=None)
        db.session.add(record)
    record.vm_name = entry['vm_name']
    record.vm_type = entry['vm_type']
    record.node = entry['node']
    record.template_name = entry['template_name']
    record.student_name = entry['student_name']
    record.memory = entry.get('memory')
    record.cores = entry.get('cores')
    record.storage = entry.get('storage')
    record.expires_at = expires_at
    record.created_by_user_id = entry.get('created_by_user_id')
    record.is_entry_point = entry.get('is_entry_point', False)
    record.owner_group_id = entry.get('owner_group_id')
    record.koth_game_id = entry.get('koth_game_id')
    record.destroyed = False
    record.destroyed_at = None
    record.deletion_retry_count = 0
    record.deletion_last_attempt_at = None
    record.deletion_alert_sent = False
    return record


def record_untracked_survivors(entries, reason):
    """Append one line per survivor and fsync. Returns the file path."""
    path = untracked_survivors_file()
    recorded_at = datetime.now().isoformat(timespec='seconds')
    data = ''.join(
        json.dumps({**entry, 'reason': reason, 'recorded_at': recorded_at},
                   sort_keys=True) + '\n'
        for entry in entries
    ).encode()
    with _file_lock:
        _append(path, data)
    return path


def recover_untracked_survivors():
    """Import pending survivor lines into VMRecords.

    Returns ``{'imported', 'already_tracked', 'corrupt'}`` (corrupt is a list
    of line numbers), or None when the database could not take the import;
    the lines then stay on disk for the next pass. A vmid that already has a
    live record is left alone, so a repeated import changes nothing.
    """
    path = untracked_survivors_file()
    importing = path + '.importing'
    report = {'imported': 0, 'already_tracked': 0, 'corrupt': []}

    # Hand new lines over to the import file under the lock, so an append
    # racing this pass lands in a file that is read in full afterwards.
    with _file_lock:
        try:
            with open(path, 'rb') as handle:
                pending = handle.read()
        except FileNotFoundError:
            pending = None
        if pending is not None:
            if pending:
                _append(importing, pending)
            os.unlink(path)

    try:
        with open(importing, 'rb') as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return report

    entries = []
    corrupt_lines = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entries.append(_parse(line))
        except ValueError as error:
            report['corrupt'].append(number)
            corrupt_lines.append(line)
            logger.error("Skipping corrupt untracked-survivor line %d in %s: %s",
                         number, importing, error)

    expires_at = datetime.now() - timedelta(seconds=1)
    try:
        for entry in entries:
            record = VMRecord.query.filter_by(vmid=entry['vmid']).first()
            if record is not None and not record.destroyed:
                report['already_tracked'] += 1
                continue
            _drop_missing_references(entry)
            track_survivor(entry, expires_at, record=record)
            report['imported'] += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Could not import untracked survivors from %s; "
                         "retrying on the next pass", importing)
        return None

    if corrupt_lines:
        rejected = path + '.corrupt'
        with _file_lock:
            _append(rejected, b'\n'.join(corrupt_lines) + b'\n')
        logger.error("Moved %d corrupt untracked-survivor line(s) to %s; "
                     "check those guests by hand", len(corrupt_lines), rejected)
    os.unlink(importing)
    if report['imported']:
        logger.warning("Imported %d untracked failed-deploy survivor(s) for "
                       "immediate expiry", report['imported'])
    return report


def _append(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    created = not os.path.exists(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        # Make the new directory entry itself durable.
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _parse(line):
    try:
        data = json.loads(line)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f'not JSON ({error})') from None
    if not isinstance(data, dict):
        raise ValueError('not a JSON object')
    vmid = data.get('vmid')
    if type(vmid) is not int or vmid <= 0:
        raise ValueError('vmid must be a positive integer')
    for key in ('node', 'vm_name'):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f'{key} must be a non-empty string')
    if data.get('vm_type') not in ('qemu', 'lxc'):
        raise ValueError("vm_type must be 'qemu' or 'lxc'")
    for key in _OPTIONAL_INTS:
        value = data.get(key)
        if value is not None and type(value) is not int:
            raise ValueError(f'{key} must be an integer')
    entry = {key: data.get(key) for key in _OPTIONAL_INTS}
    entry.update(
        vmid=vmid,
        node=data['node'],
        vm_name=data['vm_name'],
        vm_type=data['vm_type'],
        template_name=_text(data, 'template_name',
                            'failed-deploy-leftover/unknown'),
        student_name=_text(data, 'student_name', 'unknown'),
        is_entry_point=data.get('is_entry_point') is True,
    )
    return entry


def _text(data, key, default):
    value = data.get(key)
    return value if isinstance(value, str) and value else default


def _drop_missing_references(entry):
    """A user, group or game deleted during the outage must not make the
    whole import fail its foreign keys; the guest still needs tracking."""
    for key, model in _REFERENCES:
        if entry.get(key) is not None and db.session.get(model, entry[key]) is None:
            entry[key] = None
