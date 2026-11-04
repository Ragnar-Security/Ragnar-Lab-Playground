"""Safety guards of the opt-in live AWS and GCP contract runs (tests/live).

The live runs launch and delete real cloud resources. Every guard that keeps
them inside their dedicated site, and every teardown promise, is verified
here against the in-memory backend, so CI proves the safety logic without a
cloud account, an SDK client or a network call.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.backends.base import BackendError, DestroyPolicy
from tests.backends.fake_backend import FakeBackend
from tests.live.cloud_harness import (
    CloudCleanupError,
    CloudContractRun,
    CloudLiveConfig,
    UnsafeLiveRun,
    cloud_live_skip_reason,
    is_missing_resource_refusal,
    required_env,
    select_live_skip_reason,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 11, 4, 9, 0, 0)


class InjectedFailure(Exception):
    pass


def aws_env(**overrides):
    env = {
        'LIVE_AWS': '1', 'ENABLED_BACKENDS': 'aws', 'LAB_SITE_ID': 'live-test',
        'AWS_MAX_VMS': '1', 'AWS_REGION': 'us-test-1',
        'AWS_AVAILABILITY_ZONE': 'us-test-1a', 'AWS_LAB_VPC_ID': 'vpc-placeholder',
        'AWS_LAB_CIDR': '172.20.0.0/16', 'AWS_ROUTER_INSTANCE_ID': 'i-placeholder',
        'LIVE_AWS_IMAGE_ID': 'ami-placeholder', 'LIVE_AWS_INSTANCE_TYPE': 't3.micro',
    }
    env.update(overrides)
    return env


def gcp_env(**overrides):
    env = {
        'LIVE_GCP': '1', 'ENABLED_BACKENDS': 'gcp', 'LAB_SITE_ID': 'live-test',
        'GCP_MAX_VMS': '1', 'GCP_PROJECT': 'example-project',
        'GCP_REGION': 'test-region1', 'GCP_ZONE': 'test-region1-a',
        'GCP_NETWORK': 'lab-vpc', 'GCP_LAB_CIDR': '172.21.0.0/16',
        'GCP_ROUTER_INSTANCE': 'lab-router',
        'LIVE_GCP_IMAGE': 'projects/example-project/global/images/placeholder',
        'LIVE_GCP_MACHINE_TYPE': 'e2-small',
    }
    env.update(overrides)
    return env


ENVS = {'aws': aws_env, 'gcp': gcp_env}
MARKERS = {'aws': 'live_aws', 'gcp': 'live_gcp'}


def fake_with_one_managed_resource():
    fake = FakeBackend('aws')
    fake.ensure_slot(7)
    [existing] = fake.list_managed()
    assert existing.kind == 'slot-network'
    return fake, existing


def _run(fake, backend='aws', **kwargs):
    return CloudContractRun(fake, CloudLiveConfig.from_env(backend, ENVS[backend]()),
                            now=lambda: FIXED_NOW, sleep=lambda _seconds: None, **kwargs)


# --- opt-in and collection gating ---------------------------------------------


@pytest.mark.parametrize(('backend', 'flag'), [('aws', 'LIVE_AWS'), ('gcp', 'LIVE_GCP')])
def test_cloud_live_run_is_skipped_without_explicit_opt_in(backend, flag):
    # Catches CI constructing a real cloud client during collection.
    assert f'{flag}=1' in cloud_live_skip_reason(backend, {})
    assert f'{flag}=1' in cloud_live_skip_reason(backend, ENVS[backend](**{flag: 'yes'}))


@pytest.mark.parametrize(('backend', 'name'), [
    (backend, name) for backend in ('aws', 'gcp')
    for name in required_env(backend)[1:]])
def test_cloud_live_run_is_skipped_when_required_env_is_missing(backend, name):
    # Catches an opted-in but half-configured run reaching a cloud anyway.
    env = ENVS[backend]()
    del env[name]
    reason = cloud_live_skip_reason(backend, env)
    assert reason is not None and name in reason
    assert select_live_skip_reason({'live', MARKERS[backend]}, env) == reason


@pytest.mark.parametrize('backend', ['aws', 'gcp'])
def test_cloud_live_run_needs_its_backend_enabled(backend):
    env = ENVS[backend](ENABLED_BACKENDS='proxmox')
    assert f'ENABLED_BACKENDS containing {backend}' in cloud_live_skip_reason(backend, env)
    assert cloud_live_skip_reason(backend, ENVS[backend](ENABLED_BACKENDS=f'proxmox, {backend.upper()}')) is None
    assert cloud_live_skip_reason(backend, ENVS[backend]()) is None


@pytest.mark.parametrize(('markers', 'skipped'), [
    ({'live', 'live_aws'}, False),
    ({'live', 'live_gcp'}, True),
    ({'live'}, True),
])
def test_cloud_marker_bypasses_the_proxmox_guard_only_for_its_provider(
        markers, skipped):
    # Catches LIVE_AWS=1 runs being skipped by the LIVE_PROXMOX guard (I15).
    env = aws_env()
    assert 'LIVE_PROXMOX' not in env
    assert (select_live_skip_reason(markers, env) is not None) is skipped


def test_unmarked_items_are_never_skipped_and_plain_live_keeps_the_proxmox_guard():
    assert select_live_skip_reason(set(), {}) is None
    assert select_live_skip_reason({'parametrize'}, {}) is None
    assert 'LIVE_PROXMOX=1' in select_live_skip_reason({'live'}, aws_env())


# A pytest run of the live cloud modules in a fresh interpreter in which every
# way of building a cloud client or sending HTTP raises and is counted.
_NO_CLIENT_PROBE = r'''
import sys
import boto3
import boto3.session
import google.auth
import requests

calls = []


def refusing(what):
    def refuse(*args, **kwargs):
        calls.append(what)
        raise AssertionError(what + ' was called while the live cloud run is skipped')
    return refuse


boto3.client = refusing('boto3.client')
boto3.resource = refusing('boto3.resource')
boto3.session.Session.client = refusing('boto3.session.Session.client')
boto3.session.Session.resource = refusing('boto3.session.Session.resource')
google.auth.default = refusing('google.auth.default')
requests.Session.request = refusing('requests.Session.request')

import pytest

status = pytest.main(sys.argv[1:])
loaded = [name for name in ('app.backends.aws', 'app.backends.gcp') if name in sys.modules]
print('CLOUD-PROBE calls=%d loaded=%s' % (len(calls), ','.join(loaded) or '-'))
sys.exit(status)
'''


def _skipped_live_cloud_run(extra_env):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('LIVE_', 'AWS_', 'GCP_', 'GOOGLE_', 'CLOUDSDK_'))}
    # Even a guard bug could find no credentials here.
    env.update({'AWS_CONFIG_FILE': os.devnull, 'AWS_SHARED_CREDENTIALS_FILE': os.devnull,
                'COLUMNS': '400'}, **extra_env)
    result = subprocess.run(
        [sys.executable, '-c', _NO_CLIENT_PROBE,
         'tests/live/test_aws_contract.py', 'tests/live/test_gcp_contract.py',
         '-q', '-rs', '-p', 'no:cacheprovider'],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert re.search(r'\d+ skipped', output), output
    assert not re.search(r'\d+ (passed|failed|error)', output), output
    assert 'CLOUD-PROBE calls=0 loaded=-' in output, output
    return output


def test_skipped_live_cloud_modules_build_no_client_and_import_no_backend():
    # Catches a fixture or module import reaching a cloud while CI collects.
    output = _skipped_live_cloud_run({})
    assert 'LIVE_AWS=1' in output and 'LIVE_GCP=1' in output


def test_opted_in_but_incomplete_cloud_runs_skip_on_their_own_guard_not_proxmox():
    # Catches the collection hook applying LIVE_PROXMOX to cloud items (I15),
    # and an opted-in run with a missing variable building a client anyway.
    env = {**aws_env(), **gcp_env(), 'ENABLED_BACKENDS': 'aws,gcp'}
    del env['LIVE_AWS_IMAGE_ID'], env['LIVE_GCP_MACHINE_TYPE']
    output = _skipped_live_cloud_run(env)
    assert 'live AWS contract run is missing: LIVE_AWS_IMAGE_ID' in output
    assert 'live GCP contract run is missing: LIVE_GCP_MACHINE_TYPE' in output
    assert 'LIVE_PROXMOX' not in output


# --- configuration --------------------------------------------------------------


def test_cloud_live_guard_requires_dedicated_site_cap_one_and_thirty_minute_expiry():
    with pytest.raises(ValueError, match='live-'):
        CloudLiveConfig.from_env('aws', aws_env(LAB_SITE_ID='production'))
    with pytest.raises(ValueError, match='AWS_MAX_VMS=1'):
        CloudLiveConfig.from_env('aws', aws_env(AWS_MAX_VMS='2'))
    assert CloudLiveConfig.from_env('aws', aws_env()).expiry == timedelta(minutes=30)


@pytest.mark.parametrize('site', ['live-', 'livex', 'prod-live-a', 'live-Upper',
                                  'live-' + 'x' * 16, 'live-trailing-'])
@pytest.mark.parametrize('backend', ['aws', 'gcp'])
def test_cloud_live_site_must_be_a_valid_dedicated_live_site(backend, site):
    with pytest.raises(ValueError):
        CloudLiveConfig.from_env(backend, ENVS[backend](LAB_SITE_ID=site))


@pytest.mark.parametrize('cap', ['0', '2', '10', 'one', ' '])
@pytest.mark.parametrize(('backend', 'name'), [('aws', 'AWS_MAX_VMS'), ('gcp', 'GCP_MAX_VMS')])
def test_cloud_live_cap_must_be_exactly_one(backend, name, cap):
    with pytest.raises(ValueError, match=name):
        CloudLiveConfig.from_env(backend, ENVS[backend](**{name: cap}))


def test_cloud_live_config_pins_the_validated_values_and_the_template():
    aws = CloudLiveConfig.from_env('aws', aws_env(ENABLED_BACKENDS='proxmox,aws'))
    assert aws.settings == {
        'ENABLED_BACKENDS': ('proxmox', 'aws'), 'LAB_SITE_ID': 'live-test',
        'AWS_MAX_VMS': 1, 'AWS_REGION': 'us-test-1', 'AWS_AVAILABILITY_ZONE': 'us-test-1a',
        'AWS_LAB_VPC_ID': 'vpc-placeholder', 'AWS_LAB_CIDR': '172.20.0.0/16',
        'AWS_ROUTER_INSTANCE_ID': 'i-placeholder'}
    assert aws.template() == {'image_id': 'ami-placeholder', 'instance_type': 't3.micro',
                              'disk_gb': 20}
    gcp = CloudLiveConfig.from_env('gcp', gcp_env())
    assert gcp.settings['GCP_MAX_VMS'] == 1 and gcp.settings['GCP_ZONE'] == 'test-region1-a'
    assert gcp.template() == {
        'image': 'projects/example-project/global/images/placeholder',
        'machine_type': 'e2-small', 'disk_gb': 20}


# --- preflight and teardown -----------------------------------------------------


def test_cloud_live_preflight_refuses_existing_site_resources_without_deleting_them():
    # Catches teardown deleting resources the live run did not create.
    fake, existing = fake_with_one_managed_resource()
    config = CloudLiveConfig.from_env('aws', aws_env())
    run = CloudContractRun(fake, config)
    with pytest.raises(UnsafeLiveRun):
        run.__enter__()
    assert fake.list_managed() == [existing]


def test_cloud_live_cleanup_runs_after_injected_failure_and_proves_empty():
    # Catches a failing live body leaving billable resources behind.
    fake = FakeBackend('aws')
    config = CloudLiveConfig.from_env('aws', aws_env())
    with pytest.raises(InjectedFailure):
        with CloudContractRun(fake, config) as run:
            run.launch_one()
            raise InjectedFailure()
    assert fake.list_managed() == []
    assert fake.router_running is False


def test_each_launch_gets_the_thirty_minute_expiry_and_the_full_teardown():
    fake = FakeBackend('gcp')
    with _run(fake, 'gcp') as run:
        ref = run.launch_one()
        assert fake.router_running is True
        assert fake.expiry[ref.vmid] == int((FIXED_NOW + timedelta(minutes=30)).timestamp())
        assert ref.provider_id == f'provider-{ref.vmid}'
        assert fake.reservations and fake.slot_networks and fake.deployment_networks
    assert fake.list_managed() == []
    assert fake.reservations == set()
    assert fake.destroyed == {ref.vmid: DestroyPolicy.BEST_EFFORT}
    assert fake.router_running is False


def test_an_instance_whose_create_raised_after_launch_is_still_destroyed(capsys):
    # Catches the claim being taken only when create returns.
    fake = FakeBackend('aws')
    fake.fail_create_after_launch = True
    with pytest.raises(RuntimeError, match='injected create failure'):
        with _run(fake) as run:
            run.launch_one()
    # Destroyed through its claimed ref by normal cleanup, not found later
    # by the leftover sweep.
    assert list(fake.destroyed.values()) == [DestroyPolicy.BEST_EFFORT]
    assert 'normal cleanup left' not in capsys.readouterr().err
    assert fake.list_managed() == []


class _UnverifiableFake(FakeBackend):
    """Destroy cannot verify identity, so the instance stays listed."""

    def destroy(self, ref, policy):
        raise BackendError(f'identity of VM {ref.vmid} could not be verified')


def test_a_cleanup_failure_fails_the_run_with_provider_ids_even_after_a_body_failure(capsys):
    # Catches a teardown failure being swallowed behind the body's exception.
    fake = _UnverifiableFake('aws')
    with pytest.raises(CloudCleanupError) as raised:
        with _run(fake) as run:
            ref = run.launch_one()
            raise InjectedFailure()
    message = str(raised.value)
    assert f'instance of VM {ref.vmid} (provider-{ref.vmid}' in message
    assert 'aws ec2 describe-instances --region us-test-1' in message
    assert isinstance(raised.value.__context__, InjectedFailure)
    # The leftover was removed from the fresh inventory alone.
    assert fake.list_managed() == []
    assert 'NOT cleaned up' in capsys.readouterr().err


class _UndeletableNetworkFake(FakeBackend):
    def cleanup_deployment(self, deployment_id):
        pass  # reports success, removes nothing

    def delete_resource(self, resource):
        if resource.kind == 'deployment-network':
            raise BackendError(f'{resource.provider_id} is still in use')
        super().delete_resource(resource)


def test_teardown_fails_unless_the_second_inventory_is_empty():
    # Catches a run passing while its site still holds a billable resource.
    fake = _UndeletableNetworkFake('gcp')
    with pytest.raises(CloudCleanupError) as raised:
        with _run(fake, 'gcp', settle_seconds=0) as run:
            ref = run.launch_one()
    message = str(raised.value)
    assert f"live site 'live-test' still holds deployment-network deployment-{ref.deployment_id}" in message
    assert "gcloud compute firewall-rules list --project example-project" in message


def test_leftovers_normal_cleanup_missed_are_swept_from_fresh_inventory(capsys):
    fake = FakeBackend('aws')
    with _run(fake) as run:
        run.launch_one()
        # A network the run never recorded; the site was empty at preflight,
        # so it can only be this run's.
        fake.deployment_networks.add('e' * 32)
    assert fake.list_managed() == []
    assert f"deployment-network deployment-{'e' * 32}" in capsys.readouterr().err


class _BusyNetworkFake(FakeBackend):
    """Refuses a security-group delete twice, as EC2 does while a terminated
    instance's interface is still detaching."""

    def __init__(self, name):
        super().__init__(name)
        self.busy = 2

    def cleanup_deployment(self, deployment_id):
        if self.busy:
            self.busy -= 1
            raise BackendError('DependencyViolation: resource has a dependent object')
        super().cleanup_deployment(deployment_id)


