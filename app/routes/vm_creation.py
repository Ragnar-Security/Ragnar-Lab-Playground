"""
VM Creation routes module - REFACTORED FOR CLARITY
Handles VM creation from deployment groups with parallel processing
"""

from contextlib import contextmanager, nullcontext
from flask import Blueprint, request, jsonify, current_app, has_app_context
from datetime import datetime, timedelta
import time
import re
import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import threading
from typing import Dict
from uuid import uuid4

logger = logging.getLogger(__name__)

from ..models import db, VMRecord
from ..proxmox_service import (
    get_proxmox_connection,
    get_next_available_vmid,
    release_vmid_reservation,
    renew_vmid_reservation,
    wait_for_proxmox_task,
    vm_lifecycle_lock,
)
from .. import vm_provisioning
from ..auth import login_required, get_authenticated_user
from ..backends import enabled_backends, get_backend
from ..backends.base import (
    BACKEND_PROXMOX, GUEST_ABSENT, GUEST_MATCH, DestroyPolicy, VMRef, VMSpec,
)
from ..backends.template_spec import (
    backend_block, resolve_group_backend, validate_group_backends,
)
from ..utils import sanitize_name
from ..template_manager import load_template_config, get_deployment_group_by_name, get_group_mode
from ..models import StudentGroup, StudentGroupMember
from ..untracked_survivors import record_untracked_survivors, track_survivor

# Create blueprint
vm_creation_bp = Blueprint('vm_creation', __name__)

# Constants
MAX_CONCURRENT_WORKERS = 5
RESERVATION_HEARTBEAT_SECONDS = 60
MAX_CONCURRENT_CLONES = 2  # ZFS limitation

# ZFS clone concurrency is a property of the NODE's storage pool, not of
# whoever happens to be asking. These used to be rebuilt inside every
# request, which meant the limit only ever applied within one deploy: 25
# groups deploying at once produced up to 25 x nodes x 2 concurrent full
# clones instead of 2 per node, and a clone that outran the 300s task
# timeout failed its whole deploy and triggered cleanup_vms. One registry,
# shared by every request in this worker.
#
# `threading.Semaphore` is the right primitive here even though production
# runs gevent: monkey.patch_all() makes it greenlet-aware, and the single
# worker means process-wide == cluster-wide for this app.
_node_clone_semaphores: Dict[str, threading.Semaphore] = {}
_node_clone_semaphores_lock = threading.Lock()


def get_node_clone_semaphores(node_names) -> Dict[str, threading.Semaphore]:
    """Return the shared per-node clone semaphores, creating any missing.

    Always returns the SAME object for a given node name, so concurrent
    deploys contend with each other rather than each getting a fresh
    allowance.

    `node_names` is materialised first: callers pass a generator over
    `proxmox.nodes.get()`, and iterating it twice would leave the returned
    mapping empty — which makes `create_single_vm_task` take its "no
    semaphore for this node" fallback and clone with no limit at all.
    """
    names = list(node_names)
    with _node_clone_semaphores_lock:
        for name in names:
            if name not in _node_clone_semaphores:
                _node_clone_semaphores[name] = threading.Semaphore(
                    MAX_CONCURRENT_CLONES)
        return {name: _node_clone_semaphores[name] for name in names}


# ============================================================================
# Helper Functions (Extracted for clarity and testability)
# ============================================================================

def validate_user_vlans(user):
    """Validate user has required VLAN tags assigned"""
    if not user:
        return False, 'User not found'
    if user.entry_vlan_tag is None or user.internal_vlan_tag is None:
        return False, 'VLANs not assigned. Please contact an administrator.'
    return True, None


def check_vm_exists(proxmox, vm_name):
    """Check if VM with given name already exists on any node"""
    for node in proxmox.nodes.get():
        node_name = node['node']
        # Check LXC
        for container in proxmox.nodes(node_name).lxc.get():
            if container['name'] == vm_name:
                return True, node_name
        # Check QEMU
        for vm in proxmox.nodes(node_name).qemu.get():
            if vm['name'] == vm_name:
                return True, node_name
    return False, None


def build_vm_name(student_name, template_name):
    """Build standardized VM name"""
    return f"student-{sanitize_name(student_name)}-{sanitize_name(template_name)}"


def build_group_vm_name(group_name, template_name):
    """Name format: group-{slug}-{template}. Parallel to build_vm_name()."""
    return f"group-{sanitize_name(group_name)}-{sanitize_name(template_name)}"


def build_koth_vm_name(game_id, template_name):
    """Name format: koth-{game_id}-{template}. Parallel to build_vm_name()."""
    return f"koth-{game_id}-{sanitize_name(template_name)}"


def extract_template_config(template_config):
    """Extract and provide defaults for template configuration"""
    return {
        'name': template_config.get('name', 'vm'),
        'type': template_config.get('type', 'lxc'),
        'memory': template_config.get('memory', 2048),
        'cores': template_config.get('cores', 2),
        'storage': template_config.get('storage', 20),
        'storage_device': template_config.get('storage_device', 'local-lvm'),
        'storage_locations': template_config.get('storage_locations', {}),
        'ostemplate': template_config.get('ostemplate'),
        'is_entry_point': template_config.get('entry_point', False),
        'template_locations': template_config.get('template_locations', {}),
        'source_vmid': template_config.get('source_vmid'),
        'static_mac': template_config.get('static_mac'),
        # Linked clones for large Windows templates: a full clone of a 100 GB
        # image exceeds the 300 s task timeout and the pool. Defaults True so
        # every existing template keeps its current behaviour.
        'full_clone': template_config.get('full_clone', True),
        # A single-VM lab needs one NIC. A second one costs a second DHCP
        # lease, an ambiguous second 10.x address in the UI, and a second
        # Windows firewall profile classified as Public.
        'internal_nic': template_config.get('internal_nic', True),
        # How a student reaches this VM. None keeps the SSH-only presentation
        # every existing template uses.
        'connect': template_config.get('connect'),
    }


# ============================================================================
# VM Creation Functions
# ============================================================================

def create_lxc_container(proxmox, node, vmid, vm_name, config, expiration_time,
                        internal_vlan_tag=None, entry_vlan_tag=None):
    """Create LXC container with VLAN configuration"""
    params = {
        'vmid': vmid,
        'hostname': vm_name,
        'memory': config['memory'],
        'cores': config['cores'],
        'storage': config['storage_device'],
        'ostype': 'debian',
        'ostemplate': config['ostemplate']
    }

    # Configure networks with VLAN tags

    params['net0'] = f'name=eth0,bridge=vmbr0,tag={entry_vlan_tag},ip=dhcp' if entry_vlan_tag \
                     else f'name=eth0,bridge=vmbr0,tag={internal_vlan_tag},ip=dhcp'
    if entry_vlan_tag:
        params['net1'] = f'name=eth1,bridge=vmbr0,tag={internal_vlan_tag},ip=dhcp'


    proxmox.nodes(node).lxc.create(**params)
    proxmox.nodes(node).lxc(vmid).config.put(
        description=f"Expires: {expiration_time.isoformat()}"
    )


