"""Stateful in-memory VMBackend and the shared contract-case harness.

Later tasks (T7, T8, T16, T19) assert against FakeBackend's public state, so
its attribute names are part of the plan's contract (ruling I5): tests read
what the fake now holds, never a list of mock calls.
"""

import importlib
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Set
from unittest.mock import patch

from app.backends import register_backend_factory, reset_backends_for_tests
from app.backends.aws import AWSBackend
from app.backends.base import (
    GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, BackendError,
    DestroyPolicy, DestroyRefused, LaunchedVM, ManagedResource,
    SlotNetworkMissing, VMBackend, VMRef, VMSpec,
)
from app.backends.cloud_common import TAG_KIND, TAG_SITE

MEMORY_LOCATION = 'memory-zone'
MEMORY_IP = '192.0.2.10'


class FakeBackend:
    """A cloud-shaped backend whose whole provider state is these fields."""

    def __init__(self, name: str):
        self.name = name
        self.instances: Dict[int, LaunchedVM] = {}
        self.reservations: Set[str] = set()
        self.deployment_networks: Set[str] = set()
        self.slot_networks: Set[int] = set()
        self.router_running = False
        self.destroyed: Dict[int, DestroyPolicy] = {}
        self.expiry: Dict[int, int] = {}
        self.observed_locks: Dict[str, Set[int]] = {}
        # Failure knobs.
        self.inspect_result: Optional[str] = None
        self.fail_create_after_launch = False
        self.fail_next_create = False
        self.fail_set_expiry = False
        self.lose_slot_once = False
        # What a real provider would carry as tags on the instance.
        self._instance_tags: Dict[int, Dict[str, object]] = {}

    def _observe_locks(self, method: str) -> None:
        # The lifecycle-lock table is the seam T7 asserts lock scope through:
        # which keys this thread held when the operation ran, not which
        # context managers were entered somewhere.
        from app import proxmox_service
        me = threading.get_ident()
        with proxmox_service._vm_lifecycle_locks_guard:
            held = {key for key, entry in proxmox_service._vm_lifecycle_locks.items()
                    if entry['owner'] == me}
        self.observed_locks.setdefault(method, set()).update(held)

    # Admission

    def admit(self, spec: VMSpec, *, reservation_id: str,
              load_balancing_config: Optional[Mapping]) -> str:
        self.reservations.add(reservation_id)
        return MEMORY_LOCATION

    def renew_admission(self, reservation_id: str) -> bool:
        return reservation_id in self.reservations

    def release_admission(self, reservation_id: Optional[str]) -> bool:
        if reservation_id not in self.reservations:
            return False
        self.reservations.discard(reservation_id)
        return True

    # Instances

    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None]) -> LaunchedVM:
        self._observe_locks('create')
        slot = spec.network_slot
        if self.lose_slot_once:
            # Models the slot being released between ensure_slot and create.
            self.lose_slot_once = False
            self.slot_networks.discard(slot)
        if slot not in self.slot_networks:
            raise SlotNetworkMissing(f'slot network {slot} does not exist')
        if self.fail_next_create:
            self.fail_next_create = False
            raise RuntimeError('injected create failure before launch')
        heartbeat()
        launched = LaunchedVM(vmid=spec.vmid, vm_type=self.name,
                              location=MEMORY_LOCATION, network_slot=slot,
                              provider_id=f'provider-{spec.vmid}')
        self.instances[spec.vmid] = launched
        self._instance_tags[spec.vmid] = {
            'vm_name': spec.vm_name, 'deployment_id': spec.deployment_id}
        self.expiry[spec.vmid] = int(spec.expires_at.timestamp())
        self.deployment_networks.add(spec.deployment_id)
        if self.fail_create_after_launch:
            raise RuntimeError('injected create failure')
        return launched

    def inspect(self, ref: VMRef) -> str:
        if self.inspect_result is not None:
            return self.inspect_result
        launched = self.instances.get(ref.vmid)
        if launched is None:
            return GUEST_ABSENT
        # No provider id (create raised after launch) resolves by VMID alone,
        # as the clouds resolve by site + VMID tags (ruling C7).
        if ref.provider_id is not None and ref.provider_id != launched.provider_id:
            return GUEST_MISMATCH
        return GUEST_MATCH

    def _forget(self, vmid: int) -> None:
        self.instances.pop(vmid, None)
        self._instance_tags.pop(vmid, None)
        self.expiry.pop(vmid, None)

    def destroy(self, ref: VMRef, policy: DestroyPolicy) -> None:
        identity = self.inspect(ref)
        if identity == GUEST_MISMATCH:
            raise DestroyRefused(f'VMID {ref.vmid} identity does not match')
        if identity not in (GUEST_MATCH, GUEST_ABSENT):
            raise BackendError(f'VMID {ref.vmid} identity is {identity}')
        self.destroyed[ref.vmid] = policy
        self._forget(ref.vmid)

    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        return self.inspect(ref) == GUEST_ABSENT

    def ip_addresses(self, ref: VMRef) -> List[str]:
        return [MEMORY_IP] if self.inspect(ref) == GUEST_MATCH else []

    def status(self, ref: VMRef) -> str:
        return 'running' if self.inspect(ref) == GUEST_MATCH else 'unknown'

    def set_expiry(self, ref: VMRef, expires_at: datetime) -> None:
        if self.fail_set_expiry:
            raise BackendError('injected set_expiry failure')
        if self.inspect(ref) != GUEST_MATCH:
            raise BackendError(f'VMID {ref.vmid} is not a matching instance')
        self.expiry[ref.vmid] = int(expires_at.timestamp())

    # Inventory

    def list_managed(self) -> List[ManagedResource]:
        resources = [
            ManagedResource(
                backend=self.name, kind='instance', provider_id=launched.provider_id,
                name=self._instance_tags[vmid]['vm_name'], vmid=vmid,
                location=launched.location,
                deployment_id=self._instance_tags[vmid]['deployment_id'],
                network_slot=launched.network_slot,
                expires_at_epoch=self.expiry.get(vmid), status='running')
            for vmid, launched in sorted(self.instances.items())]
        resources += [
            ManagedResource(backend=self.name, kind='deployment-network',
                            provider_id=f'deployment-{deployment_id}',
                            name=f'deployment-{deployment_id}',
                            deployment_id=deployment_id)
            for deployment_id in sorted(self.deployment_networks)]
        resources += [
            ManagedResource(backend=self.name, kind='slot-network',
                            provider_id=f'slot-{slot}', name=f'slot-{slot}',
                            network_slot=slot)
            for slot in sorted(self.slot_networks)]
        return resources

    def delete_resource(self, resource: ManagedResource) -> None:
        if resource.backend != self.name:
            raise BackendError(f'{resource.provider_id} belongs to {resource.backend}')
        if resource.kind == 'instance':
            launched = self.instances.get(resource.vmid)
            if launched is not None and launched.provider_id != resource.provider_id:
                raise DestroyRefused(f'VMID {resource.vmid} identity does not match')
            self._forget(resource.vmid)
        elif resource.kind == 'deployment-network':
            self.deployment_networks.discard(resource.deployment_id)
        elif resource.kind == 'slot-network':
            self.slot_networks.discard(resource.network_slot)
        else:
            raise BackendError(f'unknown resource kind {resource.kind!r}')

    # Networks and router

    def cleanup_deployment(self, deployment_id: str) -> None:
        self.deployment_networks.discard(deployment_id)

    def ensure_slot(self, slot: int) -> None:
        self._observe_locks('ensure_slot')
        self.slot_networks.add(slot)

    def release_slot(self, slot: int) -> None:
        self.slot_networks.discard(slot)

    def ensure_router_running(self) -> None:
        self._observe_locks('ensure_router_running')
        self.router_running = True

    def stop_router(self) -> bool:
        was_running, self.router_running = self.router_running, False
        return was_running

    def router_stopped(self) -> bool:
        return not self.router_running


