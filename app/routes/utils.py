"""
Shared utility functions for routes
Used by multiple route modules to prevent circular dependencies
"""

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

from ..models import db, User, StudentGroupMember
from ..proxmox_service import is_proxmox_not_found

logger = logging.getLogger(__name__)

# Cap the bytes we'll read out of any guest-agent file. Plenty for a token
# (~50 chars) or a flag UUID (~36 chars) and prevents a hostile root inside
# the box from spamming us via /koth/claim with a multi-megabyte file.
_GUEST_FILE_MAX_BYTES = 4096

# VLAN management constants
ENTRY_VLAN_MIN = 100
ENTRY_VLAN_MAX = 200
INTERNAL_VLAN_MIN = 200
INTERNAL_VLAN_MAX = 300

# Track config issue alerts to prevent spam (vmid -> timestamp)
_config_issue_alerts_sent = {}


# VM state as far as Proxmox is concerned. The distinction that matters is
# MISSING vs UNKNOWN: only MISSING may lead to deleting a database row.
VM_PRESENT = 'present'
VM_MISSING = 'missing'
VM_CONFIG_ERROR = 'config_error'
VM_UNKNOWN = 'unknown'

# Substrings Proxmox uses when a VM's config cannot be read. Note that a
# DELETED VM and a CORRUPTED config produce the same HTTP 500 and the same
# wording, which is exactly why this alone cannot decide `missing`.
_CONFIG_UNREADABLE_HINTS = ('does not exist', 'configuration file', 'no such file')

# --- orphan confirmation ----------------------------------------------------
# `cluster/resources` is NOT an independent witness. Verified on a live
# cluster: its vmid set is byte-identical to `/etc/pve/.vmlist`,
# which is the union of the `<vmid>.conf` files - the same artifact whose
# absence produced the config-read error we are classifying. So if a .conf
# disappears while the guest process keeps running, BOTH signals say "gone"
# and we would delete the row of a live VM.
#
# Time is the only witness left. A VM must be seen missing twice, at least
# ORPHAN_CONFIRM_SECONDS apart, before anything deletes its row. One bad
# observation - an inventory gap, a pmxcfs hiccup, a half-finished destroy -
# can then never destroy a record on its own. The cost is that a genuinely
# deleted VM lingers on the dashboard until the next render after the
# window, which is harmless.
#
# In memory on purpose: a restart simply means the next sighting starts the
# clock again, which errs toward keeping rows. That is the safe direction,
# and it avoids another schema migration.
_missing_since = {}
_missing_since_lock = threading.Lock()
ORPHAN_CONFIRM_SECONDS = 300.0


def note_vm_state(node, vmid, state):
    """Record this observation and report whether a delete is justified.

    Returns True only when the VM has read as `missing` continuously for
    at least ORPHAN_CONFIRM_SECONDS. Any other state clears the record.
    """
    key = (node, vmid)
    now = time.monotonic()
    with _missing_since_lock:
        if state != VM_MISSING:
            _missing_since.pop(key, None)
            return False
        first_seen = _missing_since.get(key)
        if first_seen is None:
            _missing_since[key] = now
            return False
        return (now - first_seen) >= ORPHAN_CONFIRM_SECONDS


def forget_vm_state(node, vmid):
    """Drop the confirmation record for a VM whose row is going away."""
    with _missing_since_lock:
        _missing_since.pop((node, vmid), None)


def _looks_unreadable(exc) -> bool:
    """True when the error says the VM's config could not be read at all.

    Everything else - timeouts, connection resets, 401/403, generic 5xx -
    is a failure to ask the question, not an answer to it.

    This deliberately does NOT search the message for "404". proxmoxer
    puts the request URL in a transport error, and that URL contains the
    vmid: a plain connection failure for vmid 404, 1404 or 4042 would
    then read as "the config is gone" and reach the delete decision.
    `status_code` already covers every real 404 from proxmoxer's
    ResourceException, so the substring added no coverage - only that
    hole. vmids walk a wide range here because rows are hard-deleted and
    `get_next_available_vmid` is max+1.
    """
    return (
        is_proxmox_not_found(exc)
        or any(hint in str(exc).lower() for hint in _CONFIG_UNREADABLE_HINTS)
    )


