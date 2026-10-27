"""
Background tasks module
Handles periodic tasks like checking for expired VMs
"""

import time
import logging
import os
import json
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from .proxmox_service import (
    confirm_proxmox_absence, get_cluster_vm_index, get_proxmox_connection,
    inspect_vm_guest, reset_cluster_vm_index, vm_lifecycle_lock,
    wait_for_proxmox_task, VM_GUEST_ABSENT, VM_GUEST_MATCH,
)
from .orphan_reaper import maybe_reap_orphaned_vms
from .alert_outbox import dispatch_pending_alerts, enqueue_alert
from .untracked_survivors import recover_untracked_survivors
from .models import db, VMRecord, AdminAlert

logger = logging.getLogger(__name__)


def _forget_vm_caches(node, vmid):
    """Drop every cache entry keyed to a vmid whose row has just gone.

    Imported lazily to avoid an import cycle through app.routes.
    """
    try:
        from .routes.utils import forget_vm_state, invalidate_vm_ip_cache
        invalidate_vm_ip_cache(node, vmid)
        forget_vm_state(node, vmid)
        reset_cluster_vm_index()
    except Exception:
        # Cache bookkeeping must never break the expiry loop.
        logger.debug("cache invalidation failed for vm %s on %s", vmid, node,
                     exc_info=True)


def _retire_vm_record(vm_record):
    """Remove a normal VM row, but retain KOTH history as destroyed.

    KothBox.vm_record_id deliberately has no cascading delete: the box and
    score history outlive the VM.  Hard-deleting its VMRecord either violates
    the production FK or leaves a dangling reference on permissive databases.
    """
    if vm_record.koth_game_id is not None:
        vm_record.destroyed = True
        vm_record.destroyed_at = datetime.now()
    else:
        db.session.delete(vm_record)


# Retry limit for VM deletion attempts
VM_DELETION_MAX_RETRIES = 5
VM_DELETION_MAX_BACKOFF_MINUTES = 60


def _deletion_retry_due(vm_record, now):
    """Whether exponential retry backoff permits another Proxmox attempt."""
    if not vm_record.deletion_last_attempt_at or vm_record.deletion_retry_count <= 0:
        return True
    delay_minutes = min(
        2 ** (vm_record.deletion_retry_count - 1),
        VM_DELETION_MAX_BACKOFF_MINUTES,
    )
    return now >= (
        vm_record.deletion_last_attempt_at + timedelta(minutes=delay_minutes)
    )


def reconcile_missing_vm_records(proxmox):
    """Retire live records whose guest was deleted outside the portal.

    A guest deleted by hand in Proxmox otherwise keeps its record, and with
    it the admission commitment, until expiry. A record is retired only when
    fresh cluster inventory has not listed its VMID on two sightings at
    least ORPHAN_CONFIRM_SECONDS apart with no sighting of it in between,
    the last confirmed under the VMID lifecycle lock. A failed inventory
    read is never evidence: it restarts the count. Returns retired VMIDs.
    """
    # Lazy import: app.routes imports this module.
    from .routes.utils import (
        note_vm_state, VM_MISSING, VM_PRESENT, VM_UNKNOWN,
    )

    now = datetime.now()
    live = [(vm.id, vm.vmid, vm.node) for vm in
            VMRecord.query.filter(VMRecord.destroyed == False,  # noqa: E712
                                  VMRecord.expires_at > now).all()]
    if not live:
        return []
    try:
        index = get_cluster_vm_index(proxmox, max_age=0, allow_stale=False)
    except Exception as error:
        logger.warning("Record reconciliation skipped; inventory "
                       "unavailable: %s", error)
        return []

    retired = []
    for record_id, vmid, node in live:
        if int(vmid) in index:
            note_vm_state(node, vmid, VM_PRESENT)
            continue
        with vm_lifecycle_lock(vmid):
            # Fresh transaction under the lock: a concurrent destroy or
            # replacement must be visible before deciding.
            db.session.rollback()
            vm_record = db.session.get(VMRecord, record_id)
            if (vm_record is None or vm_record.destroyed
                    or vm_record.vmid != vmid):
                continue
            absent = confirm_proxmox_absence(proxmox, vmid)
            if not note_vm_state(node, vmid,
                                 VM_MISSING if absent else VM_UNKNOWN):
                if absent:
                    logger.warning(
                        "VM %s (%s) is not in the cluster inventory; keeping "
                        "its record until a later sighting confirms it",
                        vmid, vm_record.vm_name)
                continue

            message = (f"VM {vmid} ({vm_record.vm_name}) on node {node} was "
                       "deleted outside the portal: the Proxmox cluster "
                       "inventory no longer lists it. Its record has been "
                       "retired before its expiry.")
            db.session.add(AdminAlert(
                alert_type='vm_record_reconciled',
                severity='warning',
                message=message,
                vmid=vmid,
                vm_name=vm_record.vm_name,
                details=json.dumps({
                    'reason': 'absent_from_cluster_inventory',
                    'vm_type': vm_record.vm_type,
                    'node': node,
                    'student_name': vm_record.student_name,
                    'template_name': vm_record.template_name,
                    'expires_at': vm_record.expires_at.isoformat(),
                }),
            ))
            _retire_vm_record(vm_record)
            db.session.commit()
            _forget_vm_caches(node, vmid)
            logger.warning("Retired record for VM %s: %s", vmid, message)
            retired.append(vmid)
    return retired