@dataclass
class BackendContractCase:
    backend: VMBackend
    spec: VMSpec
    expected_ips: list[str]
    expected_status: str
    cleanup: Callable[[], None]
    # Proxmox's inventory is the cluster-index projection, which carries no
    # expiry (ruling C5); its case sets this False.
    inventory_reports_expiry: bool = True

    def ref(self, launched: LaunchedVM, location: str) -> VMRef:
        """The ref a persisted record of this launch would produce.

        `location` is what admit() returned; the record keeps where the guest
        actually launched (a cloud admits per backend but launches per zone).
        """
        return VMRef(vmid=self.spec.vmid, vm_name=self.spec.vm_name,
                     vm_type=launched.vm_type, location=launched.location,
                     backend=self.spec.backend, provider_id=launched.provider_id,
                     deployment_id=self.spec.deployment_id,
                     network_slot=launched.network_slot)


def make_memory_backend_case(app, name: str = 'aws') -> BackendContractCase:
    backend = FakeBackend(name)
    spec = VMSpec(
        vmid=100, vm_name=f'{name}-memory-100', display_name='memory-100',
        backend=name,
        template={'image_id': 'ami-placeholder', 'instance_type': 'small',
                  'disk_gb': 20},
        memory_mb=1024, cores=1, storage_gb=20,
        is_entry_point=True, entry_slot=101, internal_slot=201,
        expires_at=datetime.now() + timedelta(hours=1),
        deployment_id='a' * 32, owner='student-a')

    def cleanup() -> None:
        # The shared tests start the router and ensure the slot (ruling C3);
        # the fake owns nothing else.
        backend.release_slot(spec.network_slot)
        backend.stop_router()

    return BackendContractCase(backend=backend, spec=spec,
                               expected_ips=[MEMORY_IP],
                               expected_status='running', cleanup=cleanup)


