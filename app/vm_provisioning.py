"""Backend-generic VM provisioning: admit, network, create, destroy, retire.

Callers hold VMSpec/VMRef values and never a provider client. The create
order is fixed: admit -> start the router -> ensure the slot network ->
create. For a cloud backend the router lock is held only around
ensure_router_running() and the slot lock only around ensure_slot(); create()
runs holding neither, because the router lock would otherwise serialize every
launch on the backend (a class of 90 waiting one launch each) and stall the
expiry loop's router step behind it. What keeps the router up instead: the
admission reservation is inserted before ensure_router_running() and renewed
until the VMRecord commits, and the router is stopped only when, under the
router lock, no live record and no unexpired reservation exists for that
backend. No path takes the router lock while holding a slot lock.

Proxmox has no router or slot networks (its hooks are no-ops), so it takes
neither lock.
"""

import logging
import threading
from contextlib import contextmanager, nullcontext
from typing import (
    Any, Callable, ContextManager, Iterator, Mapping, Optional, Tuple,
)

from app import coordination
from app.backends import backend_for, get_backend
from app.backends.base import (
    BACKEND_PROXMOX, DestroyPolicy, LaunchedVM, SlotNetworkMissing, VMBackend,
    VMRef, VMSpec,
)
from app.backends.cloud_common import router_lock_key, slot_lock_key
from app.models import NodeMemoryReservation, VMIDReservation, VMRecord, db
from app.proxmox_service import vm_lifecycle_lock

logger = logging.getLogger(__name__)


def _ensure_slot(backend: VMBackend, spec: VMSpec) -> None:
    with vm_lifecycle_lock(slot_lock_key(spec.backend, spec.network_slot)):
        backend.ensure_slot(spec.network_slot)


@contextmanager
def _renewing_admission(heartbeat: Callable[[], None]
                        ) -> Iterator[Callable[[], None]]:
    """Run `heartbeat` every RESERVATION_HEARTBEAT_SECONDS in the background
    and yield a guard that raises the renewal's error once one failed.

    A provider call can outlast the admission TTL without calling heartbeat
    (AWS's running waiter), as can the wait for a router or slot lock; the
    lease must still hold until the caller's VMRecord commits (ruling C4).
    A thread like the deploy's other renewers: a greenlet under gevent. On a
    normal exit the guard runs once more after the renewer has stopped, so
    a renewal that failed during teardown is raised too; an exception
    already leaving the body takes precedence.
    """
    # Lazy: app.routes imports this module.
    from app.routes import vm_creation
    stopped = threading.Event()
    lost = []

    def renew():
        while not stopped.wait(vm_creation.RESERVATION_HEARTBEAT_SECONDS):
            try:
                heartbeat()
            except Exception as error:
                lost.append(error)
                return

    def guard() -> None:
        if lost:
            raise lost[0]

    renewer = threading.Thread(target=renew, name='admission-heartbeat',
                               daemon=True)
    renewer.start()
    try:
        yield guard
    finally:
        stopped.set()
        renewer.join()
    guard()


def ensure_router_running(backend_name: str) -> None:
    """Start `backend_name`'s router under its lifecycle lock (ruling C4)."""
    with vm_lifecycle_lock(router_lock_key(backend_name)):
        get_backend(backend_name).ensure_router_running()


