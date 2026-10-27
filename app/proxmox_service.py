"""
Proxmox API service module
Handles all interactions with the Proxmox API
"""

from contextlib import contextmanager
import threading
import time
import logging
from typing import Optional, Dict, List, Any, Tuple
from proxmoxer import ProxmoxAPI
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from . import coordination
from .config import (
    PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD, PROXMOX_TOKEN_ID,
    PROXMOX_TOKEN_SECRET, proxmox_tls_verify,
)
from .models import NodeMemoryReservation, VMIDReservation, VMRecord, db

logger = logging.getLogger(__name__)


# Active creates renew this lease every minute. Fifteen minutes is long enough
# to survive one five-minute clone plus transient scheduler stalls, while a
# crashed path eventually heals without a process restart.
RESERVATION_TTL_SECONDS = 15 * 60

# Node-memory and VMID reservations live in the application database (see
# app/coordination.py), so every worker admits and allocates against the
# same ledger. VMID reservations bridge the gap between taking a cluster/DB
# inventory and committing VMRecord rows.
_NODE_RESERVATIONS = NodeMemoryReservation.__table__
_VMID_RESERVATIONS = VMIDReservation.__table__

# Longer than any lifecycle body (a stop plus a destroy, each bounded by
# wait_for_proxmox_task's 300 s), so only a stuck holder makes a waiter fail.
VM_LIFECYCLE_LOCK_TIMEOUT_SECONDS = 30 * 60

# Every mutation of a guest is serialized by VMID. Greenlets of one worker
# queue on a process lock (monkey-patched threading locks coordinate
# greenlets as well as helper threads); on MySQL a named server lock then
# excludes other workers. Entries are reference-counted so the table does
# not grow forever as the allocation high-water mark advances.
_vm_lifecycle_locks: Dict[int, Dict[str, Any]] = {}
_vm_lifecycle_locks_guard = threading.Lock()


@contextmanager
def _process_vm_lifecycle_lock(key: int):
    with _vm_lifecycle_locks_guard:
        entry = _vm_lifecycle_locks.get(key)
        if entry is None:
            entry = {'lock': threading.Lock(), 'users': 0}
            _vm_lifecycle_locks[key] = entry
        entry['users'] += 1
    try:
        with entry['lock']:
            yield
    finally:
        with _vm_lifecycle_locks_guard:
            entry['users'] -= 1
            if entry['users'] == 0 and _vm_lifecycle_locks.get(key) is entry:
                _vm_lifecycle_locks.pop(key)


def _mysql_engine_or_none():
    from flask import has_app_context
    if not has_app_context():
        return None
    engine = db.engine
    return engine if engine.dialect.name == 'mysql' else None


@contextmanager
def vm_lifecycle_lock(vmid: int):
    """Serialize allocation/destruction decisions for one VMID.

    On MySQL this holds GET_LOCK('vm-lifecycle-<vmid>') across workers.
    SQLite (tests, scripts) and callers without an app context get the
    process lock only.
    """
    key = int(vmid)
    with _process_vm_lifecycle_lock(key):
        engine = _mysql_engine_or_none()
        if engine is None:
            yield
            return
        with coordination.mysql_named_lock(
                engine, f'vm-lifecycle-{key}',
                VM_LIFECYCLE_LOCK_TIMEOUT_SECONDS):
            yield


def _clear_vmid_reservations() -> None:
    """Clear VMID reservations and the high-water mark for deterministic tests."""
    coordination.reset_for_tests(
        [_VMID_RESERVATIONS], state_rows=[coordination.VMID_ALLOCATOR])


def release_vmid_reservation(reservation_id: Optional[str]) -> bool:
    """Release VMIDs after their VMRecords commit or cleanup completes."""
    if not reservation_id:
        return False
    return coordination.release_lease(_VMID_RESERVATIONS, reservation_id)


def renew_vmid_reservation(reservation_id: Optional[str]) -> bool:
    """Keep an active VMID allocation alive for another TTL window."""
    if not reservation_id:
        return False
    return coordination.renew_lease(
        _VMID_RESERVATIONS, reservation_id, RESERVATION_TTL_SECONDS)


def _purge_expired_vmid_reservations(conn, now: float) -> None:
    expired = conn.execute(
        select(_VMID_RESERVATIONS.c.reservation_id,
               _VMID_RESERVATIONS.c.vmid)
        .where(_VMID_RESERVATIONS.c.expires_at <= now)).all()
    if not expired:
        return
    leaked: Dict[str, List[int]] = {}
    for reservation_id, vmid in expired:
        leaked.setdefault(reservation_id, []).append(vmid)
    for reservation_id, vmids in leaked.items():
        logger.warning("Expired leaked VMID reservation %s (%s)",
                       reservation_id, sorted(vmids))
    conn.execute(delete(_VMID_RESERVATIONS)
                 .where(_VMID_RESERVATIONS.c.expires_at <= now))


