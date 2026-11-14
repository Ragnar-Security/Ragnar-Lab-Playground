"""Conservatively reap expired guests and cloud resources left by interrupted deploys.

The database remains the source of truth for normal lifecycle handling. This
sweeper only acts when a provider still has an app-owned guest or network but
no database or KOTH reference survived the deploy that created it.

Two passes share one pass budget: the raw Proxmox sweep (unchanged, ruling
M11) and a provider-generic sweep over each enabled cloud backend's
list_managed() inventory.
"""

import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from . import coordination
from .backends import enabled_backends, get_backend
from .backends.base import BACKEND_PROXMOX
from .backends.cloud_common import deployment_key, slot_lock_key
from .config import (
    ORPHAN_REAPER_DRY_RUN,
    ORPHAN_REAPER_ENABLED,
    ORPHAN_REAPER_GRACE_MINUTES,
    ORPHAN_REAPER_MAX_PASS_GUESTS,
    ORPHAN_REAPER_MAX_PASS_SECONDS,
)
from .alert_outbox import enqueue_alert
from .models import (
    KothBox, NodeMemoryReservation, VMIDReservation, VMRecord, db,
)
from .proxmox_service import (
    VM_GUEST_ABSENT,
    VM_GUEST_MATCH,
    confirm_proxmox_absence,
    inspect_vm_guest,
    is_proxmox_not_found,
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


def maybe_reap_orphaned_vms(proxmox=None, *, monotonic_now=None):
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


def reap_orphaned_vms(proxmox=None, *, now=None, grace_minutes=None, dry_run=None,
                      max_seconds=None, max_candidates=None, clock=None):
    """Run one conservative orphan sweep: Proxmox, then every enabled cloud.

    Returns one report entry for each guest that reached the stop phase and
    each cloud resource whose delete was started. Guests rejected by a
    safety guard are logged and omitted from the report. ``proxmox=None``
    (Proxmox not enabled, or unreachable this pass) runs the cloud pass only.

    The sweep starts no further guest or resource once ``max_seconds`` have
    elapsed or ``max_candidates`` items were started (None: the configured
    pass budget), counted across both passes. Items left over are found
    again by the next sweep.
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

    reports = []
    if proxmox is not None:
        try:
            _reap_proxmox_orphans(proxmox, reports, current_time, grace,
                                  is_dry_run, budget_seconds, budget_guests,
                                  monotonic, started)
        except Exception:
            # Same rule as maybe_reap_orphaned_vms: an unknown state is never
            # evidence of an orphan. It ends the Proxmox pass only; the
            # clouds are other providers with their own inventories.
            db.session.rollback()
            logger.exception("Orphan reaper Proxmox pass failed; no further "
                             "guests considered")

    reports.extend(reap_orphaned_cloud_resources(
        now_epoch=current_time.timestamp(), grace_minutes=grace,
        dry_run=is_dry_run,
        max_seconds=budget_seconds - (monotonic() - started),
        max_candidates=budget_guests - len(reports), clock=monotonic))
    return reports


def _reap_proxmox_orphans(proxmox, reports, current_time, grace, is_dry_run,
                          budget_seconds, budget_guests, monotonic, started):
    """The raw Proxmox sweep; appends each report to ``reports``."""
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
            if is_proxmox_not_found(exc):
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

    # The listing is as old as the sweep, and a stop may wait up to 60 s:
    # re-read the VMID's identity before each mutation, so a guest that
    # replaced this one at the same VMID is never stopped or deleted.
    changed = _identity_changed(proxmox, report, vmid, node, name, vm_type, 'stop')
    if changed is not None:
        return changed
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
            if confirm_proxmox_absence(proxmox, vmid, exc):
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

    changed = _identity_changed(proxmox, report, vmid, node, name, vm_type, 'delete')
    if changed is not None:
        return changed
    try:
        delete_upid = guest.delete()
        delete_result = wait_for_proxmox_task(
            proxmox, node, delete_upid, timeout=120, poll_interval=1)
    except Exception as exc:
        if confirm_proxmox_absence(proxmox, vmid, exc):
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


def _identity_changed(proxmox, report, vmid, node, name, vm_type, action):
    """None while fresh inventory still names this guest; otherwise the
    report to return instead of the mutation."""
    identity = inspect_vm_guest(proxmox, vmid, node=node, vm_name=name,
                                vm_type=vm_type)
    if identity == VM_GUEST_MATCH:
        return None
    if identity == VM_GUEST_ABSENT:
        logger.info("Orphan reaper VM %s (%s) vanished before %s", vmid, name, action)
        return {**report, 'status': 'not_found'}
    logger.error("Orphan reaper kept VM %s (%s): its identity changed or could not be "
                 "verified before %s (%s)", vmid, name, action, identity)
    return {**report, 'status': 'identity_unverified', 'error': identity}


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
    """Queue one admin-visible alert for a VM positively reaped.

    Database channel only: the reaper has never emailed or paged anyone.
    """
    message = (f"Orphan reaper destroyed expired {vm_type} VM {vmid} "
               f"({name}) on node {node}.")
    try:
        enqueue_alert(
            alert_type='vm_orphan_reaped',
            severity='warning',
            message=message,
            vmid=vmid,
            vm_name=name,
            details={
                'vm_type': vm_type,
                'node': node,
                'expires_at': expires_at.isoformat(),
            },
            channels=('database',),
        )
        db.session.commit()
        logger.info("Orphan reaper queued admin alert for VM %s", vmid)
    except Exception:
        # The VM is already gone. Preserve that truthful outcome while making
        # the lost notification visible to operators in application logs.
        db.session.rollback()
        logger.exception("Orphan reaper could not queue admin alert for VM %s", vmid)


# ---- cloud resources -------------------------------------------------------

_KIND_INSTANCE = 'instance'
_KIND_DEPLOYMENT_NETWORK = 'deployment-network'
_KIND_SLOT_NETWORK = 'slot-network'
# Returned by a recheck when the pass budget ran out before its delete.
_OUT_OF_BUDGET = object()


def reap_orphaned_cloud_resources(*, now_epoch=None, grace_minutes=None,
                                  dry_run=None, max_seconds=None,
                                  max_candidates=None, clock=None):
    """Delete leftover instances and networks of every enabled cloud backend.

    Inventory comes only from each backend's list_managed(), which reports
    this site's resources alone, and deletes go only through its
    delete_resource(), which re-proves that ownership first. A resource is
    deleted only on positive evidence that nothing owns it; any unknown
    (missing timestamp, malformed identity, template, foreign backend, failed
    inventory) keeps it. Returns one ``{"backend", "kind", "provider_id",
    "name", "status"}`` report per started delete, status ``reaped``,
    ``dry_run`` or ``delete_failed``.
    """
    current = time.time() if now_epoch is None else now_epoch
    grace_seconds = 60 * (ORPHAN_REAPER_GRACE_MINUTES
                          if grace_minutes is None else grace_minutes)
    is_dry_run = ORPHAN_REAPER_DRY_RUN if dry_run is None else dry_run
    budget = _PassBudget(
        ORPHAN_REAPER_MAX_PASS_SECONDS if max_seconds is None else max_seconds,
        ORPHAN_REAPER_MAX_PASS_GUESTS if max_candidates is None else max_candidates,
        time.monotonic if clock is None else clock)

    clouds = [name for name in enabled_backends() if name != BACKEND_PROXMOX]
    if not clouds or not budget.may_start():
        return []

    # Once, before any inventory snapshot: whatever commits after this is
    # either younger than the grace period or caught by the recheck under
    # the resource's lock.
    db.session.rollback()
    owners = _CloudOwners.load()

    reports = []
    for name in clouds:
        try:
            backend = get_backend(name)
            inventory = backend.list_managed()
        except Exception:
            # A failed listing is not an empty one.
            logger.exception("Orphan reaper skipped backend %s: inventory "
                             "unavailable", name)
            continue
        for report in _sweep_cloud_inventory(
                backend, name, inventory, owners, current, grace_seconds,
                is_dry_run, budget):
            if report is _OUT_OF_BUDGET:
                return reports
            reports.append(report)
    return reports


class _PassBudget:
    """Elapsed-time and started-item limits for one cloud pass."""

    def __init__(self, seconds, items, clock):
        self._seconds = seconds
        self._items = items
        self._clock = clock
        self._started = clock()
        self.started_items = 0

    def may_start(self):
        elapsed = self._clock() - self._started
        if elapsed >= self._seconds or self.started_items >= self._items:
            logger.warning(
                "Orphan reaper stopped the cloud sweep after %.0fs and %d "
                "item(s) (remaining budget %ss / %d); the rest wait for the "
                "next sweep", elapsed, self.started_items, self._seconds,
                self._items)
            return False
        return True


class _CloudOwners:
    """What the database says owns cloud resources, read once per sweep."""

    def __init__(self, instances, slots, deployment_keys, leased_vmids,
                 admitting_backends):
        self.instances = instances                # {(backend, vmid)}
        self.slots = slots                        # {(backend, slot)}
        self.deployment_keys = deployment_keys    # {16-character key}
        self.leased_vmids = leased_vmids          # {vmid}
        self.admitting_backends = admitting_backends  # {backend}

    @classmethod
    def load(cls):
        records = (db.session.query(VMRecord.backend, VMRecord.vmid,
                                    VMRecord.deployment_id, VMRecord.network_slot)
                   .filter(VMRecord.destroyed == False)  # noqa: E712
                   .all())
        # Reservations are written with lease_clock(), so they expire by it,
        # not by the sweep's now_epoch.
        lease_now = coordination.lease_clock()
        leases = (db.session.query(VMIDReservation.vmid,
                                   VMIDReservation.reservation_id)
                  .filter(VMIDReservation.expires_at > lease_now).all())
        admitting = (db.session.query(NodeMemoryReservation.node)
                     .filter(NodeMemoryReservation.expires_at > lease_now)
                     .all())
        # Deployment identity compares by key (ruling C1): a network reports
        # the 16-character key, records and reservations the full id. A
        # bundle's reservation id is its deployment id.
        keys = {deployment_key(deployment) for _b, _v, deployment, _s in records
                if deployment}
        keys |= {deployment_key(reservation_id) for _vmid, reservation_id in leases
                 if reservation_id}
        return cls(
            instances={(backend, int(vmid)) for backend, vmid, _d, _s in records},
            slots={(backend, int(slot)) for backend, _v, _d, slot in records
                   if slot is not None},
            deployment_keys=keys,
            leased_vmids={int(vmid) for vmid, _rid in leases},
            admitting_backends={node for (node,) in admitting})


def _sweep_cloud_inventory(backend, name, inventory, owners, now_epoch,
                           grace_seconds, is_dry_run, budget):
    """Yield the report of each started delete of one backend, ending with
    _OUT_OF_BUDGET when the pass budget ran out."""
    own = []
    for resource in inventory:
        if getattr(resource, 'backend', None) != name:
            logger.warning("Orphan reaper kept %r: listed by %s but names "
                           "another backend", resource, name)
            continue
        own.append(resource)

    # What this snapshot's machines still use. A machine whose deployment or
    # slot is unknown might use any of them, so none of that kind is
    # provably unused this sweep.
    instances = [r for r in own if r.kind == _KIND_INSTANCE]
    instance_keys = {deployment_key(r.deployment_id) for r in instances
                     if _identity_text(r.deployment_id)}
    instance_slots = {r.network_slot for r in instances
                      if _identity_int(r.network_slot) is not None}
    unknown_deployment = any(not _identity_text(r.deployment_id) for r in instances)
    unknown_slot = any(_identity_int(r.network_slot) is None for r in instances)

    for resource in own:
        reason = _cloud_keep_reason(
            resource, owners, instance_keys, instance_slots,
            unknown_deployment, unknown_slot, now_epoch, grace_seconds)
        if reason is not None:
            logger.info("Orphan reaper kept %s %s %s (%s): %s", name,
                        resource.kind, resource.provider_id, resource.name,
                        reason)
            continue
        report = _recheck_and_delete_cloud(backend, resource, is_dry_run, budget)
        if report is not None:
            yield report
            if report is _OUT_OF_BUDGET:
                return


def _cloud_keep_reason(resource, owners, instance_keys, instance_slots,
                       unknown_deployment, unknown_slot, now_epoch,
                       grace_seconds):
    """Why ``resource`` is not provably orphaned, or None when it is."""
    if resource.is_template:
        return 'template'
    if resource.kind == _KIND_INSTANCE:
        vmid = _identity_int(resource.vmid)
        if vmid is None:
            return 'no VMID'
        if (resource.backend, vmid) in owners.instances:
            return 'VMRecord exists'
        if vmid in owners.leased_vmids:
            return 'active create reservation'
        return _age_reason(resource.expires_at_epoch, now_epoch, grace_seconds,
                           'expiry')
    if resource.kind == _KIND_DEPLOYMENT_NETWORK:
        if not _identity_text(resource.deployment_id):
            return 'no deployment identity'
        key = deployment_key(resource.deployment_id)
        if key in owners.deployment_keys:
            return 'deployment has a record or reservation'
        if key in instance_keys or unknown_deployment:
            return 'a listed instance may use it'
        return _age_reason(resource.created_at_epoch, now_epoch, grace_seconds,
                           'creation')
    if resource.kind == _KIND_SLOT_NETWORK:
        slot = _identity_int(resource.network_slot)
        if slot is None:
            return 'no slot identity'
        if (resource.backend, slot) in owners.slots:
            return 'VMRecord uses the slot'
        if slot in instance_slots or unknown_slot:
            return 'a listed instance may use it'
        if resource.backend in owners.admitting_backends:
            return 'an admission on this backend may launch into it'
        return _age_reason(resource.created_at_epoch, now_epoch, grace_seconds,
                           'creation')
    return f'unknown kind {resource.kind!r}'


def _age_reason(stamp, now_epoch, grace_seconds, label):
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)):
        return f'missing/unparseable {label} time'
    if not stamp + grace_seconds < now_epoch:
        return f'{label} time is inside the grace period'
    return None


def _identity_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _identity_text(value):
    return isinstance(value, str) and bool(value)


def _recheck_and_delete_cloud(backend, resource, is_dry_run, budget):
    """Re-check under the resource's lock, then delete it while holding it.

    Returns a report, None when an owner appeared, or _OUT_OF_BUDGET.
    """
    if resource.kind == _KIND_INSTANCE:
        # The lock every create/destroy of this VMID decides under (I14).
        with vm_lifecycle_lock(resource.vmid):
            db.session.rollback()
            # Lease first, record second: allocation holds the lease until
            # its record commits, so this order leaves no gap.
            if is_vmid_reserved(resource.vmid):
                logger.info("Orphan reaper kept %s instance %s (VMID %s): create "
                            "reservation appeared during sweep", resource.backend,
                            resource.provider_id, resource.vmid)
                return None
            if _live_cloud_record(backend=resource.backend, vmid=resource.vmid):
                logger.info("Orphan reaper kept %s instance %s (VMID %s): "
                            "VMRecord appeared during sweep", resource.backend,
                            resource.provider_id, resource.vmid)
                return None
            return _delete_cloud_resource(backend, resource, is_dry_run, budget)
    if resource.kind == _KIND_SLOT_NETWORK:
        # The lock ensure_slot and release_slot_if_unused take.
        slot = resource.network_slot
        with vm_lifecycle_lock(slot_lock_key(resource.backend, slot)):
            db.session.rollback()
            admitting = NodeMemoryReservation.query.filter(
                NodeMemoryReservation.node == resource.backend,
                NodeMemoryReservation.expires_at > coordination.lease_clock(),
            ).first()
            if admitting is not None or _live_cloud_record(
                    backend=resource.backend, network_slot=slot):
                logger.info("Orphan reaper kept %s slot network %s (slot %s): "
                            "an admission or VM appeared during sweep",
                            resource.backend, resource.provider_id, slot)
                return None
            return _delete_cloud_resource(backend, resource, is_dry_run, budget)
    # Deployment networks: every deployment's network is younger than the
    # grace period while it is being created, so the first query suffices.
    return _delete_cloud_resource(backend, resource, is_dry_run, budget)


def _live_cloud_record(**identity):
    return VMRecord.query.filter_by(destroyed=False, **identity).first() is not None


def _delete_cloud_resource(backend, resource, is_dry_run, budget):
    if not budget.may_start():
        return _OUT_OF_BUDGET
    budget.started_items += 1
    report = {'backend': resource.backend, 'kind': resource.kind,
              'provider_id': resource.provider_id, 'name': resource.name}
    if is_dry_run:
        logger.warning("Orphan reaper dry-run would delete %s %s %s (%s)",
                       resource.backend, resource.kind, resource.provider_id,
                       resource.name)
        return {**report, 'status': 'dry_run'}
    try:
        backend.delete_resource(resource)
    except Exception as exc:
        # An ownership refusal or provider error: the resource stays for a
        # later sweep, and is never reported gone.
        logger.error("Orphan reaper failed to delete %s %s %s (%s): %s",
                     resource.backend, resource.kind, resource.provider_id,
                     resource.name, exc)
        return {**report, 'status': 'delete_failed'}
    logger.warning("Orphan reaper deleted %s %s %s (%s)", resource.backend,
                   resource.kind, resource.provider_id, resource.name)
    return {**report, 'status': 'reaped'}
