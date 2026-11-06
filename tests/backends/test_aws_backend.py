"""AWSBackend instance lifecycle, capped admission, slot networks,
deployment isolation and router lifecycle against moto's EC2.

Every provider assertion reads moto's EC2 state through a real boto3 client;
no test inspects boto call lists.
"""

import hashlib
import importlib
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from app import coordination
from app.backends import get_backend
from app.backends.aws import AWSBackend, _client_token, _waiter_config
from app.backends.base import (
    BACKEND_AWS, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, GUEST_UNKNOWN,
    AdmissionRefused, BackendError, DestroyPolicy, DestroyRefused,
    LaunchedVM, SlotNetworkMissing, VMRef,
)
from app.backends.cloud_common import (
    TAG_CREATED, TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT,
    TAG_VMID, deployment_key, deployment_network_name, expires_epoch,
    router_lock_key, slot_lock_key, slot_network_name,
)
from app.backends.network_slots import cloud_slot_cidr
from app.models import NodeMemoryReservation, VMRecord, db
from app.proxmox_service import vm_lifecycle_lock
from app.routes.vm_creation import named_instances
from tests.backends.fake_backend import (
    AWS_TEST_CONFIG, AWS_TEST_ENV, aws_test_spec, register_aws_test_image,
)

ROOT = Path(__file__).resolve().parents[2]


def _tags(pairs):
    return [{'Key': key, 'Value': value} for key, value in pairs.items()]


def _patch_aws_config(monkeypatch):
    config = importlib.import_module('app.config')
    for name, value in AWS_TEST_CONFIG.items():
        monkeypatch.setattr(config, name, value)
    return config


def _fake_credentials(monkeypatch):
    for name, value in AWS_TEST_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('AWS_PROFILE', raising=False)


@pytest.fixture
def aws_instance_backend(app, monkeypatch):
    _fake_credentials(monkeypatch)
    config = _patch_aws_config(monkeypatch)
    with mock_aws(), app.app_context():
        ec2 = boto3.client('ec2', region_name='us-east-1')
        vpc_id = ec2.create_vpc(CidrBlock='172.20.0.0/16')['Vpc']['VpcId']
        # The resolver hands out slot 1's real subnet in the lab VPC: every
        # mutation re-proves an instance's actual placement, not its tags.
        monkeypatch.setattr(config, 'AWS_LAB_VPC_ID', vpc_id)
        monkeypatch.setattr(config, 'AWS_LAB_CIDR', '172.20.0.0/16')
        subnet_id = _subnet(ec2, vpc_id, 1, _slot_tags(1))
        security_group_id = ec2.create_security_group(
            GroupName='test-instance-sg', Description='test', VpcId=vpc_id)['GroupId']
        image_id = register_aws_test_image(ec2)
        foreign_instance_id = ec2.run_instances(
            ImageId=image_id, InstanceType='t3.micro', MinCount=1, MaxCount=1,
            SubnetId=subnet_id,
            TagSpecifications=[{'ResourceType': 'instance', 'Tags': _tags(
                {TAG_SITE: 'other-site', TAG_VMID: '700'})}],
        )['Instances'][0]['InstanceId']
        backend = AWSBackend(
            ec2_client=ec2,
            network_resolver=lambda spec: (subnet_id, security_group_id))
        spec = aws_test_spec(image_id)

        def ref(launched):
            return VMRef(
                vmid=spec.vmid, vm_name=spec.vm_name, vm_type='aws',
                location=launched.location, backend='aws',
                provider_id=launched.provider_id,
                deployment_id=spec.deployment_id,
                network_slot=launched.network_slot)

        resources = {
            'vpc_id': vpc_id,
            'subnet_id': subnet_id,
            'security_group_id': security_group_id,
            'foreign_instance_id': foreign_instance_id,
            'ref': ref,
            'ref_without_provider': VMRef(
                vmid=spec.vmid, vm_name=spec.vm_name, vm_type='aws',
                location='us-east-1a', backend='aws', provider_id=None,
                deployment_id=spec.deployment_id, network_slot=1),
        }
        yield backend, spec, ec2, resources


@pytest.fixture
def aws_network_backend(app, monkeypatch):
    """AWSBackend with the production network path (ruling I13): no
    resolver, no pre-created slot subnet or security group, and a running
    router instance configured as AWS_ROUTER_INSTANCE_ID."""
    _fake_credentials(monkeypatch)
    config = _patch_aws_config(monkeypatch)
    with mock_aws(), app.app_context():
        ec2 = boto3.client('ec2', region_name='us-east-1')
        vpc_id = ec2.create_vpc(CidrBlock='172.20.0.0/16')['Vpc']['VpcId']
        image_id = register_aws_test_image(ec2)
        # Tagged as the operator provisions it (TAG_SITE + TAG_KIND=router).
        router_instance_id = ec2.run_instances(
            ImageId=image_id, InstanceType='t3.micro', MinCount=1, MaxCount=1,
            TagSpecifications=[{'ResourceType': 'instance', 'Tags': _tags(
                {TAG_SITE: 'test-site', TAG_KIND: 'router'})}],
        )['Instances'][0]['InstanceId']
        monkeypatch.setattr(config, 'AWS_LAB_VPC_ID', vpc_id)
        monkeypatch.setattr(config, 'AWS_LAB_CIDR', '172.20.0.0/16')
        monkeypatch.setattr(config, 'AWS_ROUTER_INSTANCE_ID', router_instance_id)
        backend = AWSBackend(ec2_client=ec2)
        spec = aws_test_spec(image_id)

        def ref(launched):
            return VMRef(
                vmid=spec.vmid, vm_name=spec.vm_name, vm_type='aws',
                location=launched.location, backend='aws',
                provider_id=launched.provider_id,
                deployment_id=spec.deployment_id,
                network_slot=launched.network_slot)

        yield backend, spec, ec2, {
            'vpc_id': vpc_id, 'router_instance_id': router_instance_id, 'ref': ref}


def _flatten_group_rules(permissions):
    rules = set()
    for permission in permissions:
        for pair in permission.get('UserIdGroupPairs', []):
            rules.add((permission['IpProtocol'], 'group', pair['GroupId']))
        for address in permission.get('IpRanges', []):
            rules.add((permission['IpProtocol'], 'cidr', address['CidrIp']))
    return rules


def _instance_state(ec2, instance_id):
    return ec2.describe_instances(InstanceIds=[instance_id])[
        'Reservations'][0]['Instances'][0]['State']['Name']


def _site_subnets(ec2, slot):
    return ec2.describe_subnets(Filters=[
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']},
        {'Name': f'tag:{TAG_SLOT}', 'Values': [str(slot)]},
    ])['Subnets']


def _deployment_groups(ec2, deployment_id, site='test-site'):
    return ec2.describe_security_groups(Filters=[
        {'Name': f'tag:{TAG_DEPLOYMENT}', 'Values': [deployment_id]},
        {'Name': f'tag:{TAG_SITE}', 'Values': [site]},
    ])['SecurityGroups']


def _subnet(ec2, vpc_id, slot, tags):
    return ec2.create_subnet(
        VpcId=vpc_id, CidrBlock=cloud_slot_cidr('172.20.0.0/16', slot),
        AvailabilityZone='us-east-1a',
        TagSpecifications=[{'ResourceType': 'subnet', 'Tags': _tags(tags)}],
    )['Subnet']['SubnetId']


def _slot_tags(slot, site='test-site'):
    return {'Name': slot_network_name(site, slot), TAG_SITE: site,
            TAG_SLOT: str(slot), TAG_KIND: 'slot-network',
            TAG_CREATED: str(int(time.time()))}


def _group(ec2, vpc_id, deployment_id, site='test-site', name=None):
    return ec2.create_security_group(
        GroupName=name or deployment_network_name(site, deployment_id),
        Description='test', VpcId=vpc_id,
        TagSpecifications=[{'ResourceType': 'security-group', 'Tags': _tags({
            TAG_SITE: site, TAG_DEPLOYMENT: deployment_id,
            TAG_KIND: 'deployment-network',
            TAG_CREATED: str(int(time.time()))})}],
    )['GroupId']


def _ensure_slot(backend, slot):
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, slot)):
        backend.ensure_slot(slot)


