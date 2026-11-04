"""Google Compute Engine behind the VMBackend contract: instance lifecycle
and capped admission, over the v1 JSON REST API only (no gRPC client).

Identity is the label set written at insert, never the DB row: an instance
is this record's only when its name and every contract label agree. The
instance name is deterministic, so a ref without a provider id (a create that
raised after the insert) needs no lookup (ruling C7). Absence needs positive
evidence, an HTTP 404 from a fresh GET; every other failure is GUEST_UNKNOWN,
so a transient error can never retire a record whose instance still bills.

Every mutation returns an operation, which is waited on until DONE within
GCP_OPERATION_TIMEOUT_SECONDS. An operation that finishes with errors, or
does not finish in time, raises; neither is ever read as success or absence.

Configuration is read as `config.<NAME>` at call time (ruling I8): this
module is imported lazily by the registry factory, and a module-level
`from app.config import ...` would also defeat tests that patch config.
google.auth is imported only when a backend builds its own session.
"""

import importlib
import re
import sys
import time
import uuid
from datetime import datetime
from typing import Callable, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote

import requests

from app.backends.base import (
    BACKEND_GCP, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, GUEST_UNKNOWN,
    BackendError, DestroyPolicy, DestroyRefused, LaunchedVM, ManagedResource,
    VMRef, VMSpec,
)
from app.backends.cloud_admission import reserve_cloud_capacity
from app.backends.cloud_common import (
    TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT, TAG_VMID,
    deployment_network_name, expires_epoch, instance_name, site_prefix,
    slot_network_name, validate_site_id,
)

API_ROOT = 'https://compute.googleapis.com/compute/v1/projects/'
COMPUTE_SCOPE = 'https://www.googleapis.com/auth/compute'
KIND_INSTANCE = 'instance'

# Fixed, so a requestId derived from the same identity is the same in every
# process: a retried insert then returns the original operation.
_REQUEST_NAMESPACE = uuid.UUID('98db2938-13e7-466b-9ee7-1aeb5bc01359')
_SET_LABELS_ATTEMPTS = 3
_HTTP_TIMEOUT_SECONDS = 60
# Compute's server-side /wait returns within about two minutes; a longer read
# timeout lets it, and the operation deadline still bounds the whole wait.
_WAIT_READ_TIMEOUT_CAP_SECONDS = 150
_NAME = re.compile(r'[a-z]([-a-z0-9]{0,61}[a-z0-9])?')
_ZONE = re.compile(r'[a-z]+-[a-z]+[0-9]+-[a-z]')
_DECIMAL = re.compile(r'[0-9]+')
_STATUS = {
    'RUNNING': 'running', 'TERMINATED': 'stopped',
    'PROVISIONING': 'starting', 'STAGING': 'starting',
}

# The operation deadline's clock; a test replaces it to reach the deadline.
_clock = time.monotonic


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

    def __init__(self, verdict: str, zone: str, name: str, instance=None, reason: str = ''):
        self.verdict = verdict  # GUEST_MATCH / _ABSENT / _MISMATCH / _UNKNOWN
        self.zone = zone
        self.name = name
        self.instance = instance
        self.reason = reason