def create_qemu_vm(proxmox, target_node, source_node, vmid, vm_name, source_vmid,
                   config, expiration_time, internal_vlan_tag=None, entry_vlan_tag=None):
    """Create QEMU VM by cloning from template"""

    # Use source node for local storage
    if source_node != target_node:
        target_node = source_node
        logger.info(f"Using source node {source_node} for local storage")

    # Clone VM
    full_clone = 1 if config.get('full_clone', True) else 0
    logger.info(f"Cloning VM {vmid} from template {source_vmid} "
                f"({'full' if full_clone else 'linked'})...")
    clone_args = {'newid': vmid, 'name': vm_name, 'full': full_clone}
    # Proxmox takes a target storage only for full clones. Without a
    # storage_locations entry for this node, omit it so the clone lands on the
    # template's own storage; the legacy 'local-lvm' default exists on neither node.
    node_storage = config.get('storage_locations', {}).get(source_node)
    if full_clone and node_storage:
        clone_args['storage'] = node_storage
    task_upid = proxmox.nodes(source_node).qemu(source_vmid).clone.post(**clone_args)

    # Wait for clone to complete
    task_result = wait_for_proxmox_task(proxmox, source_node, task_upid, timeout=300)
    if not task_result['success']:
        raise Exception(f"Clone failed: {task_result.get('error', 'Unknown')}")

    logger.info(f"Clone completed for VM {vmid}")

    # Configure VM
    proxmox.nodes(target_node).qemu(vmid).config.put(
        memory=config['memory'],
        cores=config['cores'],
        description=f"Expires: {expiration_time.isoformat()}"
    )

    # Configure networks
    static_mac = config.get('static_mac')
    mac_suffix = f',macaddr={static_mac}' if static_mac else ''
    if entry_vlan_tag:
        net0 = f'virtio,bridge=vmbr0,tag={entry_vlan_tag}{mac_suffix}'
        proxmox.nodes(target_node).qemu(vmid).config.put(net0=net0)
        if config.get('internal_nic', True):
            net1 = f'virtio,bridge=vmbr0,tag={internal_vlan_tag}'
            proxmox.nodes(target_node).qemu(vmid).config.put(net1=net1)
            logger.info(f"Configured entry VLAN {entry_vlan_tag} + internal "
                        f"VLAN {internal_vlan_tag} for VM {vmid}")
        else:
            proxmox.nodes(target_node).qemu(vmid).config.put(delete="net1")
            logger.info(f"Configured entry VLAN {entry_vlan_tag} only (single NIC) "
                        f"for VM {vmid}")
    else:
        proxmox.nodes(target_node).qemu(vmid).config.put(delete="net1")

        net0 = f'virtio,bridge=vmbr0,tag={internal_vlan_tag}{mac_suffix}' if internal_vlan_tag \
            else f'virtio,bridge=vmbr0{mac_suffix}'

        proxmox.nodes(target_node).qemu(vmid).config.put(net0=net0)

    return target_node


def start_vm(proxmox, node, vmid, vm_type):
    """Start a VM (LXC or QEMU)"""
    if vm_type == 'lxc':
        proxmox.nodes(node).lxc(vmid).status.start.post()
    else:
        proxmox.nodes(node).qemu(vmid).status.start.post()


def _entry_backend(vm_info):
    # Entries written before backends existed (and every Proxmox result
    # without the key) are Proxmox guests.
    return vm_info.get('backend') or vm_info.get('_backend') or BACKEND_PROXMOX


def cleanup_vms(vm_list):
    """Destroy guests through their backends, returning only VMIDs confirmed absent.

    Each entry carries vmid, node, vm_type and vm_name (or name), plus the
    backend identity (backend or _backend, provider_id, deployment_id,
    network_slot); an entry without a backend is a Proxmox guest. Every
    destroy is BEST_EFFORT, the failed-deploy and cascade policy.
    """
    destroyed: set = set()
    if not vm_list:
        return destroyed

    logger.info(f"Cleaning up {len(vm_list)} VMs...")

    for vm_info in vm_list:
        vmid = vm_info.get('vmid')
        node = vm_info.get('node')
        vm_type = vm_info.get('vm_type', 'qemu')
        vm_name = vm_info.get('vm_name') or vm_info.get('name')
        identity_fields = {
            'backend': _entry_backend(vm_info),
            'provider_id': vm_info.get('provider_id'),
            'deployment_id': vm_info.get('deployment_id'),
            'network_slot': vm_info.get('network_slot'),
        }

        if vmid is None or node is None:
            logger.error(f"cleanup_vms: skipping malformed entry {vm_info!r}")
            continue

        with vm_lifecycle_lock(vmid):
            # A cleanup may target an uncommitted clone, so no row is normal.
            # If a row does exist, refresh it under the lifecycle lock and use
            # its identity rather than stale caller data.
            if has_app_context():
                try:
                    # End MySQL's REPEATABLE READ snapshot under the lifecycle
                    # lock before checking whether this VMID still has a row.
                    db.session.rollback()
                    record = VMRecord.query.filter_by(vmid=vmid).one_or_none()
                except Exception:
                    # Cleanup after a failed VMRecord commit must still be able
                    # to remove the uncommitted guest during a DB outage. The
                    # caller's exact name plus fresh inventory remain the
                    # identity boundary in that case.
                    logger.warning(
                        "cleanup_vms: VMRecord refresh failed for VM %s",
                        vmid, exc_info=True)
                    record = None
                if record is not None:
                    if record.destroyed:
                        continue
                    node = record.node
                    vm_type = record.vm_type
                    vm_name = record.vm_name
                    identity_fields = {
                        'backend': record.backend or BACKEND_PROXMOX,
                        'provider_id': record.provider_id,
                        'deployment_id': record.deployment_id,
                        'network_slot': record.network_slot,
                    }

            if vm_name is None:
                logger.error(
                    "cleanup_vms: refusing to mutate VM %s without an "
                    "expected guest name", vmid)
                continue

            ref = VMRef(vmid=vmid, vm_name=vm_name, vm_type=vm_type,
                        location=node, **identity_fields)
            try:
                if _cleanup_one(get_backend(ref.backend), ref):
                    destroyed.add(vmid)
            except Exception as error:
                # The provider could not answer for this guest. It stays
                # unconfirmed (a survivor record tracks it) and the next
                # entry is still cleaned.
                logger.error("cleanup_vms: could not clean up VM %s on %s: %s",
                             vmid, ref.backend, error)

    return destroyed


