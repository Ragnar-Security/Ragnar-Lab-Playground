"""Conservatively reap expired Proxmox guests left by interrupted deploys.

The database remains the source of truth for normal lifecycle handling. This
sweeper only acts when Proxmox still has an app-named guest but no database or
KOTH reference survived the deploy that created it.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from .config import (
    ORPHAN_REAPER_DRY_RUN,
    ORPHAN_REAPER_ENABLED,
    ORPHAN_REAPER_GRACE_MINUTES,
    ORPHAN_REAPER_MAX_PASS_GUESTS,
    ORPHAN_REAPER_MAX_PASS_SECONDS,
)
from .models import AdminAlert, KothBox, VMRecord, db
from .proxmox_service import (
    is_vmid_reserved,
    reset_cluster_vm_index,
    vm_lifecycle_lock,
    wait_for_proxmox_task,
)


logger = logging.getLogger(__name__)

ORPHAN_REAPER_INTERVAL_SECONDS = 10 * 60
_EXPIRATION_LINE = re.compile(r"(?:^|\r?\n)\s*Expires:\s*([^\s<]+)", re.MULTILINE)
_schedule_lock = threading.Lock()
_last_sweep_at = None


def reset_reaper_schedule():
    """Reset process-local scheduling state (used when a worker starts/tests)."""
    global _last_sweep_at
    with _schedule_lock:
        _last_sweep_at = None


def maybe_reap_orphaned_vms(proxmox, *, monotonic_now=None):
    """Run one sweep when enabled and at least ten minutes since the last one."""
    global _last_sweep_at

    if not ORPHAN_REAPER_ENABLED:
        logger.info("Orphan reaper disabled by ORPHAN_REAPER_ENABLED")
        return []

    tick = time.monotonic() if monotonic_now is None else monotonic_now
    with _schedule_lock:
        if (_last_sweep_at is not None
                and tick - _last_sweep_at < ORPHAN_REAPER_INTERVAL_SECONDS):
            logger.info("Orphan reaper skipped; ten-minute interval has not elapsed")
            return []
        # Reserve the interval before making network calls. Two expiry-loop
        # greenlets must never run destructive sweeps concurrently.
        _last_sweep_at = tick

    try:
        return reap_orphaned_vms(proxmox)
    except Exception:
        # A failed SQL statement leaves this scoped session unusable until it
        # is rolled back; without this, the normal expiry pass in the same
        # loop iteration would fail too.
        db.session.rollback()
        # An inventory or database outage is an unknown state, never evidence
        # that a guest is orphaned. Leave every VM alone and let the next
        # scheduled sweep retry.
        logger.exception("Orphan reaper sweep failed; no further guests considered")
        return []


def reap_orphaned_vms(proxmox, *, now=None, grace_minutes=None, dry_run=None,
                      max_seconds=None, max_candidates=None, clock=None):
    """Run one conservative cluster-wide orphan sweep.

    Returns one report entry for each guest that reached the stop phase. Guests
    rejected by a safety guard are logged and omitted from the report.

    The sweep starts no further guest once ``max_seconds`` have elapsed or
    ``max_candidates`` guests reached the stop phase (None: the configured
    pass budget). Guests left over are found again by the next sweep.
    """
    current_time = datetime.now() if now is None else now
    grace = (ORPHAN_REAPER_GRACE_MINUTES
             if grace_minutes is None else grace_minutes)
    is_dry_run = ORPHAN_REAPER_DRY_RUN if dry_run is None else dry_run
    budget_seconds = (ORPHAN_REAPER_MAX_PASS_SECONDS
                      if max_seconds is None else max_seconds)
    budget_guests = (ORPHAN_REAPER_MAX_PASS_GUESTS
                     if max_candidates is None else max_candidates)
    monotonic = time.monotonic if clock is None else clock
    started = monotonic()
    attempted = 0

    # This direct call is intentionally not served from the shared inventory
    # cache: every destructive sweep needs one coherent, current cluster view.
    resources = proxmox.cluster.resources.get(type='vm') or []

    # Include destroyed rows. A retained row is evidence that the guest is
    # known to the portal, not permission for this fallback path to second-
    # guess the normal lifecycle worker.
    tracked_rows = db.session.query(VMRecord.vmid, VMRecord.node).all()
    tracked_vmids = {int(vmid) for vmid, _node in tracked_rows}
    tracked_node_vmids = {(node, int(vmid)) for vmid, node in tracked_rows}

    # KOTH boxes normally point through VMRecord. Names are checked as a
    # second backstop for partially-created/manual rows whose record link is
    # still NULL; KOTH ownership must never be inferred from absence alone.
    koth_names = {name for (name,) in db.session.query(KothBox.name).all() if name}
    koth_vm_rows = (
        db.session.query(VMRecord.vmid, VMRecord.node)
        .join(KothBox, KothBox.vm_record_id == VMRecord.id)
        .all()
    )
    koth_vmids = {int(vmid) for vmid, _node in koth_vm_rows}
    koth_node_vmids = {(node, int(vmid)) for vmid, node in koth_vm_rows}

    reports = []
    for resource in resources:
        vmid, node, name, vm_type = _resource_identity(resource)
        if vmid is None:
            logger.info("Orphan reaper skipped malformed cluster resource: %r", resource)
            continue
        if not _has_app_name(name):
            logger.info("Orphan reaper kept VM %s on %s: name %r is not app-owned",
                        vmid, node, name)
            continue
        if _is_template(resource.get('template')):
            logger.info("Orphan reaper kept VM %s (%s): guest is a template", vmid, name)
            continue
        if vm_type not in ('qemu', 'lxc') or not node:
            logger.info("Orphan reaper kept VM %s (%s): unsupported type/node %r/%r",
                        vmid, name, vm_type, node)
            continue
        if is_vmid_reserved(vmid):
            logger.info(
                "Orphan reaper kept VM %s (%s): active create reservation",
                vmid, name)
            continue
        if (vmid in tracked_vmids or (node, vmid) in tracked_node_vmids):
            logger.info("Orphan reaper kept VM %s (%s): VMRecord exists", vmid, name)
            continue
        if (vmid in koth_vmids or (node, vmid) in koth_node_vmids
                or name in koth_names):
            logger.info("Orphan reaper kept VM %s (%s): KOTH box references it",
                        vmid, name)
            continue

        # Checked before the first network call for this guest: the expiry
        # pass of the next loop iteration waits for this sweep to return.
        elapsed = monotonic() - started
        if elapsed >= budget_seconds or attempted >= budget_guests:
            logger.warning(
                "Orphan reaper stopped this sweep after %.0fs and %d guest(s) "
                "(budget %ss / %d); remaining guests wait for the next sweep",
                elapsed, attempted, budget_seconds, budget_guests)
            break

        guest = getattr(proxmox.nodes(node), vm_type)(vmid)
        try:
            vm_config = guest.config.get()
        except Exception as exc:
            if _is_not_found(exc):
                logger.info("Orphan reaper skipped VM %s (%s): it vanished before config read",
                            vmid, name)
            else:
                logger.error("Orphan reaper kept VM %s (%s): config read failed: %s",
                             vmid, name, exc)
            continue

        expires_at = _parse_expiration((vm_config or {}).get('description'))
        if expires_at is None:
            logger.info("Orphan reaper kept VM %s (%s): missing/unparseable Expires timestamp",
                        vmid, name)
            continue
        if not _past_grace(expires_at, current_time, grace):
            logger.info("Orphan reaper kept VM %s (%s): expiry %s is inside grace period",
                        vmid, name, expires_at.isoformat())
            continue

        # Destroy paths hold this lock while they decide and commit. Taking it
        # here makes the final re-check and the stop/delete one decision that
        # cannot interleave with them.
        with vm_lifecycle_lock(vmid):
            report = _recheck_and_reap(proxmox, guest, resource, vmid, node,
                                       name, vm_type, expires_at, is_dry_run)
        if report is not None:
            attempted += 1
            reports.append(report)

    return reports


def _recheck_and_reap(proxmox, guest, resource, vmid, node, name, vm_type,
                      expires_at, is_dry_run):
    """Re-check one candidate, then stop and delete it.

    Returns a report entry, or None when the guest turned out to be owned.
    The caller holds vm_lifecycle_lock(vmid).
    """
    # End the initial inventory transaction before re-checking. Production
    # MySQL uses REPEATABLE READ, so another deploy's newly committed row
    # would otherwise remain invisible in this session's first snapshot.
    db.session.rollback()
    # Allocation owns the VMID until after its VMRecord commits. Check the
    # lease first and the database second so that handoff has no gap: if
    # the lease disappears between these checks, the committed row is
    # already visible to the following fresh query.
    if is_vmid_reserved(vmid):
        logger.info(
            "Orphan reaper kept VM %s (%s): create reservation appeared "
            "during sweep", vmid, name)
        return None
    if _has_live_reference(vmid, node, name):
        logger.info("Orphan reaper kept VM %s (%s): reference appeared during sweep",
                    vmid, name)
        return None

    report = {'vmid': vmid, 'node': node, 'name': name}
    if is_dry_run:
        logger.warning("Orphan reaper dry-run would stop/delete VM %s (%s) on %s",
                       vmid, name, node)
        return {**report, 'status': 'dry_run'}

    if resource.get('status') == 'stopped':
        # Interrupted deploys often die before start_vm, and a later sweep
        # may have completed stop but failed delete. Proxmox rejects a
        # second LXC stop with HTTP 500, so inventory-confirmed stopped
        # guests go directly to the awaited delete.
        logger.info("Orphan reaper VM %s (%s) is already stopped", vmid, name)
    else:
        try:
            stop_upid = guest.status.stop.post()
            stop_result = wait_for_proxmox_task(
                proxmox, node, stop_upid, timeout=60, poll_interval=1)
        except Exception as exc:
            if _is_not_found(exc):
                logger.info("Orphan reaper VM %s (%s) vanished before stop", vmid, name)
                return {**report, 'status': 'not_found'}
            logger.error("Orphan reaper failed to stop VM %s (%s): %s",
                         vmid, name, exc)
            return {**report, 'status': 'stop_failed', 'error': str(exc)}
        if not stop_result.get('success'):
            error = stop_result.get('error', stop_result.get('status', 'unknown'))
            logger.error("Orphan reaper stop task failed for VM %s (%s): %s",
                         vmid, name, error)
            return {**report, 'status': 'stop_failed', 'error': error}

    try:
        delete_upid = guest.delete()
        delete_result = wait_for_proxmox_task(
            proxmox, node, delete_upid, timeout=120, poll_interval=1)
    except Exception as exc:
        if _is_not_found(exc):
            logger.info("Orphan reaper VM %s (%s) was already deleted", vmid, name)
            return {**report, 'status': 'not_found'}
        logger.error("Orphan reaper failed to request delete for VM %s (%s): %s",
                     vmid, name, exc)
        return {**report, 'status': 'delete_failed', 'error': str(exc)}
    if not delete_result.get('success'):
        error = delete_result.get('error', delete_result.get('status', 'unknown'))
        logger.error("Orphan reaper delete task failed for VM %s (%s): %s",
                     vmid, name, error)
        return {**report, 'status': 'delete_failed', 'error': error}

    reset_cluster_vm_index()
    logger.warning("Orphan reaper destroyed expired VM %s (%s) on %s",
                   vmid, name, node)
    _record_reaped_alert(vmid, name, node, vm_type, expires_at)
    return {**report, 'status': 'reaped'}


def _resource_identity(resource):
    try:
        vmid = int(resource['vmid'])
    except (KeyError, TypeError, ValueError):
        return None, resource.get('node'), resource.get('name'), resource.get('type')
    return vmid, resource.get('node'), resource.get('name'), resource.get('type')


def _has_app_name(name):
    return isinstance(name, str) and name.startswith(('student-', 'group-'))


def _is_template(value):
    return value not in (None, False, 0, '0', '', 'false', 'False')


def _parse_expiration(description):
    if not isinstance(description, str):
        return None
    match = _EXPIRATION_LINE.search(description)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1))
    except (TypeError, ValueError):
        return None


def _past_grace(expires_at, now, grace_minutes):
    deadline = expires_at + timedelta(minutes=grace_minutes)
    if deadline.tzinfo is None:
        comparable_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
    elif now.tzinfo is None:
        comparable_now = now.astimezone(deadline.tzinfo)
    else:
        comparable_now = now.astimezone(deadline.tzinfo)
    return comparable_now > deadline


def _is_not_found(exc):
    # Proxmoxer ResourceException exposes the HTTP status here. Never search
    # the message for "404": URLs contain VMIDs such as 404/1404 and caused
    # live guests to be mistaken for missing in the past.
    return getattr(exc, 'status_code', None) == 404


def _has_live_reference(vmid, node, name):
    vm_record = (
        db.session.query(VMRecord.id)
        .filter(or_(
            VMRecord.vmid == vmid,
            and_(VMRecord.node == node, VMRecord.vmid == vmid),
        ))
        .first()
    )
    if vm_record is not None:
        return True
    return (
        db.session.query(KothBox.id)
        .outerjoin(VMRecord, KothBox.vm_record_id == VMRecord.id)
        .filter(or_(
            KothBox.name == name,
            VMRecord.vmid == vmid,
            and_(VMRecord.node == node, VMRecord.vmid == vmid),
        ))
        .first()
        is not None
    )


def _record_reaped_alert(vmid, name, node, vm_type, expires_at):
    """Create one admin-visible alert for a VM positively reaped."""
    message = (f"Orphan reaper destroyed expired {vm_type} VM {vmid} "
               f"({name}) on node {node}.")
    try:
        db.session.add(AdminAlert(
            alert_type='vm_orphan_reaped',
            severity='warning',
            message=message,
            vmid=vmid,
            vm_name=name,
            details=json.dumps({
                'vm_type': vm_type,
                'node': node,
                'expires_at': expires_at.isoformat(),
            }),
        ))
        db.session.commit()
        logger.info("Orphan reaper created admin alert for VM %s", vmid)
    except Exception:
        # The VM is already gone. Preserve that truthful outcome while making
        # the lost notification visible to operators in application logs.
        db.session.rollback()
        logger.exception("Orphan reaper could not create admin alert for VM %s", vmid)