# Statuses cluster/resources reports for a VM whose config Proxmox can
# read. Anything else (or nothing) means we still have to ask per-VM.
_HEALTHY_CLUSTER_STATUSES = frozenset({'running', 'stopped', 'paused', 'suspended'})


def _verify_vm_exists(proxmox, node, vmid, vm_type, *, index=None):
    """Determine whether a VM still exists in Proxmox.

    Returns `(state, error_msg)` where state is one of:
        VM_PRESENT      — the VM is there.
        VM_MISSING      — the cluster inventory confirms no such vmid.
                          This is the ONLY state a caller may delete a
                          VMRecord on.
        VM_CONFIG_ERROR — the cluster still lists the vmid but its config
                          is unreadable. The VM is real and probably needs
                          `qm destroy <vmid> --purge`.
        VM_UNKNOWN      — we could not find out. Network blip, timeout,
                          permissions, an unclassified 5xx, or the
                          inventory call itself failing.

    Why the inventory lookup: `config.get()` on a VM that was deleted and
    on a VM whose config file is corrupt both raise HTTP 500 with
    "... does not exist". An earlier version answered `exists=False` for
    *every* non-404 error, including transient ones, and `/list` deleted
    the VMRecord on that basis. Because the expiry daemon only ever reads
    database rows, the VM then ran in Proxmox until somebody noticed by
    hand — a single API blip during a class refresh was enough. Absence
    is now something we confirm, never something we infer from a failure.

    Pass `index` (from `get_cluster_vm_index`) to answer the common case
    without a per-VM call: a caller checking many VMs fetches the
    inventory once instead of making two Proxmox round trips each. A VM
    the snapshot lists with a healthy status is PRESENT outright. Any
    other answer still goes through the per-VM read, so a stale snapshot
    can only cost an extra call, never a wrong verdict.
    """
    from ..proxmox_service import (
        get_cluster_vm_index, VM_INDEX_DECISION_MAX_AGE,
    )
    if index is not None:
        try:
            entry = index.get(int(vmid))
        except (TypeError, ValueError):
            entry = None
        if entry is not None and entry.get('status') in _HEALTHY_CLUSTER_STATUSES:
            return (VM_PRESENT, None)

    try:
        if vm_type == 'lxc':
            proxmox.nodes(node).lxc(vmid).config.get()
        else:
            proxmox.nodes(node).qemu(vmid).config.get()
        return (VM_PRESENT, None)
    except Exception as e:
        error_msg = str(e)
        if not _looks_unreadable(e):
            logger.warning("VM %s on %s: config read failed and the error is "
                           "not a missing-config error; treating as unknown: %s",
                           vmid, node, error_msg)
            return (VM_UNKNOWN, error_msg)

    # The config is unreadable. Ask the cluster whether the VM is actually
    # gone. This answer may delete a database row, so it has to be a fresh
    # one: no stale-snapshot fallback, and a tight max_age. The 1s window
    # still collapses a whole `/list` with several broken VMs into a
    # single cluster call.
    try:
        index = get_cluster_vm_index(
            proxmox, max_age=VM_INDEX_DECISION_MAX_AGE, allow_stale=False)
    except Exception as index_error:
        logger.warning("VM %s on %s: config unreadable (%s) and the cluster "
                       "inventory is unavailable (%s); treating as unknown",
                       vmid, node, error_msg, index_error)
        return (VM_UNKNOWN, error_msg)

    try:
        known = int(vmid) in index
    except (TypeError, ValueError):
        return (VM_UNKNOWN, error_msg)

    if known:
        return (VM_CONFIG_ERROR, error_msg)
    return (VM_MISSING, error_msg)