def _cleanup_one(backend, ref):
    """Destroy one verified guest; return whether it is confirmed absent."""
    with vm_provisioning.provider_session(backend):
        identity = backend.inspect(ref)
        if identity == GUEST_ABSENT:
            logger.info(f"VM {ref.vmid} is absent from fresh inventory")
            return True
        if identity != GUEST_MATCH:
            logger.error(
                "cleanup_vms: refusing to mutate unverified VM %s", ref.vmid)
            return False
        try:
            vm_provisioning.destroy_vm(ref, DestroyPolicy.BEST_EFFORT)
        except Exception as error:
            # A destroy that raised may still have removed the guest (a
            # timed-out terminate); only fresh inventory says so.
            if backend.confirm_absent(ref, error):
                logger.info(f"VM {ref.vmid} confirmed absent after a "
                            f"failed destroy: {error}")
                return True
            logger.error(f"Destroy of VM {ref.vmid} failed: {error}")
            return False
    logger.info(f"Cleaned up VM {ref.vmid}")
    return True


def _vm_name_prefix(*, owning_identifier, owner_group_id=None, koth_game_id=None):
    """Return the unique VM-name prefix this deploy uses.

    Inverse of build_vm_name / build_group_vm_name / build_koth_vm_name —
    used to sweep Proxmox for VMs that belong to a deploy but slipped past
    our in-process tracking (parallel workers that finished after the
    failure-snapshot of `created_vms` was taken).
    """
    if koth_game_id is not None:
        return f"koth-{koth_game_id}-"
    if owner_group_id is not None:
        return f"group-{sanitize_name(owning_identifier)}-"
    return f"student-{sanitize_name(owning_identifier)}-"

def _expected_vm_names(templates, *, owning_identifier,
                       owner_group_id=None, koth_game_id=None):
    """Return only the exact VM names one deployment can create."""
    if koth_game_id is not None:
        return {
            build_koth_vm_name(koth_game_id, template.get('name', 'vm'))
            for template in templates
        }
    if owner_group_id is not None:
        return {
            build_group_vm_name(
                owning_identifier, template.get('name', 'vm'))
            for template in templates
        }
    return {
        build_vm_name(owning_identifier, template.get('name', 'vm'))
        for template in templates
    }


def named_instances(backend, prefix):
    """[{vmid, node, vm_type, name}] of the backend's guests named `prefix`*.

    One list_managed() read; templates are never cleanup targets. Raises
    when the inventory cannot be read, so callers fall back to what they
    already know instead of reading an outage as "nothing there".
    """
    return [
        {'vmid': resource.vmid, 'node': resource.location,
         'vm_type': resource.vm_type or backend.name, 'name': resource.name}
        for resource in backend.list_managed()
        if resource.kind == 'instance' and not resource.is_template
        and resource.vmid is not None
        and (resource.name or '').startswith(prefix)
    ]


def _sweep_proxmox_by_name(payload, *, prefix, templates, owning_identifier,
                           owner_group_id, koth_game_id):
    """Add Proxmox guests with this deployment's exact names to `payload`."""
    expected_names = _expected_vm_names(
        templates,
        owning_identifier=owning_identifier,
        owner_group_id=owner_group_id,
        koth_game_id=koth_game_id,
    )
    known = {(vm['vmid'], vm['node']) for vm in payload}
    for vm in named_instances(get_backend(BACKEND_PROXMOX), prefix):
        if (vm.get('name') in expected_names
                and (vm['vmid'], vm['node']) not in known):
            matching_template = next(
                (
                    template for template in templates
                    if vm['name'] in _expected_vm_names(
                        [template],
                        owning_identifier=owning_identifier,
                        owner_group_id=owner_group_id,
                        koth_game_id=koth_game_id,
                    )
                ),
                {},
            )
            config = extract_template_config(matching_template)
            payload.append({
                'vmid': vm['vmid'],
                'node': vm['node'],
                'vm_type': vm['vm_type'],
                'vm_name': vm['name'],
                'vm_display_name': config['name'],
                'memory': config['memory'],
                'cores': config['cores'],
                'storage': config['storage'],
                'is_entry_point': config['is_entry_point'],
            })


def _sweep_cloud_by_deployment(payload, created_vms, backend_name):
    """Give each cloud result the instance its create left behind.

    A create can raise after the provider launched the instance (a waiter
    timeout), leaving a result without a provider id (ruling C7). The match
    is by (deployment_id, vmid), never by name: GCP names an instance
    lp-<site>-<vmid>, not the portal's vm_name. Results are updated in
    place, so the survivor record of an instance that will not die names
    where it actually runs.
    """
    wanted = {(vm.get('deployment_id'), vm.get('vmid')): vm
              for vm in created_vms
              if vm.get('deployment_id') and vm.get('vmid') is not None}
    if not wanted:
        return
    listed = {id(vm) for vm in payload}
    for resource in get_backend(backend_name).list_managed():
        result = wanted.get((resource.deployment_id, resource.vmid))
        if resource.kind != 'instance' or result is None:
            continue
        result['provider_id'] = result.get('provider_id') or resource.provider_id
        result['node'] = resource.location or result.get('node')
        if id(result) not in listed:
            payload.append(result)
            listed.add(id(result))


def _cleanup_with_sweep(created_vms, *, templates, owning_identifier,
                        owner_group_id=None, koth_game_id=None,
                        attempted_vms=None, backend_name=BACKEND_PROXMOX):
    """Clean every guest this deployment may own; return only confirmed deletions.

    A Proxmox bundle also sweeps its exact names; a cloud bundle matches
    the backend's instances by deployment and VMID instead, and never sweeps
    Proxmox by name (a same-named Proxmox lab of the same owner is not this
    deployment's). ``attempted_vms`` receives the complete cleanup payload
    when supplied so callers can durably track any guest whose deletion
    remains uncertain.
    """
    prefix = _vm_name_prefix(owning_identifier=owning_identifier,
                             owner_group_id=owner_group_id,
                             koth_game_id=koth_game_id)
    payload = [
        vm for vm in created_vms
        if vm.get('vmid') is not None
        and vm.get('node')
        and vm.get('vm_type')
    ]
    try:
        if backend_name == BACKEND_PROXMOX:
            _sweep_proxmox_by_name(
                payload, prefix=prefix, templates=templates,
                owning_identifier=owning_identifier,
                owner_group_id=owner_group_id, koth_game_id=koth_game_id)
        else:
            _sweep_cloud_by_deployment(payload, created_vms, backend_name)
    except Exception:
        logger.exception(
            "_cleanup_with_sweep: %s inventory sweep failed for %r "
            "(falling back to known VM results only)", backend_name, prefix)
    if attempted_vms is not None:
        attempted_vms.extend(payload)
    destroyed = cleanup_vms(payload)
    attempted = [item['vmid'] for item in payload]
    failed = [vmid for vmid in attempted if vmid not in destroyed]
    if failed:
        logger.error(
            "_cleanup_with_sweep: %d/%d VM(s) could not be destroyed for "
            "prefix %r: %s — durable cleanup tracking required",
            len(failed), len(attempted), prefix, failed)
    return destroyed


