"""Deletion-failure alerts must survive a crash between commit and delivery.

The expiry loop records ``deletion_alert_sent`` so an operator hears about a
stuck VM exactly once. Delivery is coupled to that flag through an outbox row
committed in the same transaction; these tests inject crashes and transport
failures at the delivery boundary (``requests.post``) and check what an
operator actually receives.
"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from sqlalchemy import create_engine, inspect

from app.models import AdminAlert, VMRecord, db
from tests.test_vm_destroy_safety import (
    _expired_vm, _proxmox_with_qemu_endpoints)
import app.tasks as vm_tasks


WEBHOOK = 'https://hooks.example.edu/alerts'


class _ProcessCrash(BaseException):
    """Stands in for the worker dying mid-delivery (not an Exception, so no
    handler in the application can swallow it)."""


class _StopLoop(BaseException):
    pass


def _run_cycle(app):
    """Run one expiry pass; a crash ends it the way a dead process would."""
    sleeps = 0

    def fake_sleep(seconds):
        nonlocal sleeps
        if seconds == 60:
            sleeps += 1
            if sleeps == 2:
                raise _StopLoop

    with patch('app.tasks.time.sleep', side_effect=fake_sleep):
        try:
            vm_tasks.check_and_destroy_expired_vms(app)
        except (_StopLoop, _ProcessCrash):
            pass


class _Webhook:
    """The operator's webhook endpoint as seen through requests.post."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.delivered = []
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.delivered.append(json)
        response = MagicMock()
        response.raise_for_status.return_value = None
        return response


@pytest.fixture
def alert_env(monkeypatch, tmp_path):
    monkeypatch.setenv('ALERT_WEBHOOK_URL', WEBHOOK)
    monkeypatch.setenv('ALERT_WEBHOOK_TYPE', 'slack')
    for name in ('SMTP_SERVER', 'SMTP_USERNAME', 'SMTP_PASSWORD',
                 'SMTP_FROM_EMAIL', 'ADMIN_ALERT_EMAILS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(vm_tasks, 'ALERT_LOG_FILE', str(tmp_path / 'failed.log'))
    return tmp_path / 'failed.log'


def _dispatch(now=None):
    from app.alert_outbox import dispatch_pending_alerts
    return dispatch_pending_alerts(now=now)


@patch('app.proxmox_service.ProxmoxAPI')
def test_crash_during_alert_delivery_still_alerts_on_next_pass(
        mock_api, app, alert_env):
    vm_id, _ = _expired_vm(app, retry_count=vm_tasks.VM_DELETION_MAX_RETRIES - 1)
    _, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].status.stop.post.side_effect = Exception('cluster locked')
    webhook = _Webhook(failures=[_ProcessCrash()])

    with patch('app.tasks.requests.post', side_effect=webhook.post):
        for _ in range(3):
            _run_cycle(app)

    assert webhook.calls >= 2, 'the crashed delivery must be attempted again'
    assert len(webhook.delivered) == 1
    assert 'VM 100' in webhook.delivered[0]['text']
    with app.app_context():
        assert db.session.get(VMRecord, vm_id).deletion_alert_sent is True
        assert AdminAlert.query.filter_by(
            alert_type='vm_deletion_failed').count() == 1
    assert alert_env.read_text().count('CRITICAL') == 1


@patch('app.proxmox_service.ProxmoxAPI')
def test_alert_is_queued_atomically_with_the_alert_flag(
        mock_api, app, alert_env):
    """The commit that sets deletion_alert_sent is the commit that queues the
    alert; nothing is sent from inside the expiry transaction."""
    _expired_vm(app, retry_count=vm_tasks.VM_DELETION_MAX_RETRIES - 1)
    _, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].status.stop.post.side_effect = Exception('cluster locked')
    webhook = _Webhook()

    with patch('app.tasks.requests.post', side_effect=webhook.post), \
         patch('app.tasks._send_webhook_alert',
               side_effect=_ProcessCrash):
        _run_cycle(app)

    from app.models import AlertOutbox
    with app.app_context():
        row = AlertOutbox.query.one()
        assert row.status == 'pending'
        assert row.alert_type == 'vm_deletion_failed'

    with patch('app.tasks.requests.post', side_effect=webhook.post):
        with app.app_context():
            _dispatch()
    assert len(webhook.delivered) == 1