def _read_guest_file(proxmox, node, vmid, path) -> Optional[str]:
    """Read a small text file from a QEMU VM via the Proxmox guest agent.

    Used by the KOTH claim poller. Returns the file's text content (no
    trailing-newline trim — caller does that), or None on any failure.

    The Proxmox API returns `{'bytes-read': N, 'content': '<text>'}` where
    `content` is a Python `str` containing the file's UTF-8 text. We do
    NOT base64-decode it: I assumed base64 wrapping based on the QMP
    spec's `buf-b64` field, but the Proxmox REST layer decodes that for
    us before handing back the dict. Decoding again produced garbage and
    silently broke claim polling — verified empirically on Proxmox 8.x.

    Failure modes that all collapse to None:
      - guest agent unreachable (VM off, agent not installed, agent crash)
      - file does not exist on the box
      - permission denied / path is a directory
      - response missing/malformed `content` field
      - file content not a string

    File size is capped at `_GUEST_FILE_MAX_BYTES` characters to keep a
    hostile or misbehaving guest from feeding us megabytes per tick.
    """
    if not path:
        return None
    try:
        # Proxmox `file-read` is a GET that takes `file` as a query param,
        # mirroring how `network-get-interfaces` is called elsewhere in
        # this module via `.get()`. proxmoxer translates kwargs to params.
        result = proxmox.nodes(node).qemu(vmid).agent('file-read').get(file=path)
    except Exception as e:
        # Most common: guest agent down, file missing, or QEMU error 5xx.
        # Log at debug so we don't spam logs every tick when a VM is off.
        logger.debug("guest file-read failed for vm %s:%s path=%s: %s",
                     node, vmid, path, e)
        return None
    content = result.get('content') if isinstance(result, dict) else None
    if not isinstance(content, str) or not content:
        return None
    if len(content) > _GUEST_FILE_MAX_BYTES:
        logger.debug(
            "guest file-read content truncated for vm %s:%s path=%s "
            "(got %d chars, cap %d)", node, vmid, path,
            len(content), _GUEST_FILE_MAX_BYTES)
        content = content[:_GUEST_FILE_MAX_BYTES]
    return content


# (node, vmid) -> (monotonic timestamp, ips). A guest-agent call is one
# HTTPS round trip per VM; an admin `/list` over 175 VMs made 175 of them
# on every page refresh. IPs change about as often as a VM reboots, so a
# short TTL costs nothing and removes almost all of that.
_IP_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_IP_CACHE_LOCK = threading.Lock()
_IP_CACHE_TTL = 45.0
# A VM that has not reported an IP yet is usually mid-boot, so re-ask
# sooner — otherwise a student watching a fresh deploy stares at 'N/A'
# for the full TTL.
_IP_CACHE_NEGATIVE_TTL = 10.0
# Bounds the dict across a term's worth of VMs. Eviction is oldest-first.
_IP_CACHE_MAX = 2000


def _ip_cache_get(key):
    now = time.monotonic()
    with _IP_CACHE_LOCK:
        entry = _IP_CACHE.get(key)
        if entry is None:
            return None
        stamped, ips = entry
        ttl = _IP_CACHE_NEGATIVE_TTL if ips == ['N/A'] else _IP_CACHE_TTL
        if now - stamped >= ttl:
            _IP_CACHE.pop(key, None)
            return None
        _IP_CACHE.move_to_end(key)
        return list(ips)


def _ip_cache_put(key, ips):
    with _IP_CACHE_LOCK:
        _IP_CACHE[key] = (time.monotonic(), list(ips))
        _IP_CACHE.move_to_end(key)
        while len(_IP_CACHE) > _IP_CACHE_MAX:
            _IP_CACHE.popitem(last=False)


def invalidate_vm_ip_cache(node, vmid):
    """Forget a VM's cached IPs. Call after destroying or recreating it so
    a recycled vmid cannot show the previous tenant's address."""
    with _IP_CACHE_LOCK:
        _IP_CACHE.pop((node, int(vmid)), None)


