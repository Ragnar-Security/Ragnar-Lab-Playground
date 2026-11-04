"""Amazon EC2 behind the VMBackend contract: instance lifecycle and capped
admission.

Identity is the tag set written at launch, never the DB row: an instance is
this record's only when every contract tag agrees, and a ref without a
provider id (a create that raised after run_instances) is resolved from its
site/VMID/deployment tags. Absence needs positive evidence (NotFound or a
terminated instance); every other provider failure is GUEST_UNKNOWN, so a
transient error can never retire a record whose instance still bills.

Configuration is read as `config.<NAME>` at call time (ruling I8): this
module is imported lazily by the registry factory, and a module-level
`from app.config import ...` would also defeat tests that patch config.
"""

import hashlib
import importlib
import logging
import math
import re
from datetime import datetime
from typing import Callable, List, Mapping, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from app.backends.base import (
    BACKEND_AWS, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, GUEST_UNKNOWN,
    BackendError, DestroyPolicy, DestroyRefused, LaunchedVM, ManagedResource,
    VMRef, VMSpec,
)
from app.backends.cloud_admission import reserve_cloud_capacity
from app.backends.cloud_common import (
    TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT, TAG_VMID,
    expires_epoch,
)

logger = logging.getLogger(__name__)

KIND_INSTANCE = 'instance'
_NOT_FOUND = 'InvalidInstanceID.NotFound'
# The states a live (billing or restartable) instance can be in.
_LIVE_STATES = ('pending', 'running', 'shutting-down', 'stopping', 'stopped')
_WAITER_DELAY_SECONDS = 5
# EC2's two instance-id shapes. Anything else is malformed, which EC2 itself
# reports as InvalidInstanceID.Malformed; checked locally so the verdict
# never depends on how a provider (or moto) phrases the rejection.
_INSTANCE_ID = re.compile(r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})')
_PROVIDER_ERRORS = (BotoCoreError, ClientError)

NetworkResolver = Callable[[VMSpec], Tuple[str, str]]


def _config():
    # Not `from app import config`: the package re-exports the config-class
    # mapping under that name, shadowing the module.
    return importlib.import_module('app.config')


def _client_token(deployment_id: str, vmid: int) -> str:
    """The run_instances idempotency token: one per (deployment, VMID), the
    same in every process, so a retried launch can never start a twin."""
    return hashlib.sha256(f'{deployment_id}:{vmid}'.encode('utf-8')).hexdigest()


def _waiter_config(timeout_seconds: int) -> dict:
    if timeout_seconds <= 0:
        raise BackendError(f'waiter timeout must be positive, got {timeout_seconds}')
    return {'Delay': _WAITER_DELAY_SECONDS,
            'MaxAttempts': max(1, math.ceil(timeout_seconds / _WAITER_DELAY_SECONDS))}


def _error_code(error: Optional[BaseException]) -> Optional[str]:
    if isinstance(error, ClientError):
        return error.response.get('Error', {}).get('Code')
    return None


def _tag_map(resource) -> dict:
    return {tag['Key']: tag['Value'] for tag in resource.get('Tags') or []}


def _state(instance) -> str:
    return instance.get('State', {}).get('Name') or 'unknown'


def _optional_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tags(pairs: dict) -> List[dict]:
    return [{'Key': key, 'Value': value} for key, value in pairs.items()]


def _filters(pairs: dict) -> List[dict]:
    return [{'Name': f'tag:{key}', 'Values': [value]} for key, value in pairs.items()]


def _carries(resource, pairs: dict) -> bool:
    # Server-side filters are re-checked locally: identity must not depend
    # on the provider (or a mock) filtering exactly.
    tags = _tag_map(resource)
    return all(tags.get(key) == value for key, value in pairs.items())


class _Resolution:
    """The instance a ref names, or why it names none."""

    def __init__(self, verdict: str, instance=None, reason: str = ''):
        self.verdict = verdict  # GUEST_MATCH / _ABSENT / _MISMATCH / _UNKNOWN
        self.instance = instance
        self.reason = reason


