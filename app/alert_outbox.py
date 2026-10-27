"""Transactional outbox for operator alerts.

A caller stages an alert with `enqueue_alert()` in the same transaction as
the state change that raised it, then commits once. `dispatch_pending_alerts()`
runs from the expiry loop and fans each row out to its channels:

- ``file``     -> tasks._log_alert_to_file
- ``database`` -> an AdminAlert row, committed with the channel's completion
- ``email``    -> tasks._send_email_alert (skipped when SMTP is unconfigured)
- ``webhook``  -> tasks._send_webhook_alert (skipped when unconfigured)

Each delivered channel is struck from the row in its own commit. A failed
channel is retried with exponential backoff while the delivered ones are not
repeated. The database channel is exactly-once; email and webhook are
at-least-once, because a crash between the remote send and its commit
resends that one channel on the next pass.
"""

import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from .models import AdminAlert, AlertOutbox, db

logger = logging.getLogger(__name__)

ALL_CHANNELS = ('file', 'database', 'email', 'webhook')
STATUS_PENDING = 'pending'
STATUS_SENT = 'sent'
STATUS_ABANDONED = 'abandoned'

# 1, 2, 4, ... minutes capped at an hour: about a day of retries before a
# permanently broken channel stops being attempted.
MAX_ATTEMPTS = 30
MAX_BACKOFF_MINUTES = 60
DISPATCH_BATCH = 50


def enqueue_alert(*, alert_type, severity, message, vmid=None, vm_name=None,
                  details=None, vm=None, channels=ALL_CHANNELS):
    """Stage an alert in the caller's open transaction; the caller commits.

    `vm` is a JSON-safe snapshot of the VM fields the senders print. The
    alert must not depend on the VMRecord still existing at delivery time.
    """
    now = datetime.now()
    row = AlertOutbox(
        alert_type=alert_type,
        payload=json.dumps({
            'severity': severity,
            'message': message,
            'vmid': vmid,
            'vm_name': vm_name,
            'details': details,
            'vm': vm,
        }),
        pending_channels=','.join(channels),
        status=STATUS_PENDING,
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    db.session.add(row)
    return row


def dispatch_pending_alerts(now=None, limit=DISPATCH_BATCH):
    """Deliver due alerts. Returns how many were fully delivered.

    Never raises for delivery or database trouble: the rows stay pending and
    the next pass retries them.
    """
    now = now or datetime.now()
    try:
        rows = (
            AlertOutbox.query
            .filter(AlertOutbox.status == STATUS_PENDING,
                    AlertOutbox.next_attempt_at <= now)
            .order_by(AlertOutbox.id)
            .limit(limit)
            .all()
        )
    except Exception:
        db.session.rollback()
        logger.exception("Alert outbox: could not read pending alerts")
        return 0

    delivered = 0
    for row in rows:
        row_id = row.id
        try:
            if _deliver(row, now):
                delivered += 1
        except Exception:
            # Most likely the database went away mid-pass. Whatever was not
            # committed is retried next pass; stop rather than hammer it.
            db.session.rollback()
            logger.exception(
                "Alert outbox: could not record delivery of alert %s", row_id)
            break
    return delivered


def _deliver(row, now):
    row_id = row.id
    payload = json.loads(row.payload)
    pending = [channel for channel in row.pending_channels.split(',') if channel]
    failed = []
    for channel in list(pending):
        sender = _SENDERS.get(channel)
        if sender is None:
            failed.append(f'{channel}: unknown channel')
            continue
        try:
            ok = sender(row, payload)
        except Exception as error:
            if channel == 'database':
                db.session.rollback()
            # Only the type: transport errors can carry the webhook URL,
            # whose path is a credential.
            logger.error("Alert outbox: %s delivery of alert %s raised %s",
                         channel, row_id, type(error).__name__)
            ok = False
        if not ok:
            failed.append(f'{channel}: failed')
            continue
        pending.remove(channel)
        row.pending_channels = ','.join(pending)
        if not pending:
            row.status = STATUS_SENT
            row.sent_at = datetime.now()
        db.session.commit()

    if not pending:
        logger.info("Alert outbox: delivered alert %s (%s)", row_id, row.alert_type)
        return True

    row.attempts = (row.attempts or 0) + 1
    row.last_error = '; '.join(failed)
    if row.attempts >= MAX_ATTEMPTS:
        row.status = STATUS_ABANDONED
        logger.error(
            "Alert outbox: giving up on alert %s (%s) after %d attempts; "
            "undelivered channels: %s",
            row.id, row.alert_type, row.attempts, row.pending_channels)
    else:
        delay = min(2 ** (row.attempts - 1), MAX_BACKOFF_MINUTES)
        row.next_attempt_at = now + timedelta(minutes=delay)
        logger.warning(
            "Alert outbox: alert %s (%s) still pending on %s; retry in %d min",
            row.id, row.alert_type, row.pending_channels, delay)
    db.session.commit()
    return False


def _vm_view(payload):
    """The VM snapshot shaped like the VMRecord the senders were written for."""
    vm = dict(payload.get('vm') or {})
    for key in ('created_at', 'expires_at'):
        if vm.get(key):
            vm[key] = datetime.fromisoformat(vm[key])
    return SimpleNamespace(**vm)


def _send_file(row, payload):
    from . import tasks  # tasks imports this module at load time
    return tasks._log_alert_to_file(_vm_view(payload), payload['message'])


def _send_database(row, payload):
    details = payload.get('details')
    db.session.add(AdminAlert(
        alert_type=row.alert_type,
        severity=payload['severity'],
        message=payload['message'],
        vmid=payload.get('vmid'),
        vm_name=payload.get('vm_name'),
        details=json.dumps(details) if details is not None else None,
    ))
    return True


def _send_email(row, payload):
    from . import tasks
    return tasks._send_email_alert(_vm_view(payload), payload['message'])


def _send_webhook(row, payload):
    from . import tasks
    return tasks._send_webhook_alert(_vm_view(payload), payload['message'])


_SENDERS = {
    'file': _send_file,
    'database': _send_database,
    'email': _send_email,
    'webhook': _send_webhook,
}