def _fetch_vm_ip_addresses(proxmox, node, vmid, vm_type):
    """One uncached lookup. Returns a list of 10.x addresses, or ['N/A']."""
    ip_addresses = []
    if vm_type == 'lxc':
        try:
            interfaces = proxmox.nodes(node).lxc(vmid).interfaces.get()
            for iface in interfaces or []:
                if iface.get('name', '') == 'lo':
                    continue
                inet = iface.get('inet', '')
                if inet:
                    # CIDR, e.g. "10.0.1.50/24"
                    ip_addr = inet.split('/')[0]
                    if ip_addr.startswith('10.'):
                        ip_addresses.append(ip_addr)
        except Exception as e:
            logger.debug("LXC interfaces unavailable for VM %s: %s", vmid, e)
        return ip_addresses or ['N/A']

    try:
        interfaces = proxmox.nodes(node).qemu(vmid).agent(
            'network-get-interfaces').get()
    except Exception as agent_error:
        # Normal while a VM is booting or if the guest agent isn't installed.
        logger.debug("guest agent unavailable for VM %s: %s", vmid, agent_error)
        return ['N/A']

    for iface in (interfaces or {}).get('result', []):
        if iface.get('name', '') == 'lo':
            continue
        for ip_info in iface.get('ip-addresses', []):
            ip_addr = ip_info.get('ip-address', '')
            if ip_info.get('ip-address-type') == 'ipv4' and ip_addr.startswith('10.'):
                ip_addresses.append(ip_addr)
    return ip_addresses or ['N/A']


def _get_vm_ip_addresses(proxmox, node, vmid, vm_type):
    """IP addresses of a VM, cached for `_IP_CACHE_TTL` seconds.

    Returns a list of 10.x addresses, or the sentinel `['N/A']` when none
    could be determined. Never raises: a failure here must not take down
    the page that is merely trying to display an address.
    """
    try:
        key = (node, int(vmid))
    except (TypeError, ValueError):
        return _fetch_vm_ip_addresses(proxmox, node, vmid, vm_type)

    cached = _ip_cache_get(key)
    if cached is not None:
        return cached
    try:
        ips = _fetch_vm_ip_addresses(proxmox, node, vmid, vm_type)
    except Exception:
        logger.exception("error getting IP addresses for VM %s", vmid)
        return ['N/A']
    _ip_cache_put(key, ips)
    return ips


_IPV4_RE = re.compile(r'^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$')
_IP_DISPLAY_SENTINELS = frozenset({'N/A', '-', 'CONFIG ERROR', 'Starting...'})


def mask_ip_address(ip_str):
    """Mask the host octet of each IPv4 in a comma-separated list.

    Example: "10.101.4.56, 10.101.4.57" -> "10.101.4.xxx, 10.101.4.xxx".
    Sentinel values (N/A, -, CONFIG ERROR, Starting...) and unparseable
    parts pass through unchanged so callers can apply this blindly.
    """
    if not ip_str or ip_str in _IP_DISPLAY_SENTINELS:
        return ip_str
    parts = [p.strip() for p in ip_str.split(',')]
    masked = []
    for p in parts:
        m = _IPV4_RE.match(p)
        masked.append(f"{m.group(1)}.xxx" if m else p)
    return ', '.join(masked)


def _format_time_remaining(time_delta):
    """
    Format a time delta into a human-readable string

    Args:
        time_delta (timedelta): Time remaining

    Returns:
        str: Formatted time string
    """
    total_seconds = int(time_delta.total_seconds())

    if total_seconds < 0:
        return "EXPIRED"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def user_can_access_vm(user, vm):
    """
    Return True if `user` may view/modify/destroy/extend `vm`.

    Rules:
        - Admins: always True.
        - Group-owned VM (vm.owner_group_id is not None): True iff user is a
          member of that group.
        - Individual VM: True iff user is the creator.
    """
    if user is None or vm is None:
        return False
    if getattr(user, 'is_admin', False):
        return True
    if vm.owner_group_id is not None:
        return db.session.query(StudentGroupMember.id).filter_by(
            group_id=vm.owner_group_id, user_id=user.id,
        ).first() is not None
    if vm.koth_game_id is not None:
        from ..models import KothTeam
        membership = StudentGroupMember.query.filter_by(user_id=user.id).first()
        if not membership:
            return False
        return KothTeam.query.filter_by(
            game_id=vm.koth_game_id, student_group_id=membership.group_id
        ).first() is not None
    return vm.created_by_user_id == user.id
