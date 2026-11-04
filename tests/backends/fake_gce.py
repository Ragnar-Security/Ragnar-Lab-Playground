"""A stateful fake of the Compute Engine v1 JSON REST API.

FakeGCESession presents the requests.Session.request() surface GCPBackend
calls, and keeps provider state in dictionaries keyed by provider identity:
instances by (zone, name), subnetworks by (region, name), firewalls by name,
operations by (scope, location, name) and request-id results by
(method, path, requestId). Tests assert against that state and the resource
fields; there is deliberately no ordered call log.

Mutations create operations that are already DONE, unless a test asks for a
stalled operation or an operation error. Absent resources answer 404, a stale
labelFingerprint 412, and a repeated mutation with the same endpoint and
requestId returns the original operation without changing state.

None of this observes Google's real behaviour: the list-filter grammar, the
status codes chosen for synchronous refusals, and `/wait` returning an
unchanged non-DONE operation are modelled, not measured (T19 live-confirms).
"""

import copy
import ipaddress
import itertools
import json as jsonlib
import re
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import requests

API_ROOT = 'https://compute.googleapis.com/compute/v1/projects/'
# Compute's own links use this host, not the API root: a backend must read
# only their final segment.
LINK_ROOT = 'https://www.googleapis.com/compute/v1/projects/'
CREATION_TIMESTAMP = '2026-01-02T03:04:05.000-08:00'

_NAME = re.compile(r'[a-z]([-a-z0-9]{0,61}[a-z0-9])?')
_LABEL_KEY = re.compile(r'[a-z][a-z0-9_-]{0,62}')
_LABEL_VALUE = re.compile(r'[a-z0-9_-]{0,63}')
_SUBNETWORK_PATH = re.compile(r'(?:.*/)?regions/([^/]+)/subnetworks/([^/]+)')
_NETWORK_PATH = re.compile(r'(?:.*/)?global/networks/([^/]+)')
# One parenthesized clause of Compute's list filter: (field op value).
_CLAUSE = re.compile(
    r'\(\s*([A-Za-z0-9_.-]+)\s*(=|!=|eq|ne)\s*("(?:[^"\\]|\\.)*"|[^\s()]+)\s*\)')
_BARE = re.compile(r'\s*([A-Za-z0-9_.-]+)\s+(eq|ne)\s+(\S+)\s*')


class FakeResponse:
    """The slice of requests.Response GCPBackend reads."""

    def __init__(self, status_code: int, body=None, *, url: str = '',
                 invalid_json: bool = False):
        self.status_code = status_code
        self.url = url
        self._body = body
        self._invalid_json = invalid_json

    @property
    def text(self) -> str:
        return '<html>not json</html>' if self._invalid_json else jsonlib.dumps(self._body)

    def json(self):
        if self._invalid_json:
            raise ValueError('response body is not JSON')
        return copy.deepcopy(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f'{self.status_code} Error for url: {self.url}', response=self)


@dataclass
class _Fault:
    method: str
    path: re.Pattern
    status: Optional[int]
    invalid_json: bool
    transport: bool
    remaining: int


def _error(status: int, reason: str, message: str, url: str) -> FakeResponse:
    return FakeResponse(status, {'error': {
        'code': status, 'message': message,
        'errors': [{'reason': reason, 'message': message}]}}, url=url)


def _valid_labels(labels) -> bool:
    return isinstance(labels, dict) and len(labels) <= 64 and all(
        isinstance(k, str) and isinstance(v, str)
        and _LABEL_KEY.fullmatch(k) and _LABEL_VALUE.fullmatch(v)
        for k, v in labels.items())