# Alert file path
ALERT_LOG_FILE = "/app/data/failed_vms.log"


def _drain_durable_queues():
    """Import survivors recorded during a database outage, then deliver
    queued alerts. Runs before any Proxmox call so a Proxmox outage cannot
    hold back the alerts that matter most during one."""
    for step in (recover_untracked_survivors, dispatch_pending_alerts):
        try:
            step()
        except Exception:
            db.session.rollback()
            logger.exception("%s failed; retrying next pass", step.__name__)


def initialize_vm_monitoring(app):
    """
    Initialize VM monitoring on app startup
    
    Checks the database for VMs that haven't been destroyed and ensures
    they are being monitored for expiration, even if the app was restarted.
    
    Args:
        app: Flask application instance
    """
    try:
        with app.app_context():
            # Before listing: a survivor recorded while the database was
            # down becomes a tracked, already-expired VM here.
            recover_untracked_survivors()
            logger.info("Initializing VM monitoring from database...")
            active_vms = VMRecord.query.filter_by(destroyed=False).all()
            if active_vms:
                logger.info(f"Found {len(active_vms)} active VMs in database:")
                for vm in active_vms:
                    status = "EXPIRED" if datetime.now() > vm.expires_at else "ACTIVE"
                    retry_info = f" (retry {vm.deletion_retry_count}/{VM_DELETION_MAX_RETRIES})" if vm.deletion_retry_count > 0 else ""
                    logger.info(f"  - {vm.vm_name} (ID: {vm.vmid}): {status}, expires at {vm.expires_at}{retry_info}")
            else:
                logger.info("No active VMs found in database")
    except Exception as e:
        logger.error(f"Error initializing VM monitoring: {str(e)}")