# The moto EC2 world the focused AWS tests and the AWS contract case share.
AWS_TEST_ZONE = 'us-east-1a'
AWS_TEST_VPC_CIDR = '172.20.0.0/16'
AWS_TEST_CONFIG = {
    'AWS_REGION': 'us-east-1', 'AWS_AVAILABILITY_ZONE': AWS_TEST_ZONE,
    'LAB_SITE_ID': 'test-site', 'AWS_MAX_VMS': 2,
    'AWS_LAUNCH_TIMEOUT_SECONDS': 10, 'AWS_TERMINATE_TIMEOUT_SECONDS': 10,
}
AWS_TEST_ENV = {
    'AWS_ACCESS_KEY_ID': 'testing', 'AWS_SECRET_ACCESS_KEY': 'testing',
    'AWS_SECURITY_TOKEN': 'testing', 'AWS_SESSION_TOKEN': 'testing',
    'AWS_DEFAULT_REGION': 'us-east-1',
}


def register_aws_test_image(ec2) -> str:
    """A moto AMI with a real snapshot-backed root device."""
    volume_id = ec2.create_volume(Size=8, AvailabilityZone=AWS_TEST_ZONE)['VolumeId']
    snapshot_id = ec2.create_snapshot(VolumeId=volume_id)['SnapshotId']
    return ec2.register_image(
        Name='test-image', Architecture='x86_64', RootDeviceName='/dev/sda1',
        VirtualizationType='hvm',
        BlockDeviceMappings=[{'DeviceName': '/dev/sda1', 'Ebs': {
            'SnapshotId': snapshot_id, 'VolumeSize': 8,
            'DeleteOnTermination': True, 'VolumeType': 'gp3'}}])['ImageId']


def aws_test_spec(image_id: str) -> VMSpec:
    return VMSpec(
        vmid=700, vm_name='test-aws-700', display_name='Test AWS VM',
        backend='aws',
        template={'image_id': image_id, 'instance_type': 't3.micro', 'disk_gb': 24},
        memory_mb=1024, cores=1, storage_gb=24, is_entry_point=True,
        entry_slot=1, internal_slot=2,
        expires_at=datetime.now() + timedelta(hours=1),
        deployment_id='d' * 32, owner='test-owner')


