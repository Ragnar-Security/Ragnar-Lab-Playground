"""Proxmox VE behind the VMBackend contract.

An adapter, not a rewrite. Admission, identity, absence, inventory and task
waits are the incident-hardened functions in app.proxmox_service, the clone
and NIC helpers are the ones in app.routes.vm_creation, and the display
probes are the ones in app.routes.utils; this module only sequences them.
Every call goes through the module attribute so the existing patch points
(app.proxmox_service.ProxmoxAPI, .wait_for_proxmox_task, ...) still govern.

Callers hold vm_lifecycle_lock(vmid) around every mutation, as they do today.
"""

import functools
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, List, Mapping, Optional, Tuple

from app import proxmox_service
from app.backends.base import (
    BACKEND_PROXMOX, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH,
    AdmissionRefused, BackendError, DestroyPolicy, DestroyRefused, LaunchedVM,
    ManagedResource, VMRef, VMSpec,
)

logger = logging.getLogger(__name__)

# Stop errors that mean the guest is already down (or already gone, which the
# delete then confirms against fresh inventory). The same classification the
# expiry daemon's stop has always used, so STRICT refuses exactly what it did.
_ALREADY_STOPPED_HINTS = (
    'already stopped', 'not running', 'does not exist', 'configuration file',
)

STOP_TIMEOUT_SECONDS = 60
DELETE_TIMEOUT_SECONDS = 120

# _stop outcomes. _GONE: fresh inventory proved the guest absent after the
# stop raised, so there is nothing left to delete.
_STOPPED = 'stopped'
_STOP_FAILED = 'stop_failed'
_GONE = 'gone'
# The display index memo outside session() (never memoised: nothing would
# clear it) and inside one before the first display read.
_NO_SESSION = object()
_UNREAD = object()


def _route_utils():
    # Imported lazily: app.routes imports every blueprint, and those import
    # the provisioning layer that imports this module.
    from app.routes import utils
    return utils


def _is_template(entry) -> bool:
    return entry.get('template') in (1, '1')


