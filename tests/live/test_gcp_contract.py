"""Contract checks against real Compute Engine, through the app's GCPBackend.

The unit suite runs GCPBackend against a fake REST session, which models the
public schema but is not evidence of the deployed API: its filter parser, its
warning codes, how long /wait blocks, which refusals are immediate, and what a
reused requestId returns. These tests check exactly those. Opt-in only
(LIVE_GCP=1): see tests/live/README.md. Every resource is created and removed
through CloudContractRun (tests/live/cloud_harness.py), in a dedicated live
site with a cap of one.

The network and router checks use the documented VMBackend methods
(ensure_slot, release_slot, cleanup_deployment, ensure_router_running,
stop_router), which GCPBackend gains with its network support.

Nothing here imports a provider module at collection time: a skipped run
must never build a client.
"""

import ipaddress
import time
import uuid
import warnings
from datetime import timedelta
from urllib.parse import quote

import pytest
import requests

from app.backends.base import (
    GUEST_ABSENT, GUEST_MATCH, AdmissionRefused, BackendError, DestroyPolicy,
    SlotNetworkMissing, VMRef,
)
from app.backends.cloud_common import (
    TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT, TAG_VMID,
    deployment_network_name, expires_epoch, instance_name, site_prefix,
    slot_network_name,
)
from app.backends.network_slots import cloud_slot_cidr
from tests.live.cloud_harness import EXPIRY, LIVE_SLOT, CloudContractRun

pytestmark = [pytest.mark.live, pytest.mark.live_gcp]

COMPUTE_ROOT = 'https://compute.googleapis.com/compute/v1/projects/'
COMPUTE_SCOPE = 'https://www.googleapis.com/auth/compute'
# A slot no run ever ensures: preflight proves the site owns no subnetwork.
MISSING_SLOT = LIVE_SLOT + 1
OPERATION_DEADLINE_SECONDS = 600


class LiveObservation(UserWarning):
    """What the real API answered, kept in the warnings summary as evidence."""


def _observe(message):
    warnings.warn(message, LiveObservation, stacklevel=2)


class ComputeProbe:
    """A Compute v1 REST client of its own, for what the backend does not
    expose. Google ADC; nothing is echoed."""

    def __init__(self, project):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=[COMPUTE_SCOPE])
        self._session = AuthorizedSession(credentials)
        self._root = COMPUTE_ROOT + quote(project, safe='') + '/'

    def call(self, method, *path, params=None, body=None, timeout=60):
        url = self._root + '/'.join(quote(str(part), safe='') for part in path)
        return self._session.request(method, url, params=params, json=body,
                                     timeout=timeout)

    def json(self, method, *path, **kwargs):
        response = self.call(method, *path, **kwargs)
        response.raise_for_status()
        return response.json()

    def aggregated_pages(self, filter_expression):
        pages, token = [], None
        while True:
            params = {'filter': filter_expression}
            if token:
                params['pageToken'] = token
            page = self.json('GET', 'aggregated', 'instances', params=params)
            pages.append(page)
            token = page.get('nextPageToken')
            if not token:
                return pages

    def instances(self, filter_expression):
        """(zone, instance) for every instance the aggregated list returns."""
        return [(scope.rsplit('/', 1)[-1], instance)
                for page in self.aggregated_pages(filter_expression)
                for scope, scoped in (page.get('items') or {}).items()
                for instance in scoped.get('instances') or []]

    @staticmethod
    def scope(operation):
        for key, collection in (('zone', 'zones'), ('region', 'regions')):
            if operation.get(key):
                return (collection, operation[key].rsplit('/', 1)[-1])
        return ('global',)

    def wait(self, operation, *, timeout):
        return self.json('POST', *self.scope(operation), 'operations', operation['name'],
                         'wait', timeout=timeout)

    def wait_done(self, operation, *, read_timeout=150):
        """Wait until DONE; return the final operation and, per /wait call,
        (returned status, seconds it blocked)."""
        deadline = time.monotonic() + OPERATION_DEADLINE_SECONDS
        calls = []
        while operation.get('status') != 'DONE':
            assert time.monotonic() < deadline, f'operation never finished: {operation}'
            started = time.monotonic()
            operation = self.wait(operation, timeout=read_timeout)
            calls.append((operation.get('status'), time.monotonic() - started))
        return operation, calls