def create_vm(spec: VMSpec, *, reservation_id: str,
              load_balancing_config: Optional[Mapping],
              heartbeat: Callable[[], None],
              create_options: Optional[Mapping[str, Any]] = None
              ) -> Tuple[str, LaunchedVM]:
    """Admit, prepare the network and create; return (location, launched).

    The caller releases the admission reservation, never this function: a
    create that raised may still have left a provider resource that
    failed-deploy cleanup must resolve first. `create_options` are extra
    keyword arguments for this backend's create() (Proxmox: its clone
    semaphores and claim callback). For a cloud backend `heartbeat` also
    runs in the background from admission until this returns; once it has
    raised, the create raises that error rather than report a launch its
    lease no longer covers.
    """
    backend = get_backend(spec.backend)
    location = backend.admit(spec, reservation_id=reservation_id,
                             load_balancing_config=load_balancing_config)
    options = dict(create_options or {})
    if spec.backend == BACKEND_PROXMOX:
        return location, backend.create(spec, location, heartbeat=heartbeat, **options)

    with _renewing_admission(heartbeat) as guard:
        def guarded_heartbeat() -> None:
            guard()
            heartbeat()

        with vm_lifecycle_lock(router_lock_key(spec.backend)):
            guard()
            backend.ensure_router_running()
        _ensure_slot(backend, spec)
        guard()
        try:
            launched = backend.create(spec, location,
                                      heartbeat=guarded_heartbeat, **options)
        except SlotNetworkMissing:
            # A retirement released the slot between ensure_slot and create
            # (release_slot_if_unused cannot see an admission that has not
            # launched yet). Re-ensuring once is enough: the next release
            # must again wait for this reservation to go.
            logger.warning("Slot %s of %s vanished before launch of VM %s; "
                           "re-ensuring it once", spec.network_slot,
                           spec.backend, spec.vmid)
            _ensure_slot(backend, spec)
            launched = backend.create(spec, location,
                                      heartbeat=guarded_heartbeat, **options)
    # The renewer has stopped and its last renewal was checked on the way
    # out: a launch the lease stopped covering is a failed create, and the
    # caller's cleanup resolves the instance by VMID (ruling C7).
    return location, launched


def destroy_vm(ref: VMRef, policy: DestroyPolicy) -> None:
    backend_for(ref).destroy(ref, policy)


def provider_session(backend: VMBackend) -> ContextManager:
    """One provider connection for several operations on one guest.

    Proxmox opens a connection per operation (a login each); a lifecycle
    path that inspects then destroys, or a /list render, groups them into
    one, as the routes always did. Backends without connections of their
    own need nothing.
    """
    session = getattr(backend, 'session', None)
    return session() if session is not None else nullcontext()



def release_slot_if_unused(backend: str, slot: int) -> None:
    """Release a cloud slot network that no VM uses or is about to use.

    Never raises: a slot left behind is reaped by the sweeper (T15).
    """
    try:
        with vm_lifecycle_lock(slot_lock_key(backend, slot)):
            # End the REPEATABLE READ snapshot taken before the lock.
            db.session.rollback()
            in_use = VMRecord.query.filter_by(
                backend=backend, network_slot=slot, destroyed=False).first()
            # An unexpired admission on this backend may be about to launch
            # into the slot; its create re-ensures a slot released anyway.
            admitting = NodeMemoryReservation.query.filter(
                NodeMemoryReservation.node == backend,
                NodeMemoryReservation.expires_at > coordination.lease_clock(),
            ).first()
            if in_use is None and admitting is None:
                get_backend(backend).release_slot(slot)
    except Exception:
        logger.exception("Could not release slot %s of backend %s; the "
                         "sweeper will retry", slot, backend)


def after_record_retired(ref: VMRef) -> None:
    """Clean up what a retired record's deployment and slot no longer need.

    Call after the retirement commits. Never raises: leftovers belong to the
    sweeper (T15), and the retirement itself already succeeded.
    """
    if ref.deployment_id:
        try:
            db.session.rollback()
            live = VMRecord.query.filter_by(
                deployment_id=ref.deployment_id, destroyed=False).first()
            allocating = VMIDReservation.query.filter(
                VMIDReservation.reservation_id == ref.deployment_id,
                VMIDReservation.expires_at > coordination.lease_clock(),
            ).first()
            if live is None and allocating is None:
                backend_for(ref).cleanup_deployment(ref.deployment_id)
        except Exception:
            logger.exception("Could not clean up deployment %s of backend %s; "
                             "the sweeper will retry", ref.deployment_id,
                             ref.backend)
    if ref.backend != BACKEND_PROXMOX and ref.network_slot is not None:
        release_slot_if_unused(ref.backend, ref.network_slot)
