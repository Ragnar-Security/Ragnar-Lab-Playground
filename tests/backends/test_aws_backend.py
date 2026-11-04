"""AWSBackend instance lifecycle and capped admission against moto's EC2.

Every provider assertion reads moto's EC2 state through a real boto3 client;
no test inspects boto call lists.
"""

import hashlib
import importlib
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from app import coordination
from app.backends import get_backend
from app.backends.aws import AWSBackend, _client_token, _waiter_config
from app.backends.base import (
    GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, GUEST_UNKNOWN,
    AdmissionRefused, BackendError, DestroyPolicy, DestroyRefused,
    LaunchedVM, VMRef, VMSpec,
)
from app.backends.cloud_common import (
    TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT, TAG_VMID,
    expires_epoch,
)
from app.models import NodeMemoryReservation, VMRecord, db
from app.routes.vm_creation import named_instances

ROOT = Path(__file__).resolve().parents[2]


def _tags(pairs):
    return [{'Key': key, 'Value': value} for key, value in pairs.items()]


def _register_test_image(ec2):
    volume_id = ec2.create_volume(Size=8, AvailabilityZone='us-east-1a')['VolumeId']
    snapshot_id = ec2.create_snapshot(VolumeId=volume_id)['SnapshotId']
    return ec2.register_image(
        Name='test-image', Architecture='x86_64', RootDeviceName='/dev/sda1',
        VirtualizationType='hvm',
        BlockDeviceMappings=[{'DeviceName': '/dev/sda1', 'Ebs': {
            'SnapshotId': snapshot_id, 'VolumeSize': 8,
            'DeleteOnTermination': True, 'VolumeType': 'gp3'}}])['ImageId']


def _patch_aws_config(monkeypatch):
    config = importlib.import_module('app.config')
    for name, value in {
            'AWS_REGION': 'us-east-1', 'AWS_AVAILABILITY_ZONE': 'us-east-1a',
            'LAB_SITE_ID': 'test-site', 'AWS_MAX_VMS': 2,
            'AWS_LAUNCH_TIMEOUT_SECONDS': 10,
            'AWS_TERMINATE_TIMEOUT_SECONDS': 10}.items():
        monkeypatch.setattr(config, name, value)
    return config


def _fake_credentials(monkeypatch):
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
                 'AWS_SECURITY_TOKEN', 'AWS_SESSION_TOKEN'):
        monkeypatch.setenv(name, 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.delenv('AWS_PROFILE', raising=False)


@pytest.fixture
def aws_instance_backend(app, monkeypatch):
    _fake_credentials(monkeypatch)
    _patch_aws_config(monkeypatch)
    with mock_aws(), app.app_context():
        ec2 = boto3.client('ec2', region_name='us-east-1')
        vpc_id = ec2.create_vpc(CidrBlock='172.20.0.0/16')['Vpc']['VpcId']
        subnet_id = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock='172.20.250.0/24',
            AvailabilityZone='us-east-1a')['Subnet']['SubnetId']
        security_group_id = ec2.create_security_group(
            GroupName='test-instance-sg', Description='test', VpcId=vpc_id)['GroupId']
        image_id = _register_test_image(ec2)
        foreign_instance_id = ec2.run_instances(
            ImageId=image_id, InstanceType='t3.micro', MinCount=1, MaxCount=1,
            SubnetId=subnet_id,
            TagSpecifications=[{'ResourceType': 'instance', 'Tags': _tags(
                {TAG_SITE: 'other-site', TAG_VMID: '700'})}],
        )['Instances'][0]['InstanceId']
        backend = AWSBackend(
            ec2_client=ec2,
            network_resolver=lambda spec: (subnet_id, security_group_id))
        spec = VMSpec(
            vmid=700, vm_name='test-aws-700', display_name='Test AWS VM',
            backend='aws',
            template={'image_id': image_id, 'instance_type': 't3.micro', 'disk_gb': 24},
            memory_mb=1024, cores=1, storage_gb=24, is_entry_point=True,
            entry_slot=1, internal_slot=2,
            expires_at=datetime.now() + timedelta(hours=1),
            deployment_id='d' * 32, owner='test-owner')

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


def test_default_network_resolver_finds_exactly_one_tagged_subnet_and_group(app, monkeypatch):
    # Catches a launch landing in an untagged, foreign-site or ambiguous network.
    _fake_credentials(monkeypatch)
    _patch_aws_config(monkeypatch)
    with mock_aws():
        ec2 = boto3.client('ec2', region_name='us-east-1')
        vpc_id = ec2.create_vpc(CidrBlock='172.20.0.0/16')['Vpc']['VpcId']
        backend = AWSBackend(ec2_client=ec2)
        spec = VMSpec(
            vmid=700, vm_name='test-aws-700', display_name='Test AWS VM',
            backend='aws', template={}, memory_mb=1024, cores=1, storage_gb=24,
            is_entry_point=True, entry_slot=1, internal_slot=2,
            expires_at=datetime.now() + timedelta(hours=1),
            deployment_id='d' * 32, owner='test-owner')
        with pytest.raises(BackendError):
            backend._resolve_network(spec)

        def subnet(cidr, site):
            subnet_id = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr,
                AvailabilityZone='us-east-1a')['Subnet']['SubnetId']
            ec2.create_tags(Resources=[subnet_id], Tags=_tags(
                {TAG_SITE: site, TAG_SLOT: '1', TAG_KIND: 'slot-network'}))
            return subnet_id

        def group(name, site):
            group_id = ec2.create_security_group(
                GroupName=name, Description='test', VpcId=vpc_id)['GroupId']
            ec2.create_tags(Resources=[group_id], Tags=_tags(
                {TAG_SITE: site, TAG_DEPLOYMENT: 'd' * 32,
                 TAG_KIND: 'deployment-network'}))
            return group_id

        subnet('172.20.1.0/24', 'other-site')
        group('test-foreign-sg', 'other-site')
        slot_subnet = subnet('172.20.2.0/24', 'test-site')
        with pytest.raises(BackendError):
            backend._resolve_network(spec)
        deployment_group = group('test-deployment-sg', 'test-site')
        assert backend._resolve_network(spec) == (slot_subnet, deployment_group)
        subnet('172.20.3.0/24', 'test-site')
        with pytest.raises(BackendError):
            backend._resolve_network(spec)


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