@pytest.fixture(scope='session')
def compute(gcp_live_config):
    return ComputeProbe(gcp_live_config.settings['GCP_PROJECT'])


def _managed(backend, kind):
    return [r for r in backend.list_managed() if r.kind == kind]


def _insert_body(config, spec, location, *, slot, image):
    """The instance the backend's create would insert, labelled so that
    anything it creates is inventory the run's teardown removes."""
    site, settings = config.site_id, config.settings
    return {
        'name': instance_name(site, spec.vmid),
        'description': spec.vm_name,
        'machineType': f'zones/{location}/machineTypes/{spec.template["machine_type"]}',
        'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
            'sourceImage': image, 'diskSizeGb': str(spec.template['disk_gb'])}}],
        'networkInterfaces': [{'subnetwork': (
            f'projects/{settings["GCP_PROJECT"]}/regions/{settings["GCP_REGION"]}'
            f'/subnetworks/{slot_network_name(site, slot)}')}],
        'labels': {TAG_SITE: site, TAG_VMID: str(spec.vmid),
                   TAG_DEPLOYMENT: spec.deployment_id,
                   TAG_EXPIRES: str(expires_epoch(spec.expires_at)),
                   TAG_SLOT: str(slot), TAG_KIND: 'instance'},
    }


def _wait_cap():
    from app.backends import gcp
    return gcp._WAIT_READ_TIMEOUT_CAP_SECONDS


def test_lifecycle_against_real_compute(gcp_backend, gcp_live_config):
    settings = gcp_live_config.settings
    with CloudContractRun(gcp_backend, gcp_live_config) as run:
        spec = run.new_spec()
        location = run.admit(spec)
        assert location == settings['GCP_ZONE']
        run.ensure_router()
        run.ensure_slot(spec.network_slot)
        ref = run.create(spec, location)

        assert gcp_backend.inspect(ref) == GUEST_MATCH
        assert gcp_backend.status(ref) == 'running'
        ips = gcp_backend.ip_addresses(ref)
        slot_cidr = ipaddress.ip_network(
            cloud_slot_cidr(settings['GCP_LAB_CIDR'], spec.network_slot))
        assert ips, 'a running instance reports no private address'
        for ip in ips:
            assert ipaddress.ip_address(ip).is_private, ips
            assert ipaddress.ip_address(ip) in slot_cidr, (ips, slot_cidr)

        [row] = _managed(gcp_backend, 'instance')
        assert (row.provider_id, row.vmid, row.name) == (ref.provider_id, spec.vmid, spec.vm_name)
        assert row.expires_at_epoch == expires_epoch(spec.expires_at)
        # Earlier, never later, than the 30-minute expiry it launched with.
        sooner = run.now() + EXPIRY - timedelta(minutes=5)
        gcp_backend.set_expiry(ref, sooner)
        [row] = _managed(gcp_backend, 'instance')
        assert row.expires_at_epoch == expires_epoch(sooner)

        with pytest.raises(AdmissionRefused):
            run.admit(run.new_spec())
    assert gcp_backend.list_managed() == []


def test_deployment_firewall_is_accepted_unchanged(gcp_run, compute, gcp_live_config):
    # The fake keeps what it is sent; the project's API could normalise or
    # refuse an all-protocol rule that names only source tags.
    ref = gcp_run.launch_one()
    name = deployment_network_name(gcp_live_config.site_id, ref.deployment_id)
    firewall = compute.json('GET', 'global', 'firewalls', name)

    assert firewall['network'].endswith(
        f'/global/networks/{gcp_live_config.settings["GCP_NETWORK"]}'), firewall['network']
    assert firewall['direction'] == 'INGRESS'
    assert firewall['priority'] == 1000
    assert firewall['sourceTags'] == [name]
    assert firewall['targetTags'] == [name]
    assert firewall['allowed'] == [{'IPProtocol': 'all'}]
    assert 'sourceRanges' not in firewall and 'destinationRanges' not in firewall, firewall