def _mark_cleaned_records_destroyed(destroyed_vmids) -> None:
    """Close live DB records for VMs cleanup actually removed."""
    if not destroyed_vmids:
        return
    try:
        records = VMRecord.query.filter(
            VMRecord.vmid.in_(destroyed_vmids),
            VMRecord.destroyed == False,  # noqa: E712
        ).all()
        if not records:
            return
        destroyed_at = datetime.now()
        for record in records:
            record.destroyed = True
            record.destroyed_at = destroyed_at
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Keeping the row live is conservative for admission, but operators
        # need to know why reclaimed capacity still appears committed.
        logger.exception(
            "Cleanup destroyed VMs %s but their VMRecords stayed live",
            sorted(destroyed_vmids))


# Per-target deploy locks. Keyed on the VM-name prefix (group-<slug>-,
# student-<login>-, koth-<game_id>-). Prevents two concurrent deploys for
# the same target from racing the pre-flight idempotency check — without
# this, both could see "fresh" and both kick off clones with the same names.
_deploy_locks_mutex = threading.Lock()
_deploy_locks: "dict[str, threading.Lock]" = {}


def _acquire_deploy_lock(prefix):
    """Non-blocking acquire of the per-target deploy lock. Returns the
    lock object (caller must release in a finally) or None if another
    deploy for this same target is currently in flight."""
    with _deploy_locks_mutex:
        lock = _deploy_locks.setdefault(prefix, threading.Lock())
    return lock if lock.acquire(blocking=False) else None


def _preflight_deployment_group(templates, *, owning_identifier,
                                owner_group_id=None, koth_game_id=None,
                                backend_name=BACKEND_PROXMOX):
    """Inspect `backend_name` for VMs the new deploy would shadow. Returns one of:

    - ('already_deployed', list[vm]) — every expected VM name is already
      present. Caller should no-op and report success; the student already
      has what they're asking for. Double-clicking deploy is a free retry.
    - ('partial', list[vm]) — some expected names exist, some don't. The
      bundle is in broken state (VLANs/firewall likely misconfigured for the
      missing peers). Caller should destroy the returned list, then proceed
      with a fresh deploy of all templates.
    - ('fresh', []) — no matching VMs. Caller proceeds normally.

    Compares by exact name match, so unrelated VMs sharing the prefix are
    ignored. Only the names this deploy would create are considered.
    """
    expected_names = _expected_vm_names(
        templates,
        owning_identifier=owning_identifier,
        owner_group_id=owner_group_id,
        koth_game_id=koth_game_id,
    )

    prefix = _vm_name_prefix(owning_identifier=owning_identifier,
                             owner_group_id=owner_group_id,
                             koth_game_id=koth_game_id)
    try:
        inventory = named_instances(get_backend(backend_name), prefix)
    except Exception:
        # As the per-node listing always did: an unreadable inventory
        # reports nothing to shadow, and the deploy itself then meets the
        # outage.
        logger.exception("Pre-flight inventory read failed for %r", prefix)
        inventory = []
    existing = [vm for vm in inventory if vm.get('name') in expected_names]
    if not existing:
        return ('fresh', [])
    if {vm.get('name') for vm in existing} == expected_names:
        return ('already_deployed', existing)
    return ('partial', existing)


def create_single_vm_task(vm_index, template_config, assigned_vmid, student_name,
                          load_balancing_config, user_entry_vlan, user_internal_vlan,
                          expiration_time, node_semaphores, owner_group_id=None,
                          koth_game_id=None, flask_app=None,
                          vmid_reservation_id=None, backend_name=BACKEND_PROXMOX):
    """Create one VM on `backend_name` while holding its admission reservation.

    The caller owns a successful task's reservation and releases it only
    after the corresponding VMRecord commit. Failed task reservations are
    returned too, so bundle cleanup can finish before capacity is reusable.
    `node_semaphores` are Proxmox's per-node clone limits (None for a cloud
    bundle). Every result names its backend, admission reservation and
    deployment so finalization can release and clean up through it.
    """
    config = {}
    spec = None
    vm_name = None
    # (vmid, node) of a Proxmox guest this task may have created.
    claimed = None
    reservation_id = uuid4().hex
    identity = {
        '_backend': backend_name,
        '_reservation_id': reservation_id,
        '_vmid_reservation_id': vmid_reservation_id,
        'deployment_id': vmid_reservation_id,
    }
    try:
        block = backend_block(template_config, backend_name)
        if backend_name == BACKEND_PROXMOX:
            config = extract_template_config(block)
            template = config
        else:
            # Name, entry point and the recorded sizes are catalog-level; the
            # provider's machine spec is the backend's own block.
            config = dict(extract_template_config(template_config),
                          storage=block['disk_gb'])
            template = block
        if koth_game_id is not None:
            vm_name = build_koth_vm_name(koth_game_id, config['name'])
        elif owner_group_id is not None:
            vm_name = build_group_vm_name(student_name, config['name'])
        else:
            vm_name = build_vm_name(student_name, config['name'])

        if flask_app is None:
            raise RuntimeError(
                'VM creation requires an app context for admission control')
        backend = get_backend(backend_name)
        spec = VMSpec(
            vmid=assigned_vmid, vm_name=vm_name, display_name=config['name'],
            backend=backend_name, template=template,
            memory_mb=config['memory'], cores=config['cores'],
            storage_gb=config['storage'],
            is_entry_point=config['is_entry_point'],
            entry_slot=user_entry_vlan if config['is_entry_point'] else None,
            internal_slot=user_internal_vlan,
            expires_at=expiration_time,
            deployment_id=vmid_reservation_id,
            owner=student_name,
        )

        def heartbeat():
            if not backend.renew_admission(reservation_id):
                raise RuntimeError('Admission reservation expired')

        create_options = None
        connection = nullcontext()
        if backend_name == BACKEND_PROXMOX:
            def on_claim(vmid, node):
                nonlocal claimed
                claimed = None if vmid is None else (vmid, node)

            create_options = {'clone_semaphores': node_semaphores,
                              'on_claim': on_claim}
            # One connection for this task's admission and clone, as the
            # task always opened.
            connection = backend.session()

        # ThreadPoolExecutor workers do not inherit Flask's app context.
        # Supplying it here gives each admission a fresh scoped DB session,
        # so a just-committed VMRecord is visible before its reservation is
        # released; the collision retry's allocation and distributed router
        # and slot locks need it too.
        with flask_app.app_context(), connection:
            _location, launched = vm_provisioning.create_vm(
                spec, reservation_id=reservation_id,
                load_balancing_config=load_balancing_config,
                heartbeat=heartbeat, create_options=create_options)

        return {
            'success': True,
            'vm_index': vm_index,
            'vmid': launched.vmid,
            'vm_name': vm_name,
            'vm_display_name': config['name'],
            'vm_type': launched.vm_type,
            'node': launched.location,
            'memory': config['memory'],
            'cores': config['cores'],
            'storage': config['storage'],
            'is_entry_point': config['is_entry_point'],
            'provider_id': launched.provider_id,
            'network_slot': launched.network_slot,
            **identity,
        }
    except Exception as e:
        logger.error(f"Error creating VM {config.get('name', vm_index)}: {e}")
        result = {
            'success': False,
            'error': str(e),
            'template_name': config.get('name', f'vm{vm_index}'),
            'vm_index': vm_index,
            'provider_id': None,
            'network_slot': spec.network_slot if spec is not None else None,
            **identity,
        }
        if claimed is not None and config and vm_name:
            claimed_vmid, claimed_node = claimed
            result.update({
                'vmid': claimed_vmid,
                'vm_name': vm_name,
                'vm_display_name': config['name'],
                'vm_type': config['type'],
                'node': claimed_node,
                'memory': config['memory'],
                'cores': config['cores'],
                'storage': config['storage'],
                'is_entry_point': config['is_entry_point'],
            })
        elif backend_name != BACKEND_PROXMOX and spec is not None:
            # The bundle's VMID reservation makes this VMID ours alone, so
            # a cloud instance carrying it and this deployment can only be
            # this create's, even when create() raised after the launch
            # (ruling C7). Cleanup inspects it; the cloud sweep supplies the
            # provider id and zone when the instance exists.
            result.update({
                'vmid': spec.vmid,
                'vm_name': vm_name,
                'vm_display_name': config['name'],
                'vm_type': backend_name,
                'node': backend_name,
                'memory': config['memory'],
                'cores': config['cores'],
                'storage': config['storage'],
                'is_entry_point': config['is_entry_point'],
            })
        return result