def check_and_destroy_expired_vms(app):
    """
    Background task that checks and destroys expired VMs created by this app
    
    Runs every 60 seconds. Only checks database records for VMs created by
    this app, stops and deletes expired VMs, and updates the database accordingly.
    Does NOT clean up VMs that were not created through this Flask app.
    
    Implements 4-phase batch processing:
    1. Stop all expired VMs whose retry backoff has elapsed
    2. Delete all stopped VMs and await each delete task
    3. Retire only VMs confirmed deleted
    4. Keep failed VMs tracked, retry with backoff, and alert once
    
    Args:
        app: Flask application instance
    """
    worker_id = os.environ.get('GUNICORN_WORKER_ID', 'dev')
    logger.info(f"VM expiration checker thread started (Worker: {worker_id}, PID: {os.getpid()})")
    
    while True:
        try:
            time.sleep(60)  # Check every 60 seconds
            
            with app.app_context():
                logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Worker {worker_id}] Checking for expired VMs...")
                _drain_durable_queues()

                proxmox = get_proxmox_connection()
                
                # Check all active VMs in database for expiration
                current_time = datetime.now()
                active_vms = VMRecord.query.filter_by(destroyed=False).all()
                
                logger.info(f"[Worker {worker_id}] Found {len(active_vms)} active VMs in database")
                
                # Collect all expired VMs first
                expired_vms = []
                for vm_record in active_vms:
                    try:
                        time_remaining = (vm_record.expires_at - current_time).total_seconds()
                        
                        if current_time > vm_record.expires_at:
                            retry_info = f" (retry {vm_record.deletion_retry_count}/{VM_DELETION_MAX_RETRIES})" if vm_record.deletion_retry_count > 0 else ""
                            if _deletion_retry_due(vm_record, current_time):
                                logger.info(f"[Worker {worker_id}] VM {vm_record.vmid} ({vm_record.vm_name}) has EXPIRED{retry_info}")
                                expired_vms.append(vm_record)
                            else:
                                logger.info(f"[Worker {worker_id}] VM {vm_record.vmid} deletion retry is in backoff{retry_info}")
                        else:
                            logger.info(f"[Worker {worker_id}] VM {vm_record.vmid} ({vm_record.vm_name}) still active. Expires in {int(time_remaining/60)} minutes")
                    except Exception as e:
                        logger.info(f"[Worker {worker_id}] Error checking VM {vm_record.vmid}: {str(e)}")
                
                for candidate in expired_vms:
                    vmid = candidate.vmid
                    record_id = candidate.id
                    with vm_lifecycle_lock(vmid):
                        # End MySQL's REPEATABLE READ snapshot while holding
                        # the VMID lock, then load the current row.
                        db.session.rollback()
                        vm_record = db.session.get(VMRecord, record_id)
                        if vm_record is None or vm_record.destroyed:
                            continue
                        # The batch was selected before this lock was held,
                        # so an extension may have committed since. The
                        # extend route writes expires_at under this same
                        # lock, so this fresh read is the deciding one.
                        if vm_record.expires_at > datetime.now():
                            logger.info(
                                f"[Worker {worker_id}] VM {vmid} was extended "
                                f"to {vm_record.expires_at}; not destroying")
                            continue

                        identity = inspect_vm_guest(
                            proxmox, vm_record.vmid,
                            node=vm_record.node,
                            vm_name=vm_record.vm_name,
                            vm_type=vm_record.vm_type,
                        )
                        deleted = identity == VM_GUEST_ABSENT
                        if identity == VM_GUEST_MATCH:
                            deleted = (
                                _stop_vm(proxmox, vm_record)
                                and _delete_vm(proxmox, vm_record)
                            )

                        if deleted:
                            node = vm_record.node
                            _retire_vm_record(vm_record)
                            db.session.commit()
                            _forget_vm_caches(node, vmid)
                            logger.info(
                                f"[Worker {worker_id}] Removed VM {vmid} "
                                "from database")
                        else:
                            vm_record.deletion_retry_count += 1
                            vm_record.deletion_last_attempt_at = current_time
                            retry_count = vm_record.deletion_retry_count
                            should_alert = (
                                retry_count >= VM_DELETION_MAX_RETRIES
                                and not vm_record.deletion_alert_sent
                            )
                            if should_alert:
                                vm_record.deletion_alert_sent = True
                                # Same commit as the flag: a crash after it
                                # can no longer swallow the only alert.
                                _enqueue_deletion_failure_alert(vm_record)
                            db.session.commit()
                            logger.info(
                                f"[Worker {worker_id}] VM {vmid} deletion "
                                f"failed (attempt {retry_count}); record "
                                "retained for retry")
                    time.sleep(1)

                reconcile_missing_vm_records(proxmox)

                # Orphan cleanup runs after expiry and is time-boxed
                # (ORPHAN_REAPER_MAX_PASS_*): a backlog of hung orphans must
                # never stand between an expired VM and its destroy.
                maybe_reap_orphaned_vms(proxmox)
        
        except Exception as e:
            logger.error(f"Error in expiration checker: {str(e)}")
            import traceback
            traceback.print_exc()