class AWSBackend:
    name = BACKEND_AWS

    def __init__(self, ec2_client=None, network_resolver: Optional[NetworkResolver] = None):
        # No credentials here: boto3's standard chain (instance role, env,
        # shared config) stays the only source.
        self._ec2 = ec2_client or boto3.client('ec2', region_name=_config().AWS_REGION)
        self._network_resolver = network_resolver or self._resolve_network

    # Admission

    def admit(self, spec: VMSpec, *, reservation_id: str,
              load_balancing_config: Optional[Mapping]) -> str:
        config = _config()
        return reserve_cloud_capacity(
            BACKEND_AWS, config.AWS_AVAILABILITY_ZONE, config.AWS_MAX_VMS,
            reservation_id)

    def renew_admission(self, reservation_id: str) -> bool:
        from app import proxmox_service
        return proxmox_service.renew_node_reservation(reservation_id)

    def release_admission(self, reservation_id: Optional[str]) -> bool:
        from app import proxmox_service
        return proxmox_service.release_node_reservation(reservation_id)

    # Networks (lookup only; the slot subnet and deployment group already
    # exist when create runs)

    def _one(self, kind: str, describe, key: str, pairs: dict) -> str:
        try:
            pages = self._ec2.get_paginator(describe).paginate(Filters=_filters(pairs))
            found = [item for page in pages for item in page[key]
                     if _carries(item, pairs)]
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'could not look up the {kind}: {error}') from error
        if len(found) != 1:
            raise BackendError(f'expected exactly one {kind} tagged {pairs}, '
                               f'found {len(found)}')
        return found[0]['SubnetId' if key == 'Subnets' else 'GroupId']

    def _resolve_network(self, spec: VMSpec) -> Tuple[str, str]:
        site = _config().LAB_SITE_ID
        subnet_id = self._one(
            'slot subnet', 'describe_subnets', 'Subnets',
            {TAG_SITE: site, TAG_SLOT: str(spec.network_slot),
             TAG_KIND: 'slot-network'})
        group_id = self._one(
            'deployment security group', 'describe_security_groups',
            'SecurityGroups',
            {TAG_SITE: site, TAG_DEPLOYMENT: spec.deployment_id,
             TAG_KIND: 'deployment-network'})
        return subnet_id, group_id

    # Create

    def _root_device_name(self, image_id: str) -> str:
        try:
            images = self._ec2.describe_images(ImageIds=[image_id])['Images']
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'could not describe image {image_id}: {error}') from error
        if len(images) != 1 or not images[0].get('RootDeviceName'):
            raise BackendError(f'image {image_id} has no single root device')
        return images[0]['RootDeviceName']

    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None]) -> LaunchedVM:
        config = _config()
        template = spec.template
        subnet_id, group_id = self._network_resolver(spec)
        root_device = self._root_device_name(template['image_id'])
        tags = {
            'Name': spec.vm_name,
            TAG_SITE: config.LAB_SITE_ID,
            TAG_VMID: str(spec.vmid),
            TAG_DEPLOYMENT: spec.deployment_id,
            TAG_EXPIRES: str(expires_epoch(spec.expires_at)),
            TAG_SLOT: str(spec.network_slot),
            TAG_KIND: KIND_INSTANCE,
        }
        running_wait = _waiter_config(config.AWS_LAUNCH_TIMEOUT_SECONDS)
        heartbeat()
        try:
            # Tagged at launch, so a crash before the DB commit still leaves
            # an instance the sweeper can attribute.
            response = self._ec2.run_instances(
                ImageId=template['image_id'],
                InstanceType=template['instance_type'],
                MinCount=1, MaxCount=1,
                ClientToken=_client_token(spec.deployment_id, spec.vmid),
                Placement={'AvailabilityZone': location},
                NetworkInterfaces=[{
                    'DeviceIndex': 0,
                    'SubnetId': subnet_id,
                    'Groups': [group_id],
                    'AssociatePublicIpAddress': False,
                    'DeleteOnTermination': True,
                }],
                BlockDeviceMappings=[{
                    'DeviceName': root_device,
                    'Ebs': {'VolumeSize': template['disk_gb'],
                            'DeleteOnTermination': True},
                }],
                TagSpecifications=[{'ResourceType': 'instance', 'Tags': _tags(tags)}],
            )
            instance_id = response['Instances'][0]['InstanceId']
            self._ec2.get_waiter('instance_running').wait(
                InstanceIds=[instance_id], WaiterConfig=running_wait)
        except (*_PROVIDER_ERRORS, WaiterError) as error:
            raise BackendError(f'launch of VM {spec.vmid} failed: {error}') from error
        heartbeat()
        return LaunchedVM(vmid=spec.vmid, vm_type=BACKEND_AWS, location=location,
                          network_slot=spec.network_slot, provider_id=instance_id)

    # Identity

    def _identity_tags(self, ref: VMRef) -> dict:
        pairs = {TAG_SITE: _config().LAB_SITE_ID, TAG_VMID: str(ref.vmid)}
        if ref.deployment_id is not None:
            pairs[TAG_DEPLOYMENT] = ref.deployment_id
        return pairs

    def _verdict(self, ref: VMRef, instance) -> str:
        if _state(instance) == 'terminated':
            return GUEST_ABSENT
        expected = dict(self._identity_tags(ref), **{TAG_KIND: KIND_INSTANCE})
        if ref.network_slot is not None:
            expected[TAG_SLOT] = str(ref.network_slot)
        return GUEST_MATCH if _carries(instance, expected) else GUEST_MISMATCH

    def _resolve(self, ref: VMRef) -> _Resolution:
        """A fresh describe of the instance `ref` names. Location is never
        consulted: a survivor may carry location='aws' (T8)."""
        if ref.provider_id is not None:
            if not _INSTANCE_ID.fullmatch(ref.provider_id):
                return _Resolution(GUEST_UNKNOWN,
                                   reason=f'malformed instance id {ref.provider_id!r}')
            try:
                reservations = self._ec2.describe_instances(
                    InstanceIds=[ref.provider_id])['Reservations']
            except ClientError as error:
                if _error_code(error) == _NOT_FOUND:
                    return _Resolution(GUEST_ABSENT)
                return _Resolution(GUEST_UNKNOWN, reason=str(error))
            except BotoCoreError as error:
                return _Resolution(GUEST_UNKNOWN, reason=str(error))
            instances = [i for r in reservations for i in r['Instances']
                         if i.get('InstanceId') == ref.provider_id]
            if not instances:
                return _Resolution(GUEST_ABSENT)
            return _Resolution(self._verdict(ref, instances[0]), instances[0])

        identity = self._identity_tags(ref)
        filters = _filters(identity) + [
            {'Name': 'instance-state-name', 'Values': list(_LIVE_STATES)}]
        try:
            pages = self._ec2.get_paginator('describe_instances').paginate(Filters=filters)
            candidates = [i for page in pages for r in page['Reservations']
                          for i in r['Instances']
                          if _carries(i, identity) and _state(i) in _LIVE_STATES]
        except _PROVIDER_ERRORS as error:
            return _Resolution(GUEST_UNKNOWN, reason=str(error))
        if not candidates:
            return _Resolution(GUEST_ABSENT)
        if len(candidates) > 1:
            return _Resolution(
                GUEST_UNKNOWN,
                reason=f'{len(candidates)} instances carry the identity of VM {ref.vmid}')
        return _Resolution(self._verdict(ref, candidates[0]), candidates[0])

    def inspect(self, ref: VMRef) -> str:
        return self._resolve(ref).verdict

    def _terminate(self, instance_id: str) -> None:
        wait = _waiter_config(_config().AWS_TERMINATE_TIMEOUT_SECONDS)
        try:
            self._ec2.terminate_instances(InstanceIds=[instance_id])
            self._ec2.get_waiter('instance_terminated').wait(
                InstanceIds=[instance_id], WaiterConfig=wait)
        except (*_PROVIDER_ERRORS, WaiterError) as error:
            raise BackendError(f'termination of {instance_id} failed: {error}') from error

    def destroy(self, ref: VMRef, policy: DestroyPolicy) -> None:
        # EC2 terminate has no separate stop/delete decision, so every policy
        # is the same; identity is always re-read, even after an inspect.
        resolution = self._resolve(ref)
        if resolution.verdict == GUEST_ABSENT:
            return
        if resolution.verdict == GUEST_MISMATCH:
            raise DestroyRefused(
                f'instance {resolution.instance["InstanceId"]} no longer carries '
                f'the identity of VM {ref.vmid}')
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'identity of VM {ref.vmid} could not be verified: {resolution.reason}')
        self._terminate(resolution.instance['InstanceId'])

    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        if _error_code(error) == _NOT_FOUND:
            return True
        return self._resolve(ref).verdict == GUEST_ABSENT

    # Display reads

    def _live_match(self, ref: VMRef):
        resolution = self._resolve(ref)
        return resolution.instance if resolution.verdict == GUEST_MATCH else None

    def ip_addresses(self, ref: VMRef) -> List[str]:
        instance = self._live_match(ref)
        address = instance.get('PrivateIpAddress') if instance else None
        return [address] if address else []

    def status(self, ref: VMRef) -> str:
        instance = self._live_match(ref)
        return _state(instance) if instance else 'unknown'

    def set_expiry(self, ref: VMRef, expires_at: datetime) -> None:
        # create_tags does not prove the instance exists, and must never
        # retag one this record no longer owns: verify identity first.
        resolution = self._resolve(ref)
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'could not write expiry of VM {ref.vmid}: identity is '
                f'{resolution.verdict} {resolution.reason}'.rstrip())
        try:
            self._ec2.create_tags(
                Resources=[resolution.instance['InstanceId']],
                Tags=_tags({TAG_EXPIRES: str(expires_epoch(expires_at))}))
        except _PROVIDER_ERRORS as error:
            raise BackendError(
                f'could not write expiry of VM {ref.vmid}: {error}') from error

    # Inventory

    def list_managed(self) -> List[ManagedResource]:
        """Every non-terminated instance this site launched, from one fresh
        read. A failed read propagates; an empty list would mean "all gone"."""
        site = {TAG_SITE: _config().LAB_SITE_ID}
        try:
            pages = self._ec2.get_paginator('describe_instances').paginate(
                Filters=_filters(site))
            instances = [i for page in pages for r in page['Reservations']
                         for i in r['Instances']]
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'EC2 inventory unavailable: {error}') from error
        resources = []
        for instance in instances:
            tags = _tag_map(instance)
            # The site's router carries the site tag too; only machines this
            # backend launched are instances it may report or sweep.
            if (not _carries(instance, site) or tags.get(TAG_KIND) != KIND_INSTANCE
                    or _state(instance) == 'terminated'):
                continue
            launch_time = instance.get('LaunchTime')
            resources.append(ManagedResource(
                backend=BACKEND_AWS, kind=KIND_INSTANCE,
                provider_id=instance['InstanceId'],
                name=tags.get('Name', ''),
                vmid=_optional_int(tags.get(TAG_VMID)),
                location=instance.get('Placement', {}).get('AvailabilityZone'),
                deployment_id=tags.get(TAG_DEPLOYMENT),
                network_slot=_optional_int(tags.get(TAG_SLOT)),
                expires_at_epoch=_optional_float(tags.get(TAG_EXPIRES)),
                created_at_epoch=launch_time.timestamp() if launch_time else None,
                status=_state(instance)))
        return resources

    def delete_resource(self, resource: ManagedResource) -> None:
        if resource.backend != self.name or resource.kind != KIND_INSTANCE:
            raise BackendError(
                f'{resource.backend} {resource.kind} {resource.provider_id} '
                'is not an AWS instance')
        self._terminate(resource.provider_id)