def _admission_backend(result):
    # Results without a backend (test doubles of create_single_vm_task)
    # are Proxmox admissions, as every result was before backends existed.
    return get_backend(result.get('_backend') or BACKEND_PROXMOX)


def _release_vm_reservations(vm_results) -> None:
    for result in vm_results:
        _admission_backend(result).release_admission(
            result.get('_reservation_id'))


@contextmanager
def _heartbeat_deployment_reservations(vm_results, vmid_reservation_id):
    """Renew create leases until cleanup and survivor tracking finish."""
    reservations = [
        (_admission_backend(result), result['_reservation_id'])
        for result in vm_results
        if result.get('_reservation_id')
    ]
    stopped = threading.Event()

    def heartbeat():
        while not stopped.is_set():
            if vmid_reservation_id:
                renew_vmid_reservation(vmid_reservation_id)
            for backend, reservation_id in reservations:
                backend.renew_admission(reservation_id)
            stopped.wait(RESERVATION_HEARTBEAT_SECONDS)

    worker = threading.Thread(
        target=heartbeat,
        name='failed-deploy-reservation-heartbeat',
        daemon=True,
    )
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()


def _persist_failed_deploy_leftovers(
        vm_results, destroyed_vmids, *, deployment_group_name,
        owning_identifier, user_id, owner_group_id=None, koth_game_id=None):
    """Commit promptly-expiring records for every uncertain cleanup result.

    If the database refuses the commit, the survivors go to the durable
    fallback file (app/untracked_survivors.py) for import once it is back,
    and the error still propagates so their admission reservations stay held.
    """
    survivors = []
    seen_vmids = set()
    for vm_info in vm_results:
        vmid = vm_info.get('vmid')
        if (vmid is None or vmid in destroyed_vmids or vmid in seen_vmids
                or not vm_info.get('node')
                or not vm_info.get('vm_type')):
            continue
        seen_vmids.add(vmid)
        survivors.append(vm_info)

    if not survivors:
        return []

    marker_group = f"failed-deploy-leftover/{deployment_group_name}"
    entries = [
        {
            'vmid': vm_info['vmid'],
            'node': vm_info['node'],
            'vm_name': vm_info['vm_name'],
            'vm_type': vm_info['vm_type'],
            'template_name': (
                f"{marker_group}/"
                f"{vm_info.get('vm_display_name', 'unknown')}"),
            'student_name': owning_identifier,
            'created_by_user_id': user_id,
            'owner_group_id': owner_group_id,
            'koth_game_id': koth_game_id,
            'memory': vm_info.get('memory'),
            'cores': vm_info.get('cores'),
            'storage': vm_info.get('storage'),
            'is_entry_point': bool(vm_info.get('is_entry_point', False)),
            # Enough identity for the expiry daemon to find the guest again
            # through its own backend after a restart.
            'backend': _entry_backend(vm_info),
            'provider_id': vm_info.get('provider_id'),
            'deployment_id': vm_info.get('deployment_id'),
            'network_slot': vm_info.get('network_slot'),
        }
        for vm_info in survivors
    ]
    retry_at = datetime.now() - timedelta(seconds=1)
    try:
        for entry in entries:
            track_survivor(
                entry, retry_at,
                record=VMRecord.query.filter_by(vmid=entry['vmid']).first())
        db.session.commit()
    except Exception as error:
        db.session.rollback()
        logger.exception(
            "Could not commit cleanup tracking for surviving VMIDs %s; "
            "their admission reservations remain held",
            sorted(seen_vmids),
        )
        try:
            path = record_untracked_survivors(
                entries,
                reason=f"tracking commit failed ({type(error).__name__})")
        except Exception:
            logger.critical(
                "Surviving VMIDs %s are UNTRACKED: neither the database nor "
                "the fallback file took them. Only the orphan reaper, after "
                "the guests' Expires: time plus grace, will remove them.",
                sorted(seen_vmids), exc_info=True)
        else:
            logger.error(
                "Recorded surviving VMIDs %s in %s; they are imported for "
                "immediate expiry once the database answers",
                sorted(seen_vmids), path)
        raise
    return survivors


def _finalize_failed_deployment(
        vm_results, *, vmid_reservation_id, templates,
        deployment_group_name, owning_identifier, user_id,
        owner_group_id=None, koth_game_id=None, backend_name=BACKEND_PROXMOX):
    """Clean a failed deployment and transfer survivors into VMRecords."""
    attempted_vms = list(vm_results)
    with _heartbeat_deployment_reservations(
            vm_results, vmid_reservation_id):
        try:
            destroyed = _cleanup_with_sweep(
                vm_results,
                templates=templates,
                owning_identifier=owning_identifier,
                owner_group_id=owner_group_id,
                koth_game_id=koth_game_id,
                attempted_vms=attempted_vms,
                backend_name=backend_name,
            )
        except Exception:
            destroyed = set()
            logger.exception(
                "Failed-deploy cleanup aborted; tracking every known guest "
                "for immediate expiry retry")
        _mark_cleaned_records_destroyed(destroyed)
        survivors = _persist_failed_deploy_leftovers(
            attempted_vms,
            destroyed,
            deployment_group_name=deployment_group_name,
            owning_identifier=owning_identifier,
            user_id=user_id,
            owner_group_id=owner_group_id,
            koth_game_id=koth_game_id,
        )
        if backend_name != BACKEND_PROXMOX and not survivors:
            # Every launched instance is confirmed gone, so nothing uses the
            # deployment's network. With a survivor it stays for that
            # record's retirement, or for the sweeper.
            try:
                get_backend(backend_name).cleanup_deployment(vmid_reservation_id)
            except Exception:
                logger.exception("Could not clean up deployment %s of %s; the "
                                 "sweeper will retry", vmid_reservation_id,
                                 backend_name)
    _release_vm_reservations(vm_results)
    if backend_name != BACKEND_PROXMOX:
        # Slot networks carry no deployment id, so only this release keeps a
        # failed deploy from leaking its subnet (ruling I4). After the
        # bundle's own admissions are gone, which would otherwise hold it.
        for slot in sorted({vm['network_slot'] for vm in vm_results
                            if vm.get('network_slot') is not None}):
            vm_provisioning.release_slot_if_unused(backend_name, slot)
    return destroyed, survivors