def _after_enumeration(monkeypatch, ec2, operation, change):
    """Run `change(pages)` once, right after the backend's next paginated
    `operation` read returns: a concurrent writer acting between an
    enumeration and whatever the backend does with its result."""
    real = ec2.get_paginator
    fired = []

    class _Changing:
        def __init__(self, paginator):
            self._paginator = paginator

        def paginate(self, **kwargs):
            pages = list(self._paginator.paginate(**kwargs))
            fired.append(True)
            change(pages)
            return pages

    def get_paginator(name):
        paginator = real(name)
        return paginator if name != operation or fired else _Changing(paginator)

    monkeypatch.setattr(ec2, 'get_paginator', get_paginator)
    return fired


def _retag(ec2, list_key, id_field, tags):
    """A change that retags every item an enumeration of `list_key` returned."""
    def change(pages):
        ids = [item[id_field] for page in pages for item in page[list_key]]
        assert ids, 'the enumeration found nothing to retag'
        ec2.create_tags(Resources=ids, Tags=_tags(tags))
    return change


def test_admit_counts_live_records_and_unexpired_reservations_atomically(app, aws_instance_backend):
    # Catches concurrent creates exceeding the mandatory AWS_MAX_VMS cap.
    backend, spec, ec2, resources = aws_instance_backend
    assert backend.admit(spec, reservation_id='r1', load_balancing_config=None) == 'us-east-1a'
    assert NodeMemoryReservation.query.filter_by(
        reservation_id='r1', node='aws', memory_bytes=1).one()
    assert backend.admit(spec, reservation_id='r2', load_balancing_config=None) == 'us-east-1a'
    with pytest.raises(AdmissionRefused):
        backend.admit(spec, reservation_id='r3', load_balancing_config=None)
    assert backend.renew_admission('r1') is True
    assert backend.release_admission('r1') is True
    assert backend.release_admission('r2') is True


def test_admit_counts_live_aws_records_but_not_expired_or_other_backend_leases(
        app, aws_instance_backend):
    # Catches stale/cross-cloud reservations consuming AWS cap, or live AWS VMs being omitted.
    backend, spec, ec2, resources = aws_instance_backend
    now = coordination.lease_clock()
    db.session.add(VMRecord(
        vmid=699, vm_name='test-aws-699', vm_type='aws', node='us-east-1a',
        template_name='test/aws', student_name='test-owner',
        memory=1024, cores=1, storage=24, is_entry_point=True,
        expires_at=spec.expires_at, destroyed=False, backend='aws',
        provider_id='i-live-record', deployment_id='c' * 32, network_slot=1))
    db.session.add_all([
        NodeMemoryReservation(reservation_id='expired-aws', node='aws',
                              memory_bytes=1, expires_at=now - 1),
        NodeMemoryReservation(reservation_id='live-gcp', node='gcp',
                              memory_bytes=1, expires_at=now + 600),
    ])
    db.session.commit()
    assert backend.admit(spec, reservation_id='live-aws-reservation',
                         load_balancing_config=None) == 'us-east-1a'
    with pytest.raises(AdmissionRefused):
        backend.admit(spec, reservation_id='over-cap',
                      load_balancing_config=None)
    assert db.session.get(NodeMemoryReservation, 'expired-aws') is None
    assert db.session.get(NodeMemoryReservation, 'live-gcp') is not None
    assert backend.release_admission('live-aws-reservation') is True


