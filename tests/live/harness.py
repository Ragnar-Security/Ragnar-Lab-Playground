"""Safety harness for the opt-in live Proxmox contract run.

The contract run clones, boots, stops and deletes real guests. Everything
destructive goes through ``ContractRun``, which enforces five rules:

* it only ever allocates VMIDs inside ``LIVE_VMID_RANGE``, and refuses to
  start while any guest in that range lacks the ``lab-contract-test-`` prefix;
* on a cluster the portal deploys to, the range must not reach above the
  portal's VMID high-water mark, which lives in the portal database and
  never goes down;
* each guest gets a unique ``lab-contract-test-<random>`` name and the app's
  ``Expires:`` description, 30 minutes out;
* cleanup runs on exit even when the body raised;
* it starts, stops or destroys only a guest it created in this run, and
  only while fresh inventory still shows the exact name and node it gave
  that guest.

The guards are unit-tested against a fake API in
``tests/test_live_contract_guards.py``.
"""

import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Mapping, Optional

from sqlalchemy import create_engine, func, inspect, select

from app.coordination import VMID_ALLOCATOR
from app.models import CoordinationState, VMRecord
from app.proxmox_service import (
    confirm_proxmox_absence,
    reset_cluster_vm_index,
    wait_for_proxmox_task,
)
from app.routes.vm_creation import create_qemu_vm


NAME_PREFIX = 'lab-contract-test-'
EXPIRY = timedelta(minutes=30)
# Proxmox accepts VMIDs 100..999999999.
MIN_VMID = 100
MAX_VMID = 999_999_999

REQUIRED_ENV = ('PROXMOX_HOST', 'LIVE_TEMPLATE_VMID', 'LIVE_NODE',
                'LIVE_VMID_RANGE')


class UnsafeLiveRun(Exception):
    """A guard refused an action that could touch a guest this run does not own."""


class LiveCleanupError(Exception):
    """A guest this run created could not be removed."""


def parse_vmid_range(text: str) -> range:
    """Parse ``LOW-HIGH`` (inclusive) into a range of VMIDs."""
    match = re.fullmatch(r'\s*(\d+)\s*-\s*(\d+)\s*', text or '')
    if not match:
        raise ValueError(f'LIVE_VMID_RANGE must look like 99000-99099, got {text!r}')
    low, high = int(match.group(1)), int(match.group(2))
    if not MIN_VMID <= low <= high <= MAX_VMID:
        raise ValueError(
            f'LIVE_VMID_RANGE {text!r} must satisfy '
            f'{MIN_VMID} <= low <= high <= {MAX_VMID}')
    return range(low, high + 1)


def live_skip_reason(environ: Mapping[str, str]) -> Optional[str]:
    """Why the live run must be skipped, or None when it is fully configured."""
    if environ.get('LIVE_PROXMOX') != '1':
        return 'live Proxmox contract run is opt-in: set LIVE_PROXMOX=1'
    missing = [name for name in REQUIRED_ENV if not environ.get(name, '').strip()]
    has_token = (environ.get('PROXMOX_TOKEN_ID', '').strip()
                 and environ.get('PROXMOX_TOKEN_SECRET', '').strip())
    if not has_token and not environ.get('PROXMOX_PASSWORD', '').strip():
        missing.append('credentials (PROXMOX_TOKEN_ID + PROXMOX_TOKEN_SECRET, '
                       'or PROXMOX_PASSWORD)')
    if missing:
        return 'live Proxmox contract run is missing: ' + ', '.join(missing)
    return None


def read_portal_vmid_mark(database_url: str) -> int:
    """The highest VMID the portal has allocated: its stored high-water mark
    or its highest VMRecord, whichever is larger. Only reads."""
    state, records = CoordinationState.__table__, VMRecord.__table__
    engine = create_engine(database_url)
    try:
        existing = set(inspect(engine).get_table_names())
        if not {state.name, records.name} & existing:
            raise ValueError(
                'LIVE_PORTAL_DATABASE_URL has no portal tables '
                f'({state.name}, {records.name})')
        mark = 0
        with engine.connect() as conn:
            if state.name in existing:
                stored = conn.execute(
                    select(state.c.value)
                    .where(state.c.name == VMID_ALLOCATOR)).scalar()
                mark = max(mark, int(stored or 0))
            if records.name in existing:
                highest = conn.execute(select(func.max(records.c.vmid))).scalar()
                mark = max(mark, int(highest or 0))
        return mark
    finally:
        engine.dispose()