def _collect_vm_results(
        futures, created_vms, failed_vms, vmid_reservation_id=None) -> None:
    """Collect clone results while heartbeating deployment reservations."""
    pending = set(futures)
    while pending:
        done, pending = wait(
            pending,
            timeout=RESERVATION_HEARTBEAT_SECONDS,
            return_when=FIRST_COMPLETED,
        )
        if (vmid_reservation_id
                and not renew_vmid_reservation(vmid_reservation_id)):
            logger.error(
                "VMID reservation %s expired before bundle finalization",
                vmid_reservation_id)
        for future in done:
            result = future.result()
            (created_vms if result['success'] else failed_vms).append(result)

        # A fast VM can finish long before a slow sibling gets a clone slot.
        # Keep both successful and failed reservations until the orchestrator
        # can commit or clean up the whole bundle.
        for result in created_vms + failed_vms:
            reservation_id = result.get('_reservation_id')
            if (reservation_id
                    and not _admission_backend(result).renew_admission(
                        reservation_id)):
                logger.error(
                    "Admission reservation %s expired before bundle "
                    "finalization", reservation_id)


class DeployFailed(Exception):
    """Raised by deploy_bundle when one or more VMs failed to clone.

    By the time this is raised, every partially-created VM is either
    confirmed deleted or committed as a promptly-expiring cleanup record;
    the message lists which templates failed and why.
    """


def deploy_bundle(*, deployment_group_name, templates, owning_identifier,
                  entry_vlan, internal_vlan, uptime_minutes, user_id,
                  load_balancing_config=None, owner_group_id=None,
                  koth_game_id=None, backend_name=BACKEND_PROXMOX):
    """Parallel-create a bundle of templates and persist VMRecord rows.

    This is the same orchestration the /create handler uses (admission,
    per-node ZFS clone semaphores, ThreadPoolExecutor across templates,
    cleanup on partial failure). KOTH calls this so its VMs go through the
    exact same code path as a regular group-mode lab.

    Parameters:
        deployment_group_name: stored in VMRecord.template_name as
            "<deployment_group_name>/<template_display_name>".
        templates: catalog machine entries from templates.yaml; each is
            resolved to `backend_name`'s block.
        owning_identifier: stored in VMRecord.student_name. For student
            deploys, this is the student login_id; for group deploys, the
            group slug; for KOTH, the game id stringified or 'koth'.
        entry_vlan / internal_vlan: per-deployment VLAN tags (a cloud
            backend's network slots).
        uptime_minutes: initial expiration window.
        user_id: created_by_user_id on the VMRecord (the admin/student
            who clicked deploy).
        load_balancing_config: deployment_group's load_balancing dict.
        owner_group_id: set for group-mode deploys (drives the group-naming
            path in create_single_vm_task and the FK on VMRecord).
        koth_game_id: set for KOTH deploys. Mutually exclusive with
            owner_group_id from a naming standpoint (koth wins).
        backend_name: the backend every machine of the bundle runs on.

    Returns: list[VMRecord] in the same order the templates were submitted
    (insertion order preserved by collecting via a dict, not as_completed).

    Raises: DeployFailed on any template clone failure (already cleaned up).
    """
    get_backend(backend_name)  # refuse a disabled backend before reserving
    # Whenever Proxmox is enabled the allocator must see its inventory, even
    # for a cloud bundle: VMIDs are one namespace, and a manually created
    # Proxmox guest must not share one with a cloud VM. A cloud-only portal
    # has no cluster to read.
    proxmox = (get_backend(BACKEND_PROXMOX).connect()
               if BACKEND_PROXMOX in enabled_backends() else None)
    flask_app = current_app._get_current_object()
    vmid_reservation_id = uuid4().hex
    base_vmid = get_next_available_vmid(
        proxmox,
        count=len(templates),
        reservation_id=vmid_reservation_id,
    )
    vmid_safe_to_release = True
    try:
        vm_assignments = [
            (idx + 1, template, base_vmid + idx)
            for idx, template in enumerate(templates)
        ]
        logger.info(
            f"deploy_bundle: {deployment_group_name} ({len(templates)} VMs) "
            f"on {backend_name}, "
            f"vmids {base_vmid}-{base_vmid + len(templates) - 1}, "
            f"owning={owning_identifier!r} group={owner_group_id} "
            f"koth={koth_game_id}"
        )

        node_semaphores = None
        if backend_name == BACKEND_PROXMOX:
            all_nodes = proxmox.nodes.get()
            node_semaphores = get_node_clone_semaphores(
                n['node'] for n in all_nodes)

        initial_expiration = (
            datetime.now() + timedelta(minutes=uptime_minutes))
        created_vms = []
        failed_vms = []

        max_workers = min(len(vm_assignments), MAX_CONCURRENT_WORKERS)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            vmid_safe_to_release = False
            futures = {}
            for idx, template, vmid in vm_assignments:
                try:
                    future = executor.submit(
                        create_single_vm_task,
                        idx, template, vmid,
                        owning_identifier, load_balancing_config,
                        entry_vlan, internal_vlan,
                        initial_expiration, node_semaphores,
                        owner_group_id, koth_game_id, flask_app,
                        vmid_reservation_id, backend_name,
                    )
                except Exception as submit_error:
                    logger.exception(
                        "Could not submit VM %s for creation", vmid)
                    failed_vms.append({
                        'success': False,
                        'error': f'executor submission failed: {submit_error}',
                        'template_name': template.get(
                            'name', f'vm{idx}'),
                        'vm_index': idx,
                        '_backend': backend_name,
                        '_reservation_id': None,
                        '_vmid_reservation_id': vmid_reservation_id,
                        'deployment_id': vmid_reservation_id,
                        'provider_id': None,
                        'network_slot': None,
                    })
                    break
                futures[future] = (idx, template, vmid)
            if futures:
                _collect_vm_results(
                    futures, created_vms, failed_vms,
                    vmid_reservation_id)

        if failed_vms:
            _destroyed, survivors = _finalize_failed_deployment(
                created_vms + failed_vms,
                vmid_reservation_id=vmid_reservation_id,
                templates=templates,
                deployment_group_name=deployment_group_name,
                owning_identifier=owning_identifier,
                user_id=user_id,
                owner_group_id=owner_group_id,
                koth_game_id=koth_game_id,
                backend_name=backend_name,
            )
            vmid_safe_to_release = True
            leftover = [vm['vmid'] for vm in survivors]
            details = ", ".join(
                f"{vm['template_name']}: {vm['error']}"
                for vm in failed_vms)
            cleanup_msg = (
                "cleanup of created VMs succeeded" if not leftover
                else f"cleanup retained {len(leftover)} VM(s) for immediate "
                     f"expiry retry (vmids: {leftover})")
            raise DeployFailed(
                f"failed to create {len(failed_vms)}/"
                f"{len(vm_assignments)} VM(s) ({details}); {cleanup_msg}")

        # Recompute expiration from now so the countdown reflects when VMs
        # are ready, not when the request was received.
        expiration_time = (
            datetime.now() + timedelta(minutes=uptime_minutes))
        vm_records = []
        # A cloud admission must stay live until its VMRecord commits, or the
        # router idle step could stop the router under it (ruling C4).
        renewing = (nullcontext() if backend_name == BACKEND_PROXMOX else
                    _heartbeat_deployment_reservations(
                        created_vms, vmid_reservation_id))
        try:
            with renewing:
                by_index = {vm['vm_index']: vm for vm in created_vms}
                for idx, _template, _vmid in vm_assignments:
                    vm_info = by_index[idx]
                    rec = log_vm_to_database(
                        vm_info, deployment_group_name, owning_identifier,
                        expiration_time, user_id, owner_group_id, koth_game_id)
                    vm_records.append(rec)
                db.session.commit()
        except Exception as db_error:
            logger.error(
                "deploy_bundle: DB logging failed; rolling back: %s",
                db_error)
            db.session.rollback()
            _destroyed, survivors = _finalize_failed_deployment(
                created_vms,
                vmid_reservation_id=vmid_reservation_id,
                templates=templates,
                deployment_group_name=deployment_group_name,
                owning_identifier=owning_identifier,
                user_id=user_id,
                owner_group_id=owner_group_id,
                koth_game_id=koth_game_id,
                backend_name=backend_name,
            )
            vmid_safe_to_release = True
            leftover = [vm['vmid'] for vm in survivors]
            cleanup_msg = (
                "all VMs have been cleaned up" if not leftover
                else f"cleanup retained {len(leftover)} VM(s) for immediate "
                     f"expiry retry (vmids: {leftover})")
            raise DeployFailed(
                f"VMs created in Proxmox but DB logging failed: {db_error}; "
                f"{cleanup_msg}")

        vmid_safe_to_release = True
        _release_vm_reservations(created_vms)
        return vm_records
    finally:
        if vmid_safe_to_release:
            release_vmid_reservation(vmid_reservation_id)


