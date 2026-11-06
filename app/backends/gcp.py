"""Google Compute Engine behind the VMBackend contract: instance lifecycle,
capped admission, slot subnetworks, per-deployment firewalls and the router,
over the v1 JSON REST API only (no gRPC client).

Identity is the label set written at insert, never the DB row: an instance
is this record's only when its name and every contract label agree. The
instance name is deterministic, so a ref without a provider id (a create that
raised after the insert) needs no lookup (ruling C7). Where an instance is,
and whether it exists at all, comes only from the project-wide aggregated
list, never from a record's location: a 404 from the wrong zone says nothing.
Absence needs that list to have reached every zone and to hold no instance of
the name; every other answer is GUEST_UNKNOWN, so neither a transient error
nor a stale zone can retire a record whose instance still bills.

Every mutation returns an operation, which is waited on until DONE within
GCP_OPERATION_TIMEOUT_SECONDS. An operation that finishes with errors, or
does not finish in time, raises; neither is ever read as success or absence.

Networks carry no labels, so their ownership is their name: a subnetwork is
this site's slot network only as exactly `lp-<site>-s-<slot>` in the lab
network, and a firewall its deployment network only as exactly
`lp-<site>-d-<16 hex>` whose source and target tags are that same name.
Each also needs a parseable creationTimestamp. Anything merely sharing the
site prefix (the operator's static tailnet rule above all) is never listed
or deleted, and every delete re-reads the resource first. Lock scope belongs
to the caller (ruling C4): no method here takes a lifecycle lock.

Configuration is read as `config.<NAME>` at call time (ruling I8): this
module is imported lazily by the registry factory, and a module-level
`from app.config import ...` would also defeat tests that patch config.
google.auth is imported only when a backend builds its own session.
"""

import importlib
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Iterable, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import quote

import requests

from app.backends.base import (
    BACKEND_GCP, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, GUEST_UNKNOWN,
    BackendError, DestroyPolicy, DestroyRefused, LaunchedVM, ManagedResource,
    SlotNetworkMissing, VMRef, VMSpec,
)
from app.backends.cloud_admission import reserve_cloud_capacity
from app.backends.cloud_common import (
    TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT, TAG_VMID,
    deployment_key, deployment_network_name, expires_epoch, instance_name,
    site_prefix, slot_network_name, validate_site_id,
)
from app.backends.network_slots import SLOT_MAX, cloud_slot_cidr

API_ROOT = 'https://compute.googleapis.com/compute/v1/projects/'
COMPUTE_SCOPE = 'https://www.googleapis.com/auth/compute'
KIND_INSTANCE = 'instance'
KIND_SLOT_NETWORK = 'slot-network'
KIND_DEPLOYMENT_NETWORK = 'deployment-network'
# The operator-provisioned router's kind label.
KIND_ROUTER = 'router'

# Fixed, so a requestId derived from the same identity is the same in every
# process: a retried insert then returns the original operation.
_REQUEST_NAMESPACE = uuid.UUID('98db2938-13e7-466b-9ee7-1aeb5bc01359')
# The only aggregated-list warning that leaves the answer complete: a scope
# that simply holds nothing on this page. Any other (UNREACHABLE above all)
# means some zone may hold an instance the list did not show.
_COMPLETE_WARNINGS = ('NO_RESULTS_ON_PAGE',)
_SET_LABELS_ATTEMPTS = 3
_HTTP_TIMEOUT_SECONDS = 60
# Compute's server-side /wait returns within about two minutes; a longer read
# timeout lets it, and the operation deadline still bounds the whole wait.
_WAIT_READ_TIMEOUT_CAP_SECONDS = 150
_NAME = re.compile(r'[a-z]([-a-z0-9]{0,61}[a-z0-9])?')
_DEPLOYMENT_SUFFIX = re.compile(r'[0-9a-f]{16}')
_SLOT_SUFFIX = re.compile(r'0|[1-9][0-9]*')
_NETWORK_LINK = re.compile(
    r'(?:https://(?:www|compute)\.googleapis\.com/compute/v1/)?'
    r'projects/([^/]+)/global/networks/([^/]+)')
_SUBNETWORK_LINK = re.compile(r'(?:.*/)?projects/([^/]+)/regions/([^/]+)/subnetworks/([^/]+)')
_FIREWALL_LINK = re.compile(r'(?:.*/)?projects/([^/]+)/global/firewalls/([^/]+)')
# The firewall a deployment gets, field by field (besides name and network).
_DEPLOYMENT_RULE = {'direction': 'INGRESS', 'priority': 1000,
                    'allowed': [{'IPProtocol': 'all'}]}
# Fields that would widen a deployment rule beyond its own tag.
_WIDENING_FIELDS = ('sourceRanges', 'destinationRanges', 'sourceServiceAccounts',
                    'targetServiceAccounts', 'denied')
# Router states that are on their way to each target.
_TRANSITIONS = {'RUNNING': ('PROVISIONING', 'STAGING'), 'TERMINATED': ('STOPPING',)}
_ROUTER_POLL_SECONDS = 5
_DECIMAL = re.compile(r'[0-9]+')
_STATUS = {
    'RUNNING': 'running', 'TERMINATED': 'stopped',
    'PROVISIONING': 'starting', 'STAGING': 'starting',
}

# The operation deadline's clock; a test replaces it to reach the deadline.
_clock = time.monotonic
_sleep = time.sleep


class OperationFailed(BackendError):
    """A Compute operation finished with errors. `operation` is its final
    body, kept as evidence: the error codes, messages and target."""

    def __init__(self, message: str, operation: dict):
        super().__init__(message)
        self.operation = operation


class _ProviderFailure(requests.RequestException):
    """A provider-boundary failure with no HTTP status: an undecodable body,
    or credentials that could not be refreshed."""