def _per_node(text: str, nodes: List[str], *, name: str,
              required: bool) -> Dict[str, str]:
    """Read ``value`` (every node) or ``node:value,node:value``."""
    entries = [part.strip() for part in (text or '').split(',') if part.strip()]
    if not entries:
        values: Dict[str, str] = {}
    elif all(':' not in entry for entry in entries):
        if len(entries) != 1:
            raise ValueError(f'{name} takes one value or node:value pairs, got {text!r}')
        values = {node: entries[0] for node in nodes}
    else:
        values = {}
        for entry in entries:
            node, sep, value = (piece.strip() for piece in entry.partition(':'))
            if not sep or not node or not value:
                raise ValueError(f'{name} entry {entry!r} is not node:value')
            if node not in nodes:
                raise ValueError(f'{name} names {node!r}, which is not in LIVE_NODE')
            values[node] = value
    if required:
        absent = [node for node in nodes if node not in values]
        if absent:
            raise ValueError(f'{name} has no value for node(s): {", ".join(absent)}')
    return values


@dataclass
class LiveConfig:
    nodes: List[str]
    templates: Dict[str, int]
    storage: Dict[str, str]
    vmid_range: range
    portal_database_url: Optional[str] = None
    separate_cluster: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> 'LiveConfig':
        nodes = [n.strip() for n in environ.get('LIVE_NODE', '').split(',') if n.strip()]
        if not nodes:
            raise ValueError('LIVE_NODE names no node')
        templates = {
            node: int(vmid) for node, vmid in _per_node(
                environ.get('LIVE_TEMPLATE_VMID', ''), nodes,
                name='LIVE_TEMPLATE_VMID', required=True).items()}
        storage = _per_node(environ.get('LIVE_CLONE_STORAGE', ''), nodes,
                            name='LIVE_CLONE_STORAGE', required=False)
        portal_database_url = environ.get('LIVE_PORTAL_DATABASE_URL', '').strip() or None
        separate_cluster = environ.get('LIVE_SEPARATE_CLUSTER', '').strip() == '1'
        if portal_database_url and separate_cluster:
            raise ValueError(
                'set LIVE_PORTAL_DATABASE_URL (cluster shared with the portal) '
                'or LIVE_SEPARATE_CLUSTER=1 (cluster it never deploys to), '
                'not both')
        return cls(nodes, templates, storage,
                   parse_vmid_range(environ.get('LIVE_VMID_RANGE', '')),
                   portal_database_url, separate_cluster)

    def template_for(self, node: str) -> int:
        return self.templates[node]

    def storage_for(self, node: str) -> Optional[str]:
        return self.storage.get(node)


@dataclass(frozen=True)
class Guest:
    vmid: int
    node: str
    name: str


def _truthy(value) -> bool:
    return value not in (None, False, 0, '0', '', 'false', 'False')


def manual_cleanup_commands(guest: Guest) -> str:
    vmid, node = guest.vmid, guest.node
    return (
        f'  confirm first:  qm config {vmid} | grep "^name: {guest.name}$"   (on {node})\n'
        f'  on {node}:  qm stop {vmid}; qm destroy {vmid} --purge\n'
        f'  via API:   pvesh create /nodes/{node}/qemu/{vmid}/status/stop; '
        f'pvesh delete /nodes/{node}/qemu/{vmid} --purge 1')