def log_vm_to_database(vm_info, deployment_group_name, student_name, expiration_time, user_id,
                       owner_group_id=None, koth_game_id=None):
    """Stage one VMRecord; the bundle orchestrator owns the transaction.

    The backend identity is what lets every later lifecycle path find and
    destroy the guest after a restart.
    """
    vm_record = VMRecord(
        vmid=vm_info['vmid'],
        vm_name=vm_info['vm_name'],
        vm_type=vm_info['vm_type'],
        node=vm_info['node'],
        template_name=f"{deployment_group_name}/{vm_info['vm_display_name']}",
        student_name=student_name,
        memory=vm_info['memory'],
        cores=vm_info['cores'],
        storage=vm_info['storage'],
        ip_address=None,  # Queried live
        created_at=datetime.now(),
        expires_at=expiration_time,
        created_by_user_id=user_id,
        is_entry_point=vm_info.get('is_entry_point', False),
        owner_group_id=owner_group_id,
        koth_game_id=koth_game_id,
        backend=vm_info.get('_backend') or BACKEND_PROXMOX,
        provider_id=vm_info.get('provider_id'),
        deployment_id=vm_info.get('deployment_id'),
        network_slot=vm_info.get('network_slot'),
    )
    db.session.add(vm_record)
    return vm_record


# ============================================================================
# Main Route
# ============================================================================