def is_vmid_reserved(vmid: int) -> bool:
    """Return whether an unexpired create in any worker owns ``vmid``.

    Fails closed: if the ledger cannot be read, the orphan reaper must keep
    the guest.
    """
    try:
        normalized_vmid = int(vmid)
    except (TypeError, ValueError):
        return False
    now = coordination.lease_clock()
    try:
        with coordination.current_engine().connect() as conn:
            row = conn.execute(
                select(_VMID_RESERVATIONS.c.vmid)
                .where(_VMID_RESERVATIONS.c.vmid == normalized_vmid,
                       _VMID_RESERVATIONS.c.expires_at > now)).first()
    except SQLAlchemyError:
        logger.warning("Could not read VMID reservations; treating VM %s as "
                       "reserved", normalized_vmid, exc_info=True)
        return True
    return row is not None


def _clear_node_reservations() -> None:
    """Clear admission reservations (used to isolate deterministic tests)."""
    coordination.reset_for_tests([_NODE_RESERVATIONS])


def release_node_reservation(reservation_id: Optional[str]) -> bool:
    """Release a pending admission reservation after success or failure."""
    if not reservation_id:
        return False
    return coordination.release_lease(_NODE_RESERVATIONS, reservation_id)


def renew_node_reservation(reservation_id: Optional[str]) -> bool:
    """Keep an active creation reservation alive for another TTL window."""
    if not reservation_id:
        return False
    return coordination.renew_lease(
        _NODE_RESERVATIONS, reservation_id, RESERVATION_TTL_SECONDS)


def _reservation_totals_locked(conn, now: float) -> Dict[str, int]:
    table = _NODE_RESERVATIONS
    expired = conn.execute(
        select(table.c.reservation_id, table.c.node, table.c.memory_bytes)
        .where(table.c.expires_at <= now)).all()
    for reservation_id, node, memory_bytes in expired:
        logger.warning(
            "Expired leaked admission reservation %s on %s (%.2fGB)",
            reservation_id, node, memory_bytes / (1024 ** 3))
    if expired:
        conn.execute(delete(table).where(table.c.expires_at <= now))

    rows = conn.execute(
        select(table.c.node, func.sum(table.c.memory_bytes))
        .group_by(table.c.node)).all()
    return {node: int(total) for node, total in rows}


def _apply_memory_admission(
        nodes: List[Dict[str, Any]],
        vm_records: List[Any],
        guest_resources: List[Dict[str, Any]],
        reserved_by_node: Dict[str, int]) -> List[Dict[str, Any]]:
    """Annotate node snapshots with app-owned commitment and reservations."""
    guest_memory: Dict[int, int] = {}
    for guest in guest_resources or []:
        try:
            guest_memory[int(guest['vmid'])] = max(0, int(guest.get('mem', 0)))
        except (KeyError, TypeError, ValueError):
            continue

    committed_by_node: Dict[str, int] = {}
    for record in vm_records:
        if getattr(record, 'destroyed', False):
            continue
        node = getattr(record, 'node', None)
        try:
            vmid = int(record.vmid)
            configured = max(0, int(record.memory or 0)) * 1024 * 1024
        except (AttributeError, TypeError, ValueError):
            continue
        unrealized = max(0, configured - guest_memory.get(vmid, 0))
        committed_by_node[node] = committed_by_node.get(node, 0) + unrealized

    annotated = []
    for original in nodes:
        node = dict(original)
        node_name = node['node']
        available = node.get('memory_available', node.get('memory_free', 0))
        committed = committed_by_node.get(node_name, 0)
        reserved = reserved_by_node.get(node_name, 0)
        node['memory_available'] = available
        node['memory_committed'] = committed
        node['memory_reserved'] = reserved
        node['memory_effective_available'] = available - committed - reserved
        annotated.append(node)
    return annotated


def _load_live_vm_records(conn) -> List[Any]:
    # Read inside the admission transaction, which began after the admission
    # lock: every VMRecord committed before another worker released its
    # reservation is visible, so that memory is counted once, not zero times.
    # The process-local store (no app context) has no VMRecords.
    if not coordination.is_application_database(conn.engine):
        return []
    records = VMRecord.__table__
    return conn.execute(
        select(records.c.vmid, records.c.node, records.c.memory,
               records.c.destroyed)
        .where(records.c.destroyed == False)).all()  # noqa: E712


