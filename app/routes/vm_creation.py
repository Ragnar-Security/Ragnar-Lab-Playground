"""
VM Creation routes module - REFACTORED FOR CLARITY
Handles VM creation from deployment groups with parallel processing
"""

from contextlib import contextmanager
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
    select_best_node,
    release_node_reservation,
    renew_node_reservation,
    release_vmid_reservation,
    renew_vmid_reservation,
    wait_for_proxmox_task,
    find_vms_by_name_prefix,
    confirm_proxmox_absence,
    inspect_vm_guest,
    reset_cluster_vm_index,
    vm_lifecycle_lock,
    VM_GUEST_ABSENT,
    VM_GUEST_MATCH,
)
from ..auth import login_required, get_authenticated_user
from ..utils import sanitize_name
from ..template_manager import load_template_config, get_deployment_group_by_name, get_group_mode
from ..models import StudentGroup, StudentGroupMember

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


def cleanup_vms(vm_list):
    """Stop and destroy VMs, returning only VMIDs confirmed absent."""
    destroyed: set = set()
    if not vm_list:
        return destroyed

    logger.info(f"Cleaning up {len(vm_list)} VMs...")
    proxmox = get_proxmox_connection()

    for vm_info in vm_list:
        vmid = vm_info.get('vmid')
        node = vm_info.get('node')
        vm_type = vm_info.get('vm_type', 'qemu')
        vm_name = vm_info.get('vm_name') or vm_info.get('name')

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

            if vm_name is None:
                logger.error(
                    "cleanup_vms: refusing to mutate VM %s without an "
                    "expected guest name", vmid)
                continue

            identity = inspect_vm_guest(
                proxmox, vmid, node=node, vm_name=vm_name, vm_type=vm_type)
            if identity == VM_GUEST_ABSENT:
                logger.info(f"VM {vmid} is absent from fresh cluster inventory")
                destroyed.add(vmid)
                continue
            if identity != VM_GUEST_MATCH:
                logger.error(
                    "cleanup_vms: refusing to mutate unverified VM %s", vmid)
                continue

            endpoint = (proxmox.nodes(node).lxc(vmid) if vm_type == 'lxc'
                        else proxmox.nodes(node).qemu(vmid))

            try:
                stop_upid = endpoint.status.stop.post()
                stop_result = wait_for_proxmox_task(
                    proxmox, node, stop_upid, timeout=60, poll_interval=1)
                if not stop_result['success']:
                    logger.debug(
                        f"Stop task for VM {vmid} returned non-OK: "
                        f"{stop_result.get('error', stop_result['status'])}")
            except Exception as stop_err:
                if confirm_proxmox_absence(proxmox, vmid, stop_err):
                    logger.info(
                        f"VM {vmid} confirmed absent after stop request")
                    destroyed.add(vmid)
                    continue
                # The VM may merely be stopped or have a corrupt config. The
                # destroy endpoint is still worth trying, but the row remains
                # unless deletion succeeds or fresh inventory confirms absence.
                logger.debug(f"Stop request for VM {vmid} raised: {stop_err}")

            try:
                delete_upid = endpoint.delete()
            except Exception as del_err:
                if confirm_proxmox_absence(proxmox, vmid, del_err):
                    logger.info(
                        f"VM {vmid} confirmed absent after destroy request")
                    destroyed.add(vmid)
                else:
                    logger.error(
                        f"Error submitting destroy for VM {vmid}: {del_err}")
                continue

            try:
                delete_result = wait_for_proxmox_task(
                    proxmox, node, delete_upid, timeout=120, poll_interval=1)
            except TimeoutError as timeout_error:
                logger.error(
                    f"Destroy task for VM {vmid} timed out: {timeout_error}")
                continue
            if delete_result['success']:
                destroyed.add(vmid)
                reset_cluster_vm_index()
                logger.info(f"Cleaned up VM {vmid}")
            else:
                logger.error(
                    f"Destroy task for VM {vmid} failed: "
                    f"{delete_result.get('error', delete_result['status'])}")

    return destroyed


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