def test_create_launches_one_private_tagged_instance_with_disposable_root_disk(aws_instance_backend):
    # Catches duplicate/public instances and EBS volumes that keep billing after termination.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    tags = {tag['Key']: tag['Value'] for tag in instance['Tags']}
    assert instance['State']['Name'] == 'running'
    assert instance['Placement']['AvailabilityZone'] == 'us-east-1a'
    assert instance['SubnetId'] == resources['subnet_id']
    # moto 5.2.5 reports a NetworkInterfaces-launched instance's groups only
    # on its interface (EC2 reports both), and always renders an empty
    # Association element; an empty one is "no public association".
    [interface] = instance['NetworkInterfaces']
    assert resources['security_group_id'] in [g['GroupId'] for g in interface['Groups']]
    assert 'PublicIpAddress' not in instance
    assert not interface.get('Association')
    assert instance['BlockDeviceMappings'][0]['Ebs']['DeleteOnTermination'] is True
    [root_volume] = ec2.describe_volumes(
        VolumeIds=[instance['BlockDeviceMappings'][0]['Ebs']['VolumeId']])['Volumes']
    assert root_volume['Size'] == 24
    # The root volume carries the ownership tags from launch, so a volume
    # left behind by a failed DeleteOnTermination is attributable.
    assert {tag['Key']: tag['Value'] for tag in root_volume.get('Tags', [])} == {
        'Name': spec.vm_name,
        TAG_SITE: 'test-site', TAG_VMID: '700', TAG_DEPLOYMENT: 'd' * 32,
        TAG_SLOT: '1', TAG_KIND: 'instance-volume',
    }
    assert tags == {
        'Name': spec.vm_name,
        TAG_SITE: 'test-site', TAG_VMID: '700',
        TAG_DEPLOYMENT: 'd' * 32, TAG_EXPIRES: str(expires_epoch(spec.expires_at)),
        TAG_SLOT: '1', TAG_KIND: 'instance',
    }
    assert launched == LaunchedVM(vmid=700, vm_type='aws', location='us-east-1a',
                                  network_slot=1, provider_id=instance['InstanceId'])
    site_instances = ec2.describe_instances(Filters=[
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']}])['Reservations']
    assert [i['InstanceId'] for r in site_instances for i in r['Instances']] == [
        launched.provider_id]


def test_create_heartbeats_before_launch_and_after_running(aws_instance_backend):
    # Catches a slow launch letting the admission and VMID leases expire.
    backend, spec, ec2, resources = aws_instance_backend
    seen = []

    def heartbeat():
        listed = ec2.describe_instances(Filters=[
            {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']}])['Reservations']
        seen.append([i['State']['Name'] for r in listed for i in r['Instances']])

    backend.create(spec, 'us-east-1a', heartbeat=heartbeat)
    assert seen == [[], ['running']]


def test_client_token_and_waiter_budget_are_stable_and_bounded():
    # Catches retry tokens changing across processes and provider waits becoming unbounded.
    expected = hashlib.sha256(f"{'d' * 32}:700".encode('utf-8')).hexdigest()
    assert _client_token('d' * 32, 700) == expected
    assert len(expected) == 64
    assert _waiter_config(1) == {'Delay': 5, 'MaxAttempts': 1}
    assert _waiter_config(6) == {'Delay': 5, 'MaxAttempts': 2}
    assert _waiter_config(600) == {'Delay': 5, 'MaxAttempts': 120}
    with pytest.raises(BackendError):
        _waiter_config(0)


def test_inspect_requires_site_and_vmid_identity(aws_instance_backend):
    # Catches destroying an instance reused by another portal or record.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    assert backend.inspect(ref) == GUEST_MATCH
    ec2.create_tags(Resources=[launched.provider_id],
                    Tags=[{'Key': TAG_VMID, 'Value': '701'}])
    assert backend.inspect(ref) == GUEST_MISMATCH


@pytest.mark.parametrize('tag, value', [
    (TAG_SITE, 'other-site'), (TAG_KIND, 'router'),
    (TAG_DEPLOYMENT, 'e' * 32), (TAG_SLOT, '2'),
])
def test_inspect_by_provider_id_checks_every_contract_tag(aws_instance_backend, tag, value):
    # Catches a retagged instance passing identity on the tags not compared.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ec2.create_tags(Resources=[launched.provider_id], Tags=[{'Key': tag, 'Value': value}])
    assert backend.inspect(resources['ref'](launched)) == GUEST_MISMATCH


def test_inspect_is_unknown_for_malformed_ids_and_provider_failures(aws_instance_backend):
    # Catches a describe failure being read as "absent" and the record retired.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    assert backend.inspect(replace(ref, provider_id='malformed')) == GUEST_UNKNOWN
    throttled = ClientError({'Error': {'Code': 'RequestLimitExceeded',
                                       'Message': 'slow down'}}, 'DescribeInstances')

    def fail(**kwargs):
        raise throttled

    backend._ec2.describe_instances = fail
    assert backend.inspect(ref) == GUEST_UNKNOWN
    assert backend.confirm_absent(ref, throttled) is False
    with pytest.raises(BackendError):
        backend.destroy(ref, DestroyPolicy.STRICT)


def test_destroy_and_confirm_absent_require_positive_fresh_evidence(aws_instance_backend):
    # Catches retiring a record merely because describe failed transiently.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    backend.destroy(ref, DestroyPolicy.STRICT)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'terminated'
    assert backend.confirm_absent(ref) is True
    not_found = ClientError({'Error': {'Code': 'InvalidInstanceID.NotFound',
                                       'Message': 'not found'}}, 'DescribeInstances')
    assert backend.confirm_absent(ref, not_found) is True
    assert backend.confirm_absent(replace(ref, provider_id='malformed')) is False
    assert backend.inspect(ref) == GUEST_ABSENT
    backend.destroy(ref, DestroyPolicy.STRICT)  # idempotent on an absent instance


def test_ips_status_expiry_and_inventory_come_from_moto_state(aws_instance_backend):
    # Catches UI and sweeper results coming from stale DB fields instead of EC2.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert backend.ip_addresses(ref) == [instance['PrivateIpAddress']]
    assert backend.status(ref) == 'running'
    extended = spec.expires_at + timedelta(hours=1)
    backend.set_expiry(ref, extended)
    [managed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    assert managed.kind == 'instance'
    assert managed.vmid == 700
    assert managed.deployment_id == 'd' * 32
    assert managed.network_slot == 1
    assert managed.expires_at_epoch == expires_epoch(extended)
    assert managed.location == 'us-east-1a'
    assert managed.status == 'running'
    assert managed.created_at_epoch == instance['LaunchTime'].timestamp()
    assert resources['foreign_instance_id'] not in {
        resource.provider_id for resource in backend.list_managed()
    }
    gone = replace(ref, provider_id='i-00000000000000000')
    assert backend.ip_addresses(gone) == []
    assert backend.status(gone) == 'unknown'


def test_list_managed_names_round_trip_to_the_portal_vm_name(aws_instance_backend):
    # Catches /create pre-flight missing an existing AWS VM because its
    # inventory name is not the portal's vm_name.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [managed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    assert managed.name == spec.vm_name
    assert named_instances(backend, 'test-aws-') == [
        {'vmid': 700, 'node': 'us-east-1a', 'vm_type': 'aws', 'name': spec.vm_name}]


def test_set_expiry_raises_backend_error_when_ec2_cannot_find_instance(aws_instance_backend):
    # Catches a failed provider tag update being counted as a successful extension (I2).
    backend, spec, ec2, resources = aws_instance_backend
    missing = replace(resources['ref_without_provider'], provider_id='i-00000000000000000')
    with pytest.raises(BackendError):
        backend.set_expiry(missing, spec.expires_at + timedelta(hours=1))


def test_destroy_refuses_a_retagged_instance_without_terminating_it(aws_instance_backend):
    # Catches a stale destroy terminating an instance another record or portal now owns (I1).
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ec2.create_tags(Resources=[launched.provider_id],
                    Tags=[{'Key': TAG_VMID, 'Value': '701'}])
    for policy in DestroyPolicy:
        with pytest.raises(DestroyRefused):
            backend.destroy(resources['ref'](launched), policy)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'running'


def test_ref_without_provider_id_resolves_by_site_vmid_and_deployment_tags(aws_instance_backend):
    # Catches a launch-then-waiter-timeout survivor never being destroyed and billing forever (C7).
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    survivor = resources['ref_without_provider']
    assert backend.inspect(survivor) == GUEST_MATCH
    backend.destroy(survivor, DestroyPolicy.BEST_EFFORT)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'terminated'
    assert backend.confirm_absent(survivor) is True
    [foreign] = ec2.describe_instances(InstanceIds=[resources['foreign_instance_id']])['Reservations'][0]['Instances']
    assert foreign['State']['Name'] == 'running'


def test_survivor_resolution_ignores_ref_location(aws_instance_backend):
    # Catches a survivor recorded with location='aws' (T8) never resolving.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    survivor = replace(resources['ref_without_provider'], location='aws')
    assert backend.inspect(survivor) == GUEST_MATCH
    backend.destroy(survivor, DestroyPolicy.STRICT)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'terminated'
    assert backend.confirm_absent(survivor) is True


def test_survivor_resolution_is_unknown_when_two_instances_claim_the_identity(aws_instance_backend):
    # Catches tag resolution picking one of two candidates and terminating the wrong one.
    backend, spec, ec2, resources = aws_instance_backend
    first = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    # A twin carrying the same identity tags, launched outside the backend
    # (the backend's own retry would reuse its ClientToken).
    [described] = ec2.describe_instances(
        InstanceIds=[first.provider_id])['Reservations'][0]['Instances']
    ec2.run_instances(
        ImageId=spec.template['image_id'], InstanceType='t3.micro',
        MinCount=1, MaxCount=1, SubnetId=resources['subnet_id'],
        TagSpecifications=[{'ResourceType': 'instance', 'Tags': described['Tags']}])
    survivor = resources['ref_without_provider']
    assert backend.inspect(survivor) == GUEST_UNKNOWN
    assert backend.confirm_absent(survivor) is False
    with pytest.raises(BackendError):
        backend.destroy(survivor, DestroyPolicy.BEST_EFFORT)
    [instance] = ec2.describe_instances(InstanceIds=[first.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'running'


def test_delete_resource_terminates_listed_instance_and_rejects_other_kinds(aws_instance_backend):
    # Catches the sweeper's delete leaving a listed instance billing.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [managed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    with pytest.raises(BackendError):
        backend.delete_resource(replace(managed, kind='slot-network'))
    backend.delete_resource(managed)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'terminated'
    backend.delete_resource(managed)  # already terminated: done
    backend.delete_resource(replace(managed, provider_id='i-00000000000000000'))  # NotFound: done


@pytest.mark.parametrize('tag, value', [
    (TAG_SITE, 'other-site'), (TAG_KIND, 'router'),
    (TAG_VMID, '701'), (TAG_DEPLOYMENT, 'e' * 32),
])
def test_delete_resource_refuses_an_instance_retagged_since_listing(
        aws_instance_backend, tag, value):
    # Catches a sweep terminating an instance another portal or record owns now.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [managed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    ec2.create_tags(Resources=[launched.provider_id], Tags=[{'Key': tag, 'Value': value}])
    with pytest.raises(DestroyRefused):
        backend.delete_resource(managed)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'running'


def test_delete_resource_propagates_unknown_identity(aws_instance_backend):
    # Catches a describe failure during a sweep being read as permission to terminate.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [managed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    with pytest.raises(BackendError):
        backend.delete_resource(replace(managed, provider_id='malformed'))

    def fail(**kwargs):
        raise ClientError({'Error': {'Code': 'RequestLimitExceeded',
                                     'Message': 'slow down'}}, 'DescribeInstances')

    backend._ec2.describe_instances = fail
    with pytest.raises(BackendError) as raised:
        backend.delete_resource(managed)
    assert not isinstance(raised.value, DestroyRefused)
    del backend._ec2.describe_instances  # the fixture's client is the backend's
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'running'


# Instance placement on mutation paths

def _contract_tags(spec):
    """The exact tag set create() writes for `spec` in slot 1."""
    return {'Name': spec.vm_name, TAG_SITE: 'test-site', TAG_VMID: str(spec.vmid),
            TAG_DEPLOYMENT: spec.deployment_id,
            TAG_EXPIRES: str(expires_epoch(spec.expires_at)),
            TAG_SLOT: '1', TAG_KIND: 'instance'}


def _placement(ec2, resources, where):
    """run_instances placement for a same-tagged twin outside its slot."""
    if where == 'default VPC':
        return {}
    if where == 'other VPC':
        other_vpc = ec2.create_vpc(CidrBlock='172.20.0.0/16')['Vpc']['VpcId']
        return {'SubnetId': _subnet(ec2, other_vpc, 1, _slot_tags(1))}
    if where == 'other slot':
        return {'SubnetId': _subnet(ec2, resources['vpc_id'], 2, _slot_tags(2))}
    assert where == 'foreign subnet'
    return {'SubnetId': _subnet(ec2, resources['vpc_id'], 3,
                                _slot_tags(1, site='other-site'))}


def _instance(ec2, instance_id):
    [instance] = ec2.describe_instances(
        InstanceIds=[instance_id])['Reservations'][0]['Instances']
    return instance


@pytest.mark.parametrize('where', ['default VPC', 'other VPC', 'other slot',
                                   'foreign subnet'])
def test_mutations_refuse_a_same_tagged_instance_outside_the_lab_placement(
        aws_instance_backend, where):
    # Catches destroy, a sweep or an extension acting on an instance that
    # carries a copy of the record's tags but sits outside the lab VPC or
    # outside its slot's subnet.
    backend, spec, ec2, resources = aws_instance_backend
    twin = ec2.run_instances(
        ImageId=spec.template['image_id'], InstanceType='t3.micro',
        MinCount=1, MaxCount=1, **_placement(ec2, resources, where),
        TagSpecifications=[{'ResourceType': 'instance',
                            'Tags': _tags(_contract_tags(spec))}],
    )['Instances'][0]['InstanceId']
    ref = replace(resources['ref_without_provider'], provider_id=twin)
    survivor = resources['ref_without_provider']
    [listed] = [r for r in backend.list_managed() if r.provider_id == twin]
    before = _instance(ec2, twin)['Tags']
    assert backend.inspect(ref) == backend.inspect(survivor) == GUEST_MISMATCH
    for candidate in (ref, survivor):
        for policy in DestroyPolicy:
            with pytest.raises(DestroyRefused):
                backend.destroy(candidate, policy)
        with pytest.raises(BackendError):
            backend.set_expiry(candidate, spec.expires_at + timedelta(hours=2))
        assert backend.confirm_absent(candidate) is False
    with pytest.raises(DestroyRefused):
        backend.delete_resource(listed)
    instance = _instance(ec2, twin)
    assert instance['State']['Name'] == 'running'
    assert instance['Tags'] == before


def test_mutations_refuse_an_instance_without_a_vpc_placement(aws_instance_backend):
    # Catches an instance that reports no VPC or subnet (no lab NIC)
    # passing identity on tags alone.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    [listed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    real = ec2.describe_instances

    def unplaced(**kwargs):
        response = real(**kwargs)
        for reservation in response['Reservations']:
            for instance in reservation['Instances']:
                for field in ('VpcId', 'SubnetId', 'NetworkInterfaces'):
                    instance.pop(field, None)
        return response

    backend._ec2.describe_instances = unplaced
    assert backend.inspect(ref) == GUEST_MISMATCH
    with pytest.raises(DestroyRefused):
        backend.destroy(ref, DestroyPolicy.STRICT)
    with pytest.raises(DestroyRefused):
        backend.delete_resource(listed)
    del backend._ec2.describe_instances  # the fixture's client is the backend's
    assert _instance(ec2, launched.provider_id)['State']['Name'] == 'running'


def test_placement_that_cannot_be_verified_is_unknown_never_absent(aws_instance_backend):
    # Catches an ambiguous slot subnet being read as absence (retiring a
    # record whose instance bills) or as ownership (terminating it).
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    [listed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    _subnet(ec2, resources['vpc_id'], 9, _slot_tags(1))  # a second slot-1 subnet
    assert backend.inspect(ref) == GUEST_UNKNOWN
    assert backend.confirm_absent(ref) is False
    for mutate in (lambda: backend.destroy(ref, DestroyPolicy.BEST_EFFORT),
                   lambda: backend.delete_resource(listed)):
        with pytest.raises(BackendError) as raised:
            mutate()
        assert not isinstance(raised.value, DestroyRefused)
    assert _instance(ec2, launched.provider_id)['State']['Name'] == 'running'


# Launch block devices

def _with_image_mappings(monkeypatch, ec2, extra=(), root=None):
    """moto 5.2.5 drops every registered mapping but the root from
    DescribeImages, so an AMI's further disks are added to its answer."""
    real = ec2.describe_images

    def describe_images(**kwargs):
        response = real(**kwargs)
        for image in response['Images']:
            mappings = image['BlockDeviceMappings']
            if root is not None:
                mappings = [root]
            image['BlockDeviceMappings'] = mappings + list(extra)
        return response

    monkeypatch.setattr(ec2, 'describe_images', describe_images)


def test_create_makes_every_inherited_ebs_volume_disposable(aws_instance_backend, monkeypatch):
    # Catches an AMI data disk with DeleteOnTermination=false surviving the
    # instance's termination and billing forever.
    backend, spec, ec2, resources = aws_instance_backend
    _with_image_mappings(monkeypatch, ec2, extra=[
        {'DeviceName': '/dev/sdf', 'Ebs': {'VolumeSize': 5, 'VolumeType': 'standard',
                                           'DeleteOnTermination': False}},
        {'DeviceName': '/dev/sdb', 'VirtualName': 'ephemeral0'},
        {'DeviceName': '/dev/sdc', 'NoDevice': ''},
    ])
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    instance = _instance(ec2, launched.provider_id)
    mappings = {m['DeviceName']: m['Ebs'] for m in instance['BlockDeviceMappings']}
    assert set(mappings) == {'/dev/sda1', '/dev/sdf'}
    assert all(ebs['DeleteOnTermination'] is True for ebs in mappings.values())
    volumes = ec2.describe_volumes(
        VolumeIds=[ebs['VolumeId'] for ebs in mappings.values()])['Volumes']
    assert sorted(v['Size'] for v in volumes) == [5, 24]
    for volume in volumes:
        tags = {tag['Key']: tag['Value'] for tag in volume.get('Tags', [])}
        assert tags[TAG_KIND] == 'instance-volume'
        assert tags[TAG_VMID] == '700'
    backend.destroy(resources['ref'](launched), DestroyPolicy.STRICT)
    remaining = {v['VolumeId'] for v in ec2.describe_volumes()['Volumes']}
    assert not remaining & {ebs['VolumeId'] for ebs in mappings.values()}


class _RequestBuilt(Exception):
    """Raised once RunInstances is built: not a provider error, so create()
    lets it out with nothing launched."""


def _stop_at_run_instances(ec2):
    """Record the parameters of the next RunInstances request, as botocore
    hands them to the serializer, and stop it there. moto 5.2.5 drops Iops
    when it creates a launch's volumes, so it refuses every io1/io2 launch;
    the request itself is what is under test."""
    sent = []

    def record(params, **kwargs):
        sent.append(params)
        raise _RequestBuilt()

    ec2.meta.events.register('provide-client-params.ec2.RunInstances', record)
    return sent


@pytest.mark.parametrize('inherited, sent_ebs', [
    ({'VolumeSize': 50, 'VolumeType': 'io2', 'Iops': 4000,
      'Encrypted': True, 'KmsKeyId': 'alias/example-lab-key'},
     {'VolumeSize': 50, 'VolumeType': 'io2', 'Iops': 4000,
      'Encrypted': True, 'KmsKeyId': 'alias/example-lab-key'}),
    # Encrypted=False is the AMI reporting "not encrypted", not a request to
    # stay so: it is left out, so the snapshot's state and the account's
    # encryption-by-default decide, as for any disk the request omits.
    ({'VolumeSize': 40, 'VolumeType': 'gp3', 'Iops': 3000, 'Throughput': 250,
      'Encrypted': False},
     {'VolumeSize': 40, 'VolumeType': 'gp3', 'Iops': 3000, 'Throughput': 250}),
], ids=['io2-encrypted', 'gp3-unencrypted'])
def test_disposable_ami_disks_keep_every_inherited_ebs_setting(
        aws_instance_backend, monkeypatch, inherited, sent_ebs):
    # Catches restating an inherited data disk from its snapshot and size
    # alone: the launch would silently drop its volume type, IOPS,
    # throughput or encryption key, changing the disk the AMI defines; or an
    # explicit Encrypted=False overriding the account's encryption default.
    backend, spec, ec2, resources = aws_instance_backend
    _with_image_mappings(monkeypatch, ec2, extra=[
        {'DeviceName': '/dev/sdf', 'Ebs': dict(inherited, DeleteOnTermination=False)}])
    sent = _stop_at_run_instances(ec2)
    with pytest.raises(_RequestBuilt):
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [request] = sent
    mappings = {m['DeviceName']: m['Ebs'] for m in request['BlockDeviceMappings']}
    assert mappings['/dev/sdf'] == dict(sent_ebs, DeleteOnTermination=True)
    assert ('Encrypted' in mappings['/dev/sdf']) is inherited['Encrypted']
    # The root keeps its resize-only rule: everything else is the AMI's.
    assert mappings['/dev/sda1'] == {'VolumeSize': 24, 'DeleteOnTermination': True}


@pytest.mark.parametrize('extra, root', [
    ([{'Ebs': {'VolumeSize': 5, 'DeleteOnTermination': False}}], None),
    ([{'DeviceName': '/dev/sdf', 'Ebs': {'DeleteOnTermination': False}}], None),
    ([{'DeviceName': '/dev/sdf'}], None),
    ([], {'DeviceName': '/dev/sda1', 'VirtualName': 'ephemeral0'}),
])
def test_create_refuses_an_image_mapping_it_cannot_make_disposable(
        aws_instance_backend, monkeypatch, extra, root):
    # Catches a launch inheriting an AMI disk whose deletion at termination
    # the request cannot pin.
    backend, spec, ec2, resources = aws_instance_backend
    _with_image_mappings(monkeypatch, ec2, extra=extra, root=root)
    with pytest.raises(BackendError):
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    assert not [r for r in backend.list_managed() if r.kind == 'instance']


def test_ensure_slot_creates_one_private_subnet_with_exact_cidr_az_and_tags(aws_network_backend):
    # Catches slot drift, duplicate subnets, and automatic public-address assignment.
    backend, spec, ec2, resources = aws_network_backend
    before = int(time.time())
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 3)):
        backend.ensure_slot(3)
        backend.ensure_slot(3)
    subnets = ec2.describe_subnets(Filters=[
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']},
        {'Name': f'tag:{TAG_SLOT}', 'Values': ['3']},
    ])['Subnets']
    assert len(subnets) == 1
    subnet = subnets[0]
    assert subnet['VpcId'] == resources['vpc_id']
    assert subnet['CidrBlock'] == cloud_slot_cidr('172.20.0.0/16', 3)
    assert subnet['AvailabilityZone'] == 'us-east-1a'
    assert subnet['MapPublicIpOnLaunch'] is False
    tags = {t['Key']: t['Value'] for t in subnet['Tags']}
    assert before <= int(tags.pop(TAG_CREATED)) <= int(time.time())
    assert tags == {
        'Name': slot_network_name('test-site', 3),
        TAG_SITE: 'test-site', TAG_SLOT: '3', TAG_KIND: 'slot-network',
    }


def test_network_inventory_carries_creation_time_and_deployment_key(aws_network_backend):
    # Catches leaked SGs/subnets being invisible to the age-gated sweeper (C2)
    # and SG identity disagreeing with GCP's 16-character key (C1).
    backend, spec, ec2, resources = aws_network_backend
    before = int(time.time())
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    managed = {r.kind: r for r in backend.list_managed() if r.kind != 'instance'}
    assert managed['deployment-network'].deployment_id == deployment_key(spec.deployment_id)
    assert managed['slot-network'].network_slot == 1
    for resource in managed.values():
        assert before <= resource.created_at_epoch <= int(time.time())


def test_network_inventory_reports_no_age_for_a_missing_or_malformed_created_tag(
        aws_network_backend):
    # Catches a garbled timestamp reading as an age the sweeper may act on.
    backend, spec, ec2, resources = aws_network_backend
    vpc_id = resources['vpc_id']
    group_id = _group(ec2, vpc_id, 'f' * 32)
    subnet_id = _subnet(ec2, vpc_id, 5, _slot_tags(5))
    ec2.create_tags(Resources=[group_id], Tags=_tags({TAG_CREATED: 'soon'}))
    ec2.delete_tags(Resources=[subnet_id], Tags=[{'Key': TAG_CREATED}])
    managed = {r.provider_id: r for r in backend.list_managed()}
    assert managed[group_id].created_at_epoch is None
    assert managed[group_id].deployment_id == 'f' * 16
    assert managed[subnet_id].created_at_epoch is None
    assert managed[subnet_id].network_slot == 5


def test_create_without_slot_subnet_raises_slot_network_missing(aws_network_backend):
    # Catches a launch into a slot a concurrent retirement just released (I3).
    backend, spec, ec2, resources = aws_network_backend
    with pytest.raises(SlotNetworkMissing):
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    assert not [r for r in backend.list_managed() if r.kind == 'instance']


def test_create_launches_only_into_this_sites_single_slot_subnet(aws_network_backend):
    # Catches a launch landing in a foreign-site, foreign-group or ambiguous network.
    backend, spec, ec2, resources = aws_network_backend
    vpc_id = resources['vpc_id']
    _subnet(ec2, vpc_id, 400, _slot_tags(1, site='other-site'))
    foreign_group = _group(ec2, vpc_id, spec.deployment_id, site='other-site')
    with pytest.raises(SlotNetworkMissing):
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    _ensure_slot(backend, 1)
    [own_subnet] = _site_subnets(ec2, 1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [instance] = ec2.describe_instances(
        InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    [own_group] = _deployment_groups(ec2, spec.deployment_id)
    assert instance['SubnetId'] == own_subnet['SubnetId']
    assert [g['GroupId'] for g in instance['NetworkInterfaces'][0]['Groups']] == [
        own_group['GroupId']]
    assert own_group['GroupId'] != foreign_group
    backend.destroy(resources['ref'](launched), DestroyPolicy.STRICT)
    _subnet(ec2, vpc_id, 401, _slot_tags(1))
    with pytest.raises(BackendError) as raised:
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    assert not isinstance(raised.value, SlotNetworkMissing)


def test_ensure_slot_refuses_a_misplaced_or_foreign_subnet(aws_network_backend):
    # Catches a slot reusing a subnet at the wrong CIDR/AZ/VPC, or adopting
    # another portal's subnet that occupies the slot's CIDR.
    backend, spec, ec2, resources = aws_network_backend
    vpc_id = resources['vpc_id']
    _subnet(ec2, vpc_id, 400, _slot_tags(2))
    with pytest.raises(BackendError):
        _ensure_slot(backend, 2)
    assert len(_site_subnets(ec2, 2)) == 1
    other_vpc = ec2.create_vpc(CidrBlock='172.20.0.0/16')['Vpc']['VpcId']
    _subnet(ec2, other_vpc, 3, _slot_tags(3))
    with pytest.raises(BackendError):
        _ensure_slot(backend, 3)
    foreign = _subnet(ec2, vpc_id, 4, _slot_tags(4, site='other-site'))
    with pytest.raises(BackendError):
        _ensure_slot(backend, 4)
    assert _site_subnets(ec2, 4) == []
    [kept] = ec2.describe_subnets(SubnetIds=[foreign])['Subnets']
    assert {t['Key']: t['Value'] for t in kept['Tags']}[TAG_SITE] == 'other-site'


def test_ensure_slot_turns_off_public_addressing_on_a_reused_subnet(aws_network_backend):
    # Catches a reused slot subnet handing student VMs public addresses.
    backend, spec, ec2, resources = aws_network_backend
    subnet_id = _subnet(ec2, resources['vpc_id'], 6, _slot_tags(6))
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={'Value': True})
    _ensure_slot(backend, 6)
    [subnet] = _site_subnets(ec2, 6)
    assert subnet['SubnetId'] == subnet_id
    assert subnet['MapPublicIpOnLaunch'] is False


def test_create_uses_exact_deployment_security_group_rules(aws_network_backend):
    # Catches lateral access between deployments and the default allow-all egress surviving.
    backend, spec, ec2, resources = aws_network_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    networked_spec = replace(spec, vmid=701, deployment_id='e' * 32)
    launched = backend.create(networked_spec, 'us-east-1a', heartbeat=lambda: None)
    [group] = ec2.describe_security_groups(Filters=[
        {'Name': f'tag:{TAG_DEPLOYMENT}', 'Values': ['e' * 32]},
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']},
    ])['SecurityGroups']
    ingress = _flatten_group_rules(group['IpPermissions'])
    egress = _flatten_group_rules(group['IpPermissionsEgress'])
    expected = {('-1', 'group', group['GroupId']),
                ('-1', 'cidr', '100.64.0.0/10')}
    assert ingress == expected
    assert egress == expected
    assert ('-1', 'cidr', '0.0.0.0/0') not in egress
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    # moto 5.2.5 reports a NetworkInterfaces-launched instance's groups only
    # on its interface (EC2 reports both; see the T11 create test).
    assert [g['GroupId'] for g in instance['NetworkInterfaces'][0]['Groups']] == [
        group['GroupId']]
    assert 'PublicIpAddress' not in instance
    tags = {t['Key']: t['Value'] for t in group['Tags']}
    assert int(tags.pop(TAG_CREATED)) <= int(time.time())
    assert tags == {
        'Name': deployment_network_name('test-site', 'e' * 32),
        TAG_SITE: 'test-site', TAG_DEPLOYMENT: 'e' * 32,
        TAG_KIND: 'deployment-network',
    }
    assert group['GroupName'] == deployment_network_name('test-site', 'e' * 32)
    assert group['VpcId'] == resources['vpc_id']


def test_create_converges_a_reused_deployment_group_to_the_exact_rules(aws_network_backend):
    # Catches a reused group (a concurrent creator's, or one edited since)
    # keeping internet access or SSH from anywhere.
    backend, spec, ec2, resources = aws_network_backend
    group_id = _group(ec2, resources['vpc_id'], spec.deployment_id)
    ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[{
        'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22,
        'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}])
    _ensure_slot(backend, 1)
    backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    # A second VM of the deployment finds the rules already exact.
    backend.create(replace(spec, vmid=703), 'us-east-1a', heartbeat=lambda: None)
    [group] = _deployment_groups(ec2, spec.deployment_id)
    assert group['GroupId'] == group_id
    expected = {('-1', 'group', group_id), ('-1', 'cidr', '100.64.0.0/10')}
    assert _flatten_group_rules(group['IpPermissions']) == expected
    assert _flatten_group_rules(group['IpPermissionsEgress']) == expected


def test_create_refuses_a_deployment_group_it_cannot_prove_is_its_own(aws_network_backend):
    # Catches a launch joining another deployment's group (shared 16-char
    # key, so the same GroupName) or one in another VPC.
    backend, spec, ec2, resources = aws_network_backend
    vpc_id = resources['vpc_id']
    _ensure_slot(backend, 1)
    twin = 'd' * 16 + 'f' * 16
    _group(ec2, vpc_id, twin)
    with pytest.raises(BackendError):
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    assert _deployment_groups(ec2, spec.deployment_id) == []
    other_vpc = ec2.create_vpc(CidrBlock='10.9.0.0/16')['Vpc']['VpcId']
    other_spec = replace(spec, vmid=702, deployment_id='a' * 32)
    _group(ec2, other_vpc, other_spec.deployment_id)
    with pytest.raises(BackendError):
        backend.create(other_spec, 'us-east-1a', heartbeat=lambda: None)
    assert not [r for r in backend.list_managed() if r.kind == 'instance']


def test_cleanup_deployment_and_release_slot_remove_resources_after_instance(aws_network_backend):
    # Catches a free-but-isolating SG or subnet leaking after the last billable VM is gone.
    backend, spec, ec2, resources = aws_network_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    backend.cleanup_deployment(spec.deployment_id)
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.release_slot(1)
    assert ec2.describe_security_groups(Filters=[
        {'Name': f'tag:{TAG_DEPLOYMENT}', 'Values': [spec.deployment_id]}])['SecurityGroups'] == []
    assert ec2.describe_subnets(Filters=[
        {'Name': f'tag:{TAG_SLOT}', 'Values': ['1']},
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']}])['Subnets'] == []
    key = deployment_key(spec.deployment_id)
    assert all(r.deployment_id is None or deployment_key(r.deployment_id) != key
               for r in backend.list_managed())
    backend.cleanup_deployment(spec.deployment_id)  # already gone: done
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.release_slot(1)


def test_cleanup_deployment_and_release_slot_leave_other_identities(aws_network_backend):
    # Catches cleanup deleting another portal's, deployment's or slot's network.
    backend, spec, ec2, resources = aws_network_backend
    vpc_id = resources['vpc_id']
    survivors = {
        'foreign group': _group(ec2, vpc_id, spec.deployment_id, site='other-site'),
        'other deployment': _group(ec2, vpc_id, 'f' * 32),
    }
    _group(ec2, vpc_id, spec.deployment_id)
    foreign_subnet = _subnet(ec2, vpc_id, 400, _slot_tags(1, site='other-site'))
    _ensure_slot(backend, 1)
    _ensure_slot(backend, 2)
    backend.cleanup_deployment(spec.deployment_id)
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.release_slot(1)
    remaining = {g['GroupId'] for g in ec2.describe_security_groups()['SecurityGroups']}
    assert set(survivors.values()) <= remaining
    assert _deployment_groups(ec2, spec.deployment_id) == []
    assert _site_subnets(ec2, 1) == []
    assert len(_site_subnets(ec2, 2)) == 1
    assert ec2.describe_subnets(SubnetIds=[foreign_subnet])['Subnets']


def test_network_cleanup_propagates_dependency_violation(aws_network_backend):
    # Catches a still-attached group or subnet being reported as cleaned up,
    # so the caller and sweeper would never retry it.
    backend, spec, ec2, resources = aws_network_backend
    group_id = _group(ec2, resources['vpc_id'], spec.deployment_id)
    _ensure_slot(backend, 1)

    def in_use(**kwargs):
        raise ClientError({'Error': {'Code': 'DependencyViolation',
                                     'Message': 'in use'}}, 'Delete')

    backend._ec2.delete_security_group = in_use
    backend._ec2.delete_subnet = in_use
    with pytest.raises(BackendError):
        backend.cleanup_deployment(spec.deployment_id)
    with pytest.raises(BackendError):
        backend.release_slot(1)
    del backend._ec2.delete_security_group  # the fixture's client is the backend's
    del backend._ec2.delete_subnet
    assert ec2.describe_security_groups(GroupIds=[group_id])['SecurityGroups']
    assert len(_site_subnets(ec2, 1)) == 1


def test_list_managed_and_delete_resource_cover_all_aws_resource_kinds(aws_network_backend):
    # Catches the orphan sweeper being unable to see or dispatch AWS network leftovers.
    backend, spec, ec2, resources = aws_network_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    managed = backend.list_managed()
    assert {'instance', 'deployment-network', 'slot-network'} <= {r.kind for r in managed}
    instance_resource = next(r for r in managed if r.provider_id == launched.provider_id)
    backend.delete_resource(instance_resource)
    assert backend.confirm_absent(resources['ref'](launched)) is True
    for resource in [r for r in backend.list_managed()
                     if r.kind in {'deployment-network', 'slot-network'}]:
        backend.delete_resource(resource)
    key = deployment_key(spec.deployment_id)
    assert not [r for r in backend.list_managed()
                if (r.deployment_id is not None and deployment_key(r.deployment_id) == key)
                or r.network_slot == 1]


def test_delete_resource_rechecks_site_identity_before_mutation(aws_network_backend):
    # Catches a stale inventory entry deleting a resource that another portal now owns.
    backend, spec, ec2, resources = aws_network_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    resource = next(r for r in backend.list_managed()
                    if r.provider_id == launched.provider_id)
    ec2.create_tags(Resources=[launched.provider_id],
                    Tags=[{'Key': TAG_SITE, 'Value': 'other-site'}])
    with pytest.raises(BackendError):
        backend.delete_resource(resource)
    assert _instance_state(ec2, launched.provider_id) == 'running'


@pytest.mark.parametrize('kind, tag, value', [
    ('deployment-network', TAG_SITE, 'other-site'),
    ('deployment-network', TAG_KIND, 'slot-network'),
    ('deployment-network', TAG_DEPLOYMENT, 'f' * 32),
    ('slot-network', TAG_SITE, 'other-site'),
    ('slot-network', TAG_KIND, 'deployment-network'),
    ('slot-network', TAG_SLOT, '2'),
])
def test_delete_resource_refuses_a_network_retagged_since_listing(
        aws_network_backend, kind, tag, value):
    # Catches a sweep deleting a group or subnet another portal, deployment
    # or slot owns now.
    backend, spec, ec2, resources = aws_network_backend
    _group(ec2, resources['vpc_id'], spec.deployment_id)
    _ensure_slot(backend, 1)
    [resource] = [r for r in backend.list_managed() if r.kind == kind]
    ec2.create_tags(Resources=[resource.provider_id], Tags=_tags({tag: value}))
    with pytest.raises(DestroyRefused):
        backend.delete_resource(resource)
    if kind == 'deployment-network':
        assert ec2.describe_security_groups(GroupIds=[resource.provider_id])['SecurityGroups']
    else:
        assert ec2.describe_subnets(SubnetIds=[resource.provider_id])['Subnets']


def test_delete_resource_network_absent_is_done_and_unverifiable_raises(aws_network_backend):
    # Catches a re-run sweep failing on a resource already gone, or a
    # describe failure being read as permission to delete.
    backend, spec, ec2, resources = aws_network_backend
    _group(ec2, resources['vpc_id'], spec.deployment_id)
    _ensure_slot(backend, 1)
    listed = [r for r in backend.list_managed()
              if r.kind in {'deployment-network', 'slot-network'}]
    assert len(listed) == 2

    def throttled(**kwargs):
        raise ClientError({'Error': {'Code': 'RequestLimitExceeded',
                                     'Message': 'slow down'}}, 'Describe')

    backend._ec2.describe_security_groups = throttled
    backend._ec2.describe_subnets = throttled
    for resource in listed:
        with pytest.raises(BackendError) as raised:
            backend.delete_resource(resource)
        assert not isinstance(raised.value, DestroyRefused)
    del backend._ec2.describe_security_groups
    del backend._ec2.describe_subnets
    assert len([r for r in backend.list_managed()
                if r.kind in {'deployment-network', 'slot-network'}]) == 2
    for resource in listed:
        backend.delete_resource(resource)
        backend.delete_resource(resource)  # already gone: done
    assert not [r for r in backend.list_managed()
                if r.kind in {'deployment-network', 'slot-network'}]
    with pytest.raises(BackendError):
        backend.delete_resource(replace(listed[0], kind='router'))


def test_router_start_stop_is_idempotent_under_reserved_lock(aws_network_backend):
    # Catches the shared router never starting before a launch or never scaling down.
    backend, spec, ec2, resources = aws_network_backend
    router_id = resources['router_instance_id']
    ec2.stop_instances(InstanceIds=[router_id])
    ec2.get_waiter('instance_stopped').wait(
        InstanceIds=[router_id], WaiterConfig={'Delay': 1, 'MaxAttempts': 10})
    with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
        backend.ensure_router_running()
        backend.ensure_router_running()
    assert _instance_state(ec2, router_id) == 'running'
    with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
        backend.stop_router()
        backend.stop_router()
    assert _instance_state(ec2, router_id) == 'stopped'


def test_router_missing_terminated_or_unconfigured_raises(aws_network_backend, monkeypatch):
    # Catches a launch proceeding with no router to reach it, or a stop
    # call reporting success for a router that does not exist.
    backend, spec, ec2, resources = aws_network_backend
    config = importlib.import_module('app.config')
    router_id = resources['router_instance_id']
    ec2.terminate_instances(InstanceIds=[router_id])
    for configured in (router_id, 'i-00000000000000000', None):
        monkeypatch.setattr(config, 'AWS_ROUTER_INSTANCE_ID', configured)
        with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
            with pytest.raises(BackendError):
                backend.ensure_router_running()
            with pytest.raises(BackendError):
                backend.stop_router()


def test_cleanup_and_release_reverify_each_network_before_deleting(
        aws_network_backend, monkeypatch):
    # Catches a stale enumeration deleting a group or subnet that another
    # portal or deployment took over between the listing and the delete.
    backend, spec, ec2, resources = aws_network_backend
    group_id = _group(ec2, resources['vpc_id'], spec.deployment_id)
    _ensure_slot(backend, 1)
    [subnet] = _site_subnets(ec2, 1)
    fired = _after_enumeration(
        monkeypatch, ec2, 'describe_security_groups',
        _retag(ec2, 'SecurityGroups', 'GroupId', {TAG_DEPLOYMENT: 'f' * 32}))
    with pytest.raises(BackendError):
        backend.cleanup_deployment(spec.deployment_id)
    assert fired
    assert ec2.describe_security_groups(GroupIds=[group_id])['SecurityGroups']
    fired = _after_enumeration(
        monkeypatch, ec2, 'describe_subnets',
        _retag(ec2, 'Subnets', 'SubnetId', {TAG_SITE: 'other-site'}))
    with pytest.raises(BackendError):
        with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
            backend.release_slot(1)
    assert fired
    assert ec2.describe_subnets(SubnetIds=[subnet['SubnetId']])['Subnets']


def test_cleanup_refuses_a_correctly_tagged_group_outside_the_lab_vpc(aws_network_backend):
    # Catches cleanup deleting a group in a VPC this portal does not manage.
    backend, spec, ec2, resources = aws_network_backend
    other_vpc = ec2.create_vpc(CidrBlock='10.9.0.0/16')['Vpc']['VpcId']
    group_id = _group(ec2, other_vpc, spec.deployment_id)
    with pytest.raises(DestroyRefused):
        backend.cleanup_deployment(spec.deployment_id)
    assert ec2.describe_security_groups(GroupIds=[group_id])['SecurityGroups']


@pytest.mark.parametrize('tag, value', [(TAG_SITE, 'other-site'),
                                        (TAG_DEPLOYMENT, 'f' * 32)])
def test_create_sets_no_rules_on_a_group_whose_ownership_changed_after_lookup(
        aws_network_backend, monkeypatch, tag, value):
    # Catches convergence rewriting the rules of a group that is no longer
    # this deployment's by the time its fresh describe runs.
    backend, spec, ec2, resources = aws_network_backend
    group_id = _group(ec2, resources['vpc_id'], spec.deployment_id)
    _ensure_slot(backend, 1)
    fired = _after_enumeration(
        monkeypatch, ec2, 'describe_security_groups',
        _retag(ec2, 'SecurityGroups', 'GroupId', {tag: value}))
    with pytest.raises(BackendError):
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    assert fired
    [group] = ec2.describe_security_groups(GroupIds=[group_id])['SecurityGroups']
    assert _flatten_group_rules(group['IpPermissions']) == set()
    assert _flatten_group_rules(group['IpPermissionsEgress']) == {('-1', 'cidr', '0.0.0.0/0')}
    assert not [r for r in backend.list_managed() if r.kind == 'instance']


_RULE_MUTATIONS = ('revoke_security_group_egress', 'authorize_security_group_egress',
                   'revoke_security_group_ingress', 'authorize_security_group_ingress')


@pytest.mark.parametrize('after, egress, ingress', [
    # A fresh group: revoke allow-all egress, grant the two egress rules,
    # then the two ingress rules (cidr before group).
    (1, set(), set()),
    (2, {('-1', 'cidr', '100.64.0.0/10')}, set()),
    (4, 'isolation', {('-1', 'cidr', '100.64.0.0/10')}),
])
def test_convergence_stops_once_the_group_changes_hands_between_rule_mutations(
        aws_network_backend, monkeypatch, after, egress, ingress):
    # Catches convergence that proved ownership once and then kept rewriting
    # the rules of a group retagged to another deployment mid-way.
    backend, spec, ec2, resources = aws_network_backend
    group_id = _group(ec2, resources['vpc_id'], spec.deployment_id)
    _ensure_slot(backend, 1)
    calls = []
    for name in _RULE_MUTATIONS:
        def mutate(_real=getattr(ec2, name), **kwargs):
            calls.append(kwargs)
            result = _real(**kwargs)
            if len(calls) == after:
                ec2.create_tags(Resources=[group_id],
                                Tags=_tags({TAG_DEPLOYMENT: 'f' * 32}))
            return result
        monkeypatch.setattr(ec2, name, mutate)
    with pytest.raises(BackendError):
        backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    assert len(calls) == after
    [group] = ec2.describe_security_groups(GroupIds=[group_id])['SecurityGroups']
    if egress == 'isolation':
        egress = {('-1', 'group', group_id), ('-1', 'cidr', '100.64.0.0/10')}
    assert _flatten_group_rules(group['IpPermissionsEgress']) == egress
    assert _flatten_group_rules(group['IpPermissions']) == ingress
    assert not [r for r in backend.list_managed() if r.kind == 'instance']


@pytest.mark.parametrize('retagged', [False, True])
@pytest.mark.parametrize('action, transitional, waiter, settled, final', [
    ('start', 'stopping', 'instance_stopped', 'stopped', 'running'),
    ('stop', 'pending', 'instance_running', 'running', 'stopped'),
])
def test_router_ownership_is_rechecked_after_a_transitional_wait(
        aws_network_backend, monkeypatch, retagged, action, transitional, waiter,
        settled, final):
    # Catches a start or stop reaching a machine that stopped being this
    # site's router while the adapter waited out its transition: EC2's
    # state waiter never looks at tags.
    backend, spec, ec2, resources = aws_network_backend
    router_id = resources['router_instance_id']
    if action == 'start':
        ec2.stop_instances(InstanceIds=[router_id])
        ec2.get_waiter('instance_stopped').wait(
            InstanceIds=[router_id], WaiterConfig={'Delay': 1, 'MaxAttempts': 10})
    real_describe, real_waiter = ec2.describe_instances, ec2.get_waiter
    reads = []

    def describe_instances(**kwargs):
        # moto settles instantly: the first read sees the transition.
        response = real_describe(**kwargs)
        if not reads:
            for reservation in response['Reservations']:
                for instance in reservation['Instances']:
                    instance['State'] = {'Code': 0, 'Name': transitional}
        reads.append(kwargs)
        return response

    class _Retagging:
        def __init__(self, inner):
            self._inner = inner

        def wait(self, **kwargs):
            ec2.create_tags(Resources=[router_id], Tags=_tags({TAG_SITE: 'other-site'}))
            return self._inner.wait(**kwargs)

    def get_waiter(name):
        inner = real_waiter(name)
        # Only the transitional wait: the settling wait has the other name.
        return _Retagging(inner) if retagged and name == waiter else inner

    monkeypatch.setattr(ec2, 'describe_instances', describe_instances)
    monkeypatch.setattr(ec2, 'get_waiter', get_waiter)
    run = backend.ensure_router_running if action == 'start' else backend.stop_router
    with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
        if retagged:
            with pytest.raises(BackendError):
                run()
        else:
            run()
    assert _instance_state(ec2, router_id) == (settled if retagged else final)


@pytest.mark.parametrize('winner_deployment, adopted', [('d' * 32, True),
                                                        ('d' * 16 + 'f' * 16, False)])
def test_group_duplicate_adopts_only_a_winner_with_the_exact_identity(
        aws_network_backend, monkeypatch, winner_deployment, adopted):
    # Catches a lost create race joining a group of another deployment that
    # shares the 16-character key, or failing when the winner is its own.
    backend, spec, ec2, resources = aws_network_backend
    _ensure_slot(backend, 1)
    real_create = ec2.create_security_group
    winners = []

    def raced(**kwargs):
        # A concurrent creator wins the name first; ours then hits Duplicate.
        monkeypatch.setattr(ec2, 'create_security_group', real_create)
        winners.append(_group(ec2, resources['vpc_id'], winner_deployment))
        return real_create(**kwargs)

    monkeypatch.setattr(ec2, 'create_security_group', raced)
    if adopted:
        launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
        [instance] = ec2.describe_instances(
            InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
        assert [g['GroupId'] for g in instance['NetworkInterfaces'][0]['Groups']] == winners
        [group] = ec2.describe_security_groups(GroupIds=winners)['SecurityGroups']
        assert _flatten_group_rules(group['IpPermissionsEgress']) == {
            ('-1', 'group', winners[0]), ('-1', 'cidr', '100.64.0.0/10')}
    else:
        with pytest.raises(BackendError):
            backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
        assert not [r for r in backend.list_managed() if r.kind == 'instance']
        [group] = ec2.describe_security_groups(GroupIds=winners)['SecurityGroups']
        assert _flatten_group_rules(group['IpPermissionsEgress']) == {
            ('-1', 'cidr', '0.0.0.0/0')}
    assert len(winners) == 1


@pytest.mark.parametrize('winner_site, winner_zone, adopted', [
    ('test-site', 'us-east-1a', True),
    ('other-site', 'us-east-1a', False),
    ('test-site', 'us-east-1b', False),
])
def test_subnet_conflict_adopts_only_a_winner_with_exact_identity_and_placement(
        aws_network_backend, monkeypatch, winner_site, winner_zone, adopted):
    # Catches a lost create race adopting another portal's subnet, or one
    # at the slot's CIDR but in the wrong zone.
    backend, spec, ec2, resources = aws_network_backend
    real_create = ec2.create_subnet
    winners = []

    def raced(**kwargs):
        # A concurrent creator takes the CIDR first; ours then conflicts.
        monkeypatch.setattr(ec2, 'create_subnet', real_create)
        winners.append(real_create(
            VpcId=resources['vpc_id'], CidrBlock=kwargs['CidrBlock'],
            AvailabilityZone=winner_zone,
            TagSpecifications=[{'ResourceType': 'subnet',
                                'Tags': _tags(_slot_tags(7, site=winner_site))}],
        )['Subnet']['SubnetId'])
        return real_create(**kwargs)

    monkeypatch.setattr(ec2, 'create_subnet', raced)
    if adopted:
        _ensure_slot(backend, 7)
        assert [s['SubnetId'] for s in _site_subnets(ec2, 7)] == winners
    else:
        with pytest.raises(BackendError):
            _ensure_slot(backend, 7)
    assert len(winners) == 1
    vpc_subnets = ec2.describe_subnets(Filters=[
        {'Name': 'vpc-id', 'Values': [resources['vpc_id']]}])['Subnets']
    assert [s['SubnetId'] for s in vpc_subnets] == winners


@pytest.mark.parametrize('tag, value', [(TAG_SITE, 'other-site'), (TAG_KIND, 'instance')])
def test_router_of_another_site_or_kind_is_never_started_or_stopped(
        aws_network_backend, tag, value):
    # Catches a misconfigured AWS_ROUTER_INSTANCE_ID starting or stopping
    # a machine that is not this site's router.
    backend, spec, ec2, resources = aws_network_backend
    router_id = resources['router_instance_id']
    ec2.create_tags(Resources=[router_id], Tags=_tags({tag: value}))
    with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
        with pytest.raises(BackendError):
            backend.stop_router()
    assert _instance_state(ec2, router_id) == 'running'
    ec2.stop_instances(InstanceIds=[router_id])
    ec2.get_waiter('instance_stopped').wait(
        InstanceIds=[router_id], WaiterConfig={'Delay': 1, 'MaxAttempts': 10})
    with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
        with pytest.raises(BackendError):
            backend.ensure_router_running()
    assert _instance_state(ec2, router_id) == 'stopped'


def test_moto_attached_security_group_dependency_capability(aws_network_backend):
    # Documents whether this moto release can prove EC2's attached-ENI deletion refusal.
    backend, spec, ec2, resources = aws_network_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    # moto 5.2.5 reports a NetworkInterfaces-launched instance's groups only
    # on its interface; the instance-level list the brief reads is empty.
    group_id = ec2.describe_instances(InstanceIds=[launched.provider_id])[
        'Reservations'][0]['Instances'][0]['NetworkInterfaces'][0]['Groups'][0]['GroupId']
    try:
        ec2.delete_security_group(GroupId=group_id)
    except ClientError as error:
        assert error.response['Error']['Code'] == 'DependencyViolation'
        assert ec2.describe_security_groups(GroupIds=[group_id])['SecurityGroups']
    else:
        pytest.skip('selected moto does not model attached-ENI SG dependency')


def test_get_backend_aws_lazily_returns_one_aws_backend(app, monkeypatch):
    # Catches the registry building a client per call or AWS staying unregistered.
    _fake_credentials(monkeypatch)
    config = _patch_aws_config(monkeypatch)
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    with mock_aws():
        backend = get_backend('aws')
        assert isinstance(backend, AWSBackend)
        assert get_backend('aws') is backend
        assert backend._ec2.meta.region_name == 'us-east-1'


def test_importing_backends_does_not_import_aws_or_boto3_in_subprocess():
    # Catches app.backends eagerly importing aws.py (ruling I8's import cycle).
    program = '''
import sys
import app.backends
assert "app.backends.aws" not in sys.modules, "aws.py imported eagerly"
assert "boto3" not in sys.modules, "boto3 imported eagerly"
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_aws_backend_imports_after_gevent_patch_in_subprocess():
    # Catches AWSBackend adding an import that violates run_prod.py's gevent order.
    program = '''
from gevent import monkey
monkey.patch_all()
from app.backends.aws import AWSBackend
import boto3
client = boto3.client("ec2", region_name="us-east-1", aws_access_key_id="testing",
                      aws_secret_access_key="testing", endpoint_url="http://127.0.0.1:9")
AWSBackend(ec2_client=client)
assert monkey.is_module_patched("socket")
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                            env=dict(os.environ, AWS_EC2_METADATA_DISABLED='true'),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