def is_proxmox_not_found(exc: Exception) -> bool:
    """Return whether an error hints that the requested VM may be absent.

    This is never sufficient evidence for retiring a VMRecord: corrupt
    configs produce the same ``does not exist`` response while the guest
    remains in cluster inventory. Call ``confirm_proxmox_absence`` before
    treating this hint as terminal.
    """
    return (
        getattr(exc, 'status_code', None) == 404
        or 'does not exist' in str(exc).lower()
    )


def get_proxmox_connection() -> ProxmoxAPI:
    """
    Create and return a Proxmox API connection

    Uses token authentication if credentials are provided, otherwise falls back to password auth.
    Sets timeout to 300 seconds (5 minutes) to handle long-running operations like VM creation.

    Returns:
        ProxmoxAPI: Connected Proxmox API instance

    Raises:
        Exception: If connection fails
    """
    verify = proxmox_tls_verify()
    try:
        if PROXMOX_TOKEN_ID and PROXMOX_TOKEN_SECRET:
            proxmox = ProxmoxAPI(
                PROXMOX_HOST,
                user=PROXMOX_USER,
                token_name=PROXMOX_TOKEN_ID,
                token_value=PROXMOX_TOKEN_SECRET,
                verify_ssl=verify,
                timeout=300  # 5 minutes timeout for long operations
            )
        else:
            proxmox = ProxmoxAPI(
                PROXMOX_HOST,
                user=PROXMOX_USER,
                password=PROXMOX_PASSWORD,
                verify_ssl=verify,
                timeout=300  # 5 minutes timeout for long operations
            )
        return proxmox
    except Exception as e:
        raise Exception(f"Failed to connect to Proxmox: {str(e)}")


# --- cluster VM inventory ---------------------------------------------------
# One `cluster/resources` call answers "does this vmid exist anywhere in the
# cluster?" for every VM at once. It is the only authority we have for that
# question: a per-VM `config.get()` cannot tell a deleted VM apart from a
# corrupted config or a network blip, because Proxmox answers all three with
# a 5xx. Deleting a database row on the strength of a failed config read is
# what used to orphan live VMs — see `_verify_vm_exists`.
_vm_index_lock = threading.Lock()
_vm_index_cache: Optional[Dict[int, Dict[str, Any]]] = None
_vm_index_fetched_at = 0.0
VM_INDEX_MAX_AGE = 8.0
# Freshness a caller must demand before acting on "this vmid is absent".
# Short enough that the answer is current; long enough that one `/list`
# render with several unreadable VMs still costs one cluster call, not one
# per VM.
VM_INDEX_DECISION_MAX_AGE = 1.0
# How old a fallback snapshot may be before it stops being served at all.
# Serving an ancient snapshot during an outage makes every VM read as
# present, so the page shows a healthy class while nothing is reachable.
VM_INDEX_STALE_CEILING = 120.0


def get_cluster_vm_index(proxmox: ProxmoxAPI, *,
                         max_age: float = VM_INDEX_MAX_AGE,
                         allow_stale: bool = True
                         ) -> Dict[int, Dict[str, Any]]:
    """`{vmid: {vmid, name, node, type, status}}` for every VM in the cluster.

    Cached, because a single `/list` render asks about dozens of VMs.

    Callers deciding whether a VM is GONE must pass
    `max_age=VM_INDEX_DECISION_MAX_AGE, allow_stale=False`. With
    `allow_stale=True` an API failure falls back to the last good
    snapshot, which is fine for display but catastrophic for a delete
    decision: during a Proxmox outage an old snapshot would "confirm"
    that a live VM is absent, which is the exact data loss this guards
    against. `allow_stale=False` raises instead, and the caller degrades
    to "unknown".
    """
    global _vm_index_cache, _vm_index_fetched_at
    now = time.monotonic()
    with _vm_index_lock:
        if (_vm_index_cache is not None
                and now - _vm_index_fetched_at < max_age):
            return _vm_index_cache
    try:
        rows = proxmox.cluster.resources.get(type='vm')
    except Exception as e:
        if not allow_stale:
            raise
        with _vm_index_lock:
            age = now - _vm_index_fetched_at
            if _vm_index_cache is not None and age < VM_INDEX_STALE_CEILING:
                logger.warning("cluster/resources failed (%s); using a %.1fs-old "
                               "snapshot", e, age)
                return _vm_index_cache
            if _vm_index_cache is not None:
                # Past the ceiling the snapshot stops being a useful answer
                # and becomes a lie that hides the outage: every VM would
                # short-circuit to PRESENT and the dashboard would report a
                # healthy class while nothing is reachable. Note that
                # _vm_index_fetched_at is NOT refreshed on this path, so the
                # age keeps growing for as long as the outage lasts.
                logger.error("cluster/resources failed (%s) and the cached "
                             "snapshot is %.0fs old, past the %.0fs ceiling; "
                             "reporting the outage instead", e, age,
                             VM_INDEX_STALE_CEILING)
        raise
    index: Dict[int, Dict[str, Any]] = {}
    for row in rows or []:
        try:
            vmid = int(row['vmid'])
        except (KeyError, TypeError, ValueError):
            continue
        index[vmid] = {
            'vmid': vmid,
            'name': row.get('name'),
            'node': row.get('node'),
            'type': row.get('type'),
            'status': row.get('status'),
        }
    with _vm_index_lock:
        _vm_index_cache = index
        _vm_index_fetched_at = time.monotonic()
    return index