def test_aggregated_list_name_filter_is_exact_and_literal(gcp_run, gcp_backend, compute,
                                                          gcp_live_config):
    # Discovery proves absence from this filter, so a prefix or pattern match
    # would read a namesake as this instance, and a parse quirk as "absent".
    ref = gcp_run.launch_one()
    name = ref.provider_id

    assert [(zone, i['name']) for zone, i in compute.instances(f'(name = "{name}")')] == [
        (ref.location, name)]
    for near in (name[:-1], f'{name}0', f'{name[:-1]}.*', f'{name[:-2]}*'):
        found = [i['name'] for _, i in compute.instances(f'(name = "{near}")')]
        assert name not in found, (near, found)
    # A hyphenated label key parses unquoted, as list_managed writes it.
    labelled = compute.instances(f'(labels.{TAG_SITE} = "{gcp_live_config.site_id}")')
    assert name in [i['name'] for _, i in labelled]

    assert gcp_backend.inspect(ref) == GUEST_MATCH
    stranger = VMRef(vmid=ref.vmid + 1000, vm_name=ref.vm_name, vm_type=ref.vm_type,
                     location=ref.location, backend=ref.backend)
    assert gcp_backend.inspect(stranger) == GUEST_ABSENT


def test_aggregated_list_reports_only_no_results_on_page_as_benign(gcp_backend, compute,
                                                                   gcp_live_config):
    # The backend treats every warning but NO_RESULTS_ON_PAGE, and any
    # `unreachables`, as an incomplete answer. Compute cannot be made to drop
    # a zone on demand; this records what it really sends. A zone unreachable
    # during the run fails here with its exact shape, which is the evidence of
    # how an unreachable zone is reported.
    pages = compute.aggregated_pages(f'(labels.{TAG_SITE} = "{gcp_live_config.site_id}")')
    unreachables = [u for page in pages for u in page.get('unreachables') or []]
    codes = {page['warning'].get('code') for page in pages if page.get('warning')}
    codes |= {scoped['warning'].get('code') for page in pages
              for scoped in (page.get('items') or {}).values() if scoped.get('warning')}
    _observe(f'aggregated list: {len(pages)} page(s), warning codes {sorted(codes)}, '
             f'unreachables {unreachables}')

    assert unreachables == [], unreachables
    assert codes <= {'NO_RESULTS_ON_PAGE'}, codes
    assert 'NO_RESULTS_ON_PAGE' in codes, 'no empty zone was reported with the expected code'
    gcp_backend.list_managed()  # the backend accepts the same answer


def test_wait_may_return_an_unfinished_operation(gcp_run, gcp_backend, compute):
    # The backend loops while /wait answers an operation that is not DONE,
    # in the scope the operation names, and bounds each call by its read cap.
    ref = gcp_run.launch_one()
    operation = compute.json('POST', 'zones', ref.location, 'instances', ref.provider_id,
                             'stop', params={'requestId': str(uuid.uuid4())})
    initial = operation.get('status')
    cap = _wait_cap()
    final, calls = compute.wait_done(operation, read_timeout=cap)
    _observe(f'stop operation: started {initial!r}; /wait returned {calls}')

    assert final['name'] == operation['name']
    assert final.get('error') is None, final['error']
    assert all(seconds < cap for _, seconds in calls), calls
    if not any(status != 'DONE' for status, _ in calls):
        _observe('/wait always returned DONE: an unfinished /wait answer was not observed')
    assert gcp_backend.status(ref) == 'stopped'


def test_read_timeout_while_waiting_fails_the_call_and_a_retry_completes(
        gcp_run, gcp_backend, monkeypatch):
    # A client-side timeout must never read as success or absence, and the
    # retry (same requestId, same instance id) must finish the same delete.
    from app.backends import gcp
    ref = gcp_run.launch_one()
    monkeypatch.setattr(gcp, '_WAIT_READ_TIMEOUT_CAP_SECONDS', 0.5)
    try:
        gcp_backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    except BackendError as error:
        assert isinstance(error.__cause__, requests.Timeout), error
    else:
        pytest.skip('the delete finished before its first /wait: no read timeout occurred')
    monkeypatch.undo()

    gcp_backend.destroy(ref, DestroyPolicy.BEST_EFFORT)

    assert gcp_backend.confirm_absent(ref)