def _cleanup_with_sweep(created_vms, *, templates, owning_identifier,
                        owner_group_id=None, koth_game_id=None,
                        attempted_vms=None):
    """Clean every exact-name match and return only confirmed deletions.

    ``attempted_vms`` receives the complete cleanup payload when supplied so
    callers can durably track any guest whose deletion remains uncertain.
    """
    prefix = _vm_name_prefix(owning_identifier=owning_identifier,
                             owner_group_id=owner_group_id,
                             koth_game_id=koth_game_id)
    expected_names = _expected_vm_names(
        templates,
        owning_identifier=owning_identifier,
        owner_group_id=owner_group_id,
        koth_game_id=koth_game_id,
    )
    payload = [
        vm for vm in created_vms
        if vm.get('vmid') is not None
        and vm.get('node')
        and vm.get('vm_type') in ('qemu', 'lxc')
    ]
    known = {(vm['vmid'], vm['node']) for vm in payload}
    try:
        for vm in find_vms_by_name_prefix(prefix):
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
    except Exception:
        logger.exception(
            "_cleanup_with_sweep: name-prefix sweep failed for %r "
            "(falling back to known VM results only)", prefix)
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
                                owner_group_id=None, koth_game_id=None):
    """Inspect Proxmox for VMs the new deploy would shadow. Returns one of:

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
    existing = [vm for vm in find_vms_by_name_prefix(prefix)
                if vm.get('name') in expected_names]
    if not existing:
        return ('fresh', [])
    if {vm.get('name') for vm in existing} == expected_names:
        return ('already_deployed', existing)
    return ('partial', existing)


def create_single_vm_task(vm_index, template_config, assigned_vmid, student_name,
                          load_balancing_config, user_entry_vlan, user_internal_vlan,
                          expiration_time, node_semaphores, owner_group_id=None,
                          koth_game_id=None, flask_app=None,
                          vmid_reservation_id=None):
    """Create one VM while holding its admission reservation.

    The caller owns a successful task's reservation and releases it only
    after the corresponding VMRecord commit. Failed task reservations are
    returned too, so bundle cleanup can finish before capacity is reusable.
    """
    config = {}
    target_node = None
    vm_name = None
    claimed_vmid = None
    reservation_id = uuid4().hex
    try:
        config = extract_template_config(template_config)
        if koth_game_id is not None:
            vm_name = build_koth_vm_name(koth_game_id, config['name'])
        elif owner_group_id is not None:
            vm_name = build_group_vm_name(student_name, config['name'])
        else:
            vm_name = build_vm_name(student_name, config['name'])

        # Get Proxmox connection
        proxmox = get_proxmox_connection()

        available_nodes = (
            list(config['template_locations'].keys())
            if config['template_locations'] else None)

        # ThreadPoolExecutor workers do not inherit Flask's app context.
        # Supplying it here gives each selection a fresh scoped DB session,
        # so a just-committed VMRecord is visible before its reservation is
        # released.
        if flask_app is None:
            raise RuntimeError(
                'VM creation requires an app context for admission control')
        with flask_app.app_context():
            target_node = select_best_node(
                proxmox,
                required_memory_mb=config['memory'],
                required_cores=config['cores'],
                load_balancing_config=load_balancing_config,
                available_nodes=available_nodes,
                reservation_id=reservation_id,
            )

        if not target_node:
            raise Exception('No suitable nodes available')

        # Get source VMID for cloning
        source_vmid = config['template_locations'].get(target_node) or config['source_vmid']
        source_node = target_node

        # Determine VLAN tags
        internal_vlan = user_internal_vlan
        entry_vlan = user_entry_vlan if config['is_entry_point'] else None

        # Resolve storage device for the target node
        if config['storage_locations'] and target_node in config['storage_locations']:
            config['storage_device'] = config['storage_locations'][target_node]
            logger.info(f"Using node-specific storage '{config['storage_device']}' for {target_node}")

        def create_qemu_for_assigned_vmid():
            nonlocal claimed_vmid
            claimed_vmid = assigned_vmid
            try:
                return create_qemu_vm(
                    proxmox, target_node, source_node, assigned_vmid,
                    vm_name, source_vmid, config, expiration_time,
                    internal_vlan, entry_vlan)
            except Exception as create_error:
                if 'already exists' in str(create_error).lower():
                    claimed_vmid = None
                raise

        def clone_qemu_with_collision_retry():
            nonlocal assigned_vmid
            try:
                return create_qemu_for_assigned_vmid()
            except Exception as clone_error:
                if ('already exists' not in str(clone_error).lower()
                        or not vmid_reservation_id):
                    raise
                with flask_app.app_context():
                    assigned_vmid = get_next_available_vmid(
                        proxmox,
                        reservation_id=vmid_reservation_id,
                    )
                logger.warning(
                    "Clone VMID collision; retrying once with freshly "
                    "reserved VMID %s", assigned_vmid)
                return create_qemu_for_assigned_vmid()

        # Keep queued work alive while it waits behind the per-node clone
        # limit. Without heartbeats, a busy classroom can wait longer than
        # the leak-safety TTL and become invisible to admission control.
        if config['type'] == 'lxc':
            if not renew_node_reservation(reservation_id):
                raise RuntimeError('Admission reservation expired before create')
            if (vmid_reservation_id
                    and not renew_vmid_reservation(vmid_reservation_id)):
                raise RuntimeError('VMID reservation expired before create')
            claimed_vmid = assigned_vmid
            try:
                create_lxc_container(
                    proxmox, target_node, assigned_vmid, vm_name, config,
                    expiration_time, internal_vlan, entry_vlan)
            except Exception as create_error:
                if 'already exists' in str(create_error).lower():
                    claimed_vmid = None
                raise
        elif config['type'] == 'qemu':
            if not source_vmid:
                raise Exception('QEMU template requires source_vmid')

            node_semaphore = node_semaphores.get(target_node)
            if node_semaphore:
                acquired = False
                while not acquired:
                    acquired = node_semaphore.acquire(
                        timeout=RESERVATION_HEARTBEAT_SECONDS)
                    if not renew_node_reservation(reservation_id):
                        if acquired:
                            node_semaphore.release()
                        raise RuntimeError(
                            'Admission reservation expired while waiting '
                            'for clone capacity')
                    if (vmid_reservation_id
                            and not renew_vmid_reservation(
                                vmid_reservation_id)):
                        if acquired:
                            node_semaphore.release()
                        raise RuntimeError(
                            'VMID reservation expired while waiting '
                            'for clone capacity')
                try:
                    logger.info(
                        f"[VM {assigned_vmid}] Acquired clone semaphore "
                        f"for node {target_node}")
                    target_node = clone_qemu_with_collision_retry()
                finally:
                    node_semaphore.release()
                    logger.info(
                        f"[VM {assigned_vmid}] Released clone semaphore "
                        f"for node {target_node}")
            else:
                if not renew_node_reservation(reservation_id):
                    raise RuntimeError(
                        'Admission reservation expired before clone')
                if (vmid_reservation_id
                        and not renew_vmid_reservation(
                            vmid_reservation_id)):
                    raise RuntimeError(
                        'VMID reservation expired before clone')
                logger.warning(
                    f"[VM {assigned_vmid}] No semaphore found for node "
                    f"{target_node}; proceeding without clone limit")
                target_node = clone_qemu_with_collision_retry()
        else:
            raise Exception(f'Unsupported VM type: {config["type"]}')

        if not renew_node_reservation(reservation_id):
            raise RuntimeError('Admission reservation expired before VM start')
        # Once the clone exists, Proxmox inventory itself protects the VMID.
        # Renewal here is best effort; treating a just-expired lease as clone
        # failure would destroy a successfully-created guest.
        if vmid_reservation_id:
            renew_vmid_reservation(vmid_reservation_id)
        start_vm(proxmox, target_node, assigned_vmid, config['type'])
        logger.info(
            f"VM {assigned_vmid} ({vm_name}) created and started on "
            f"{target_node}")

        return {
            'success': True,
            'vm_index': vm_index,
            'vmid': assigned_vmid,
            'vm_name': vm_name,
            'vm_display_name': config['name'],
            'vm_type': config['type'],
            'node': target_node,
            'memory': config['memory'],
            'cores': config['cores'],
            'storage': config['storage'],
            'is_entry_point': config['is_entry_point'],
            '_reservation_id': reservation_id,
            '_vmid_reservation_id': vmid_reservation_id,
        }
    except Exception as e:
        logger.error(f"Error creating VM {config.get('name', vm_index)}: {e}")
        result = {
            'success': False,
            'error': str(e),
            'template_name': config.get('name', f'vm{vm_index}'),
            'vm_index': vm_index,
            '_reservation_id': reservation_id,
            '_vmid_reservation_id': vmid_reservation_id,
        }
        if claimed_vmid is not None and target_node and config and vm_name:
            result.update({
                'vmid': claimed_vmid,
                'vm_name': vm_name,
                'vm_display_name': config['name'],
                'vm_type': config['type'],
                'node': target_node,
                'memory': config['memory'],
                'cores': config['cores'],
                'storage': config['storage'],
                'is_entry_point': config['is_entry_point'],
            })
        return result


def _release_vm_reservations(vm_results) -> None:
    for result in vm_results:
        release_node_reservation(result.get('_reservation_id'))


@contextmanager
def _heartbeat_deployment_reservations(vm_results, vmid_reservation_id):
    """Renew create leases until cleanup and survivor tracking finish."""
    reservation_ids = {
        result.get('_reservation_id')
        for result in vm_results
        if result.get('_reservation_id')
    }
    stopped = threading.Event()

    def heartbeat():
        while not stopped.is_set():
            if vmid_reservation_id:
                renew_vmid_reservation(vmid_reservation_id)
            for reservation_id in reservation_ids:
                renew_node_reservation(reservation_id)
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
    """Commit promptly-expiring records for every uncertain cleanup result."""
    survivors = []
    seen_vmids = set()
    for vm_info in vm_results:
        vmid = vm_info.get('vmid')
        if (vmid is None or vmid in destroyed_vmids or vmid in seen_vmids
                or not vm_info.get('node')
                or vm_info.get('vm_type') not in ('qemu', 'lxc')):
            continue
        seen_vmids.add(vmid)
        survivors.append(vm_info)

    if not survivors:
        return []

    retry_at = datetime.now() - timedelta(seconds=1)
    try:
        for vm_info in survivors:
            record = VMRecord.query.filter_by(vmid=vm_info['vmid']).first()
            marker_group = (
                f"failed-deploy-leftover/{deployment_group_name}")
            if record is None:
                record = log_vm_to_database(
                    vm_info,
                    marker_group,
                    owning_identifier,
                    retry_at,
                    user_id,
                    owner_group_id,
                    koth_game_id,
                )
            else:
                record.vm_name = vm_info['vm_name']
                record.vm_type = vm_info['vm_type']
                record.node = vm_info['node']
                record.template_name = (
                    f"{marker_group}/"
                    f"{vm_info.get('vm_display_name', 'unknown')}")
                record.student_name = owning_identifier
                record.memory = vm_info.get('memory')
                record.cores = vm_info.get('cores')
                record.storage = vm_info.get('storage')
                record.expires_at = retry_at
                record.created_by_user_id = user_id
                record.is_entry_point = vm_info.get(
                    'is_entry_point', False)
                record.owner_group_id = owner_group_id
                record.koth_game_id = koth_game_id
                record.destroyed = False
                record.destroyed_at = None
                record.deletion_retry_count = 0
                record.deletion_last_attempt_at = None
                record.deletion_alert_sent = False
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Could not commit cleanup tracking for surviving VMIDs %s; "
            "their admission reservations remain held",
            sorted(seen_vmids),
        )
        raise
    return survivors


def _finalize_failed_deployment(
        vm_results, *, vmid_reservation_id, templates,
        deployment_group_name, owning_identifier, user_id,
        owner_group_id=None, koth_game_id=None):
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
    _release_vm_reservations(vm_results)
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
                    and not renew_node_reservation(reservation_id)):
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
                  koth_game_id=None):
    """Parallel-clone a bundle of templates and persist VMRecord rows.

    This is the same orchestration the /create handler uses (load balancer,
    per-node ZFS clone semaphores, ThreadPoolExecutor across templates,
    cleanup on partial failure). KOTH calls this so its VMs go through the
    exact same code path as a regular group-mode lab.

    Parameters:
        deployment_group_name: stored in VMRecord.template_name as
            "<deployment_group_name>/<template_display_name>".
        templates: list of template dicts from templates.yaml.
        owning_identifier: stored in VMRecord.student_name. For student
            deploys, this is the student login_id; for group deploys, the
            group slug; for KOTH, the game id stringified or 'koth'.
        entry_vlan / internal_vlan: per-deployment VLAN tags.
        uptime_minutes: initial expiration window.
        user_id: created_by_user_id on the VMRecord (the admin/student
            who clicked deploy).
        load_balancing_config: deployment_group's load_balancing dict.
        owner_group_id: set for group-mode deploys (drives the group-naming
            path in create_single_vm_task and the FK on VMRecord).
        koth_game_id: set for KOTH deploys. Mutually exclusive with
            owner_group_id from a naming standpoint (koth wins).

    Returns: list[VMRecord] in the same order the templates were submitted
    (insertion order preserved by collecting via a dict, not as_completed).

    Raises: DeployFailed on any template clone failure (already cleaned up).
    """
    proxmox = get_proxmox_connection()
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
            f"deploy_bundle: {deployment_group_name} ({len(templates)} VMs), "
            f"vmids {base_vmid}-{base_vmid + len(templates) - 1}, "
            f"owning={owning_identifier!r} group={owner_group_id} "
            f"koth={koth_game_id}"
        )

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
                        vmid_reservation_id,
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
                        '_reservation_id': None,
                        '_vmid_reservation_id': vmid_reservation_id,
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
        try:
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
    """Stage one VMRecord; the bundle orchestrator owns the transaction."""
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

    # Validate user VLANs
    valid, error = validate_user_vlans(user)
    if not valid:
        return jsonify({'success': False, 'error': error})

    logger.info(f"User {user.login_id}: entry_vlan={user.entry_vlan_tag}, internal_vlan={user.internal_vlan_tag}")

    try:
        # Load deployment group configuration
        config = load_template_config()
        deployment_group = get_deployment_group_by_name(config, deployment_group_name)

        if not deployment_group:
            return jsonify({'success': False, 'error': f'Deployment group {deployment_group_name} not found'})

        templates = deployment_group.get('templates', [])
        if not templates:
            return jsonify({'success': False, 'error': 'No templates configured'})

        uptime_minutes = deployment_group.get('uptime_minutes', 4320)
        expiration_time = datetime.now() + timedelta(minutes=uptime_minutes)
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
            # Get Proxmox connection
            proxmox = get_proxmox_connection()

            # Pre-flight: detect an already-fully-deployed state (idempotent
            # retry — no-op and report success) or a partially-deployed
            # state (orphans from a prior failed deploy — destroy them, then
            # redeploy fresh). Compares by exact VM-name match.
            status, existing = _preflight_deployment_group(
                templates,
                owning_identifier=owning_identifier,
                owner_group_id=owner_group_id)

            if status == 'already_deployed':
                # Proxmox says all expected VMs exist — but the user can
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
                        f"({len(existing)} VMs present in Proxmox and "
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

                # DB is out of sync — Proxmox has VMs the app doesn't know
                # about (prior crash between clone-success and VMRecord
                # insert, manual Proxmox-side creation, etc). Demote to
                # 'partial' so the next block destroys them all and the
                # caller does a fresh deploy with proper DB records.
                logger.warning(
                    f"Pre-flight: {deployment_group_name} for "
                    f"{owning_identifier!r} has {len(existing)} Proxmox "
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
                     'vm_name': vm.get('name')} for vm in existing])
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