VM_GUEST_MATCH = 'match'
VM_GUEST_ABSENT = 'absent'
VM_GUEST_MISMATCH = 'mismatch'
VM_GUEST_UNKNOWN = 'unknown'


def inspect_vm_guest(proxmox: ProxmoxAPI, vmid: int, *, node: str,
                     vm_name: str, vm_type: str) -> str:
    """Read fresh inventory and verify the VMID still names the intended guest.

    A replacement at the same VMID must never inherit a stale destroy request.
    ``unknown`` fails closed when inventory cannot be read.
    """
    try:
        index = get_cluster_vm_index(
            proxmox, max_age=0, allow_stale=False)
    except Exception as error:
        logger.warning(
            "Cannot verify identity for VM %s before mutation: %s",
            vmid, error)
        return VM_GUEST_UNKNOWN

    try:
        entry = index.get(int(vmid))
    except (TypeError, ValueError):
        return VM_GUEST_UNKNOWN
    if entry is None:
        return VM_GUEST_ABSENT
    if (
        entry.get('name') != vm_name
        or entry.get('node') != node
        or entry.get('type') != vm_type
    ):
        logger.warning(
            "Refusing to mutate VMID %s: expected %s/%s/%s, found %s/%s/%s",
            vmid, node, vm_type, vm_name,
            entry.get('node'), entry.get('type'), entry.get('name'))
        return VM_GUEST_MISMATCH
    return VM_GUEST_MATCH


def confirm_proxmox_absence(proxmox: ProxmoxAPI, vmid: int,
                            error: Optional[Exception] = None) -> bool:
    """Confirm an absence-like error against a fresh, non-stale inventory."""
    if error is not None and not is_proxmox_not_found(error):
        return False
    try:
        index = get_cluster_vm_index(
            proxmox, max_age=0, allow_stale=False)
        return int(vmid) not in index
    except Exception as inventory_error:
        logger.warning(
            "Could not confirm absence of VM %s after %s: %s",
            vmid, error, inventory_error)
        return False


def reset_cluster_vm_index() -> None:
    """Forget the cached inventory.

    Call after creating or destroying VMs so the next lookup reflects the
    change instead of an up-to-8s-old snapshot. Matters most on the
    destroy path: Proxmox reuses vmids, and a stale entry would make a
    brand-new VM look present on the strength of its predecessor.
    """
    global _vm_index_cache, _vm_index_fetched_at
    with _vm_index_lock:
        _vm_index_cache = None
        _vm_index_fetched_at = 0.0