def _operation(method):
    """Run one lifecycle operation on a connection of its own.

    Nested operations (destroy's inspect, delete_resource's destroy) and
    operations inside session() reuse the connection already open.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.session():
            return method(self, *args, **kwargs)
    return wrapper



class ProxmoxBackend:
    """VMBackend for Proxmox VE."""

    def __init__(self, name: str = BACKEND_PROXMOX, client=None):
        self.name = name
        # Tests and contract cases inject a fake that serves every call.
        self._injected = client
        # Greenlet-local under gevent's monkey patching: concurrent create
        # tasks share this instance but never each other's connection.
        self._local = threading.local()

    def connect(self):
        """A connection of the caller's own: the injected client, the one
        pinned by an open session(), or a new one.

        Never cached across operations. proxmoxer renews a password ticket
        only while it is still valid and does not keep the password, so a
        client left idle past the ticket lifetime can never authenticate
        again; a fresh connection per operation is what the routes always
        made.
        """
        if self._injected is not None:
            return self._injected
        pinned = getattr(self._local, 'api', None)
        if pinned is not None:
            return pinned
        return proxmox_service.get_proxmox_connection()

    @contextmanager
    def session(self):
        """Pin one connection for every operation run inside this block.

        Operations already open one each; a caller groups several (a create
        task's admit and create, a /list render) to make one connection, as
        the route did. Display reads inside it share one cluster index.
        """
        if self._injected is not None or getattr(self._local, 'api', None) is not None:
            yield
            return
        self._local.api = self.connect()
        self._local.display_index = _UNREAD
        try:
            yield
        finally:
            self._local.api = None
            self._local.display_index = _NO_SESSION

    @property
    def _api(self):
        if self._injected is not None:
            return self._injected
        api = getattr(self._local, 'api', None)
        if api is None:
            raise RuntimeError('ProxmoxBackend client used outside an operation')
        return api

    def _endpoint(self, ref: VMRef):
        node = self._api.nodes(ref.location)
        return node.lxc(ref.vmid) if ref.vm_type == 'lxc' else node.qemu(ref.vmid)

    def _forget_guest(self, ref: VMRef) -> None:
        # Proxmox reuses VMIDs: a cached snapshot or address of this guest
        # must not describe the next one.
        proxmox_service.reset_cluster_vm_index()
        if getattr(self._local, 'display_index', _NO_SESSION) is not _NO_SESSION:
            self._local.display_index = _UNREAD
        _route_utils().invalidate_vm_ip_cache(ref.location, ref.vmid)

    # Admission and create

    @_operation
    def admit(self, spec: VMSpec, *, reservation_id: str,
              load_balancing_config: Optional[Mapping]) -> str:
        locations = spec.template.get('template_locations') or {}
        node = proxmox_service.select_best_node(
            self._api,
            required_memory_mb=spec.memory_mb,
            required_cores=spec.cores,
            load_balancing_config=load_balancing_config,
            available_nodes=list(locations) if locations else None,
            reservation_id=reservation_id,
        )
        if not node:
            raise AdmissionRefused('No suitable nodes available')
        return node

    def renew_admission(self, reservation_id: str) -> bool:
        return proxmox_service.renew_node_reservation(reservation_id)

    def release_admission(self, reservation_id: Optional[str]) -> bool:
        return proxmox_service.release_node_reservation(reservation_id)

    @staticmethod
    def _renew_leases(heartbeat: Callable[[], None], spec: VMSpec,
                      when: str) -> None:
        """Renew admission, then the bundle's VMID lease; raise if either lapsed.

        Queued work waits behind the per-node clone limit: without renewals a
        busy classroom outlives the leak-safety TTL and becomes invisible to
        admission control and the allocator.
        """
        try:
            heartbeat()
        except RuntimeError as error:
            raise RuntimeError(f'{error} {when}') from error
        if (spec.deployment_id
                and not proxmox_service.renew_vmid_reservation(spec.deployment_id)):
            raise RuntimeError(f'VMID reservation expired {when}')

    @_operation
    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None],
               clone_semaphores: Optional[Mapping] = None,
               on_claim: Optional[Callable[[Optional[int], Optional[str]], None]] = None
               ) -> LaunchedVM:
        """Clone or create the guest on `location`, configure it and start it.

        `clone_semaphores` maps node -> the shared per-node ZFS clone limit;
        a node without one clones unlimited, as it always has. `on_claim`
        hears (vmid, node) before each attempt that may leave a guest of ours
        behind, and (None, None) when Proxmox refused because the VMID was
        already someone else's, so failed-deploy cleanup targets only guests
        this create can own.
        """
        # Lazy: app.routes.vm_creation imports the provisioning layer, which
        # imports this module.
        from app.routes import vm_creation
        claim = on_claim or (lambda vmid, node: None)
        config = dict(spec.template)
        node = location
        vmid = spec.vmid
        entry_vlan = spec.entry_slot if spec.is_entry_point else None
        internal_vlan = spec.internal_slot
        source_vmid = config['template_locations'].get(node) or config['source_vmid']

        if config['storage_locations'] and node in config['storage_locations']:
            config['storage_device'] = config['storage_locations'][node]
            logger.info(f"Using node-specific storage '{config['storage_device']}' for {node}")

        def create_guest(create, target_vmid):
            claim(target_vmid, node)
            try:
                return create(target_vmid)
            except Exception as error:
                if 'already exists' in str(error).lower():
                    claim(None, None)
                raise

        def clone(target_vmid):
            return vm_creation.create_qemu_vm(
                self._api, node, node, target_vmid, spec.vm_name, source_vmid,
                config, spec.expires_at, internal_vlan, entry_vlan)

        def clone_with_collision_retry():
            nonlocal vmid
            try:
                return create_guest(clone, vmid)
            except Exception as error:
                if 'already exists' not in str(error).lower() or not spec.deployment_id:
                    raise
                vmid = proxmox_service.get_next_available_vmid(
                    self._api, reservation_id=spec.deployment_id)
                logger.warning(
                    "Clone VMID collision; retrying once with freshly "
                    "reserved VMID %s", vmid)
                return create_guest(clone, vmid)

        if config['type'] == 'lxc':
            self._renew_leases(heartbeat, spec, 'before create')
            create_guest(
                lambda target_vmid: vm_creation.create_lxc_container(
                    self._api, node, target_vmid, spec.vm_name, config,
                    spec.expires_at, internal_vlan, entry_vlan),
                vmid)
        elif config['type'] == 'qemu':
            if not source_vmid:
                raise Exception('QEMU template requires source_vmid')
            semaphore = (clone_semaphores or {}).get(node)
            if semaphore:
                acquired = False
                while not acquired:
                    acquired = semaphore.acquire(
                        timeout=vm_creation.RESERVATION_HEARTBEAT_SECONDS)
                    try:
                        self._renew_leases(
                            heartbeat, spec, 'while waiting for clone capacity')
                    except Exception:
                        if acquired:
                            semaphore.release()
                        raise
                try:
                    logger.info(
                        f"[VM {vmid}] Acquired clone semaphore for node {node}")
                    node = clone_with_collision_retry()
                finally:
                    semaphore.release()
                    logger.info(
                        f"[VM {vmid}] Released clone semaphore for node {node}")
            else:
                self._renew_leases(heartbeat, spec, 'before clone')
                logger.warning(
                    f"[VM {vmid}] No semaphore found for node {node}; "
                    "proceeding without clone limit")
                node = clone_with_collision_retry()
        else:
            raise Exception(f'Unsupported VM type: {config["type"]}')

        try:
            heartbeat()
        except RuntimeError as error:
            raise RuntimeError(f'{error} before VM start') from error
        # Once the clone exists, Proxmox inventory itself protects the VMID.
        # Renewal here is best effort; treating a just-expired lease as clone
        # failure would destroy a successfully-created guest.
        if spec.deployment_id:
            proxmox_service.renew_vmid_reservation(spec.deployment_id)
        vm_creation.start_vm(self._api, node, vmid, config['type'])
        logger.info(f"VM {vmid} ({spec.vm_name}) created and started on {node}")
        return LaunchedVM(vmid=vmid, vm_type=config['type'], location=node,
                          network_slot=spec.network_slot)

    # Identity and destroy

    @_operation
    def inspect(self, ref: VMRef) -> str:
        return proxmox_service.inspect_vm_guest(
            self._api, ref.vmid, node=ref.location, vm_name=ref.vm_name,
            vm_type=ref.vm_type)

    def _wait(self, ref: VMRef, upid, timeout: int):
        # The 60 s stop / 120 s delete, 1 s poll every source path passes.
        return proxmox_service.wait_for_proxmox_task(
            self._api, ref.location, upid, timeout=timeout, poll_interval=1)

    def _stop(self, ref: VMRef, policy: DestroyPolicy) -> str:
        """_STOPPED, _STOP_FAILED (may still be running) or _GONE."""
        try:
            upid = self._endpoint(ref).status.stop.post()
            # Each source path's own wait condition: expiry (STRICT) has
            # always skipped the wait on a falsy UPID; admin bulk,
            # failed-deploy cleanup and student destroy always waited.
            if upid or policy is not DestroyPolicy.STRICT:
                result = self._wait(ref, upid, STOP_TIMEOUT_SECONDS)
                if not result.get('success'):
                    logger.warning("Stop task for VM %s failed: %s", ref.vmid,
                                   result.get('error', result.get('status')))
                    return _STOP_FAILED
            return _STOPPED
        except Exception as error:
            if policy is DestroyPolicy.STRICT:
                if any(hint in str(error).lower()
                       for hint in _ALREADY_STOPPED_HINTS):
                    logger.info("VM %s already stopped or not found", ref.vmid)
                    return _STOPPED
            elif policy is DestroyPolicy.BEST_EFFORT and (
                    proxmox_service.confirm_proxmox_absence(
                        self._api, ref.vmid, error)):
                # Failed-deploy cleanup has always confirmed a stop error
                # against fresh inventory and skipped the delete. Admin bulk
                # shares the policy and gains this on purpose: it used to
                # send a DELETE to a guest inventory had already proven gone.
                logger.info("VM %s confirmed absent after stop request",
                            ref.vmid)
                return _GONE
            logger.warning("Stop request for VM %s failed: %s", ref.vmid, error)
            return _STOP_FAILED

    def _delete(self, ref: VMRef, policy: DestroyPolicy,
                force_corrupt: bool) -> None:
        """Delete the guest; raise BackendError unless it is gone."""
        if not force_corrupt:
            kwargs = {}
        elif ref.vm_type == 'qemu':
            kwargs = {'purge': 1, 'skiplock': 1}
        else:
            kwargs = {'purge': 1, 'force': 1}
        try:
            upid = self._endpoint(ref).delete(**kwargs)
        except Exception as error:
            # A 'does not exist' answer is a hint, not proof: a corrupt
            # config says the same while the guest runs on.
            if proxmox_service.confirm_proxmox_absence(
                    self._api, ref.vmid, error):
                logger.info("VM %s confirmed absent after destroy request",
                            ref.vmid)
                return
            raise BackendError(f'destroy of VM {ref.vmid} failed: {error}') from error
        if upid is None and policy is DestroyPolicy.FORCE_CORRUPT:
            # Student destroy has only ever waited on a delete UPID it got
            # back; expiry, admin bulk and failed-deploy always waited.
            return
        try:
            result = self._wait(ref, upid, DELETE_TIMEOUT_SECONDS)
        except Exception as error:
            # The waiter raises only TimeoutError, which no source path ever
            # treated as absence: confirm_proxmox_absence refuses every error
            # that is not a 404 or "does not exist" before reading inventory.
            if proxmox_service.confirm_proxmox_absence(
                    self._api, ref.vmid, error):
                return
            raise BackendError(
                f'destroy task for VM {ref.vmid} did not finish: {error}') from error
        if not result.get('success'):
            raise BackendError(
                f"destroy task for VM {ref.vmid} failed: "
                f"{result.get('error', result.get('status'))}")

    @_operation
    def destroy(self, ref: VMRef, policy: DestroyPolicy) -> None:
        # Always a fresh identity read, even if the caller just inspected:
        # a replacement at the same VMID must never inherit this request.
        identity = self.inspect(ref)
        if identity == GUEST_ABSENT:
            self._forget_guest(ref)
            return
        if identity == GUEST_MISMATCH:
            raise DestroyRefused(f'VMID {ref.vmid} no longer names {ref.vm_name}')
        if identity != GUEST_MATCH:
            raise BackendError(f'identity of VM {ref.vmid} could not be verified')

        force_corrupt = False
        if policy is DestroyPolicy.FORCE_CORRUPT:
            utils = _route_utils()
            state, error = utils._verify_vm_exists(
                self._api, ref.location, ref.vmid, ref.vm_type)
            force_corrupt = state == utils.VM_CONFIG_ERROR
            if force_corrupt:
                # A listed guest whose config cannot be read cannot be
                # stopped either; only a forced purge removes it.
                logger.warning("VM %s has config issues (%s); force deleting",
                               ref.vmid, error)

        if not force_corrupt:
            stopped = self._stop(ref, policy)
            if stopped == _GONE:
                self._forget_guest(ref)
                return
            if stopped == _STOP_FAILED:
                if policy is DestroyPolicy.STRICT:
                    # Expiry: a guest that would not stop may be
                    # mid-operation; the record stays and the next pass
                    # retries.
                    raise DestroyRefused(
                        f'VM {ref.vmid} did not stop; not deleting')
                logger.warning("VM %s did not stop; deleting anyway (%s)",
                               ref.vmid, policy.value)

        self._delete(ref, policy, force_corrupt)
        self._forget_guest(ref)

    @_operation
    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        absent = proxmox_service.confirm_proxmox_absence(
            self._api, ref.vmid, error)
        if absent:
            self._forget_guest(ref)
        return absent

    # Display reads: the cached index (ruling C5), so one /list render costs
    # one cluster call however many VMs it shows, outage or not.

    def _display_index(self):
        """The cluster index for display reads, read once per session(): in
        an outage the first guest's read fails, not every guest's."""
        memo = getattr(self._local, 'display_index', _NO_SESSION)
        if memo is not _NO_SESSION and memo is not _UNREAD:
            return memo
        try:
            index = proxmox_service.get_cluster_vm_index(self._api)
        except Exception as error:
            logger.warning("cluster inventory unavailable (%s); reading VM "
                           "configs directly", error)
            index = None
        if memo is _UNREAD:
            self._local.display_index = index
        return index

    def _display_state(self, ref: VMRef):
        """(state, diagnostic): `_verify_vm_exists`'s verdict and error text."""
        return _route_utils()._verify_vm_exists(
            self._api, ref.location, ref.vmid, ref.vm_type, index=self._display_index())

    def _addresses(self, ref: VMRef, state) -> List[str]:
        utils = _route_utils()
        if state == utils.VM_PRESENT:
            ips = utils._get_vm_ip_addresses(
                self._api, ref.location, ref.vmid, ref.vm_type)
        elif state == utils.VM_UNKNOWN:
            # Proxmox did not answer, so the agent would not either; a
            # seconds-old cached address beats none for a student mid-SSH.
            ips = utils._ip_cache_get((ref.location, ref.vmid)) or []
        else:
            return []
        # Lab addresses only; this also drops the ['N/A'] display sentinel.
        return [ip for ip in ips if ip.startswith('10.')]

    @staticmethod
    def _status_of(state, error) -> Tuple[str, Optional[str]]:
        """status() plus Proxmox's own error text, which the config-error
        webhook has always carried so an operator sees why the read failed."""
        utils = _route_utils()
        if state == utils.VM_PRESENT:
            # What /list has always shown for a guest Proxmox can read.
            return 'running', None
        if state == utils.VM_CONFIG_ERROR:
            return 'config_error', error
        if state == utils.VM_MISSING:
            return utils.VM_MISSING, error
        return 'unknown', error

    @_operation
    def ip_addresses(self, ref: VMRef) -> List[str]:
        state, _error = self._display_state(ref)
        return self._addresses(ref, state)

    @_operation
    def status(self, ref: VMRef) -> str:
        return self._status_of(*self._display_state(ref))[0]

    @_operation
    def display_read(self, ref: VMRef) -> Tuple[str, Optional[str], List[str]]:
        """(status, diagnostic, addresses) from one probe, as /list has
        always read a guest: an unknown one's addresses come from the cache,
        never from a second probe that would only time out again."""
        state, error = self._display_state(ref)
        return (*self._status_of(state, error), self._addresses(ref, state))

    @_operation
    def uptime(self, ref: VMRef) -> Optional[int]:
        """Seconds the guest has run, for the detail page; None when unread."""
        try:
            return self._endpoint(ref).status.current.get().get('uptime')
        except Exception as error:
            logger.warning("uptime of VM %s unavailable: %s", ref.vmid, error)
            return None

    @_operation
    def set_expiry(self, ref: VMRef, expires_at: datetime) -> None:
        # The orphan reaper parses exactly this line; naive local time.
        try:
            self._endpoint(ref).config.put(
                description=f'Expires: {expires_at.isoformat()}')
        except Exception as error:
            raise BackendError(
                f'could not write expiry of VM {ref.vmid}: {error}') from error

    # Inventory

    @_operation
    def list_managed(self) -> List[ManagedResource]:
        """Every cluster guest, templates included, from one fresh read.

        Presence only (ruling C5): no per-guest config read, so no expiry.
        A failed read propagates; an empty list would mean "all gone".
        """
        index = proxmox_service.get_cluster_vm_index(
            self._api, max_age=0, allow_stale=False)
        return [
            ManagedResource(
                backend=self.name, kind='instance', provider_id=str(vmid),
                name=entry.get('name') or '', vmid=vmid,
                location=entry.get('node'), status=entry.get('status'),
                is_template=_is_template(entry), vm_type=entry.get('type'))
            for vmid, entry in sorted(index.items())]

    @_operation
    def delete_resource(self, resource: ManagedResource) -> None:
        if resource.backend != self.name or resource.kind != 'instance':
            raise BackendError(
                f'{resource.backend} {resource.kind} {resource.provider_id} '
                'is not a Proxmox instance')
        if resource.vmid is None:
            raise BackendError(f'resource {resource.provider_id} has no VMID')
        # The listing may be minutes old; recover vm_type from a fresh read
        # and let destroy() re-verify name and node against another.
        try:
            index = proxmox_service.get_cluster_vm_index(
                self._api, max_age=0, allow_stale=False)
        except Exception as error:
            raise BackendError(
                f'inventory unavailable for VM {resource.vmid}: {error}') from error
        entry = index.get(resource.vmid)
        if entry is None:
            return
        if resource.is_template or _is_template(entry):
            raise DestroyRefused(f'VMID {resource.vmid} is a template')
        ref = VMRef(vmid=resource.vmid, vm_name=resource.name,
                    vm_type=entry.get('type'), location=resource.location,
                    backend=self.name, provider_id=resource.provider_id)
        self.destroy(ref, DestroyPolicy.BEST_EFFORT)

    # Proxmox has no per-deployment network, slot network or router: a lab's
    # isolation is the VLAN tags on its guests' NICs.

    def cleanup_deployment(self, deployment_id: str) -> None:
        pass

    def ensure_slot(self, slot: int) -> None:
        pass

    def release_slot(self, slot: int) -> None:
        pass

    def ensure_router_running(self) -> None:
        pass

    def stop_router(self) -> None:
        pass