def _field(item: dict, path: str):
    value = item
    for part in path.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _parse_filter(expression: str) -> Optional[List[Tuple[str, str, str]]]:
    """Compute's filter as (field, op, value) clauses, ANDed; None if this
    fake cannot parse it (answered 400, never read as "match all")."""
    text = expression.strip()
    clauses, position = [], 0
    while position < len(text):
        match = _CLAUSE.match(text, position)
        if not match:
            break
        value = match.group(3)
        if value.startswith('"'):
            value = re.sub(r'\\(.)', r'\1', value[1:-1])
        clauses.append((match.group(1), match.group(2), value))
        position = match.end()
        separator = re.match(r'\s+(?:AND\s+)?', text[position:])
        if separator:
            position += separator.end()
    if clauses and position >= len(text):
        return clauses
    bare = _BARE.fullmatch(text)
    if bare:
        return [bare.groups()]
    return None


def _matches(item: dict, clauses) -> bool:
    for field, op, value in clauses:
        actual = _field(item, field)
        actual = '' if actual is None else str(actual)
        if op in ('=', '!='):
            hit = actual == value
        else:
            hit = re.fullmatch(value, actual) is not None
        if hit != (op in ('=', 'eq')):
            return False
    return True


class FakeGCESession(requests.Session):
    """Compute Engine in dictionaries. Knobs a test may set:

    page_size: items per list page (None: everything on one page).
    loose_filter: ignore list filters, as a provider filter that matches
        too much would; the backend's local ownership checks must hold.
    label_conflicts_remaining: that many setLabels calls lose a race to a
        concurrent writer (which bumps the fingerprint and applies
        concurrent_labels) and answer 412.
    next_operation_error: one error dict (code/message/location) or a list
        of them; the next mutation's operation is DONE with error.errors and
        its change is not applied.
    stall_operations: new operations stay RUNNING, and /wait returns them
        unchanged, until this is cleared.
    subnetwork_failure: 'sync' (an insert into a missing subnetwork is
        refused with 404, no operation) or 'async' (an operation is created
        and finishes DONE with a RESOURCE_NOT_FOUND error).
    known_zones: zones the aggregated list reports, holding instances or not.
    unreachable_zones: zones the aggregated list cannot reach: their
        instances are omitted, and the page reports them in each form named
        by unreachable_reports ('warning': a scope warning UNREACHABLE;
        'unreachables': the top-level unreachables list).
    """

    def __init__(self, project: str = 'example-project'):
        super().__init__()
        self.project = project
        self.instances: Dict[Tuple[str, str], dict] = {}
        self.subnetworks: Dict[Tuple[str, str], dict] = {}
        self.firewalls: Dict[str, dict] = {}
        self.operations: Dict[Tuple[str, Optional[str], str], dict] = {}
        self.request_results: Dict[Tuple[str, str, str], Tuple[str, Optional[str], str]] = {}
        self.page_size: Optional[int] = None
        self.loose_filter = False
        self.label_conflicts_remaining = 0
        self.concurrent_labels: Dict[str, str] = {}
        self.next_operation_error = None
        self.stall_operations = False
        self._faults: List[_Fault] = []
        self._pending: Dict[Tuple[str, Optional[str], str], Callable[[], None]] = {}
        self._ids = itertools.count(1000)
        self._next_host: Dict[Tuple[str, str], int] = {}
        self.subnetwork_failure = 'sync'
        self.known_zones = {'us-central1-a', 'us-central1-b', 'us-east1-b'}
        self.unreachable_zones: set = set()
        self.unreachable_reports: Tuple[str, ...] = ('warning', 'unreachables')

    # Test controls

    def fail(self, method: str, path: str, *, status: Optional[int] = None,
             invalid_json: bool = False, transport: bool = False, times: int = 1) -> None:
        """Make the next `times` requests whose method is `method` and whose
        project-relative path fully matches the regex `path` fail: with an
        HTTP `status`, a 200 whose body is not JSON, or a transport error."""
        assert status is not None or invalid_json or transport
        self._faults.append(_Fault(method.upper(), re.compile(path), status,
                                   invalid_json, transport, times))

    def add_subnetwork(self, region: str, name: str, ip_cidr_range: str,
                       network: str = 'lab-network') -> dict:
        subnetwork = self._new_subnetwork(region, {
            'name': name, 'ipCidrRange': ip_cidr_range,
            'network': f'projects/{self.project}/global/networks/{network}'})
        self.subnetworks[(region, name)] = subnetwork
        return subnetwork

    def add_instance(self, zone: str, name: str, *, labels: dict,
                     subnetwork: Tuple[str, str], description: str = '',
                     status: str = 'RUNNING') -> dict:
        """An instance some other actor created, written straight to state."""
        instance = self._new_instance(zone, {
            'name': name, 'description': description, 'labels': dict(labels),
            'machineType': f'zones/{zone}/machineTypes/e2-small',
            'networkInterfaces': [{'subnetwork': (
                f'projects/{self.project}/regions/{subnetwork[0]}/subnetworks/{subnetwork[1]}')}],
            'tags': {'items': []}})
        instance['status'] = status
        self.instances[(zone, name)] = instance
        return instance

    # requests.Session surface

    def request(self, method, url, params=None, data=None, headers=None,
                json=None, timeout=None, **kwargs):
        method = method.upper()
        assert url.startswith(API_ROOT), f'not a Compute v1 URL: {url}'
        parts = urlsplit(url)
        # Query values travel as `params`, never concatenated into the URL.
        assert not parts.query, f'query string concatenated into {url}'
        assert data is None, 'Compute bodies are JSON'
        segments = [unquote(s) for s in url[len(API_ROOT):].split('/')]
        assert segments[0] == self.project, f'wrong project in {url}'
        relative = segments[1:]
        path = '/'.join(relative)
        for fault in self._faults:
            if fault.remaining and fault.method == method and fault.path.fullmatch(path):
                fault.remaining -= 1
                if fault.transport:
                    raise requests.ConnectionError(f'injected transport failure: {url}')
                if fault.invalid_json:
                    return FakeResponse(200, url=url, invalid_json=True)
                return _error(fault.status, 'injected', 'injected failure', url)
        params = dict(params or {})
        return self._route(method, relative, path, params, json, url)

    # Routing

    def _route(self, method, rel, path, params, body, url) -> FakeResponse:
        match (method, rel):
            case ('POST', ['zones', zone, 'instances']):
                return self._mutation(
                    method, path, params, url, ('zones', zone), 'insert',
                    self._instance_link(zone, (body or {}).get('name')),
                    lambda: self._validate_instance_insert(zone, body, url),
                    lambda: self._insert_instance(zone, body),
                    refuse=lambda: self._missing_subnetwork(body))
            case ('GET', ['zones', zone, 'instances']):
                return self._list([v for (z, _), v in self.instances.items() if z == zone],
                                  params, url)
            case ('GET', ['aggregated', 'instances']):
                return self._aggregated_instances(params, url)
            case ('GET', ['zones', zone, 'instances', name]):
                return self._get(self.instances, (zone, name), url)
            case ('DELETE', ['zones', zone, 'instances', name]):
                return self._mutation(
                    method, path, params, url, ('zones', zone), 'delete',
                    self._instance_link(zone, name),
                    lambda: self._absent(self.instances, (zone, name), url),
                    lambda: self.instances.pop((zone, name)))
            case ('POST', ['zones', zone, 'instances', name, 'setLabels']):
                return self._mutation(
                    method, path, params, url, ('zones', zone), 'setLabels',
                    self._instance_link(zone, name),
                    lambda: self._validate_set_labels(zone, name, body, url),
                    lambda: self._set_labels(zone, name, body))
            case ('POST', ['zones', zone, 'instances', name, ('start' | 'stop') as action]):
                return self._mutation(
                    method, path, params, url, ('zones', zone), action,
                    self._instance_link(zone, name),
                    lambda: self._absent(self.instances, (zone, name), url),
                    lambda: self._set_status(zone, name, action))
            case ('POST', ['zones', zone, 'operations', name, 'wait']):
                return self._wait(('zones', zone, name), url)
            case ('POST', ['regions', region, 'operations', name, 'wait']):
                return self._wait(('regions', region, name), url)
            case ('POST', ['global', 'operations', name, 'wait']):
                return self._wait(('global', None, name), url)
            case ('POST', ['regions', region, 'subnetworks']):
                return self._mutation(
                    method, path, params, url, ('regions', region), 'insert',
                    self._subnetwork_link(region, (body or {}).get('name')),
                    lambda: self._validate_subnetwork_insert(region, body, url),
                    lambda: self._insert_subnetwork(region, body))
            case ('GET', ['regions', region, 'subnetworks']):
                return self._list([v for (r, _), v in self.subnetworks.items() if r == region],
                                  params, url)
            case ('GET', ['regions', region, 'subnetworks', name]):
                return self._get(self.subnetworks, (region, name), url)
            case ('DELETE', ['regions', region, 'subnetworks', name]):
                return self._mutation(
                    method, path, params, url, ('regions', region), 'delete',
                    self._subnetwork_link(region, name),
                    lambda: self._absent(self.subnetworks, (region, name), url),
                    lambda: self.subnetworks.pop((region, name)),
                    refuse=lambda: self._subnetwork_in_use(region, name))
            case ('POST', ['global', 'firewalls']):
                return self._mutation(
                    method, path, params, url, ('global', None), 'insert',
                    self._firewall_link((body or {}).get('name')),
                    lambda: self._validate_firewall_insert(body, url),
                    lambda: self._insert_firewall(body))
            case ('GET', ['global', 'firewalls']):
                return self._list(list(self.firewalls.values()), params, url)
            case ('GET', ['global', 'firewalls', name]):
                return self._get(self.firewalls, name, url)
            case ('DELETE', ['global', 'firewalls', name]):
                return self._mutation(
                    method, path, params, url, ('global', None), 'delete',
                    self._firewall_link(name),
                    lambda: self._absent(self.firewalls, name, url),
                    lambda: self.firewalls.pop(name))
        raise AssertionError(f'FakeGCESession has no route for {method} {path}')

    # Links

    def _project_link(self) -> str:
        return f'{LINK_ROOT}{self.project}'

    def _instance_link(self, zone, name) -> str:
        return f'{self._project_link()}/zones/{zone}/instances/{name}'

    def _subnetwork_link(self, region, name) -> str:
        return f'{self._project_link()}/regions/{region}/subnetworks/{name}'

    def _firewall_link(self, name) -> str:
        return f'{self._project_link()}/global/firewalls/{name}'

    def _fingerprint(self) -> str:
        return f'fp-{next(self._ids)}'

    # Generic reads

    def _get(self, table, key, url) -> FakeResponse:
        if key not in table:
            return _error(404, 'notFound', f'The resource {url!r} was not found', url)
        return FakeResponse(200, table[key], url=url)

    def _absent(self, table, key, url) -> Optional[FakeResponse]:
        if key not in table:
            return _error(404, 'notFound', f'The resource {url!r} was not found', url)
        return None

    def _select(self, items: List[dict], params: dict, url: str):
        """The `items` that pass the request's filter; a FakeResponse when
        the filter cannot be parsed."""
        expression = params.get('filter')
        if expression and not self.loose_filter:
            clauses = _parse_filter(expression)
            if clauses is None:
                return _error(400, 'invalid', f'Invalid list filter expression {expression!r}', url)
            items = [item for item in items if _matches(item, clauses)]
        return items

    def _page(self, items: List, params: dict) -> Tuple[List, Optional[str]]:
        start = int(params.get('pageToken') or 0)
        size = self.page_size or len(items) or 1
        if params.get('maxResults') is not None:
            size = min(size, int(params['maxResults']))
        token = str(start + size) if start + size < len(items) else None
        return items[start:start + size], token

    def _list(self, items: List[dict], params: dict, url: str) -> FakeResponse:
        items = self._select(items, params, url)
        if isinstance(items, FakeResponse):
            return items
        page, token = self._page(sorted(items, key=lambda item: item.get('name', '')), params)
        body = {'kind': 'compute#list', 'id': url}
        if page:
            body['items'] = page  # Compute omits `items` on an empty page
        if token is not None:
            body['nextPageToken'] = token
        return FakeResponse(200, body, url=url)

    def _aggregated_instances(self, params: dict, url: str) -> FakeResponse:
        reachable = [instance for (zone, _), instance in self.instances.items()
                     if zone not in self.unreachable_zones]
        items = self._select(reachable, params, url)
        if isinstance(items, FakeResponse):
            return items
        ordered = sorted(items, key=lambda item: (item['zone'], item.get('name', '')))
        page, token = self._page(ordered, params)
        zones = self.known_zones | {zone for zone, _ in self.instances}
        scopes = {}
        for zone in sorted(zones):
            scope = f'zones/{zone}'
            if zone in self.unreachable_zones:
                if 'warning' in self.unreachable_reports:
                    scopes[scope] = {'warning': {
                        'code': 'UNREACHABLE', 'message': f'{scope} is unreachable',
                        'data': [{'key': 'scope', 'value': scope}]}}
                continue
            instances = [item for item in page if item['zone'].endswith('/' + scope)]
            scopes[scope] = {'instances': instances} if instances else {'warning': {
                'code': 'NO_RESULTS_ON_PAGE',
                'message': 'There are no results for scope on this page.',
                'data': [{'key': 'scope', 'value': scope}]}}
        body = {'kind': 'compute#instanceAggregatedList', 'id': url, 'items': scopes}
        if self.unreachable_zones and 'unreachables' in self.unreachable_reports:
            body['unreachables'] = sorted(f'zones/{zone}' for zone in self.unreachable_zones)
        if token is not None:
            body['nextPageToken'] = token
        return FakeResponse(200, body, url=url)

    # Mutations and operations

    def _mutation(self, method, path, params, url, scope, operation_type, target_link,
                  validate, apply, refuse=None) -> FakeResponse:
        request_id = params.get('requestId')
        if request_id is not None:
            try:
                parsed = uuid.UUID(request_id)
            except (TypeError, ValueError):
                parsed = None
            if parsed is None or parsed.int == 0 or str(parsed) != request_id:
                return _error(400, 'invalid', f'Invalid requestId {request_id!r}', url)
            replay = self.request_results.get((method, path, request_id))
            if replay is not None:
                return FakeResponse(200, self.operations[replay], url=url)
        refusal = validate()
        if refusal is not None:
            return refusal
        location = scope[1]
        name = f'operation-{next(self._ids)}-{uuid.uuid4().hex[:12]}'
        operation = {
            'kind': 'compute#operation', 'id': str(next(self._ids)), 'name': name,
            'operationType': operation_type, 'targetLink': target_link,
            'status': 'DONE', 'progress': 100,
            'selfLink': f'{self._project_link()}/{scope[0]}/'
                        + (f'{location}/' if location else '') + f'operations/{name}',
        }
        if scope[0] == 'zones':
            operation['zone'] = f'{self._project_link()}/zones/{location}'
        elif scope[0] == 'regions':
            operation['region'] = f'{self._project_link()}/regions/{location}'
        key = (scope[0], location, name)
        errors = self.next_operation_error
        if errors is None and refuse is not None:
            errors = refuse()
        if errors is not None:
            self.next_operation_error = None
            operation['error'] = {'errors': errors if isinstance(errors, list) else [errors]}
            operation['httpErrorStatusCode'] = 400
            operation['httpErrorMessage'] = 'BAD REQUEST'
        elif self.stall_operations:
            operation['status'] = 'RUNNING'
            operation['progress'] = 0
            self._pending[key] = apply
        else:
            apply()
        self.operations[key] = operation
        if request_id is not None:
            self.request_results[(method, path, request_id)] = key
        return FakeResponse(200, operation, url=url)

    def _wait(self, key, url) -> FakeResponse:
        operation = self.operations.get(key)
        if operation is None:
            return _error(404, 'notFound', f'The resource {url!r} was not found', url)
        if operation['status'] != 'DONE' and not self.stall_operations:
            self._pending.pop(key)()
            operation['status'], operation['progress'] = 'DONE', 100
        return FakeResponse(200, operation, url=url)

    # Instances

    def _validate_instance_insert(self, zone, body, url) -> Optional[FakeResponse]:
        if not isinstance(body, dict) or not _NAME.fullmatch(body.get('name') or ''):
            return _error(400, 'invalid', 'Invalid value for field resource.name', url)
        if not str(body.get('machineType', '')).startswith(f'zones/{zone}/machineTypes/'):
            return _error(400, 'invalid', 'Invalid value for field resource.machineType', url)
        if not _valid_labels(body.get('labels', {})):
            return _error(400, 'invalid', 'Invalid value for field resource.labels', url)
        tags = (body.get('tags') or {}).get('items', [])
        if not all(isinstance(t, str) and _NAME.fullmatch(t) for t in tags):
            return _error(400, 'invalid', 'Invalid value for field resource.tags', url)
        disks = body.get('disks') or []
        if len([d for d in disks if d.get('boot')]) != 1:
            return _error(400, 'invalid', 'Exactly one boot disk is required', url)
        interfaces = body.get('networkInterfaces') or []
        if not interfaces:
            return _error(400, 'invalid', 'A network interface is required', url)
        missing = self._missing_subnetwork(body)
        if missing is not None and self.subnetwork_failure == 'sync':
            return _error(404, 'notFound', missing['message'], url)
        if (zone, body['name']) in self.instances:
            return _error(409, 'alreadyExists',
                          f'The resource {self._instance_link(zone, body["name"])!r} already exists',
                          url)
        return None

    def _missing_subnetwork(self, body) -> Optional[dict]:
        """The operation error for a NIC whose subnetwork does not exist."""
        for interface in (body or {}).get('networkInterfaces') or []:
            link = str(interface.get('subnetwork', ''))
            match = _SUBNETWORK_PATH.fullmatch(link)
            if not match or tuple(match.groups()) not in self.subnetworks:
                return {'code': 'RESOURCE_NOT_FOUND',
                        'message': f"The resource '{link}' was not found"}
        return None

    def _new_instance(self, zone, body) -> dict:
        instance = copy.deepcopy(body)
        instance.update({
            'kind': 'compute#instance', 'id': str(next(self._ids)),
            'status': 'RUNNING', 'creationTimestamp': CREATION_TIMESTAMP,
            'zone': f'{self._project_link()}/zones/{zone}',
            'selfLink': self._instance_link(zone, body['name']),
            'labels': dict(body.get('labels') or {}),
            'labelFingerprint': self._fingerprint(),
        })
        instance['tags'] = dict(instance.get('tags') or {}, fingerprint=self._fingerprint())
        for index, interface in enumerate(instance.get('networkInterfaces') or []):
            region, subnet = _SUBNETWORK_PATH.fullmatch(interface['subnetwork']).groups()
            subnetwork = self.subnetworks[(region, subnet)]
            host = self._next_host.get((region, subnet), 2)
            self._next_host[(region, subnet)] = host + 1
            interface.update({
                'name': f'nic{index}', 'network': subnetwork['network'],
                'networkIP': str(ipaddress.ip_network(subnetwork['ipCidrRange'])[host])})
            for config_index, access in enumerate(interface.get('accessConfigs') or []):
                # A public address (TEST-NET-3) only if the body asked for one.
                access['natIP'] = f'203.0.113.{10 + index * 10 + config_index}'
        return instance

    def _insert_instance(self, zone, body) -> None:
        self.instances[(zone, body['name'])] = self._new_instance(zone, body)

    def _validate_set_labels(self, zone, name, body, url) -> Optional[FakeResponse]:
        instance = self.instances.get((zone, name))
        if instance is None:
            return _error(404, 'notFound', f'The resource {url!r} was not found', url)
        if self.label_conflicts_remaining > 0:
            # A concurrent writer won the race since the caller's read.
            self.label_conflicts_remaining -= 1
            instance['labels'].update(self.concurrent_labels)
            instance['labelFingerprint'] = self._fingerprint()
        if not isinstance(body, dict) or body.get('labelFingerprint') != instance['labelFingerprint']:
            return _error(412, 'conditionNotMet',
                          'Labels fingerprint either invalid or resource labels have changed',
                          url)
        if not _valid_labels(body.get('labels')):
            return _error(400, 'invalid', 'Invalid value for field labels', url)
        return None

    def _set_labels(self, zone, name, body) -> None:
        instance = self.instances[(zone, name)]
        instance['labels'] = dict(body['labels'])
        instance['labelFingerprint'] = self._fingerprint()

    def _set_status(self, zone, name, action) -> None:
        self.instances[(zone, name)]['status'] = 'RUNNING' if action == 'start' else 'TERMINATED'

    # Subnetworks

    def _validate_subnetwork_insert(self, region, body, url) -> Optional[FakeResponse]:
        if not isinstance(body, dict) or not _NAME.fullmatch(body.get('name') or ''):
            return _error(400, 'invalid', 'Invalid value for field resource.name', url)
        try:
            cidr = ipaddress.ip_network(body.get('ipCidrRange') or '')
        except ValueError:
            return _error(400, 'invalid', 'Invalid value for field resource.ipCidrRange', url)
        if not _NETWORK_PATH.fullmatch(str(body.get('network', ''))):
            return _error(400, 'invalid', 'Invalid value for field resource.network', url)
        if (region, body['name']) in self.subnetworks:
            return _error(409, 'alreadyExists',
                          f'The resource {self._subnetwork_link(region, body["name"])!r} '
                          'already exists', url)
        network = _NETWORK_PATH.fullmatch(body['network']).group(1)
        for existing in self.subnetworks.values():
            if (_NETWORK_PATH.fullmatch(existing['network']).group(1) == network
                    and ipaddress.ip_network(existing['ipCidrRange']).overlaps(cidr)):
                return _error(400, 'invalid',
                              f'Invalid IPCidrRange: {cidr} conflicts with existing '
                              f'subnetwork {existing["name"]!r}', url)
        return None

    def _new_subnetwork(self, region, body) -> dict:
        subnetwork = copy.deepcopy(body)
        subnetwork.update({
            'kind': 'compute#subnetwork', 'id': str(next(self._ids)),
            'creationTimestamp': CREATION_TIMESTAMP,
            'region': f'{self._project_link()}/regions/{region}',
            'selfLink': self._subnetwork_link(region, body['name']),
            'gatewayAddress': str(ipaddress.ip_network(body['ipCidrRange'])[1]),
            'fingerprint': self._fingerprint(),
        })
        return subnetwork

    def _insert_subnetwork(self, region, body) -> None:
        self.subnetworks[(region, body['name'])] = self._new_subnetwork(region, body)

    def _subnetwork_in_use(self, region, name):
        for (zone, instance_name), instance in self.instances.items():
            for interface in instance.get('networkInterfaces') or []:
                match = _SUBNETWORK_PATH.fullmatch(str(interface.get('subnetwork', '')))
                if match and match.groups() == (region, name):
                    return {'code': 'RESOURCE_IN_USE_BY_ANOTHER_RESOURCE',
                            'message': f"The subnetwork resource '{name}' is already being "
                                       f"used by '{self._instance_link(zone, instance_name)}'"}
        return None

    # Firewalls

    def _validate_firewall_insert(self, body, url) -> Optional[FakeResponse]:
        if not isinstance(body, dict) or not _NAME.fullmatch(body.get('name') or ''):
            return _error(400, 'invalid', 'Invalid value for field resource.name', url)
        if not _NETWORK_PATH.fullmatch(str(body.get('network', ''))):
            return _error(400, 'invalid', 'Invalid value for field resource.network', url)
        if body['name'] in self.firewalls:
            return _error(409, 'alreadyExists',
                          f'The resource {self._firewall_link(body["name"])!r} already exists',
                          url)
        return None

    def _insert_firewall(self, body) -> None:
        firewall = copy.deepcopy(body)
        firewall.update({
            'kind': 'compute#firewall', 'id': str(next(self._ids)),
            'creationTimestamp': CREATION_TIMESTAMP,
            'selfLink': self._firewall_link(body['name']),
        })
        self.firewalls[body['name']] = firewall