def wait_for_proxmox_task(proxmox: ProxmoxAPI, node: str, upid: str,
                          timeout: int = 300, poll_interval: int = 3) -> Dict[str, Any]:
    """
    Monitor a Proxmox task until completion

    Args:
        proxmox: ProxmoxAPI connection
        node: Node where task is running
        upid: Task UPID returned from async operations
        timeout: Max seconds to wait (default 5 minutes)
        poll_interval: Seconds between status checks

    Returns:
        dict: {'success': bool, 'status': str, 'error': str}

    Raises:
        TimeoutError: If task exceeds timeout
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            task_status = proxmox.nodes(node).tasks(upid).status.get()
            status = task_status.get('status')
            exitstatus = task_status.get('exitstatus')

            if status == 'stopped':
                if exitstatus == 'OK':
                    return {'success': True, 'status': status}
                else:
                    error = task_status.get('errors', 'Unknown error')
                    return {'success': False, 'status': status, 'error': error}

            time.sleep(poll_interval)

        except Exception as e:
            logger.warning(f"Warning: Failed to check task status: {e}")
            time.sleep(poll_interval)

    raise TimeoutError(f"Task {upid} did not complete within {timeout}s")


def get_vm_by_name(proxmox, student_name):
    """
    Find a VM by student name across all nodes
    
    Args:
        proxmox (ProxmoxAPI): Proxmox API connection
        student_name (str): Student name to search for
        
    Returns:
        dict: VM info with keys (type, vmid, node, name) or None if not found
    """
    try:
        for node in proxmox.nodes.get():
            node_name = node['node']
            
            # Check LXC containers
            for container in proxmox.nodes(node_name).lxc.get():
                if student_name in container['name'].lower():
                    return {
                        'type': 'lxc',
                        'vmid': container['vmid'],
                        'node': node_name,
                        'name': container['name']
                    }
            
            # Check QEMU VMs
            for qemu in proxmox.nodes(node_name).qemu.get():
                if student_name in qemu['name'].lower():
                    return {
                        'type': 'qemu',
                        'vmid': qemu['vmid'],
                        'node': node_name,
                        'name': qemu['name']
                    }
    except Exception as e:
        logger.error(f"Error searching for VM: {str(e)}")
    
    return None


def get_vm_details(proxmox, vmid, node):
    """
    Get detailed status information about a VM
    
    Args:
        proxmox (ProxmoxAPI): Proxmox API connection
        vmid (int): VM ID
        node (str): Node name
        
    Returns:
        dict: VM status information or None if not found
    """
    try:
        # Try QEMU first
        vm = proxmox.nodes(node).qemu(vmid).status.current.get()
        return vm
    except:
        try:
            # Fall back to LXC
            vm = proxmox.nodes(node).lxc(vmid).status.current.get()
            return vm
        except:
            return None


def get_next_available_vmid(
        proxmox: ProxmoxAPI, *,
        count: int = 1,
        reservation_id: str) -> int:
    """Reserve and return the first VMID in a contiguous free block.

    The block comes from one transaction holding the allocator row, which
    also stores the high-water mark, so two workers or greenlets can never
    receive overlapping blocks. The mark, not the inventory snapshot,
    serializes the app's own allocations; the Proxmox read therefore runs
    before the lock and never holds it across a network call.

    ``reservation_id`` may be reused to extend a deployment's reservation
    when a defensive duplicate-VMID retry needs a fresh ID. Callers release
    it only after every VMRecord commits or cleanup has completed.
    """
    if count < 1:
        raise ValueError('VMID reservation count must be at least 1')
    if not reservation_id:
        raise ValueError('VMID reservation identifier is required')

    inventory_max = 0
    for row in proxmox.cluster.resources.get(type='vm') or []:
        try:
            inventory_max = max(inventory_max, int(row['vmid']))
        except (KeyError, TypeError, ValueError):
            continue

    engine = coordination.current_engine()
    table = _VMID_RESERVATIONS

    def allocate(conn) -> int:
        now = coordination.lease_clock()
        _purge_expired_vmid_reservations(conn, now)
        max_vmid = max(
            inventory_max,
            coordination.read_state(conn, coordination.VMID_ALLOCATOR))
        # Destroyed rows remain unique-indexed, so the database is part of
        # the allocation namespace even after the Proxmox guest is gone.
        if coordination.is_application_database(engine):
            db_max = conn.execute(
                select(func.max(VMRecord.__table__.c.vmid))).scalar()
            if db_max is not None:
                max_vmid = max(max_vmid, int(db_max))
        reserved_max = conn.execute(select(func.max(table.c.vmid))).scalar()
        if reserved_max is not None:
            max_vmid = max(max_vmid, int(reserved_max))

        base_vmid = max_vmid + 1
        expires_at = now + RESERVATION_TTL_SECONDS
        conn.execute(insert(table), [
            {'vmid': vmid, 'reservation_id': reservation_id,
             'expires_at': expires_at}
            for vmid in range(base_vmid, base_vmid + count)
        ])
        # A reused identifier keeps one expiry for all of its VMIDs.
        conn.execute(
            update(table)
            .where(table.c.reservation_id == reservation_id)
            .values(expires_at=expires_at))
        coordination.write_state(
            conn, coordination.VMID_ALLOCATOR, base_vmid + count - 1)
        return base_vmid

    base_vmid = coordination.run_locked(
        engine, coordination.VMID_ALLOCATOR, allocate)
    coordination.remember_lease_engine(table, reservation_id, engine)
    return base_vmid


def find_vms_by_name_prefix(prefix):
    """Return [{vmid, node, vm_type, name}, ...] for every VM in Proxmox
    whose name starts with `prefix`.

    Used to recover stranded KOTH VMs whose VMRecord rows never made it to
    the DB (e.g. the worker was SIGTERM'd mid-deploy, so our in-process
    cleanup never ran). Searches both QEMU and LXC across all cluster nodes.
    """
    proxmox = get_proxmox_connection()
    found = []
    try:
        nodes = proxmox.nodes.get()
    except Exception:
        logger.exception("find_vms_by_name_prefix: failed to enumerate nodes")
        return found
    for node in nodes:
        node_name = node['node']
        try:
            for vm in proxmox.nodes(node_name).qemu.get():
                if (vm.get('name') or '').startswith(prefix):
                    found.append({'vmid': vm['vmid'], 'node': node_name,
                                  'vm_type': 'qemu', 'name': vm.get('name')})
        except Exception:
            logger.exception(f"find_vms_by_name_prefix: qemu list failed on {node_name}")
        try:
            for lxc in proxmox.nodes(node_name).lxc.get():
                if (lxc.get('name') or '').startswith(prefix):
                    found.append({'vmid': lxc['vmid'], 'node': node_name,
                                  'vm_type': 'lxc', 'name': lxc.get('name')})
        except Exception:
            logger.exception(f"find_vms_by_name_prefix: lxc list failed on {node_name}")
    return found


def get_available_node(proxmox: ProxmoxAPI) -> Optional[str]:
    """
    Get the first available Proxmox node

    Args:
        proxmox: Proxmox API connection

    Returns:
        str: Node name or None if no nodes available
    """
    nodes = proxmox.nodes.get()
    return nodes[0]['node'] if nodes else None


def get_node_resources(proxmox: ProxmoxAPI, node_name: str) -> Dict[str, Any]:
    """
    Get resource usage statistics for a specific node

    Args:
        proxmox: Proxmox API connection
        node_name: Name of the node

    Returns:
        dict: Resource usage with keys:
            - cpu_usage (float): CPU usage percentage (0-100)
            - memory_usage (float): Memory usage percentage (0-100)
            - memory_available (int): Reclaimable memory in bytes
            - memory_free (int): Legacy alias for memory_available
            - memory_linux_free (int): Linux MemFree in bytes
            - memory_total (int): Total memory in bytes
            - cpu_count (int): Number of CPU cores
            - online (bool): Whether node is online
    """
    try:
        status = proxmox.nodes(node_name).status.get()
        
        # Calculate usage percentages
        cpu_usage = status.get('cpu', 0) * 100  # Convert to percentage
        memory = status.get('memory', {})
        memory_total = memory.get('total', 1)
        memory_used = memory.get('used', 0)
        # Linux MemFree excludes cache and ZFS ARC. Admission uses
        # MemAvailable, while retaining the old memory_free key as an alias
        # because the admin JSON endpoint already exposes that name.
        memory_linux_free = memory.get('free', 0)
        # A missing or null 'available' falls back to total - used; a real 0
        # stays 0. Clamp bad data into [0, total] so a corrupt report can
        # neither go negative nor claim more memory than the node has.
        memory_available = memory.get('available')
        if memory_available is None:
            memory_available = memory_total - memory_used
        memory_available = max(0, min(memory_available, memory_total))
        memory_usage = (memory_used / memory_total * 100) if memory_total > 0 else 0
        
        # Node is online if we can get its status and it has valid uptime
        is_online = status.get('uptime', 0) > 0
        
        return {
            'node': node_name,
            'cpu_usage': round(cpu_usage, 2),
            'memory_usage': round(memory_usage, 2),
            'memory_available': memory_available,
            'memory_free': memory_available,
            'memory_linux_free': memory_linux_free,
            'memory_total': memory_total,
            'cpu_count': status.get('cpuinfo', {}).get('cpus', 1),
            'online': is_online
        }
    except Exception as e:
        logger.error(f"Error getting node resources for {node_name}: {str(e)}")
        return {
            'node': node_name,
            'cpu_usage': 100.0,
            'memory_usage': 100.0,
            'memory_available': 0,
            'memory_free': 0,
            'memory_linux_free': 0,
            'memory_total': 1,
            'cpu_count': 1,
            'online': False
        }


def get_all_nodes_resources(proxmox: ProxmoxAPI) -> List[Dict[str, Any]]:
    """
    Get resource usage for all nodes in the cluster

    Args:
        proxmox: Proxmox API connection

    Returns:
        list: List of dictionaries containing resource info for each node
    """
    nodes_resources = []
    
    try:
        nodes = proxmox.nodes.get()
        for node in nodes:
            node_name = node['node']
            resources = get_node_resources(proxmox, node_name)
            nodes_resources.append(resources)
    except Exception as e:
        logger.error(f"Error getting cluster resources: {str(e)}")
    
    return nodes_resources


def _filter_suitable_nodes(all_nodes: List[Dict[str, Any]],
                          required_memory_mb: int,
                          required_cores: int,
                          cpu_threshold: float,
                          memory_threshold: float) -> List[Dict[str, Any]]:
    """
    Filter nodes by resource thresholds and availability.

    Args:
        all_nodes: All available nodes with resource data
        required_memory_mb: Required memory in MB
        required_cores: Required CPU cores
        cpu_threshold: Maximum CPU usage percentage
        memory_threshold: Maximum memory usage percentage

    Returns:
        list: Nodes that meet all requirements
    """
    suitable_nodes = []

    for node in all_nodes:
        # Must be online
        if not node['online']:
            logger.info(f"Skipping {node['node']}: offline")
            continue

        # Must be below CPU threshold
        if node['cpu_usage'] > cpu_threshold:
            logger.info(f"Skipping {node['node']}: CPU usage {node['cpu_usage']}% exceeds threshold {cpu_threshold}%")
            continue

        # Must be below memory threshold
        if node['memory_usage'] > memory_threshold:
            logger.info(f"Skipping {node['node']}: Memory usage {node['memory_usage']}% exceeds threshold {memory_threshold}%")
            continue

        # Preserve the existing 10% headroom, but apply it to effective
        # availability after app-owned ramp-up commitments and reservations.
        required_memory_bytes = max(0, required_memory_mb) * 1024 * 1024 * 1.1
        effective = node.get(
            'memory_effective_available',
            node.get('memory_available', node.get('memory_free', 0)))
        if effective < required_memory_bytes:
            logger.info(
                "Skipping %s: insufficient effective memory "
                "(%.2fGB available, %.2fGB committed, %.2fGB reserved, "
                "%.2fGB effective; need %.2fGB including buffer)",
                node['node'],
                node.get('memory_available', 0) / (1024 ** 3),
                node.get('memory_committed', 0) / (1024 ** 3),
                node.get('memory_reserved', 0) / (1024 ** 3),
                effective / (1024 ** 3),
                required_memory_bytes / (1024 ** 3))
            continue

        # Must have enough CPU cores
        if node['cpu_count'] < required_cores:
            logger.info(f"Skipping {node['node']}: Insufficient CPU cores ({node['cpu_count']} cores, need {required_cores})")
            continue

        suitable_nodes.append(node)

    return suitable_nodes


def _score_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Score nodes by resources left after commitments and reservations.

    Higher score indicates better resource availability.
    Weight factors: CPU (30%), Memory % (30%), Effective Memory GB (40%)

    Args:
        nodes: Nodes to score (modifies in-place)

    Returns:
        list: Nodes with 'score' field added, sorted by score (descending)
    """
    for node in nodes:
        cpu_score = 100 - node['cpu_usage']
        memory_score = 100 - node['memory_usage']
        effective_memory_gb = node.get(
            'memory_effective_available',
            node.get('memory_available', node.get('memory_free', 0)),
        ) / (1024 ** 3)

        node['score'] = (
            (cpu_score * 0.3)
            + (memory_score * 0.3)
            + (effective_memory_gb * 10 * 0.4)
        )

    # Sort by score (highest first)
    nodes.sort(key=lambda x: x['score'], reverse=True)
    return nodes