def test_insert_into_a_missing_subnetwork_is_refused_and_creates_nothing(
        gcp_run, gcp_backend, compute, gcp_live_config):
    # create_vm re-ensures a slot and retries once only on SlotNetworkMissing,
    # which the backend raises from its own subnetwork read before any
    # insert. The raw insert records how Compute itself refuses one that
    # races a release: an immediate 4xx (retry-safe), or an operation that
    # fails later (whose requestId would replay the failure).
    spec = gcp_run.new_spec(slot=MISSING_SLOT)
    location = gcp_live_config.settings['GCP_ZONE']
    gcp_run.claim(spec, location)
    body = _insert_body(gcp_live_config, spec, location, slot=MISSING_SLOT,
                        image=spec.template['image'])
    response = compute.call('POST', 'zones', location, 'instances',
                            params={'requestId': str(uuid.uuid4())}, body=body)
    if response.ok:
        final, _ = compute.wait_done(response.json())
        _observe(f'insert into a missing subnetwork was NOT refused immediately: operation '
                 f'{final.get("name")} finished with {final.get("error")}')
        assert final.get('error'), f'an insert into a missing subnetwork succeeded: {final}'
    else:
        _observe(f'insert into a missing subnetwork: immediate HTTP {response.status_code} '
                 f'{response.text[:300]}')
        assert 400 <= response.status_code < 500, response.text
    assert compute.instances(f'(name = "{body["name"]}")') == []

    with pytest.raises(SlotNetworkMissing):
        gcp_run.create(spec, location)
    assert gcp_backend.list_managed() == []


def test_request_id_reuse_after_a_failed_operation_replays_the_failure(
        gcp_run, compute, gcp_live_config):
    # The backend advances an insert's requestId only once a failure is
    # final and absence is proven, because a reused id returns the old result.
    spec = gcp_run.new_spec()
    location = gcp_live_config.settings['GCP_ZONE']
    gcp_run.ensure_slot(spec.network_slot)
    gcp_run.claim(spec, location)
    missing_image = (f'projects/{gcp_live_config.settings["GCP_PROJECT"]}/global/images/'
                     f'{site_prefix(gcp_live_config.site_id)}missing-image')
    body = _insert_body(gcp_live_config, spec, location, slot=spec.network_slot,
                        image=missing_image)
    request_id = str(uuid.uuid4())

    def insert(rid):
        return compute.call('POST', 'zones', location, 'instances',
                            params={'requestId': rid}, body=body)

    first = insert(request_id)
    if not first.ok:
        again = insert(request_id)
        _observe(f'insert with a missing image: HTTP {first.status_code}, reused id '
                 f'HTTP {again.status_code}')
        pytest.skip(f'Compute refused the insert immediately (HTTP {first.status_code}); '
                    'no failed operation exists to replay')
    failed, _ = compute.wait_done(first.json())
    assert failed.get('error'), f'an insert from a missing image succeeded: {failed}'

    replay = insert(request_id)
    replay.raise_for_status()
    replayed, _ = compute.wait_done(replay.json())
    _observe(f'reused requestId: operation {replayed.get("name")} (first {failed.get("name")}), '
             f'error {replayed.get("error")}')
    assert (replayed['name'], replayed.get('id')) == (failed['name'], failed.get('id'))
    assert replayed.get('error') == failed['error']

    fresh = insert(str(uuid.uuid4()))
    fresh.raise_for_status()
    retried, _ = compute.wait_done(fresh.json())
    assert retried['name'] != failed['name'] and retried.get('error')
    assert compute.instances(f'(name = "{body["name"]}")') == []


def test_router_is_discovered_by_its_labels_but_never_listed_as_managed(
        gcp_run, gcp_backend, compute, gcp_live_config):
    # The backend finds the router by name project-wide and trusts it only
    # with the site and router labels; inventory must never offer it for
    # deletion although it carries the site label.
    site, router = gcp_live_config.site_id, gcp_live_config.settings['GCP_ROUTER_INSTANCE']
    [(zone, instance)] = compute.instances(f'(name = "{router}")')
    labels = instance.get('labels') or {}
    assert (labels.get(TAG_SITE), labels.get(TAG_KIND)) == (site, 'router'), labels
    labelled = compute.instances(f'(labels.{TAG_SITE} = "{site}")')
    assert (zone, router) in [(z, i['name']) for z, i in labelled]

    gcp_run.ensure_router()

    assert compute.json('GET', 'zones', zone, 'instances', router)['status'] == 'RUNNING'
    managed = gcp_backend.list_managed()
    assert router not in [r.provider_id for r in managed] + [r.name for r in managed]
