"""Contract checks against real EC2, through the app's own AWSBackend.

The unit suite runs AWSBackend against moto, which is not evidence of how
EC2 itself behaves. These tests check the shapes and behaviours moto could
not prove. Opt-in only (LIVE_AWS=1): see tests/live/README.md. Every
resource is created and removed through CloudContractRun
(tests/live/cloud_harness.py), in a dedicated live site with a cap of one.

Nothing here imports a provider module at collection time: a skipped run
must never build a client.
"""

import ipaddress
import time
from datetime import timedelta

import pytest

from app.backends.base import GUEST_MATCH, AdmissionRefused, BackendError
from app.backends.cloud_common import (
    TAG_KIND, TAG_SITE, deployment_key, expires_epoch,
)
from app.backends.network_slots import cloud_slot_cidr
from tests.live.cloud_harness import EXPIRY, ROUTER_READ_LAG_SECONDS, CloudContractRun

pytestmark = [pytest.mark.live, pytest.mark.live_aws]


@pytest.fixture(scope='session')
def ec2_probe(aws_live_config):
    """A read-mostly EC2 client of its own, for what the backend does not
    expose. Standard boto3 credential chain; nothing is echoed."""
    import boto3
    return boto3.client('ec2', region_name=aws_live_config.settings['AWS_REGION'])


def _instance(ec2, instance_id):
    [reservation] = ec2.describe_instances(InstanceIds=[instance_id])['Reservations']
    [instance] = reservation['Instances']
    return instance


def _tags(resource):
    return {tag['Key']: tag['Value'] for tag in resource.get('Tags') or []}


def _managed(backend, kind):
    return [r for r in backend.list_managed() if r.kind == kind]


def _require_live_router(ec2, config):
    """The configured router, after proving it is the live site's."""
    router_id = config.settings['AWS_ROUTER_INSTANCE_ID']
    router = _instance(ec2, router_id)
    tags = _tags(router)
    if tags.get(TAG_SITE) != config.site_id or tags.get(TAG_KIND) != 'router':
        pytest.fail(f'refusing to touch {router_id}: it is not the live site router '
                    f'({TAG_SITE}={tags.get(TAG_SITE)!r}, {TAG_KIND}={tags.get(TAG_KIND)!r})')
    return router_id


def test_lifecycle_against_real_ec2(aws_backend, aws_live_config):
    settings = aws_live_config.settings
    with CloudContractRun(aws_backend, aws_live_config) as run:
        spec = run.new_spec()
        location = run.admit(spec)
        assert location == settings['AWS_AVAILABILITY_ZONE']
        run.ensure_router()
        run.ensure_slot(spec.network_slot)
        ref = run.create(spec, location)

        assert aws_backend.inspect(ref) == GUEST_MATCH
        assert aws_backend.status(ref) == 'running'
        ips = aws_backend.ip_addresses(ref)
        slot_cidr = ipaddress.ip_network(
            cloud_slot_cidr(settings['AWS_LAB_CIDR'], spec.network_slot))
        assert ips, 'a running instance reports no private address'
        for ip in ips:
            assert ipaddress.ip_address(ip).is_private, ips
            assert ipaddress.ip_address(ip) in slot_cidr, (ips, slot_cidr)

        [row] = _managed(aws_backend, 'instance')
        assert (row.provider_id, row.vmid, row.name) == (ref.provider_id, spec.vmid, spec.vm_name)
        assert row.expires_at_epoch == expires_epoch(spec.expires_at)
        # Earlier, never later, than the 30-minute expiry it launched with.
        sooner = run.now() + EXPIRY - timedelta(minutes=5)
        aws_backend.set_expiry(ref, sooner)
        [row] = _managed(aws_backend, 'instance')
        assert row.expires_at_epoch == expires_epoch(sooner)

        with pytest.raises(AdmissionRefused):
            run.admit(run.new_spec())
    assert aws_backend.list_managed() == []