def _config():
    # Not `from app import config`: the package re-exports the config-class
    # mapping under that name, shadowing the module.
    return importlib.import_module('app.config')


def _request_id(*identity) -> str:
    """A non-zero RFC-4122 UUIDv5 of an operation kind and immutable
    identity, never Python's per-process randomized hash()."""
    return str(uuid.uuid5(_REQUEST_NAMESPACE, ':'.join(str(part) for part in identity)))


def _fresh_request_id() -> str:
    """A new requestId for a mutation of a uniquely named resource. The
    name already makes a repeat harmless (409 or 404), while a stable id
    would replay a terminally failed operation on every later try."""
    return str(uuid.uuid4())


def _http_status(error: BaseException) -> Optional[int]:
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return getattr(error.response, 'status_code', None)
    return None


def _is_auth_error(error: BaseException) -> bool:
    # Only reachable through an AuthorizedSession, so google.auth is loaded.
    exceptions = sys.modules.get('google.auth.exceptions')
    return exceptions is not None and isinstance(error, exceptions.GoogleAuthError)


def _last_segment(link) -> Optional[str]:
    if not isinstance(link, str) or not link:
        return None
    return link.rstrip('/').rsplit('/', 1)[-1]


def _decimal(value) -> Optional[int]:
    return int(value) if isinstance(value, str) and _DECIMAL.fullmatch(value) else None


def _created_epoch(timestamp) -> Optional[float]:
    # RFC3339 with an offset; anything else is None, never a guessed age.
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except (TypeError, ValueError):
        return None


def _status(instance: Mapping) -> str:
    raw = instance.get('status')
    if not isinstance(raw, str) or not raw:
        return 'unknown'
    return _STATUS.get(raw, raw.lower())


def _labels(instance: Mapping) -> dict:
    labels = instance.get('labels')
    return labels if isinstance(labels, dict) else {}


def _check_warning(warning, scope: str, what: str) -> None:
    """Raise unless an aggregated-list warning leaves the answer complete."""
    if not warning:
        return
    code = warning.get('code') if isinstance(warning, dict) else None
    if code not in _COMPLETE_WARNINGS:
        message = warning.get('message') if isinstance(warning, dict) else warning
        raise BackendError(f'{what}: {scope} answered incompletely ({code}: {message})')


def _operation_errors(operation: Mapping) -> Optional[str]:
    error = operation.get('error')
    if not error:
        return None
    entries = error.get('errors') if isinstance(error, dict) else None
    details = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            detail = f'{entry.get("code")}: {entry.get("message")}'
            if entry.get('location'):
                detail += f' (at {entry["location"]})'
            details.append(detail)
    return '; '.join(details) or repr(error)


def _default_session() -> requests.Session:
    # Lazy: importing this module must neither import google.auth nor
    # discover credentials; only a backend without an injected session does.
    import google.auth
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import AuthorizedSession
    try:
        credentials, _project = google.auth.default(scopes=[COMPUTE_SCOPE])
    except GoogleAuthError as error:
        raise BackendError(f'GCP credentials unavailable: {error}') from error
    return AuthorizedSession(credentials)


class _Resolution:
    """The instance a ref names, or why it names none."""

    def __init__(self, verdict: str, zone: Optional[str], name: str, instance=None,
                 reason: str = ''):
        self.verdict = verdict  # GUEST_MATCH / _ABSENT / _MISMATCH / _UNKNOWN
        self.zone = zone        # the zone discovery found it in; None otherwise
        self.name = name
        self.instance = instance
        self.reason = reason