class ContractRun:
    """Owns every guest the contract run creates; use as a context manager."""

    def __init__(self, proxmox, vmid_range: range, *,
                 portal_vmid_mark: Optional[int] = None,
                 separate_cluster: bool = False,
                 now: Callable[[], datetime] = datetime.now):
        self.proxmox = proxmox
        self.vmid_range = vmid_range
        self.portal_vmid_mark = portal_vmid_mark
        self.separate_cluster = separate_cluster
        self.now = now
        self._owned: Dict[int, Guest] = {}

    def __enter__(self) -> 'ContractRun':
        self.preflight()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        failures = self.cleanup()
        if failures and exc_type is None:
            raise LiveCleanupError('\n\n'.join(failures))
        return False

    def owned_vmids(self) -> List[int]:
        return sorted(self._owned)

    # --- guards ------------------------------------------------------------

    def _inventory(self) -> Dict[int, dict]:
        index = {}
        for row in self.proxmox.cluster.resources.get(type='vm') or []:
            try:
                index[int(row['vmid'])] = row
            except (KeyError, TypeError, ValueError):
                continue
        return index

    def _require_clean_range(self, inventory: Dict[int, dict]) -> None:
        foreign = [
            f"{vmid} ({row.get('name')!r} on {row.get('node')})"
            for vmid, row in sorted(inventory.items())
            if vmid in self.vmid_range
            and not str(row.get('name') or '').startswith(NAME_PREFIX)
        ]
        if foreign:
            raise UnsafeLiveRun(
                'refusing to run: LIVE_VMID_RANGE '
                f'{self.vmid_range[0]}-{self.vmid_range[-1]} holds guests not '
                f'named {NAME_PREFIX}*: ' + ', '.join(foreign))

    def _require_range_below_portal_mark(self, inventory: Dict[int, dict]) -> None:
        """The portal allocates above max(cluster VMIDs, its database mark)
        and stores the result as the new mark, which never goes down. A
        contract guest above that mark, alive while a student deploys, would
        raise every later allocation for good."""
        if self.separate_cluster:
            return
        top = self.vmid_range[-1]
        span = f'{self.vmid_range[0]}-{top}'
        if self.portal_vmid_mark is not None:
            if top > self.portal_vmid_mark:
                raise UnsafeLiveRun(
                    f'refusing to run: LIVE_VMID_RANGE {span} reaches above '
                    f"the portal's VMID high-water mark {self.portal_vmid_mark}; "
                    "pick a range below the portal's VMIDs, or use a cluster "
                    'it never deploys to (LIVE_SEPARATE_CLUSTER=1)')
            return
        # Without the database, the highest guest outside the range is the
        # best available lower bound on the mark.
        ceiling = max((vmid for vmid in inventory if vmid not in self.vmid_range),
                      default=0)
        if top > ceiling:
            raise UnsafeLiveRun(
                f'refusing to run: LIVE_VMID_RANGE {span} reaches above every '
                f'other guest on the cluster (highest {ceiling}), so it may '
                "be above the portal's VMID high-water mark; pick a range "
                "below the portal's VMIDs, set LIVE_PORTAL_DATABASE_URL so "
                'the mark can be checked, or LIVE_SEPARATE_CLUSTER=1 on a '
                'cluster the portal never deploys to')

    def preflight(self) -> None:
        inventory = self._inventory()
        self._require_clean_range(inventory)
        self._require_range_below_portal_mark(inventory)

    def _allocate_vmid(self) -> int:
        inventory = self._inventory()
        # Re-checked on every allocation: a guest that appeared since
        # preflight means someone else is using this range right now.
        self._require_clean_range(inventory)
        for vmid in self.vmid_range:
            if vmid not in inventory and vmid not in self._owned:
                return vmid
        raise UnsafeLiveRun(
            f'no free VMID left in LIVE_VMID_RANGE '
            f'{self.vmid_range[0]}-{self.vmid_range[-1]}; remove leftover '
            f'{NAME_PREFIX}* guests by hand')

    def _settle_failed_clone(self, guest: Guest) -> None:
        """Keep the claim only while inventory shows this run's guest."""
        try:
            row = self._inventory().get(guest.vmid)
        except Exception:
            # Unknown state: keep the claim. Cleanup re-checks identity
            # before any mutation, so this cannot reach a foreign guest.
            return
        if row is None or row.get('name') != guest.name:
            self._owned.pop(guest.vmid, None)

    # --- lifecycle -----------------------------------------------------------

    def clone_template(self, node: str, template_vmid: int,
                       storage: Optional[str] = None) -> Guest:
        """Clone through the app's own ``create_qemu_vm``.

        Linked when the source is a template and no storage is configured
        (fast); a full clone onto ``storage`` otherwise.
        """
        template = self.proxmox.nodes(node).qemu(template_vmid).config.get()
        guest = Guest(self._allocate_vmid(), node,
                      f'{NAME_PREFIX}{secrets.token_hex(6)}')
        # Claimed before the POST, so a clone that dies half-way is still
        # cleaned up; _settle_failed_clone drops the claim if it never landed.
        self._owned[guest.vmid] = guest
        config = {
            'memory': int(template.get('memory') or 512),
            'cores': int(template.get('cores') or 1),
            'full_clone': bool(storage) or not _truthy(template.get('template')),
        }
        if storage:
            config['storage_locations'] = {node: storage}
        expires = self.now() + EXPIRY
        try:
            try:
                create_qemu_vm(self.proxmox, node, node, guest.vmid, guest.name,
                               template_vmid, config, expires)
            except Exception:
                # Storage without linked-clone support rejects the POST
                # before any guest exists; only then is a full clone safe.
                if config['full_clone'] or guest.vmid in self._inventory():
                    raise
                create_qemu_vm(self.proxmox, node, node, guest.vmid, guest.name,
                               template_vmid, dict(config, full_clone=True),
                               expires)
        except Exception:
            self._settle_failed_clone(guest)
            raise
        return guest

    def _endpoint(self, guest: Guest):
        return self.proxmox.nodes(guest.node).qemu(guest.vmid)

    def _owned_guest(self, vmid: int, action: str) -> Guest:
        guest = self._owned.get(int(vmid))
        if guest is None or guest.vmid not in self.vmid_range:
            raise UnsafeLiveRun(
                f'refusing to {action} VMID {vmid}: this run did not create it')
        return guest

    @staticmethod
    def _require_identity(guest: Guest, row: Optional[dict], action: str) -> None:
        if row is None or row.get('name') != guest.name or row.get('node') != guest.node:
            found = ('nothing' if row is None
                     else f"{row.get('name')!r} on {row.get('node')}")
            raise UnsafeLiveRun(
                f'refusing to {action} VMID {guest.vmid}: it now holds {found}, '
                f"not this run's {guest.name!r} on {guest.node}")

    def _power(self, guest: Guest, action: str) -> dict:
        if self._owned.get(guest.vmid) != guest:
            raise UnsafeLiveRun(
                f'refusing to {action} VMID {guest.vmid}: this run did not create it')
        self._require_identity(guest, self._inventory().get(guest.vmid), action)
        upid = getattr(self._endpoint(guest).status, action).post()
        result = wait_for_proxmox_task(self.proxmox, guest.node, upid,
                                       timeout=120, poll_interval=1)
        return {**result, 'upid': upid}

    def start(self, guest: Guest) -> dict:
        """Start an owned guest; return the task result plus its ``upid``."""
        return self._power(guest, 'start')

    def stop(self, guest: Guest) -> dict:
        """Stop an owned guest; return the task result plus its ``upid``."""
        return self._power(guest, 'stop')

    def destroy(self, vmid: int) -> dict:
        """Stop and delete one guest this run created; return the delete task result."""
        guest = self._owned_guest(vmid, 'destroy')
        vmid = guest.vmid
        row = self._inventory().get(vmid)
        if row is None:
            if confirm_proxmox_absence(self.proxmox, vmid):
                self._owned.pop(vmid)
                return {'success': True, 'status': 'absent'}
            raise LiveCleanupError(f'VMID {vmid}: inventory is inconclusive')
        self._require_identity(guest, row, 'destroy')

        endpoint = self._endpoint(guest)
        # cluster/resources status lags by up to a pvestatd cycle; the
        # guest's own status decides whether delete would hit a running VM.
        if endpoint.status.current.get().get('status') != 'stopped':
            stopped = self.stop(guest)
            if not stopped['success']:
                raise LiveCleanupError(
                    f"VMID {vmid}: stop task failed: {stopped.get('error')}")
        result = wait_for_proxmox_task(self.proxmox, guest.node,
                                       endpoint.delete(),
                                       timeout=180, poll_interval=1)
        reset_cluster_vm_index()
        if not result['success']:
            raise LiveCleanupError(
                f"VMID {vmid}: delete task failed: {result.get('error')}")
        if not confirm_proxmox_absence(self.proxmox, vmid):
            raise LiveCleanupError(
                f'VMID {vmid}: still in cluster inventory after delete')
        self._owned.pop(vmid)
        return result

    def cleanup(self) -> List[str]:
        """Destroy every guest still owned; return one report per failure."""
        failures = []
        for vmid in self.owned_vmids():
            guest = self._owned[vmid]
            try:
                self.destroy(vmid)
            except UnsafeLiveRun as refused:
                # The VMID no longer holds our guest. It is not ours to touch,
                # so no removal command is offered for it.
                self._owned.pop(vmid, None)
                failures.append(
                    f'{guest.name} (VMID {vmid}) was left alone: {refused}. '
                    'Inspect that VMID by hand.')
            except Exception as error:
                failures.append(
                    f'{guest.name} (VMID {vmid} on {guest.node}) was NOT '
                    f'deleted: {error}\nRemove it by hand:\n'
                    + manual_cleanup_commands(guest))
        for failure in failures:
            print(f'[live contract cleanup] {failure}', file=sys.stderr, flush=True)
        return failures