class GCPBackend:
    name = BACKEND_GCP

    def __init__(self, session: Optional[requests.Session] = None):
        # `_session`, never `session`: vm_provisioning.provider_session()
        # treats a backend attribute named `session` as a context factory.
        self._session = session if session is not None else _default_session()

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

    # Create

    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None]) -> LaunchedVM:
        config = _config()
        site = config.LAB_SITE_ID
        template = spec.template
        name = instance_name(site, spec.vmid)
        labels = {
            TAG_SITE: site,
            TAG_VMID: str(spec.vmid),
            TAG_DEPLOYMENT: spec.deployment_id,
            TAG_EXPIRES: str(expires_epoch(spec.expires_at)),
            TAG_SLOT: str(spec.network_slot),
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
                               f'/subnetworks/{slot_network_name(site, spec.network_slot)}'),
            }],
            # Labelled at insert, so a crash before the DB commit still
            # leaves an instance the sweeper can attribute.
            'labels': labels,
            'tags': {'items': [deployment_network_name(site, spec.deployment_id),
                               f'{site_prefix(site)}lab']},
        }
        heartbeat()
        self._mutate(
            'POST', ('zones', location, 'instances'), what=f'launch of VM {spec.vmid}',
            params={'requestId': _request_id('instance-insert', spec.deployment_id, spec.vmid)},
            body=body, heartbeat=heartbeat)
        resolution = self._fetch(location, name, {k: v for k, v in labels.items()
                                                  if k != TAG_EXPIRES})
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'launched VM {spec.vmid} reads back as {resolution.verdict} '
                f'{resolution.reason}'.rstrip())
        heartbeat()
        return LaunchedVM(vmid=spec.vmid, vm_type=BACKEND_GCP, location=location,
                          network_slot=spec.network_slot,
                          provider_id=resolution.instance['name'])

    # Identity

    def _zone(self, location: Optional[str]) -> str:
        # A record's node is its instance's zone. A survivor may carry
        # location='gcp' (T8), which names no zone: resolution must not
        # depend on it, so anything not shaped like a zone is the configured one.
        if isinstance(location, str) and _ZONE.fullmatch(location):
            return location
        return _config().GCP_ZONE

    def _expected_labels(self, ref: VMRef) -> dict:
        expected = {TAG_SITE: _config().LAB_SITE_ID, TAG_VMID: str(ref.vmid),
                    TAG_KIND: KIND_INSTANCE}
        if ref.deployment_id is not None:
            expected[TAG_DEPLOYMENT] = ref.deployment_id
        if ref.network_slot is not None:
            expected[TAG_SLOT] = str(ref.network_slot)
        return expected

    def _fetch(self, zone: str, name: str, expected: Mapping[str, str]) -> _Resolution:
        """A fresh GET of one instance, judged against its name and `expected`."""
        # Checked locally: a garbled name must never 404 its way to "absent".
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return _Resolution(GUEST_UNKNOWN, zone, name, reason=f'malformed name {name!r}')
        try:
            instance = self._send('GET', ('zones', zone, 'instances', name))
        except requests.RequestException as error:
            if _http_status(error) == 404:
                return _Resolution(GUEST_ABSENT, zone, name)
            return _Resolution(GUEST_UNKNOWN, zone, name, reason=str(error))
        labels = _labels(instance)
        problems = [f'{key}={labels.get(key)!r}' for key, value in expected.items()
                    if labels.get(key) != value]
        if instance.get('name') != name:
            problems.insert(0, f'name={instance.get("name")!r}')
        if problems:
            return _Resolution(GUEST_MISMATCH, zone, name, instance, ', '.join(problems))
        return _Resolution(GUEST_MATCH, zone, name, instance)

    def _resolve(self, ref: VMRef) -> _Resolution:
        name = ref.provider_id or instance_name(_config().LAB_SITE_ID, ref.vmid)
        return self._fetch(self._zone(ref.location), name, self._expected_labels(ref))

    def inspect(self, ref: VMRef) -> str:
        return self._resolve(ref).verdict

    def _delete_instance(self, resolution: _Resolution) -> None:
        """Delete an instance a fresh read just proved is the caller's; one
        already gone (404) is done. Its boot disk goes with it (autoDelete)."""
        zone, name = resolution.zone, resolution.name
        self._mutate(
            'DELETE', ('zones', zone, 'instances', name), what=f'delete of instance {name}',
            # The provider's numeric id makes a recreated namesake a
            # different request, never a replay of this one.
            params={'requestId': _request_id('instance-delete', zone, name,
                                             resolution.instance.get('id', ''))},
            tolerate=404)

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
        # an operation) says nothing about the instance. Only a 404 from a
        # fresh GET of the instance does.
        return self._resolve(ref).verdict == GUEST_ABSENT

    # Display reads

    def _live_match(self, ref: VMRef) -> Optional[dict]:
        resolution = self._resolve(ref)
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

    def _list(self, path: Tuple[str, ...], *, filter_expression: str, what: str) -> List[dict]:
        """Every item of one Compute list, following nextPageToken."""
        items, token, seen = [], None, set()
        while True:
            params = {'filter': filter_expression}
            if token is not None:
                params['pageToken'] = token
            try:
                page = self._send('GET', path, params=params)
            except requests.RequestException as error:
                raise BackendError(f'{what} unavailable: {error}') from error
            page_items = page.get('items', [])
            if not isinstance(page_items, list) or not all(
                    isinstance(item, dict) for item in page_items):
                raise BackendError(f'{what}: malformed page')
            items.extend(page_items)
            token = page.get('nextPageToken')
            if not token:
                return items
            if token in seen:
                raise BackendError(f'{what}: page token {token!r} repeated')
            seen.add(token)

    def list_managed(self) -> List[ManagedResource]:
        """Every instance this site launched, from one fresh paginated read.
        A failed read raises; an empty list would mean "all gone"."""
        config = _config()
        site = config.LAB_SITE_ID
        # Validated before it is quoted into the filter expression.
        validate_site_id(site)
        instances = self._list(
            ('zones', config.GCP_ZONE, 'instances'),
            filter_expression=f'(labels.{TAG_SITE} = "{site}")',
            what='Compute instance inventory')
        resources = []
        for instance in instances:
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
                location=_last_segment(instance.get('zone')),
                deployment_id=labels.get(TAG_DEPLOYMENT),
                network_slot=_decimal(labels.get(TAG_SLOT)),
                expires_at_epoch=float(expires) if expires is not None else None,
                created_at_epoch=_created_epoch(instance.get('creationTimestamp')),
                status=_status(instance)))
        return resources

    def delete_resource(self, resource: ManagedResource) -> None:
        """Delete one listed resource after re-proving, from a fresh GET,
        that it still carries the identity it was listed with: the listing
        may be minutes old. Absent is done; a changed identity raises
        DestroyRefused; an unverifiable one raises BackendError."""
        if resource.backend != self.name:
            raise BackendError(
                f'{resource.backend} {resource.kind} {resource.provider_id} '
                'is not a GCP resource')
        if resource.kind != KIND_INSTANCE:
            raise BackendError(
                f'GCP has no resource kind {resource.kind!r} ({resource.provider_id})')
        expected = {TAG_SITE: _config().LAB_SITE_ID, TAG_KIND: KIND_INSTANCE}
        for key, value in ((TAG_VMID, resource.vmid),
                           (TAG_DEPLOYMENT, resource.deployment_id),
                           (TAG_SLOT, resource.network_slot)):
            if value is not None:
                expected[key] = str(value)
        resolution = self._fetch(self._zone(resource.location), resource.provider_id, expected)
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