def test_private_launch_has_instance_security_groups_and_no_public_association(
        aws_run, aws_backend, ec2_probe):
    # moto 5.2.5 omits instance-level SecurityGroups and adds an Association
    # to private interfaces; identity and the no-public-IP promise rest on EC2.
    ref = aws_run.launch_one()
    [group] = [r for r in _managed(aws_backend, 'deployment-network')
               if r.deployment_id == deployment_key(ref.deployment_id)]
    instance = _instance(ec2_probe, ref.provider_id)

    assert [g['GroupId'] for g in instance.get('SecurityGroups') or []] == [group.provider_id]
    assert 'PublicIpAddress' not in instance, instance.get('PublicIpAddress')
    interfaces = instance['NetworkInterfaces']
    assert len(interfaces) == 1
    for nic in interfaces:
        assert 'Association' not in nic, nic['Association']
        assert [g['GroupId'] for g in nic['Groups']] == [group.provider_id]
        for address in nic['PrivateIpAddresses']:
            assert 'Association' not in address, address['Association']


class _LostRunInstancesResponse:
    """An EC2 client whose RunInstances reaches EC2 but whose response is
    lost, as a client-side read timeout loses it."""

    def __init__(self, client):
        self._client = client
        self.launched = []

    def __getattr__(self, name):
        return getattr(self._client, name)

    def run_instances(self, **kwargs):
        from botocore.exceptions import ReadTimeoutError
        response = self._client.run_instances(**kwargs)
        self.launched.extend(i['InstanceId'] for i in response['Instances'])
        raise ReadTimeoutError(endpoint_url=self._client.meta.endpoint_url)


def test_run_instances_client_token_replays_after_a_lost_response(
        aws_run, aws_backend, ec2_probe):
    # A retried launch must find the instance the lost call started, not a twin.
    from app.backends.aws import AWSBackend

    lossy = _LostRunInstancesResponse(ec2_probe)
    spec = aws_run.new_spec()
    location = aws_run.admit(spec)
    aws_run.ensure_router()
    aws_run.ensure_slot(spec.network_slot)
    with pytest.raises(BackendError, match='Read timeout'):
        aws_run.create(spec, location, via=AWSBackend(ec2_client=lossy))
    [first] = lossy.launched

    ref = aws_run.create(spec, location)

    assert ref.provider_id == first
    assert [r.provider_id for r in _managed(aws_backend, 'instance')] == [first]


def test_attached_security_group_delete_is_refused_and_reaches_the_caller(
        aws_run, aws_backend):
    # Deployment cleanup must fail loudly while an instance still uses the
    # group, never report success and leave it behind.
    ref = aws_run.launch_one()
    with pytest.raises(BackendError) as refused:
        aws_backend.cleanup_deployment(ref.deployment_id)
    cause = refused.value.__cause__
    assert cause is not None and cause.response['Error']['Code'] == 'DependencyViolation', (
        refused.value)
    assert [r.deployment_id for r in _managed(aws_backend, 'deployment-network')] == [
        deployment_key(ref.deployment_id)]


def test_stop_router_while_pending_waits_for_running_then_stops(
        aws_run, aws_backend, ec2_probe, aws_live_config):
    # EC2 refuses to stop an instance that is still starting.
    router_id = _require_live_router(ec2_probe, aws_live_config)
    aws_backend.stop_router()
    assert _instance(ec2_probe, router_id)['State']['Name'] == 'stopped'

    ec2_probe.start_instances(InstanceIds=[router_id])
    aws_run.note_router_started()
    # Straight after StartInstances EC2 can still answer 'stopped'
    # (read-after-write lag). That read says nothing about the start.
    deadline = time.monotonic() + ROUTER_READ_LAG_SECONDS
    observed = _instance(ec2_probe, router_id)['State']['Name']
    while observed == 'stopped' and time.monotonic() < deadline:
        time.sleep(1)
        observed = _instance(ec2_probe, router_id)['State']['Name']
    if observed == 'stopped':
        pytest.fail(f'router still read stopped {ROUTER_READ_LAG_SECONDS}s after '
                    'StartInstances')
    if observed != 'pending':
        pytest.skip(f'router was already {observed!r} when the start became visible; '
                    'the pending path was not exercised')
    assert aws_run.stop_router() is True

    assert _instance(ec2_probe, router_id)['State']['Name'] == 'stopped'