def _stop_vm(proxmox, vm_record):
    """
    Stop a VM without deleting it
    
    Args:
        proxmox (ProxmoxAPI): Proxmox API connection
        vm_record (VMRecord): Database VM record
    
    Returns:
        bool: True if stopped successfully or already stopped, False on failure
    """
    vmid = vm_record.vmid
    vm_type = vm_record.vm_type
    node = vm_record.node
    vm_name = vm_record.vm_name
    
    try:
        if vm_type == 'lxc':
            stop_upid = proxmox.nodes(node).lxc(vmid).status.stop.post()
        else:  # qemu
            stop_upid = proxmox.nodes(node).qemu(vmid).status.stop.post()
        if stop_upid:
            result = wait_for_proxmox_task(
                proxmox, node, stop_upid, timeout=60, poll_interval=1)
            if not result.get('success'):
                logger.error(
                    "Stop task for VM %s failed: %s",
                    vmid, result.get('error', result.get('status')))
                return False
        logger.info(f"Stopped {vm_type} VM {vmid} ({vm_name}) on node {node}")
        return True
    except Exception as e:
        error_msg = str(e).lower()
        # If already stopped or doesn't exist, consider it successful
        if 'already stopped' in error_msg or 'not running' in error_msg or 'does not exist' in error_msg or 'configuration file' in error_msg:
            logger.info(f"VM {vmid} already stopped or not found")
            return True
        else:
            logger.error(f"Error stopping VM {vmid}: {str(e)}")
            return False


def _delete_vm(proxmox, vm_record):
    """
    Delete a VM (should already be stopped)
    
    Args:
        proxmox (ProxmoxAPI): Proxmox API connection
        vm_record (VMRecord): Database VM record
    
    Returns:
        bool: True if deleted successfully or already deleted, False on failure
    """
    vmid = vm_record.vmid
    vm_type = vm_record.vm_type
    node = vm_record.node
    vm_name = vm_record.vm_name
    
    try:
        if vm_type == 'lxc':
            delete_upid = proxmox.nodes(node).lxc(vmid).delete()
        else:  # qemu
            delete_upid = proxmox.nodes(node).qemu(vmid).delete()
        result = wait_for_proxmox_task(
            proxmox, node, delete_upid, timeout=120, poll_interval=1)
        if result.get('success'):
            logger.info(f"Deleted {vm_type} VM {vmid} ({vm_name}) on node {node}")
            return True
        logger.error(
            "Destroy task for VM %s failed: %s",
            vmid, result.get('error', result.get('status')))
        return False
    except Exception as error:
        if confirm_proxmox_absence(proxmox, vmid, error):
            logger.info(f"VM {vmid} confirmed absent from cluster inventory")
            return True
        logger.error(f"Error deleting VM {vmid}: {str(error)}")
        return False


def _deletion_alert_details(vm_record):
    return {
        'vm_type': vm_record.vm_type,
        'node': vm_record.node,
        'student_name': vm_record.student_name,
        'template_name': vm_record.template_name,
        'retry_attempts': vm_record.deletion_retry_count,
        'created_at': vm_record.created_at.isoformat(),
        'expires_at': vm_record.expires_at.isoformat()
    }


def _enqueue_deletion_failure_alert(vm_record):
    """Queue the stuck-VM alert for every channel (file, database, email,
    webhook) in the caller's transaction; app/alert_outbox.py delivers it.

    Args:
        vm_record (VMRecord): The VM that failed to delete
    """
    message = (f"VM {vm_record.vmid} ({vm_record.vm_name}) on node {vm_record.node} "
               f"failed to delete after {VM_DELETION_MAX_RETRIES} attempts. "
               f"Manual intervention required.")
    logger.info(f"ALERT queued: {message}")
    details = _deletion_alert_details(vm_record)
    enqueue_alert(
        alert_type='vm_deletion_failed',
        severity='critical',
        message=message,
        vmid=vm_record.vmid,
        vm_name=vm_record.vm_name,
        details=details,
        vm={
            'vmid': vm_record.vmid,
            'vm_name': vm_record.vm_name,
            'vm_type': vm_record.vm_type,
            'node': vm_record.node,
            'student_name': vm_record.student_name,
            'template_name': vm_record.template_name,
            'deletion_retry_count': vm_record.deletion_retry_count,
            'created_at': details['created_at'],
            'expires_at': details['expires_at'],
        },
    )


def _log_alert_to_file(vm_record, message):
    """Log alert to file. Returns whether the entry was written."""
    try:
        with open(ALERT_LOG_FILE, 'a') as f:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            f.write(f"[{timestamp}] CRITICAL: {message}\n")
            f.write(f"  Details: VMID={vm_record.vmid}, Type={vm_record.vm_type}, Node={vm_record.node}, Student={vm_record.student_name}\n")
        logger.info(f"Alert logged to {ALERT_LOG_FILE}")
        return True
    except Exception as e:
        logger.error(f"Error logging alert to file: {str(e)}")
        return False