@vm_creation_bp.route('/create', methods=['POST'])
@login_required
def create_vm():
    """
    Create VMs from a deployment group

    SIMPLIFIED VERSION with extracted helper functions
    """
    # The decorator validated the revocable session user.
    user = get_authenticated_user()
    student_name = user.login_id.lower()
    user_id = user.id

    # KOTH pause-other-deployments gate: if an active KOTH game has the
    # pause flag set, block non-admin deployments until the game ends.
    from ..models import KothGame
    paused = KothGame.query.filter_by(is_active=True,
                                      pause_other_deployments=True).first()
    if paused and not (user and user.is_admin):
        return jsonify({'error': 'Deployments paused: KOTH game in progress.'}), 409

    # Get request parameters
    deployment_group_name = request.form.get('deployment_group', '').strip()
    if not deployment_group_name:
        return jsonify({'success': False, 'error': 'Deployment group required'})

    try:
        # Load deployment group configuration
        config = load_template_config()
        deployment_group = get_deployment_group_by_name(config, deployment_group_name)

        if not deployment_group:
            return jsonify({'success': False, 'error': f'Deployment group {deployment_group_name} not found'})

        # The backend decides which checks apply, so it is resolved before
        # any of them; a group no backend can run is refused with the
        # errors the admin's template listing shows.
        backend_errors = validate_group_backends(deployment_group, enabled_backends())
        if backend_errors:
            return jsonify({'success': False, 'error': '; '.join(backend_errors)})
        backend_name = resolve_group_backend(deployment_group)

        if backend_name == BACKEND_PROXMOX:
            valid, error = validate_user_vlans(user)
            if not valid:
                return jsonify({'success': False, 'error': error})
            logger.info(f"User {user.login_id}: entry_vlan={user.entry_vlan_tag}, internal_vlan={user.internal_vlan_tag}")

        templates = deployment_group.get('templates', [])
        if not templates:
            return jsonify({'success': False, 'error': 'No templates configured'})

        uptime_minutes = deployment_group.get('uptime_minutes', 4320)
        load_balancing_config = deployment_group.get('load_balancing')

        # Determine group_mode vs individual mode and set VLAN / owning identifiers
        group_mode = get_group_mode(deployment_group)

        if group_mode:
            if not user.is_admin:
                membership = StudentGroupMember.query.filter_by(user_id=user.id).first()
                if not membership:
                    return jsonify({'success': False,
                                    'error': "You are not in a student group; group-mode labs are unavailable."})
                group = db.session.get(StudentGroup, membership.group_id)
                existing_group_vm = VMRecord.query.filter_by(
                    owner_group_id=group.id, destroyed=False).first()
                if existing_group_vm:
                    return jsonify({'success': False,
                                    'error': (f"Your group already has an active deployment of "
                                              f"{deployment_group_name!r}. Any member can destroy it.")})
            else:
                slug = request.form.get('group_slug', '').strip()
                if not slug:
                    return jsonify({'success': False,
                                    'error': 'admin deploying a group-mode lab must set group_slug'})
                group = StudentGroup.query.filter_by(name=slug).first()
                if not group:
                    return jsonify({'success': False,
                                    'error': f'no group with slug {slug!r}'})
                existing_group_vm = VMRecord.query.filter_by(
                    owner_group_id=group.id, destroyed=False).first()
                if existing_group_vm:
                    return jsonify({'success': False,
                                    'error': (f"Group {slug!r} already has an active deployment of "
                                              f"{deployment_group_name!r}. Destroy it before redeploying.")})

            entry_vlan = group.entry_vlan_tag
            internal_vlan = group.internal_vlan_tag
            owning_identifier = group.name
            owner_group_id = group.id
        else:
            # Individual mode — existing slot rule, filter to individual-only
            if not user.is_admin:
                existing = VMRecord.query.filter_by(
                    created_by_user_id=user.id, destroyed=False,
                    owner_group_id=None,
                ).first()
                if existing:
                    return jsonify({
                        'success': False,
                        'error': 'You already have an active deployment group. Please destroy your existing VMs before launching a new one.'
                    })
            entry_vlan = user.entry_vlan_tag
            internal_vlan = user.internal_vlan_tag
            owning_identifier = student_name
            owner_group_id = None

        if backend_name != BACKEND_PROXMOX:
            # A cloud configures no VLANs, but the owner's tags are its
            # network-slot identities, so it cannot deploy without both.
            missing = [name for name, tag in (('entry_vlan_tag', entry_vlan),
                                              ('internal_vlan_tag', internal_vlan))
                       if tag is None]
            if missing:
                return jsonify({
                    'success': False,
                    'error': (f"Cloud deployment needs {' and '.join(missing)} "
                              f"assigned. Please contact an administrator.")})

        # Per-target deploy lock — prevents two concurrent deploys for the
        # same student/group from racing through the pre-flight check (both
        # would see "fresh" and both would kick off clones with the same
        # names).
        prefix = _vm_name_prefix(owning_identifier=owning_identifier,
                                 owner_group_id=owner_group_id)
        deploy_lock = _acquire_deploy_lock(prefix)
        if deploy_lock is None:
            return jsonify({
                'success': False,
                'error': (f"Another deploy for {owning_identifier!r} is "
                          f"already in progress. Please wait for it to complete.")
            }), 409

        try:
            if backend_name == BACKEND_PROXMOX:
                # An unreachable cluster fails the deploy here, as it always
                # has; a cloud deploy never needs this connection.
                get_proxmox_connection()

            # Pre-flight: detect an already-fully-deployed state (idempotent
            # retry — no-op and report success) or a partially-deployed
            # state (orphans from a prior failed deploy — destroy them, then
            # redeploy fresh). Compares by exact VM-name match.
            status, existing = _preflight_deployment_group(
                templates,
                owning_identifier=owning_identifier,
                owner_group_id=owner_group_id,
                backend_name=backend_name)

            if status == 'already_deployed':
                # The backend says all expected VMs exist — but the user can
                # only manage VMs the app knows about. Verify the DB has a
                # live VMRecord for each before declaring success; otherwise
                # a prior deploy that crashed between clone and DB-insert
                # would look "deployed" while being totally unmanageable
                # (no dashboard entry, no /destroy, no expiration timer).
                existing_vmids = {vm['vmid'] for vm in existing}
                db_query = VMRecord.query.filter(
                    VMRecord.vmid.in_(existing_vmids),
                    VMRecord.destroyed == False,  # noqa: E712
                )
                if owner_group_id is not None:
                    db_query = db_query.filter_by(owner_group_id=owner_group_id)
                else:
                    db_query = db_query.filter_by(
                        created_by_user_id=user_id, owner_group_id=None)
                tracked_vmids = {r.vmid for r in db_query.all()}
                untracked = existing_vmids - tracked_vmids

                if not untracked:
                    logger.info(
                        f"Deploy no-op: {deployment_group_name} for "
                        f"{owning_identifier!r} is already fully deployed "
                        f"({len(existing)} VMs present on {backend_name} and "
                        f"tracked in DB).")
                    return jsonify({
                        'success': True,
                        'already_deployed': True,
                        'message': (f"Deployment {deployment_group_name!r} is "
                                    f"already in place — no changes made."),
                        'deployment_group': deployment_group_name,
                        'vm_count': len(existing),
                        'vms': [{'vm_name': vm.get('name'), 'vm_id': vm['vmid'],
                                 'node': vm['node'], 'vm_type': vm['vm_type']}
                                for vm in existing],
                    })

                # DB is out of sync — the backend has VMs the app doesn't know
                # about (prior crash between clone-success and VMRecord
                # insert, manual Proxmox-side creation, etc). Demote to
                # 'partial' so the next block destroys them all and the
                # caller does a fresh deploy with proper DB records.
                logger.warning(
                    f"Pre-flight: {deployment_group_name} for "
                    f"{owning_identifier!r} has {len(existing)} {backend_name} "
                    f"VM(s) but only {len(tracked_vmids)} live VMRecord(s); "
                    f"untracked vmids: {sorted(untracked)}. Demoting to "
                    f"'partial' so we redeploy with proper DB records.")
                status = 'partial'

            if status == 'partial':
                logger.warning(
                    f"Pre-flight: {deployment_group_name} for "
                    f"{owning_identifier!r} is partial "
                    f"({len(existing)}/{len(templates)} VMs present) — "
                    f"destroying stragglers before redeploy.")
                cleaned = cleanup_vms([
                    {'vmid': vm['vmid'], 'node': vm['node'],
                     'vm_type': vm['vm_type'],
                     'vm_name': vm.get('name'),
                     'backend': backend_name} for vm in existing])
                failed = [vm for vm in existing if vm['vmid'] not in cleaned]
                _mark_cleaned_records_destroyed(cleaned)
                if failed:
                    failed_names = ', '.join(
                        vm.get('name') or str(vm['vmid']) for vm in failed)
                    return jsonify({
                        'success': False,
                        'error': (f"Pre-flight cleanup failed for "
                                  f"{len(failed)} VM(s): {failed_names}. "
                                  f"These VMs could not be destroyed "
                                  f"(likely locked or stuck). Manual "
                                  f"intervention required.")
                    }), 409

            vm_records = deploy_bundle(
                deployment_group_name=deployment_group_name,
                templates=templates,
                owning_identifier=owning_identifier,
                entry_vlan=entry_vlan,
                internal_vlan=internal_vlan,
                uptime_minutes=uptime_minutes,
                user_id=user_id,
                load_balancing_config=load_balancing_config,
                owner_group_id=owner_group_id,
                backend_name=backend_name,
            )
            result_vms = [{
                'vm_id': record.vmid,
                'vm_name': record.vm_name,
                'template_name': record.template_name.rsplit('/', 1)[-1],
                'vm_type': record.vm_type,
                'node': record.node,
                'memory': record.memory,
                'cores': record.cores,
                'storage': record.storage,
                'ip_address': 'Starting...',
                'expires_at': record.expires_at.isoformat(),
            } for record in vm_records]

            return jsonify({
                'success': True,
                'deployment_group': deployment_group_name,
                'vm_count': len(result_vms),
                'vms': result_vms,
                'output': f'Created {len(result_vms)} VM(s) successfully',
            })
        finally:
            deploy_lock.release()

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
