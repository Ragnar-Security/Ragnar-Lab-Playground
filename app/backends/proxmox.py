"""Proxmox VE behind the VMBackend contract: the read and destroy half.

An adapter, not a rewrite. Identity, absence, inventory and task waits are
the incident-hardened functions in app.proxmox_service, and the display
probes are the ones in app.routes.utils; this module only sequences them.
Every call goes through the module attribute so the existing patch points
(app.proxmox_service.ProxmoxAPI, .wait_for_proxmox_task, ...) still govern.

Callers hold vm_lifecycle_lock(vmid) around every mutation, as they do today.
"""

import logging
from datetime import datetime
from typing import List, Optional

from app import proxmox_service
from app.backends.base import (
    BACKEND_PROXMOX, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, BackendError,
    DestroyPolicy, DestroyRefused, ManagedResource, VMRef,
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


def _route_utils():
    # Imported lazily: app.routes imports every blueprint, and those import
    # the provisioning layer that imports this module.
    from app.routes import utils
    return utils


def _is_template(entry) -> bool:
    return entry.get('template') in (1, '1')


class ProxmoxBackend:
    """VMBackend for Proxmox VE. admit/create arrive in T7."""

    def __init__(self, name: str = BACKEND_PROXMOX, client=None):
        self.name = name
        self._client = client

    @property
    def client(self):
        # Lazy: a process that never touches Proxmox never connects, and
        # tests inject FakeProxmox. proxmoxer renews its ticket itself.
        if self._client is None:
            self._client = proxmox_service.get_proxmox_connection()
        return self._client

    def _endpoint(self, ref: VMRef):
        node = self.client.nodes(ref.location)
        return node.lxc(ref.vmid) if ref.vm_type == 'lxc' else node.qemu(ref.vmid)

    def _forget_guest(self, ref: VMRef) -> None:
        # Proxmox reuses VMIDs: a cached snapshot or address of this guest
        # must not describe the next one.
        proxmox_service.reset_cluster_vm_index()
        _route_utils().invalidate_vm_ip_cache(ref.location, ref.vmid)

    # Admission (create side arrives in T7)

    def renew_admission(self, reservation_id: str) -> bool:
        return proxmox_service.renew_node_reservation(reservation_id)

    def release_admission(self, reservation_id: Optional[str]) -> bool:
        return proxmox_service.release_node_reservation(reservation_id)

    # Identity and destroy

    def inspect(self, ref: VMRef) -> str:
        return proxmox_service.inspect_vm_guest(
            self.client, ref.vmid, node=ref.location, vm_name=ref.vm_name,
            vm_type=ref.vm_type)

    def _stop(self, ref: VMRef) -> bool:
        """Stop the guest; False when it may still be running."""
        try:
            upid = self._endpoint(ref).status.stop.post()
            if upid:
                result = proxmox_service.wait_for_proxmox_task(
                    self.client, ref.location, upid,
                    timeout=STOP_TIMEOUT_SECONDS, poll_interval=1)
                if not result.get('success'):
                    logger.error("Stop task for VM %s failed: %s", ref.vmid,
                                 result.get('error', result.get('status')))
                    return False
            return True
        except Exception as error:
            if any(hint in str(error).lower() for hint in _ALREADY_STOPPED_HINTS):
                logger.info("VM %s already stopped or not found", ref.vmid)
                return True
            logger.warning("Stop request for VM %s failed: %s", ref.vmid, error)
            return False

    def _delete(self, ref: VMRef, force_corrupt: bool) -> None:
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
                    self.client, ref.vmid, error):
                logger.info("VM %s confirmed absent after destroy request",
                            ref.vmid)
                return
            raise BackendError(f'destroy of VM {ref.vmid} failed: {error}') from error
        try:
            result = proxmox_service.wait_for_proxmox_task(
                self.client, ref.location, upid,
                timeout=DELETE_TIMEOUT_SECONDS, poll_interval=1)
        except Exception as error:
            raise BackendError(
                f'destroy task for VM {ref.vmid} did not finish: {error}') from error
        if not result.get('success'):
            raise BackendError(
                f"destroy task for VM {ref.vmid} failed: "
                f"{result.get('error', result.get('status'))}")

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
                self.client, ref.location, ref.vmid, ref.vm_type)
            force_corrupt = state == utils.VM_CONFIG_ERROR
            if force_corrupt:
                # A listed guest whose config cannot be read cannot be
                # stopped either; only a forced purge removes it.
                logger.warning("VM %s has config issues (%s); force deleting",
                               ref.vmid, error)

        if not force_corrupt and not self._stop(ref):
            if policy is DestroyPolicy.STRICT:
                # Expiry: a guest that would not stop may be mid-operation;
                # the record stays and the next pass retries.
                raise DestroyRefused(f'VM {ref.vmid} did not stop; not deleting')
            logger.warning("VM %s did not stop; deleting anyway (%s)",
                           ref.vmid, policy.value)

        self._delete(ref, force_corrupt)
        self._forget_guest(ref)

    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        absent = proxmox_service.confirm_proxmox_absence(
            self.client, ref.vmid, error)
        if absent:
            self._forget_guest(ref)
        return absent

    # Display reads: the cached index (ruling C5), so one /list render costs
    # one cluster call however many VMs it shows.

    def _display_state(self, ref: VMRef) -> str:
        utils = _route_utils()
        try:
            index = proxmox_service.get_cluster_vm_index(self.client)
        except Exception as error:
            logger.warning("cluster inventory unavailable (%s); reading VM %s "
                           "config directly", error, ref.vmid)
            index = None
        state, _error = utils._verify_vm_exists(
            self.client, ref.location, ref.vmid, ref.vm_type, index=index)
        return state

    def ip_addresses(self, ref: VMRef) -> List[str]:
        utils = _route_utils()
        state = self._display_state(ref)
        if state == utils.VM_PRESENT:
            ips = utils._get_vm_ip_addresses(
                self.client, ref.location, ref.vmid, ref.vm_type)
        elif state == utils.VM_UNKNOWN:
            # Proxmox did not answer, so the agent would not either; a
            # seconds-old cached address beats none for a student mid-SSH.
            ips = utils._ip_cache_get((ref.location, ref.vmid)) or []
        else:
            return []
        # Lab addresses only; this also drops the ['N/A'] display sentinel.
        return [ip for ip in ips if ip.startswith('10.')]

    def status(self, ref: VMRef) -> str:
        utils = _route_utils()
        state = self._display_state(ref)
        if state == utils.VM_PRESENT:
            # What /list has always shown for a guest Proxmox can read.
            return 'running'
        if state == utils.VM_CONFIG_ERROR:
            return 'config_error'
        if state == utils.VM_MISSING:
            return utils.VM_MISSING
        return 'unknown'

    def set_expiry(self, ref: VMRef, expires_at: datetime) -> None:
        # The orphan reaper parses exactly this line; naive local time.
        try:
            self._endpoint(ref).config.put(
                description=f'Expires: {expires_at.isoformat()}')
        except Exception as error:
            raise BackendError(
                f'could not write expiry of VM {ref.vmid}: {error}') from error

    # Inventory

    def list_managed(self) -> List[ManagedResource]:
        """Every cluster guest, templates included, from one fresh read.

        Presence only (ruling C5): no per-guest config read, so no expiry.
        A failed read propagates; an empty list would mean "all gone".
        """
        index = proxmox_service.get_cluster_vm_index(
            self.client, max_age=0, allow_stale=False)
        return [
            ManagedResource(
                backend=self.name, kind='instance', provider_id=str(vmid),
                name=entry.get('name') or '', vmid=vmid,
                location=entry.get('node'), status=entry.get('status'),
                is_template=_is_template(entry))
            for vmid, entry in sorted(index.items())]

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
                self.client, max_age=0, allow_stale=False)
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