class RecordingAWSBackend(AWSBackend):
    """The production AWSBackend, which also records each launched
    instance's private address into the case's expected_ips. It adds one
    read-only describe; no provider operation differs."""

    def __init__(self, ec2_client, *, expected_ips: List[str]):
        super().__init__(ec2_client=ec2_client)
        self._expected_ips = expected_ips

    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None]) -> LaunchedVM:
        launched = super().create(spec, location, heartbeat=heartbeat)
        [instance] = self._ec2.describe_instances(
            InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
        self._expected_ips[:] = [instance['PrivateIpAddress']]
        return launched


def make_aws_backend_case(app) -> BackendContractCase:
    """AWSBackend against moto EC2: a lab VPC, an AMI and a stopped router.

    moto and the config patches are started here and stopped by cleanup(),
    because they must outlive this call into the parametrized test.
    """
    import boto3
    from moto import mock_aws

    started = []

    def start(patcher) -> None:
        patcher.start()
        started.append(patcher)

    def stop_all() -> None:
        while started:
            started.pop().stop()

    try:
        start(patch.dict(os.environ, AWS_TEST_ENV))
        os.environ.pop('AWS_PROFILE', None)  # restored by the dict patch
        start(mock_aws())
        ec2 = boto3.client('ec2', region_name='us-east-1')
        vpc_id = ec2.create_vpc(CidrBlock=AWS_TEST_VPC_CIDR)['Vpc']['VpcId']
        image_id = register_aws_test_image(ec2)
        # The operator tags the shared router with its site and kind.
        router_id = ec2.run_instances(
            ImageId=image_id, InstanceType='t3.micro', MinCount=1, MaxCount=1,
            TagSpecifications=[{'ResourceType': 'instance', 'Tags': [
                {'Key': TAG_SITE, 'Value': AWS_TEST_CONFIG['LAB_SITE_ID']},
                {'Key': TAG_KIND, 'Value': 'router'}]}],
        )['Instances'][0]['InstanceId']
        ec2.stop_instances(InstanceIds=[router_id])
        ec2.get_waiter('instance_stopped').wait(
            InstanceIds=[router_id], WaiterConfig={'Delay': 1, 'MaxAttempts': 10})
        start(patch.multiple(
            importlib.import_module('app.config'), **AWS_TEST_CONFIG,
            AWS_LAB_VPC_ID=vpc_id, AWS_LAB_CIDR=AWS_TEST_VPC_CIDR,
            AWS_ROUTER_INSTANCE_ID=router_id))
    except BaseException:
        stop_all()
        raise

    spec = aws_test_spec(image_id)
    expected_ips: List[str] = []
    backend = RecordingAWSBackend(ec2, expected_ips=expected_ips)

    def cleanup() -> None:
        # The shared tests start the router and ensure the slot (ruling C3)
        # and have already terminated the instance.
        try:
            backend.release_slot(spec.network_slot)
            backend.stop_router()
        finally:
            stop_all()

    return BackendContractCase(backend=backend, spec=spec,
                               expected_ips=expected_ips,
                               expected_status='running', cleanup=cleanup)


@contextmanager
def registered_fake_backend(name: str) -> Iterator[FakeBackend]:
    """Enable `name` alongside the configured backends, served by a fresh fake.

    The registry is reset on exit, so a test can never hand its fake (and its
    state) to the next test through the process-wide instance cache.
    """
    config = importlib.import_module('app.config')
    enabled = tuple(config.ENABLED_BACKENDS)
    if name not in enabled:
        enabled += (name,)
    backend = FakeBackend(name)
    with patch.object(config, 'ENABLED_BACKENDS', enabled):
        register_backend_factory(name, lambda: backend)
        try:
            yield backend
        finally:
            reset_backends_for_tests()


# Display state -> the status a backend reports, for the listing fakes below.
_DISPLAY_STATUS = {'present': 'running', 'missing': 'missing',
                   'config_error': 'config_error', 'unknown': 'unknown'}


class StaticDisplayBackend:
    """A backend whose every guest reads as one display state.

    Stands in where a route-level test used to patch `_verify_vm_exists` and
    `_get_vm_ip_addresses`: `state` is the old verify verdict and `ips` the
    old address answer, so the test's assertions stay as they were.
    """

    def __init__(self, state: str, ips=(), name: str = 'proxmox'):
        self.name = name
        self.state = state
        self.ips = list(ips)

    def inspect(self, ref: VMRef) -> str:
        return GUEST_ABSENT if self.state == 'missing' else GUEST_MATCH

    def status(self, ref: VMRef) -> str:
        return _DISPLAY_STATUS[self.state]

    def ip_addresses(self, ref: VMRef) -> List[str]:
        return list(self.ips)

    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        return self.state == 'missing'


class StaticInventoryBackend:
    """A backend whose list_managed() is a fixed Proxmox-shaped inventory.

    `inventory` holds the {vmid, node, vm_type, name} rows the retired
    find_vms_by_name_prefix returned, so sweep tests keep their data.
    """

    def __init__(self, inventory, name: str = 'proxmox'):
        self.name = name
        self.inventory = list(inventory)

    def list_managed(self) -> List[ManagedResource]:
        return [ManagedResource(backend=self.name, kind='instance',
                                provider_id=str(row['vmid']), name=row.get('name'),
                                vmid=row['vmid'], location=row['node'],
                                vm_type=row['vm_type'])
                for row in self.inventory]