def test_send_failure_retries_with_backoff_and_never_duplicates(app, alert_env):
    vm_id, _ = _expired_vm(app)
    webhook = _Webhook(failures=[requests.ConnectionError('webhook down')])

    with app.app_context(), \
         patch('app.tasks.requests.post', side_effect=webhook.post):
        vm = db.session.get(VMRecord, vm_id)
        vm.deletion_retry_count = vm_tasks.VM_DELETION_MAX_RETRIES
        vm_tasks._enqueue_deletion_failure_alert(vm)
        db.session.commit()
        start = datetime.now()

        _dispatch(now=start)
        assert webhook.delivered == []
        from app.models import AlertOutbox
        row = AlertOutbox.query.one()
        assert row.status == 'pending'
        assert row.attempts == 1
        assert row.next_attempt_at > start

        _dispatch(now=start)          # still in backoff
        assert webhook.calls == 1

        _dispatch(now=start + timedelta(hours=2))
        assert len(webhook.delivered) == 1
        assert AlertOutbox.query.one().status == 'sent'

        _dispatch(now=start + timedelta(hours=4))
        _dispatch(now=start + timedelta(hours=8))

    assert webhook.calls == 2
    assert len(webhook.delivered) == 1
    with app.app_context():
        # Channels that already succeeded are not repeated on retry.
        assert AdminAlert.query.filter_by(
            alert_type='vm_deletion_failed').count() == 1
    assert alert_env.read_text().count('CRITICAL') == 1


def test_alert_survives_deletion_of_its_vm_record(app, alert_env):
    """The VM may be cleaned up before the alert goes out; the alert keeps
    its own copy of what the operator needs."""
    vm_id, _ = _expired_vm(app)
    webhook = _Webhook()
    with app.app_context(), \
         patch('app.tasks.requests.post', side_effect=webhook.post):
        vm = db.session.get(VMRecord, vm_id)
        vm.deletion_retry_count = vm_tasks.VM_DELETION_MAX_RETRIES
        vm_tasks._enqueue_deletion_failure_alert(vm)
        db.session.commit()
        db.session.delete(db.session.get(VMRecord, vm_id))
        db.session.commit()

        _dispatch()

    assert len(webhook.delivered) == 1
    fields = webhook.delivered[0]['attachments'][0]['fields']
    assert {'title': 'Name', 'value': 'vm-100', 'short': True} in fields


def test_orphan_reaped_alert_goes_through_outbox_without_new_channels(
        app, alert_env):
    from app.orphan_reaper import _record_reaped_alert
    webhook = _Webhook()
    with app.app_context(), \
         patch('app.tasks.requests.post', side_effect=webhook.post):
        _record_reaped_alert(700, 'student-alice-web', 'pve', 'qemu',
                             datetime(2026, 1, 1, 9, 0))
        _dispatch()
        _dispatch()
        alerts = AdminAlert.query.filter_by(alert_type='vm_orphan_reaped').all()
        assert len(alerts) == 1
        assert alerts[0].severity == 'warning'
    # The reaper never paged anyone before; the outbox must not start to.
    assert webhook.calls == 0


def _queue_alerts(count, channels=('webhook',)):
    """Queue `count` deletion-failure-shaped alerts on `channels` only."""
    from app.alert_outbox import enqueue_alert
    created = datetime(2026, 1, 1, 9, 0)
    for n in range(count):
        vmid = 900 + n
        enqueue_alert(
            alert_type='vm_deletion_failed', severity='critical',
            message=f'VM {vmid} failed to delete', vmid=vmid,
            vm_name=f'student-s-vm{n}', channels=channels,
            vm={'vmid': vmid, 'vm_name': f'student-s-vm{n}', 'vm_type': 'qemu',
                'node': 'pve', 'student_name': 's', 'template_name': 'lab/vm',
                'deletion_retry_count': 5,
                'created_at': created.isoformat(),
                'expires_at': (created + timedelta(hours=1)).isoformat()})
    db.session.commit()


def test_dispatch_starts_no_alert_once_its_time_budget_is_spent(app, alert_env):
    """Every send hangs for 15 s: a 20 s pass attempts two alerts, then stops.
    The rest stay due for the next pass and lose none of their attempts."""
    from app.alert_outbox import AlertOutbox, dispatch_pending_alerts
    clock = [0.0]

    def hung_post(url, json=None, timeout=None):
        clock[0] += 15
        raise requests.Timeout('no answer')

    with app.app_context(), \
         patch('app.tasks.requests.post', side_effect=hung_post) as post:
        _queue_alerts(5)
        start = datetime.now()

        dispatch_pending_alerts(now=start, max_seconds=20,
                                clock=lambda: clock[0])

        assert post.call_count == 2
        rows = AlertOutbox.query.order_by(AlertOutbox.id).all()
        assert [row.attempts for row in rows] == [1, 1, 0, 0, 0]
        assert {row.status for row in rows} == {'pending'}
        assert all(row.next_attempt_at <= start for row in rows[2:])

        # The untried alerts are first in line next pass; the tried ones
        # are in backoff.
        clock[0] = 0.0
        dispatch_pending_alerts(now=start, max_seconds=20,
                                clock=lambda: clock[0])
        rows = AlertOutbox.query.order_by(AlertOutbox.id).all()
        assert [row.attempts for row in rows] == [1, 1, 1, 1, 0]