def _create_database_alert(vm_record, message):
    """Create alert in database"""
    try:
        alert = AdminAlert(
            alert_type='vm_deletion_failed',
            severity='critical',
            message=message,
            vmid=vm_record.vmid,
            vm_name=vm_record.vm_name,
            details=json.dumps(_deletion_alert_details(vm_record))
        )
        db.session.add(alert)
        db.session.commit()
        logger.info(f"Alert created in database (ID: {alert.id})")
    except Exception as e:
        logger.error(f"Error creating database alert: {str(e)}")
        db.session.rollback()


def _send_email_alert(vm_record, message):
    """Send email alert if SMTP is configured. Returns False only when a
    configured send failed, so the alert outbox retries it."""
    try:
        # Get email settings from environment
        smtp_server = os.environ.get('SMTP_SERVER')
        smtp_port = int(os.environ.get('SMTP_PORT', '587'))
        smtp_username = os.environ.get('SMTP_USERNAME')
        smtp_password = os.environ.get('SMTP_PASSWORD')
        smtp_from = os.environ.get('SMTP_FROM_EMAIL')
        admin_emails = os.environ.get('ADMIN_ALERT_EMAILS', '').split(',')
        
        if not all([smtp_server, smtp_username, smtp_password, smtp_from]) or not admin_emails[0]:
            logger.info("Email notifications not configured - skipping email alert")
            return True
        
        # Create email
        msg = MIMEMultipart()
        msg['From'] = smtp_from
        msg['To'] = ', '.join(admin_emails)
        msg['Subject'] = f"[CRITICAL] VM Deletion Failed: {vm_record.vm_name}"
        
        body = f"""
VM Deletion Failure Alert

{message}

VM Details:
- VMID: {vm_record.vmid}
- Name: {vm_record.vm_name}
- Type: {vm_record.vm_type}
- Node: {vm_record.node}
- Student: {vm_record.student_name}
- Template: {vm_record.template_name}
- Created: {vm_record.created_at}
- Expired: {vm_record.expires_at}
- Retry Attempts: {vm_record.deletion_retry_count}

Action Required:
Please manually delete this VM from the Proxmox web interface or investigate why automated deletion failed.
"""
        
        msg.attach(MIMEText(body, 'plain'))
        
        # Send email
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
        
        logger.info(f"Email alert sent to {len(admin_emails)} recipient(s)")
        return True
    except Exception as e:
        logger.error(f"Error sending email alert: {str(e)}")
        return False


def _send_webhook_alert(vm_record, message):
    """Send webhook alert (Slack, Discord, etc.) if configured. Returns False
    only when a configured send failed, so the alert outbox retries it."""
    try:
        webhook_url = os.environ.get('ALERT_WEBHOOK_URL')
        webhook_type = os.environ.get('ALERT_WEBHOOK_TYPE', 'slack')  # 'slack' or 'discord'
        
        if not webhook_url:
            logger.info("Webhook notifications not configured - skipping webhook alert")
            return True
        
        # Format message for webhook type
        if webhook_type == 'discord':
            payload = {
                "embeds": [{
                    "title": "🚨 VM Deletion Failed",
                    "description": message,
                    "color": 15158332,  # Red
                    "fields": [
                        {"name": "VMID", "value": str(vm_record.vmid), "inline": True},
                        {"name": "Name", "value": vm_record.vm_name, "inline": True},
                        {"name": "Node", "value": vm_record.node, "inline": True},
                        {"name": "Student", "value": vm_record.student_name, "inline": True},
                        {"name": "Retries", "value": str(vm_record.deletion_retry_count), "inline": True}
                    ]
                }]
            }
        else:  # Slack format (default)
            payload = {
                "text": f"🚨 *VM Deletion Failed*\n{message}",
                "attachments": [{
                    "color": "danger",
                    "fields": [
                        {"title": "VMID", "value": str(vm_record.vmid), "short": True},
                        {"title": "Name", "value": vm_record.vm_name, "short": True},
                        {"title": "Node", "value": vm_record.node, "short": True},
                        {"title": "Student", "value": vm_record.student_name, "short": True},
                        {"title": "Retries", "value": str(vm_record.deletion_retry_count), "short": True}
                    ]
                }]
            }
        
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Webhook alert sent successfully ({webhook_type})")
        return True
    except Exception as e:
        logger.error(f"Error sending webhook alert: {str(e)}")
        return False