def _select_node_weighted_random(scored_nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Select node using weighted random distribution.

    Nodes with higher scores have higher probability of selection,
    but lower-scored nodes can still be selected to distribute load.

    Args:
        scored_nodes: Nodes with 'score' field, sorted by score

    Returns:
        dict: Selected node
    """
    import random

    if len(scored_nodes) == 1:
        return scored_nodes[0]

    total_score = sum(node['score'] for node in scored_nodes)

    if total_score > 0:
        # Weighted random selection
        rand = random.uniform(0, total_score)
        cumulative = 0

        for node in scored_nodes:
            cumulative += node['score']
            if rand <= cumulative:
                return node

    # Fallback: return first (highest scored) node
    return scored_nodes[0]


def select_best_node(proxmox: ProxmoxAPI,
                     required_memory_mb: int = 2048,
                     required_cores: int = 2,
                     load_balancing_config: Optional[Dict[str, Any]] = None,
                     cached_resources: Optional[List[Dict[str, Any]]] = None,
                     available_nodes: Optional[List[str]] = None,
                     reservation_id: Optional[str] = None) -> Optional[str]:
    """Select a suitable node and optionally reserve its requested memory.

    Every selection takes one cluster VM inventory snapshot. App-owned live
    VMRecords bridge the gap between configured RAM and the memory each guest
    has touched so far. Pending reservations bridge the earlier gap between
    selection and VMRecord commit.

    Passing ``reservation_id`` makes the capacity check and ledger insert one
    lock-protected operation. The caller must release that identifier after
    the VMRecord commit or after every failure/cleanup path.
    """
    from .config import (
        CPU_THRESHOLD, MEMORY_THRESHOLD, ENABLE_LOAD_BALANCING, FIXED_NODE)

    if load_balancing_config:
        lb_enabled = load_balancing_config.get(
            'enabled', ENABLE_LOAD_BALANCING)
        fixed_node = load_balancing_config.get('fixed_node', FIXED_NODE)
        cpu_threshold = load_balancing_config.get(
            'cpu_threshold', CPU_THRESHOLD)
        memory_threshold = load_balancing_config.get(
            'memory_threshold', MEMORY_THRESHOLD)
    else:
        lb_enabled = ENABLE_LOAD_BALANCING
        fixed_node = FIXED_NODE
        cpu_threshold = CPU_THRESHOLD
        memory_threshold = MEMORY_THRESHOLD

    logger.info(
        "Selecting node for VM (memory=%dMB, cores=%d, cpu threshold=%.1f%%, "
        "memory threshold=%.1f%%)",
        required_memory_mb, required_cores, cpu_threshold, memory_threshold)

    try:
        guest_resources = list(
            proxmox.cluster.resources.get(type='vm') or [])
    except Exception as error:
        # Treat unknown current usage as zero. That charges every live record
        # its full configured RAM and therefore fails closed during an outage.
        logger.warning(
            "Unable to read cluster VM memory usage; charging full app-owned "
            "commitment: %s", error)
        guest_resources = []
    if cached_resources is not None:
        all_nodes = cached_resources
        logger.info("Using cached node resources (%d nodes)", len(all_nodes))
    else:
        all_nodes = get_all_nodes_resources(proxmox)
        logger.info("Fetched fresh node resources (%d nodes)", len(all_nodes))

    if not all_nodes:
        logger.info("No nodes available")
        return None

    allowed_nodes = set(available_nodes) if available_nodes else None
    if not lb_enabled and fixed_node:
        allowed_nodes = (
            {fixed_node}
            if allowed_nodes is None
            else allowed_nodes.intersection({fixed_node})
        )
        logger.info("Load balancing disabled; restricting admission to %s",
                    fixed_node)

    if allowed_nodes is not None:
        all_nodes = [
            node for node in all_nodes if node['node'] in allowed_nodes]
        if not all_nodes:
            logger.info("No eligible node has the required template")
            return None


    # A negative size must never become a negative reservation, which would
    # hand capacity to every later caller.
    required_memory_bytes = max(0, required_memory_mb) * 1024 * 1024
    engine = coordination.current_engine()
    reservations = _NODE_RESERVATIONS

    def admit(conn):
        now = coordination.lease_clock()
        if reservation_id and conn.execute(
                select(reservations.c.reservation_id)
                .where(reservations.c.reservation_id == reservation_id)
        ).first():
            raise ValueError(
                f"Duplicate node reservation identifier: {reservation_id}")

        reserved_by_node = _reservation_totals_locked(conn, now)
        nodes_with_admission = _apply_memory_admission(
            all_nodes,
            _load_live_vm_records(conn),
            guest_resources,
            reserved_by_node,
        )

        for node in nodes_with_admission:
            logger.info(
                "Node %s memory: free=%.2fGB available=%.2fGB "
                "committed=%.2fGB reserved=%.2fGB effective=%.2fGB",
                node['node'],
                node.get('memory_linux_free', 0) / (1024 ** 3),
                node['memory_available'] / (1024 ** 3),
                node['memory_committed'] / (1024 ** 3),
                node['memory_reserved'] / (1024 ** 3),
                node['memory_effective_available'] / (1024 ** 3),
            )

        suitable_nodes = _filter_suitable_nodes(
            nodes_with_admission, required_memory_mb, required_cores,
            cpu_threshold, memory_threshold)
        if not suitable_nodes:
            return None, []

        scored_nodes = _score_nodes(suitable_nodes)
        best_node = (
            _select_node_weighted_random(scored_nodes)
            if lb_enabled else scored_nodes[0]
        )

        if reservation_id:
            conn.execute(insert(reservations).values(
                reservation_id=reservation_id,
                node=best_node['node'],
                memory_bytes=required_memory_bytes,
                expires_at=now + RESERVATION_TTL_SECONDS,
            ))
        return best_node, scored_nodes

    best_node, scored_nodes = coordination.run_locked(
        engine, coordination.NODE_ADMISSION, admit)
    if best_node is None:
        logger.info("No suitable nodes found")
        return None
    if reservation_id:
        coordination.remember_lease_engine(
            reservations, reservation_id, engine)

    logger.info(
        "Selected node %s (CPU %.1f%%, memory %.1f%%, score %.2f%s)",
        best_node['node'], best_node['cpu_usage'], best_node['memory_usage'],
        best_node['score'],
        f", reservation={reservation_id}" if reservation_id else "")
    other_nodes = [
        (node['node'], round(node['score'], 2))
        for node in scored_nodes if node['node'] != best_node['node']
    ]
    if other_nodes:
        logger.info("Other candidates: %s", other_nodes)

    return best_node['node']