def test_dispatch_caps_the_alerts_attempted_per_pass(app, alert_env):
    from app.alert_outbox import AlertOutbox, dispatch_pending_alerts
    webhook = _Webhook()
    with app.app_context(), \
         patch('app.tasks.requests.post', side_effect=webhook.post):
        _queue_alerts(5)
        assert dispatch_pending_alerts(max_alerts=2) == 2
        rows = AlertOutbox.query.order_by(AlertOutbox.id).all()
        assert [row.status for row in rows] == ['sent'] * 2 + ['pending'] * 3
        assert [row.attempts for row in rows[2:]] == [0, 0, 0]


def test_dispatch_budget_defaults_to_twenty_seconds():
    from app.config import (
        ALERT_DISPATCH_MAX_PASS_ALERTS, ALERT_DISPATCH_MAX_PASS_SECONDS)
    assert ALERT_DISPATCH_MAX_PASS_SECONDS == 20
    assert ALERT_DISPATCH_MAX_PASS_ALERTS > 0


_SMTP_ENV = {
    'SMTP_SERVER': 'smtp.example.edu',
    'SMTP_USERNAME': 'alerts@example.edu',
    'SMTP_PASSWORD': 'secret',
    'SMTP_FROM_EMAIL': 'alerts@example.edu',
    'ADMIN_ALERT_EMAILS': 'admin@example.edu',
}


def test_alert_senders_bound_their_network_calls(app, alert_env, monkeypatch):
    for name, value in {**_SMTP_ENV, 'SMTP_PORT': '587'}.items():
        monkeypatch.setenv(name, value)
    with app.app_context(), \
         patch('app.tasks.smtplib.SMTP') as smtp, \
         patch('app.tasks.requests.post', side_effect=_Webhook().post) as post:
        _queue_alerts(1, channels=('email', 'webhook'))
        _dispatch()
    assert smtp.call_args.kwargs.get('timeout') == 10
    assert post.call_args.kwargs.get('timeout') == 10


@pytest.mark.parametrize('smtp_env', [
    {'SMTP_PORT': ''},                    # compose's ${SMTP_PORT}, unset
    {**_SMTP_ENV, 'SMTP_PORT': ''},
    {**_SMTP_ENV},                        # SMTP_PORT absent altogether
    {**_SMTP_ENV, 'SMTP_PORT': 'smtp'},   # not a number
], ids=['empty-port-only', 'empty-port', 'unset-port', 'non-numeric-port'])
def test_email_without_a_usable_port_is_not_configured(
        app, alert_env, monkeypatch, smtp_env):
    """An unusable SMTP_PORT means email is off. Retrying cannot fix it, so
    it must not drive the alert through 30 attempts to 'abandoned'."""
    from app.alert_outbox import AlertOutbox
    monkeypatch.delenv('SMTP_PORT', raising=False)
    for name, value in smtp_env.items():
        monkeypatch.setenv(name, value)
    with app.app_context(), patch('app.tasks.smtplib.SMTP') as smtp:
        _queue_alerts(1, channels=('email',))
        _dispatch()
        row = AlertOutbox.query.one()
        assert (row.status, row.attempts) == ('sent', 0)
    smtp.assert_not_called()


def _load_migration():
    path = Path(__file__).parents[1] / 'scripts' / 'migrate_add_alert_outbox.py'
    assert path.exists(), 'alert outbox migration must ship with the model'
    spec = importlib.util.spec_from_file_location('migrate_add_alert_outbox', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_creates_outbox_matching_model_and_is_idempotent(tmp_path):
    from app.models import AlertOutbox
    migration = _load_migration()
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"

    migration.run_migration(database_url)
    migration.run_migration(database_url)

    inspector = inspect(create_engine(database_url))
    migrated = {c['name']: c for c in inspector.get_columns('alert_outbox')}
    model = AlertOutbox.__table__.columns
    assert set(migrated) == {c.name for c in model}
    for column in model:
        assert migrated[column.name]['nullable'] == column.nullable, column.name
    indexed = {tuple(i['column_names']) for i in inspector.get_indexes('alert_outbox')}
    assert ('status', 'next_attempt_at') in indexed