def test_network_cleanup_is_retried_while_the_provider_reports_it_in_use():
    fake = _BusyNetworkFake('aws')
    slept = []
    with CloudContractRun(fake, CloudLiveConfig.from_env('aws', aws_env()),
                          sleep=slept.append, clock=lambda: 0.0) as run:
        run.launch_one()
    assert fake.busy == 0 and fake.list_managed() == []
    assert len(slept) == 2


# --- live response classification ------------------------------------------------

MISSING_IMAGE = 'projects/example-project/global/images/lp-live-test-missing-image'


def _compute_error(code, message, reason):
    return {'error': {'code': code, 'message': message,
                      'errors': [{'message': message, 'domain': 'global', 'reason': reason}]}}


@pytest.mark.parametrize(('status', 'payload'), [
    (404, _compute_error(404, f"The resource '{MISSING_IMAGE}' was not found", 'notFound')),
    (400, _compute_error(400, "Invalid value for field 'resource.disks[0].initializeParams"
                              ".sourceImage': 'lp-live-test-missing-image'.", 'invalid')),
    (404, {'error': {'code': 404, 'message': 'not found', 'errors': [
        {'message': f"The resource '{MISSING_IMAGE}' was not found", 'reason': 'notFound'}]}}),
])
def test_a_missing_image_refusal_is_recognised(status, payload):
    assert is_missing_resource_refusal(status, payload, MISSING_IMAGE)


@pytest.mark.parametrize(('status', 'payload'), [
    # Auth, quota/rate limit and server failures must fail the live test, not skip it.
    (401, _compute_error(401, 'Request had invalid authentication credentials.', 'authError')),
    (403, _compute_error(403, f"Required 'compute.instances.create' permission for "
                              f"'{MISSING_IMAGE}'", 'forbidden')),
    (429, _compute_error(429, 'Rate Limit Exceeded', 'rateLimitExceeded')),
    (500, _compute_error(500, f'Internal error for {MISSING_IMAGE}', 'backendError')),
    (503, None),
    # A 404 about something else (the subnetwork, the zone) is not this refusal.
    (404, _compute_error(404, "The resource 'projects/example-project/regions/r/"
                              "subnetworks/lp-live-test-s-1' was not found", 'notFound')),
    (400, _compute_error(400, 'Invalid value for field machineType', 'invalid')),
    (404, None),
    (404, '<html>Not Found</html>'),
    (404, {'error': 'notFound'}),
])
def test_any_other_insert_failure_is_not_a_missing_image_refusal(status, payload):
    assert not is_missing_resource_refusal(status, payload, MISSING_IMAGE)