class GCPBackend:
    name = BACKEND_GCP

    def __init__(self, session: Optional[requests.Session] = None):
        # `_session`, never `session`: vm_provisioning.provider_session()
        # treats a backend attribute named `session` as a context factory.
        self._session = session if session is not None else _default_session()
        # The insert attempt per (deployment, VMID), part of its requestId.
        # In-process is enough: a later deploy has a new deployment id, and a
        # restart that starts again at 0 only replays what that id did.
        self._insert_attempts: dict = {}
        self._attempts_lock = threading.Lock()

    # REST

    def _send(self, method: str, path: Iterable[str], *, params: Optional[dict] = None,
              body: Optional[dict] = None, timeout: float = _HTTP_TIMEOUT_SECONDS) -> dict:
        """One Compute call, returning its JSON object. Raises
        requests.HTTPError for an error status and another
        requests.RequestException for any other provider failure."""
        project = _config().GCP_PROJECT
        if not project:
            raise BackendError('GCP_PROJECT is not configured')
        url = API_ROOT + '/'.join(quote(str(segment), safe='')
                                  for segment in (project, *path))
        kwargs = {'params': params, 'timeout': timeout}
        if body is not None:
            kwargs['json'] = body
        try:
            response = self._session.request(method, url, **kwargs)
        except requests.RequestException:
            raise
        except Exception as error:
            if _is_auth_error(error):
                raise _ProviderFailure(f'{method} {url}: credentials: {error}') from error
            raise
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as error:
            raise _ProviderFailure(f'{method} {url}: response is not JSON') from error
        if not isinstance(data, dict):
            raise _ProviderFailure(f'{method} {url}: response is not a JSON object')
        return data

    def _wait(self, operation: dict, *, what: str,
              heartbeat: Optional[Callable[[], None]] = None) -> dict:
        """Wait on `operation` in the scope Compute returned it in until it
        is DONE; raise if it finished with errors or the deadline passes."""
        timeout = _config().GCP_OPERATION_TIMEOUT_SECONDS
        deadline = _clock() + timeout
        while True:
            name = operation.get('name')
            if operation.get('status') == 'DONE':
                errors = _operation_errors(operation)
                if errors is not None:
                    raise OperationFailed(
                        f'{what}: operation {name} failed'
                        f' (HTTP {operation.get("httpErrorStatusCode")}): {errors}',
                        operation)
                return operation
            if not isinstance(name, str) or not name:
                raise BackendError(f'{what}: Compute returned an operation without a name')
            if heartbeat is not None:
                heartbeat()
            remaining = deadline - _clock()
            if remaining <= 0:
                raise BackendError(
                    f'{what}: operation {name} still {operation.get("status")!r} '
                    f'after {timeout}s')
            if operation.get('zone'):
                scope = ('zones', _last_segment(operation['zone']))
            elif operation.get('region'):
                scope = ('regions', _last_segment(operation['region']))
            else:
                scope = ('global',)
            try:
                operation = self._send(
                    'POST', (*scope, 'operations', name, 'wait'),
                    timeout=min(remaining, _WAIT_READ_TIMEOUT_CAP_SECONDS))
            except requests.RequestException as error:
                raise BackendError(f'{what}: waiting on operation {name} failed: '
                                   f'{error}') from error

    def _mutate(self, method: str, path: Tuple[str, ...], *, what: str,
                params: Optional[dict] = None, body: Optional[dict] = None,
                heartbeat: Optional[Callable[[], None]] = None,
                tolerate: Optional[int] = None) -> Optional[dict]:
        """Start one mutation and wait for it. Returns None when Compute
        refuses it with HTTP `tolerate`; any other failure raises."""
        try:
            operation = self._send(method, path, params=params, body=body)
        except requests.RequestException as error:
            if tolerate is not None and _http_status(error) == tolerate:
                return None
            raise BackendError(f'{what} failed: {error}') from error
        return self._wait(operation, what=what, heartbeat=heartbeat)

    def _get(self, path: Tuple[str, ...], *, what: str) -> Optional[dict]:
        """A fresh GET of one regional or global resource by its exact path.
        None on 404, which for such a path is authoritative; any other
        failure raises BackendError."""
        try:
            return self._send('GET', path)
        except requests.RequestException as error:
            if _http_status(error) == 404:
                return None
            raise BackendError(f'could not read {what}: {error}') from error

    def _delete_owned(self, path: Tuple[str, ...], *, what: str,
                      problems: Callable[[dict], List[str]]) -> None:
        """Delete one regional or global resource only if a fresh GET still
        proves it is ours; absent is done, a changed identity raises
        DestroyRefused and nothing is mutated."""
        item = self._get(path, what=what)
        if item is None:
            return
        found = problems(item)
        if found:
            raise DestroyRefused(
                f'{what} no longer carries the identity to be deleted: {", ".join(found)}')
        self._mutate('DELETE', path, what=f'delete of {what}',
                     params={'requestId': _fresh_request_id()}, tolerate=404)

    # Admission

    def admit(self, spec: VMSpec, *, reservation_id: str,
              load_balancing_config: Optional[Mapping]) -> str:
        config = _config()
        return reserve_cloud_capacity(
            BACKEND_GCP, config.GCP_ZONE, config.GCP_MAX_VMS, reservation_id)

    def renew_admission(self, reservation_id: str) -> bool:
        from app import proxmox_service
        return proxmox_service.renew_node_reservation(reservation_id)

    def release_admission(self, reservation_id: Optional[str]) -> bool:
        from app import proxmox_service
        return proxmox_service.release_node_reservation(reservation_id)

    # Network ownership: names, the lab network and a creation time.

    def _in_lab_network(self, link) -> bool:
        config = _config()
        match = _NETWORK_LINK.fullmatch(link) if isinstance(link, str) else None
        return bool(match) and match.groups() == (config.GCP_PROJECT, config.GCP_NETWORK)

    def _lab_network(self) -> str:
        config = _config()
        return f'projects/{config.GCP_PROJECT}/global/networks/{config.GCP_NETWORK}'

    def _slot_of(self, name) -> Optional[int]:
        """The slot an exactly current-site slot-network name carries."""
        prefix = f'{site_prefix(_config().LAB_SITE_ID)}s-'
        if not isinstance(name, str) or not name.startswith(prefix):
            return None
        suffix = name[len(prefix):]
        if not _SLOT_SUFFIX.fullmatch(suffix) or int(suffix) > SLOT_MAX:
            return None
        return int(suffix)

    def _key_of(self, name) -> Optional[str]:
        """The 16-hex deployment key an exactly current-site firewall name carries."""
        prefix = f'{site_prefix(_config().LAB_SITE_ID)}d-'
        if not isinstance(name, str) or not name.startswith(prefix):
            return None
        suffix = name[len(prefix):]
        return suffix if _DEPLOYMENT_SUFFIX.fullmatch(suffix) else None

    def _subnetwork_problems(self, item: dict, slot: int) -> List[str]:
        """Why `item` is not this site's slot network for `slot`."""
        problems = []
        if item.get('name') != slot_network_name(_config().LAB_SITE_ID, slot):
            problems.append(f'name={item.get("name")!r}')
        if not self._in_lab_network(item.get('network')):
            problems.append(f'network={item.get("network")!r}')
        if _created_epoch(item.get('creationTimestamp')) is None:
            problems.append(f'creationTimestamp={item.get("creationTimestamp")!r}')
        return problems

    def _firewall_problems(self, item: dict, name: str) -> List[str]:
        """Why `item` is not the deployment network named `name`."""
        problems = []
        if item.get('name') != name or self._key_of(name) is None:
            problems.append(f'name={item.get("name")!r}')
        if not self._in_lab_network(item.get('network')):
            problems.append(f'network={item.get("network")!r}')
        for field in ('sourceTags', 'targetTags'):
            if item.get(field) != [name]:
                problems.append(f'{field}={item.get(field)!r}')
        if _created_epoch(item.get('creationTimestamp')) is None:
            problems.append(f'creationTimestamp={item.get("creationTimestamp")!r}')
        return problems

    # Slots. A slot's subnetwork exists from ensure_slot until release_slot;
    # the caller holds vm_lifecycle_lock(slot_lock_key('gcp', slot)).

    def _slot_placement_problems(self, item: dict, slot: int) -> List[str]:
        """Ownership plus the exact placement a slot must have to be used."""
        config = _config()
        problems = self._subnetwork_problems(item, slot)
        if _last_segment(item.get('region')) != config.GCP_REGION:
            problems.append(f'region={item.get("region")!r}')
        if item.get('ipCidrRange') != cloud_slot_cidr(config.GCP_LAB_CIDR, slot):
            problems.append(f'ipCidrRange={item.get("ipCidrRange")!r}')
        return problems

    def _slot_path(self, slot: int) -> Tuple[str, ...]:
        config = _config()
        return ('regions', config.GCP_REGION, 'subnetworks',
                slot_network_name(config.LAB_SITE_ID, slot))

    def _usable_slot(self, slot: int, subnet: dict) -> dict:
        problems = self._slot_placement_problems(subnet, slot)
        if problems:
            raise BackendError(
                f'subnetwork {subnet.get("name")!r} cannot serve slot {slot}: '
                f'{", ".join(problems)}')
        return subnet

    def ensure_slot(self, slot: int) -> None:
        """Create or reuse the slot's private subnetwork."""
        config = _config()
        path = self._slot_path(slot)
        what = f'slot {slot} subnetwork'
        subnet = self._get(path, what=what)
        if subnet is None:
            # A concurrent creator may win the name (409); its subnetwork
            # is adopted below only at this slot's exact placement.
            self._mutate(
                'POST', ('regions', config.GCP_REGION, 'subnetworks'),
                what=f'insert of {what}', params={'requestId': _fresh_request_id()},
                body={'name': path[-1], 'network': self._lab_network(),
                      'ipCidrRange': cloud_slot_cidr(config.GCP_LAB_CIDR, slot),
                      'privateIpGoogleAccess': False},
                tolerate=409)
            subnet = self._get(path, what=what)
            if subnet is None:
                raise BackendError(f'{what} is absent after its insert')
        self._usable_slot(slot, subnet)

    def release_slot(self, slot: int) -> None:
        """Delete the slot's subnetwork; absent is done. Compute refuses
        while an instance still uses it, and that refusal propagates."""
        self._delete_owned(self._slot_path(slot), what=f'slot {slot} subnetwork',
                           problems=lambda item: self._subnetwork_problems(item, slot))

    # Deployment firewalls. Created by a deployment's first create, removed
    # by cleanup_deployment after its instances are gone.

    def _ensure_firewall(self, deployment_id: str) -> None:
        name = deployment_network_name(_config().LAB_SITE_ID, deployment_id)
        path = ('global', 'firewalls', name)
        what = f'deployment firewall {name}'
        firewall = self._get(path, what=what)
        if firewall is None:
            # Same-deployment traffic only: the tag is on this deployment's
            # instances alone. No ranges and no egress rule.
            self._mutate(
                'POST', ('global', 'firewalls'), what=f'insert of {what}',
                params={'requestId': _fresh_request_id()},
                body={'name': name, 'network': self._lab_network(), **_DEPLOYMENT_RULE,
                      'sourceTags': [name], 'targetTags': [name]},
                tolerate=409)
            firewall = self._get(path, what=what)
            if firewall is None:
                raise BackendError(f'{what} is absent after its insert')
        # A rule this name already held (a race winner, or one edited since)
        # is joined only if every security field is exactly the deployment's.
        problems = self._firewall_problems(firewall, name)
        problems += [f'{field}={firewall.get(field)!r}'
                     for field, value in _DEPLOYMENT_RULE.items()
                     if firewall.get(field) != value]
        problems += [f'{field}={firewall[field]!r}' for field in _WIDENING_FIELDS
                     if firewall.get(field)]
        if firewall.get('disabled'):
            problems.append('disabled=True')
        if problems:
            raise BackendError(f'{what} is not exactly this deployment\'s rule: '
                               f'{", ".join(problems)}')

    def cleanup_deployment(self, deployment_id: str) -> None:
        """Delete the deployment's firewall; absent is done."""
        name = deployment_network_name(_config().LAB_SITE_ID, deployment_id)
        self._delete_owned(('global', 'firewalls', name), what=f'deployment firewall {name}',
                           problems=lambda item: self._firewall_problems(item, name))

    # Router. The caller holds vm_lifecycle_lock(router_lock_key('gcp')).
    # The operator provisions it labelled TAG_SITE=<site> and TAG_KIND=router;
    # the configured name alone never proves which machine it is.

    def _router(self) -> Tuple[str, str, str]:
        """(zone, name, status) of the configured router, from discovery and
        a fresh GET; raises BackendError unless it is this site's router."""
        name = _config().GCP_ROUTER_INSTANCE
        if not name:
            raise BackendError('GCP_ROUTER_INSTANCE is not configured')
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise BackendError(f'GCP_ROUTER_INSTANCE {name!r} is not an instance name')
        found = self._discover(name)
        if found is None:
            raise BackendError(f'router {name} does not exist')
        zone = found[0]
        return zone, name, self._router_status(zone, name)

    def _router_status(self, zone: str, name: str) -> str:
        try:
            instance = self._send('GET', ('zones', zone, 'instances', name))
        except requests.RequestException as error:
            raise BackendError(f'could not read router {name}: {error}') from error
        labels = _labels(instance)
        site = _config().LAB_SITE_ID
        if (instance.get('name') != name or labels.get(TAG_SITE) != site
                or labels.get(TAG_KIND) != KIND_ROUTER):
            raise BackendError(
                f'instance {name} is not this site\'s router: '
                f'{TAG_SITE}={labels.get(TAG_SITE)!r}, {TAG_KIND}={labels.get(TAG_KIND)!r}')
        status = instance.get('status')
        return status if isinstance(status, str) else 'UNKNOWN'

    def _settle(self, zone: str, name: str, target: str) -> None:
        """Poll the router until it is `target`, within the operation
        timeout; a state not on the way there, or the deadline, raises."""
        timeout = _config().GCP_OPERATION_TIMEOUT_SECONDS
        deadline = _clock() + timeout
        while True:
            status = self._router_status(zone, name)
            if status == target:
                return
            if status not in _TRANSITIONS[target]:
                raise BackendError(f'router {name} is {status}, not on its way to {target}')
            if _clock() >= deadline:
                raise BackendError(f'router {name} still {status} after {timeout}s')
            _sleep(_ROUTER_POLL_SECONDS)

    def _router_call(self, zone: str, name: str, action: str) -> None:
        self._mutate('POST', ('zones', zone, 'instances', name, action),
                     what=f'{action} of router {name}',
                     params={'requestId': _fresh_request_id()})

    def ensure_router_running(self) -> None:
        zone, name, status = self._router()
        if status == 'RUNNING':
            return
        if status in _TRANSITIONS['TERMINATED']:
            self._settle(zone, name, 'TERMINATED')
            status = 'TERMINATED'
        if status == 'TERMINATED':
            self._router_call(zone, name, 'start')
        elif status not in _TRANSITIONS['RUNNING']:
            raise BackendError(f'router {name} is {status}')
        self._settle(zone, name, 'RUNNING')

    def stop_router(self) -> None:
        zone, name, status = self._router()
        if status == 'TERMINATED':
            return
        if status in _TRANSITIONS['RUNNING']:
            # Compute cannot stop an instance that is still starting.
            self._settle(zone, name, 'RUNNING')
            status = 'RUNNING'
        if status == 'RUNNING':
            self._router_call(zone, name, 'stop')
        elif status not in _TRANSITIONS['TERMINATED']:
            raise BackendError(f'router {name} is {status}')
        self._settle(zone, name, 'TERMINATED')

    # Create

    def _slot_released(self, slot: int) -> bool:
        try:
            return self._get(self._slot_path(slot), what=f'slot {slot} subnetwork') is None
        except BackendError:
            return False

    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None]) -> LaunchedVM:
        config = _config()
        site = config.LAB_SITE_ID
        template = spec.template
        name = instance_name(site, spec.vmid)
        slot = spec.network_slot
        # The slot a concurrent retirement may have released since
        # ensure_slot: create_vm re-ensures it and retries once (ruling I3).
        subnet = self._get(self._slot_path(slot), what=f'slot {slot} subnetwork')
        if subnet is None:
            raise SlotNetworkMissing(f'slot {slot} has no subnetwork for VM {spec.vmid}')
        self._usable_slot(slot, subnet)
        self._ensure_firewall(spec.deployment_id)
        labels = {
            TAG_SITE: site,
            TAG_VMID: str(spec.vmid),
            TAG_DEPLOYMENT: spec.deployment_id,
            TAG_EXPIRES: str(expires_epoch(spec.expires_at)),
            TAG_SLOT: str(slot),
            TAG_KIND: KIND_INSTANCE,
        }
        body = {
            'name': name,
            # Labels cannot hold the portal's vm_name (lower-case only, 63
            # characters); inventory reads it back from here.
            'description': spec.vm_name,
            'machineType': f'zones/{location}/machineTypes/{template["machine_type"]}',
            'disks': [{
                'boot': True,
                # The boot disk dies with the instance, so a destroy can
                # never leave a disk billing.
                'autoDelete': True,
                'initializeParams': {
                    'sourceImage': template['image'],
                    'diskSizeGb': str(template.get('disk_gb', 20)),
                },
            }],
            # No accessConfigs: the lab VM gets no external IP.
            'networkInterfaces': [{
                'subnetwork': (f'projects/{config.GCP_PROJECT}/regions/{config.GCP_REGION}'
                               f'/subnetworks/{slot_network_name(site, slot)}'),
            }],
            # Labelled at insert, so a crash before the DB commit still
            # leaves an instance the sweeper can attribute.
            'labels': labels,
            # The deployment tag joins its firewall; the site lab tag is the
            # target of the operator's static tailnet rule.
            'tags': {'items': [deployment_network_name(site, spec.deployment_id),
                               f'{site_prefix(site)}lab']},
        }
        key = (spec.deployment_id, spec.vmid)
        with self._attempts_lock:
            attempt = self._insert_attempts.get(key, 0)
        heartbeat()
        try:
            self._mutate(
                'POST', ('zones', location, 'instances'), what=f'launch of VM {spec.vmid}',
                params={'requestId': _request_id('instance-insert', spec.deployment_id,
                                                 spec.vmid, attempt)},
                body=body, heartbeat=heartbeat)
        except BackendError as error:
            if isinstance(error, OperationFailed):
                # Compute keeps a requestId's result, so a terminally failed
                # insert retried under the same id replays its failure
                # forever. A new id is safe only once no instance of the
                # name exists anywhere; until then the replay is the retry
                # that cannot start a twin. A timeout never gets here.
                try:
                    absent = self._discover(name) is None
                except BackendError:
                    absent = False
                if absent:
                    with self._attempts_lock:
                        if self._insert_attempts.get(key, 0) == attempt:
                            self._insert_attempts[key] = attempt + 1
            # The slot released between the check above and the insert,
            # whether Compute refused at once or in the operation.
            if self._slot_released(slot):
                raise SlotNetworkMissing(
                    f'slot {slot} lost its subnetwork during the launch of VM '
                    f'{spec.vmid}: {error}') from error
            raise
        resolution = self._fetch(location, name, {k: v for k, v in labels.items()
                                                  if k != TAG_EXPIRES})
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'launched VM {spec.vmid} reads back as {resolution.verdict} '
                f'{resolution.reason}'.rstrip())
        heartbeat()
        return LaunchedVM(vmid=spec.vmid, vm_type=BACKEND_GCP, location=location,
                          network_slot=slot, provider_id=resolution.instance['name'])

    # Identity

    def _discover(self, name: str) -> Optional[Tuple[str, dict]]:
        """(zone, listed instance) for the instance `name`, or None when
        the project holds none, from one complete aggregated list. Raises
        BackendError when the list fails, misses a zone, or finds the name
        twice."""
        found = [(zone, instance) for zone, instance in self._aggregated(
                     'instances', f'(name = "{name}")', what=f'discovery of instance {name}')
                 if instance.get('name') == name]
        if len(found) > 1:
            raise BackendError(f'{len(found)} instances are named {name}: '
                               f'zones {sorted(zone for zone, _ in found)}')
        return found[0] if found else None

    def _expected_labels(self, ref: VMRef) -> dict:
        expected = {TAG_SITE: _config().LAB_SITE_ID, TAG_VMID: str(ref.vmid),
                    TAG_KIND: KIND_INSTANCE}
        if ref.deployment_id is not None:
            expected[TAG_DEPLOYMENT] = ref.deployment_id
        if ref.network_slot is not None:
            expected[TAG_SLOT] = str(ref.network_slot)
        return expected

    def _placement_problems(self, instance: dict, slot_label: Optional[str]) -> List[str]:
        """Why the instance's NIC is not on the lab network and, when the
        caller knows its slot, that slot's subnetwork. Labels alone never
        place an instance: anything may carry a copy of them."""
        config = _config()
        interfaces = instance.get('networkInterfaces')
        nic = interfaces[0] if isinstance(interfaces, list) and interfaces else None
        if not isinstance(nic, dict):
            return ['networkInterfaces=[]']
        problems = []
        if not self._in_lab_network(nic.get('network')):
            problems.append(f'network={nic.get("network")!r}')
        if slot_label is not None:
            link = nic.get('subnetwork')
            match = _SUBNETWORK_LINK.fullmatch(link) if isinstance(link, str) else None
            # The region is the instance's own, which Compute enforces.
            if (match is None or match.group(1) != config.GCP_PROJECT
                    or match.group(3) != slot_network_name(config.LAB_SITE_ID,
                                                           int(slot_label))):
                problems.append(f'subnetwork={link!r}')
        return problems

    def _judge(self, zone: str, name: str, instance: dict,
               expected: Mapping[str, str], *, placement: bool) -> _Resolution:
        labels = _labels(instance)
        problems = [f'{key}={labels.get(key)!r}' for key, value in expected.items()
                    if labels.get(key) != value]
        if instance.get('name') != name:
            problems.insert(0, f'name={instance.get("name")!r}')
        if placement and not problems:
            problems = self._placement_problems(instance, expected.get(TAG_SLOT))
        if problems:
            return _Resolution(GUEST_MISMATCH, zone, name, instance, ', '.join(problems))
        return _Resolution(GUEST_MATCH, zone, name, instance)

    def _fetch(self, zone: str, name: str, expected: Mapping[str, str]) -> _Resolution:
        """A fresh GET of one instance in `zone` (where discovery found it, or
        where create just inserted it), judged against its name, `expected`
        and its lab placement: every caller may act on the verdict. Never
        absence: a 404 here is a delete racing discovery or a moved instance,
        so it is unknown."""
        try:
            instance = self._send('GET', ('zones', zone, 'instances', name))
        except requests.RequestException as error:
            return _Resolution(GUEST_UNKNOWN, zone, name, reason=str(error))
        return self._judge(zone, name, instance, expected, placement=True)

    def _identify(self, name, expected: Mapping[str, str], *,
                  reread: bool = True) -> _Resolution:
        """Discover `name` and judge it: from a fresh zonal GET when the
        caller may act on it, or from the listed resource itself when it
        only displays it (`reread=False`)."""
        # Checked locally: a garbled name must never be listed as "absent".
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return _Resolution(GUEST_UNKNOWN, None, name, reason=f'malformed name {name!r}')
        try:
            found = self._discover(name)
        except BackendError as error:
            return _Resolution(GUEST_UNKNOWN, None, name, reason=str(error))
        if found is None:
            return _Resolution(GUEST_ABSENT, None, name)
        zone, listed = found
        if not reread:
            # Display only: nothing acts on it, so labels suffice.
            return self._judge(zone, name, listed, expected, placement=False)
        return self._fetch(zone, name, expected)

    def _resolve(self, ref: VMRef, *, reread: bool = True) -> _Resolution:
        # ref.location is never consulted: discovery finds the zone.
        name = ref.provider_id or instance_name(_config().LAB_SITE_ID, ref.vmid)
        return self._identify(name, self._expected_labels(ref), reread=reread)

    def inspect(self, ref: VMRef) -> str:
        return self._resolve(ref).verdict

    def _delete_instance(self, resolution: _Resolution) -> None:
        """Delete an instance a fresh read just proved is the caller's. Its
        boot disk goes with it (autoDelete)."""
        zone, name = resolution.zone, resolution.name
        done = self._mutate(
            'DELETE', ('zones', zone, 'instances', name), what=f'delete of instance {name}',
            params={'requestId': _fresh_request_id()}, tolerate=404)
        # A zonal 404 is not absence on its own: only discovery decides.
        if done is None and self._discover(name) is not None:
            raise BackendError(f'delete of instance {name} got 404 in {zone}, '
                               'but the instance still exists')

    def destroy(self, ref: VMRef, policy: DestroyPolicy) -> None:
        # Compute deletes a running instance directly, so every policy is
        # the same; identity is always re-read, even after an inspect.
        resolution = self._resolve(ref)
        if resolution.verdict == GUEST_ABSENT:
            return
        if resolution.verdict == GUEST_MISMATCH:
            raise DestroyRefused(
                f'instance {resolution.name} no longer carries the identity of VM '
                f'{ref.vmid}: {resolution.reason}')
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'identity of VM {ref.vmid} could not be verified: {resolution.reason}')
        self._delete_instance(resolution)

    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        # `error` is never evidence: a 404 from another call (a subnetwork,
        # an operation, another zone) says nothing about the instance. Only
        # a complete discovery that finds no instance of the name does.
        return self._resolve(ref).verdict == GUEST_ABSENT

    # Display reads: judged from the discovered resource itself, which
    # carries everything shown; nothing here mutates.

    def _live_match(self, ref: VMRef) -> Optional[dict]:
        resolution = self._resolve(ref, reread=False)
        return resolution.instance if resolution.verdict == GUEST_MATCH else None

    def ip_addresses(self, ref: VMRef) -> List[str]:
        instance = self._live_match(ref)
        if instance is None:
            return []
        # Private addresses only; accessConfigs (external IPs) are never read.
        return [nic['networkIP'] for nic in instance.get('networkInterfaces') or []
                if isinstance(nic, dict) and isinstance(nic.get('networkIP'), str)
                and nic['networkIP']]

    def status(self, ref: VMRef) -> str:
        instance = self._live_match(ref)
        return _status(instance) if instance is not None else 'unknown'

    def set_expiry(self, ref: VMRef, expires_at: datetime) -> None:
        value = str(expires_epoch(expires_at))
        for _attempt in range(_SET_LABELS_ATTEMPTS):
            # setLabels replaces the whole mapping: re-read and re-prove
            # identity on every attempt, and change only the expiry.
            resolution = self._resolve(ref)
            if resolution.verdict != GUEST_MATCH:
                raise BackendError(
                    f'could not write expiry of VM {ref.vmid}: identity is '
                    f'{resolution.verdict} {resolution.reason}'.rstrip())
            fingerprint = resolution.instance.get('labelFingerprint')
            if not isinstance(fingerprint, str) or not fingerprint:
                raise BackendError(
                    f'could not write expiry of VM {ref.vmid}: no labelFingerprint')
            labels = dict(_labels(resolution.instance), **{TAG_EXPIRES: value})
            done = self._mutate(
                'POST', ('zones', resolution.zone, 'instances', resolution.name, 'setLabels'),
                what=f'expiry update of VM {ref.vmid}',
                body={'labels': labels, 'labelFingerprint': fingerprint},
                tolerate=412)
            if done is not None:
                return
        raise BackendError(
            f'could not write expiry of VM {ref.vmid}: its labels changed concurrently '
            f'on {_SET_LABELS_ATTEMPTS} attempts')

    # Inventory

    def _pages(self, path: Tuple[str, ...], params: dict, *, what: str) -> Iterator[dict]:
        """Every page of one Compute list, following nextPageToken."""
        token, seen = None, set()
        while True:
            query = dict(params)
            if token is not None:
                query['pageToken'] = token
            try:
                page = self._send('GET', path, params=query)
            except requests.RequestException as error:
                raise BackendError(f'{what} unavailable: {error}') from error
            yield page
            token = page.get('nextPageToken')
            if not token:
                return
            if not isinstance(token, str) or token in seen:
                raise BackendError(f'{what}: page token {token!r} repeated or malformed')
            seen.add(token)

    def _aggregated(self, collection: str, filter_expression: Optional[str], *,
                    what: str) -> List[Tuple[str, dict]]:
        """(location, item) for every instance (zone) or subnetwork (region)
        the project-wide aggregated list returns. Raises BackendError unless
        every scope answered: a list that missed one cannot prove absence."""
        scope_kind, field = {'instances': ('zones', 'zone'),
                             'subnetworks': ('regions', 'region')}[collection]
        params = {'filter': filter_expression} if filter_expression else {}
        found = []
        for page in self._pages(('aggregated', collection), params, what=what):
            if page.get('unreachables'):
                raise BackendError(f'{what}: unreachable {page["unreachables"]!r}')
            _check_warning(page.get('warning'), 'the project', what)
            scopes = page.get('items', {})
            if not isinstance(scopes, dict):
                raise BackendError(f'{what}: malformed page')
            for scope, scoped in scopes.items():
                if not isinstance(scoped, dict):
                    raise BackendError(f'{what}: malformed scope {scope!r}')
                _check_warning(scoped.get('warning'), scope, what)
                items = scoped.get(collection, [])
                if not isinstance(items, list):
                    raise BackendError(f'{what}: malformed scope {scope!r}')
                for item in items:
                    location = _last_segment(item.get(field)) if isinstance(
                        item, dict) else None
                    if not location or scope != f'{scope_kind}/{location}':
                        raise BackendError(f'{what}: malformed item in {scope!r}')
                    found.append((location, item))
        return found

    def list_managed(self) -> List[ManagedResource]:
        """Every instance, slot subnetwork and deployment firewall this site
        created, from fresh paginated reads. A failed read raises; an empty
        list would mean "all gone"."""
        return self._managed_instances() + self._managed_networks()

    def _managed_instances(self) -> List[ManagedResource]:
        site = _config().LAB_SITE_ID
        # Validated before it is quoted into the filter expression.
        validate_site_id(site)
        resources = []
        for zone, instance in self._aggregated('instances', f'(labels.{TAG_SITE} = "{site}")',
                                               what='Compute instance inventory'):
            labels = _labels(instance)
            # Re-checked locally: a loose provider filter confers no
            # ownership, and the site's router carries the site label too.
            if labels.get(TAG_SITE) != site or labels.get(TAG_KIND) != KIND_INSTANCE:
                continue
            name = instance.get('name')
            if not isinstance(name, str) or not name:
                raise BackendError('Compute instance inventory: an owned instance has no name')
            expires = _decimal(labels.get(TAG_EXPIRES))
            description = instance.get('description')
            resources.append(ManagedResource(
                backend=BACKEND_GCP, kind=KIND_INSTANCE, provider_id=name,
                name=description if isinstance(description, str) else '',
                vmid=_decimal(labels.get(TAG_VMID)),
                location=zone,
                deployment_id=labels.get(TAG_DEPLOYMENT),
                network_slot=_decimal(labels.get(TAG_SLOT)),
                expires_at_epoch=float(expires) if expires is not None else None,
                created_at_epoch=_created_epoch(instance.get('creationTimestamp')),
                status=_status(instance)))
        return resources

    def _managed_networks(self) -> List[ManagedResource]:
        resources = []
        # Every region, like instances: a slot left in a region since
        # changed away from must still reach the sweeper.
        for region, subnet in self._aggregated('subnetworks', None,
                                               what='Compute subnetwork inventory'):
            slot = self._slot_of(subnet.get('name'))
            if slot is None or self._subnetwork_problems(subnet, slot):
                continue
            resources.append(ManagedResource(
                backend=BACKEND_GCP, kind=KIND_SLOT_NETWORK,
                provider_id=subnet.get('selfLink') or subnet['name'], name=subnet['name'],
                location=region, network_slot=slot,
                created_at_epoch=_created_epoch(subnet.get('creationTimestamp'))))
        for page in self._pages(('global', 'firewalls'), {}, what='Compute firewall inventory'):
            firewalls = page.get('items', [])
            if not isinstance(firewalls, list) or not all(
                    isinstance(item, dict) for item in firewalls):
                raise BackendError('Compute firewall inventory: malformed page')
            for firewall in firewalls:
                key = self._key_of(firewall.get('name'))
                if key is None or self._firewall_problems(firewall, firewall['name']):
                    continue
                resources.append(ManagedResource(
                    backend=BACKEND_GCP, kind=KIND_DEPLOYMENT_NETWORK,
                    provider_id=firewall.get('selfLink') or firewall['name'],
                    name=firewall['name'],
                    # The 16-character key on both clouds (ruling C1).
                    deployment_id=key,
                    created_at_epoch=_created_epoch(firewall.get('creationTimestamp'))))
        return resources

    def delete_resource(self, resource: ManagedResource) -> None:
        """Delete one listed resource after re-proving, from a fresh read,
        that it still carries the identity it was listed with: the listing
        may be minutes old, and an instance's location is not trusted.
        Absent is done; a changed identity raises DestroyRefused; an
        unverifiable one raises BackendError."""
        if resource.backend != self.name:
            raise BackendError(
                f'{resource.backend} {resource.kind} {resource.provider_id} '
                'is not a GCP resource')
        if resource.kind == KIND_INSTANCE:
            self._delete_listed_instance(resource)
        elif resource.kind == KIND_SLOT_NETWORK:
            self._delete_listed_subnetwork(resource)
        elif resource.kind == KIND_DEPLOYMENT_NETWORK:
            self._delete_listed_firewall(resource)
        else:
            raise BackendError(
                f'GCP has no resource kind {resource.kind!r} ({resource.provider_id})')

    def _delete_listed_instance(self, resource: ManagedResource) -> None:
        expected = {TAG_SITE: _config().LAB_SITE_ID, TAG_KIND: KIND_INSTANCE}
        for key, value in ((TAG_VMID, resource.vmid),
                           (TAG_DEPLOYMENT, resource.deployment_id),
                           (TAG_SLOT, resource.network_slot)):
            if value is not None:
                expected[key] = str(value)
        resolution = self._identify(resource.provider_id, expected)
        if resolution.verdict == GUEST_ABSENT:
            return
        if resolution.verdict == GUEST_MISMATCH:
            raise DestroyRefused(
                f'instance {resource.provider_id} no longer carries the identity it was '
                f'listed with: {resolution.reason}')
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'identity of instance {resource.provider_id} could not be verified: '
                f'{resolution.reason}')
        self._delete_instance(resolution)

    def _listed_name(self, resource: ManagedResource,
                     link: re.Pattern) -> Tuple[Optional[str], str]:
        """(location, name) from a listed provider id: a selfLink, which must
        name this project, or a bare name."""
        provider_id = resource.provider_id or ''
        match = link.fullmatch(provider_id)
        if match is None:
            return resource.location, provider_id
        project, *rest = match.groups()
        if project != _config().GCP_PROJECT:
            raise DestroyRefused(f'{resource.kind} {provider_id} is in another project')
        return (rest[0], rest[1]) if len(rest) == 2 else (None, rest[0])

    def _delete_listed_subnetwork(self, resource: ManagedResource) -> None:
        region, name = self._listed_name(resource, _SUBNETWORK_LINK)
        slot = self._slot_of(name)
        if slot is None or (resource.network_slot is not None and resource.network_slot != slot):
            raise DestroyRefused(f'subnetwork {resource.provider_id} is not this site\'s '
                                 f'slot {resource.network_slot} network')
        if not isinstance(region, str) or not _NAME.fullmatch(region):
            raise BackendError(f'subnetwork {resource.provider_id} has no region')
        self._delete_owned(('regions', region, 'subnetworks', name),
                           what=f'slot {slot} subnetwork {name} in {region}',
                           problems=lambda item: self._subnetwork_problems(item, slot))

    def _delete_listed_firewall(self, resource: ManagedResource) -> None:
        _, name = self._listed_name(resource, _FIREWALL_LINK)
        key = self._key_of(name)
        if key is None or (resource.deployment_id is not None
                           and deployment_key(resource.deployment_id) != key):
            raise DestroyRefused(f'firewall {resource.provider_id} is not this site\'s '
                                 f'deployment {resource.deployment_id} network')
        self._delete_owned(('global', 'firewalls', name), what=f'deployment firewall {name}',
                           problems=lambda item: self._firewall_problems(item, name))
