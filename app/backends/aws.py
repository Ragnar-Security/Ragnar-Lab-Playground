"""Amazon EC2 behind the VMBackend contract: instance lifecycle, capped
admission, slot subnets, per-deployment security groups and the router.

Identity is the tag set written at launch, never the DB row: an instance is
this record's only when every contract tag agrees, and a ref without a
provider id (a create that raised after run_instances) is resolved from its
site/VMID/deployment tags. Absence needs positive evidence (NotFound or a
terminated instance); every other provider failure is GUEST_UNKNOWN, so a
transient error can never retire a record whose instance still bills.

Networks are identified the same way: a slot subnet and a deployment
security group are this site's only while their identity tags say so, and
every reuse or delete re-reads them first. Lock scope belongs to the caller
(ruling C4): the slot and router methods here never take a lifecycle lock.

Configuration is read as `config.<NAME>` at call time (ruling I8): this
module is imported lazily by the registry factory, and a module-level
`from app.config import ...` would also defeat tests that patch config.
"""

import hashlib
import importlib
import logging
import math
import re
import time
from datetime import datetime
from typing import Callable, List, Mapping, NamedTuple, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from app.backends.base import (
    BACKEND_AWS, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, GUEST_UNKNOWN,
    BackendError, DestroyPolicy, DestroyRefused, LaunchedVM, ManagedResource,
    SlotNetworkMissing, VMRef, VMSpec,
)
from app.backends.cloud_admission import reserve_cloud_capacity
from app.backends.cloud_common import (
    TAG_CREATED, TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT,
    TAG_VMID, deployment_key, deployment_network_name, expires_epoch,
    slot_network_name,
)
from app.backends.network_slots import cloud_slot_cidr

logger = logging.getLogger(__name__)

KIND_INSTANCE = 'instance'
# An instance's root EBS volume. Its own kind, never 'instance': inventory
# and identity match kind='instance', so a volume can never be mistaken for
# a machine, while a volume a failed DeleteOnTermination leaves behind still
# carries its owner's site/VMID/deployment/slot.
KIND_INSTANCE_VOLUME = 'instance-volume'
# Instance tags the root volume does not carry: its expiry lives on the
# instance (set_expiry retags only that).
_INSTANCE_ONLY_TAGS = (TAG_EXPIRES,)
_NOT_FOUND = 'InvalidInstanceID.NotFound'
# The states a live (billing or restartable) instance can be in.
_LIVE_STATES = ('pending', 'running', 'shutting-down', 'stopping', 'stopped')
_WAITER_DELAY_SECONDS = 5
# EC2's two instance-id shapes. Anything else is malformed, which EC2 itself
# reports as InvalidInstanceID.Malformed; checked locally so the verdict
# never depends on how a provider (or moto) phrases the rejection.
_INSTANCE_ID = re.compile(r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})')
_PROVIDER_ERRORS = (BotoCoreError, ClientError)

KIND_DEPLOYMENT_NETWORK = 'deployment-network'
KIND_SLOT_NETWORK = 'slot-network'
# The tailnet's CGNAT range: besides its own group, the only peers a
# deployment's VMs may reach or be reached from. No internet rule exists.
_TAILNET_CIDR = '100.64.0.0/10'
_GROUP_DUPLICATE = 'InvalidGroup.Duplicate'
_SUBNET_CONFLICT = 'InvalidSubnet.Conflict'
_RULE_DUPLICATE = 'InvalidPermission.Duplicate'
_RULE_NOT_FOUND = 'InvalidPermission.NotFound'
_DECIMAL = re.compile(r'[0-9]+')


class _NetworkKind(NamedTuple):
    """How one network resource kind is described, identified and deleted."""
    label: str
    describe: str      # client operation
    ids_param: str     # its id-list parameter
    key: str           # its response list
    id_field: str
    not_found: str     # the error code that proves absence
    delete: str        # client operation, called with {id_field: id}
    id_pattern: re.Pattern  # EC2's id shapes, the instance-id rule's twin


_NETWORK_KINDS = {
    KIND_DEPLOYMENT_NETWORK: _NetworkKind(
        'deployment security group', 'describe_security_groups', 'GroupIds',
        'SecurityGroups', 'GroupId', 'InvalidGroup.NotFound',
        'delete_security_group', re.compile(r'sg-(?:[0-9a-f]{8}|[0-9a-f]{17})')),
    KIND_SLOT_NETWORK: _NetworkKind(
        'slot subnet', 'describe_subnets', 'SubnetIds', 'Subnets', 'SubnetId',
        'InvalidSubnetID.NotFound', 'delete_subnet',
        re.compile(r'subnet-(?:[0-9a-f]{8}|[0-9a-f]{17})')),
}
_GROUPS = _NETWORK_KINDS[KIND_DEPLOYMENT_NETWORK]
_SUBNETS = _NETWORK_KINDS[KIND_SLOT_NETWORK]

# A security-group rule source: the permission field and the key inside it.
_RULE_SOURCES = {
    'group': ('UserIdGroupPairs', 'GroupId'),
    'cidr': ('IpRanges', 'CidrIp'),
    'ipv6': ('Ipv6Ranges', 'CidrIpv6'),
    'prefix-list': ('PrefixListIds', 'PrefixListId'),
}

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


def _verdict(instance, expected: dict) -> str:
    if _state(instance) == 'terminated':
        return GUEST_ABSENT
    return GUEST_MATCH if _carries(instance, expected) else GUEST_MISMATCH


def _volume_tags(instance_tags: dict) -> dict:
    tags = {key: value for key, value in instance_tags.items()
            if key not in _INSTANCE_ONLY_TAGS}
    tags[TAG_KIND] = KIND_INSTANCE_VOLUME
    return tags


def _created_epoch(tags: dict) -> Optional[int]:
    # Missing or garbled is None, which the sweeper reads as "never delete".
    value = tags.get(TAG_CREATED)
    return int(value) if isinstance(value, str) and _DECIMAL.fullmatch(value) else None


def _rule_set(permissions) -> set:
    """Each permission split into one (protocol, from, to, source kind,
    source) per source, so rule sets compare however EC2 groups them."""
    rules = set()
    for permission in permissions or []:
        protocol = permission['IpProtocol']
        # All-protocol rules have no ports, whatever a describe renders.
        ports = ((None, None) if protocol == '-1'
                 else (permission.get('FromPort'), permission.get('ToPort')))
        for kind, (field, key) in _RULE_SOURCES.items():
            rules.update((protocol, *ports, kind, source[key])
                         for source in permission.get(field) or [])
    return rules


def _permission(rule: tuple) -> dict:
    protocol, from_port, to_port, kind, source = rule
    field, key = _RULE_SOURCES[kind]
    permission = {'IpProtocol': protocol, field: [{key: source}]}
    if from_port is not None:
        permission['FromPort'] = from_port
    if to_port is not None:
        permission['ToPort'] = to_port
    return permission


def _isolation_rules(group_id: str) -> set:
    """A deployment's whole rule set, the same both ways: everything to and
    from its own group and the tailnet, nothing else."""
    return {('-1', None, None, 'group', group_id),
            ('-1', None, None, 'cidr', _TAILNET_CIDR)}


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

    # Networks. A slot subnet exists from ensure_slot until release_slot; a
    # deployment's group is created by its first launch and removed by
    # cleanup_deployment. Both are tagged at creation (never in a later
    # call), so a crash can never leave one unattributable or ageless (C2).

    def _tagged(self, kind: _NetworkKind, pairs: dict) -> list:
        """Every `kind` carrying `pairs`, from one fresh paginated read."""
        try:
            pages = self._ec2.get_paginator(kind.describe).paginate(
                Filters=_filters(pairs))
            return [item for page in pages for item in page[kind.key]
                    if _carries(item, pairs)]
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'could not look up {kind.label}s: {error}') from error

    def _delete_network(self, kind: _NetworkKind, provider_id: str) -> None:
        """Delete one network resource; absent is done. Anything else, a
        DependencyViolation included, raises so cleanup and the sweeper retry."""
        try:
            getattr(self._ec2, kind.delete)(**{kind.id_field: provider_id})
        except ClientError as error:
            if _error_code(error) == kind.not_found:
                return
            raise BackendError(
                f'could not delete {kind.label} {provider_id}: {error}') from error
        except BotoCoreError as error:
            raise BackendError(
                f'could not delete {kind.label} {provider_id}: {error}') from error

    def _slot_identity(self, slot: int) -> dict:
        return {TAG_SITE: _config().LAB_SITE_ID, TAG_SLOT: str(slot),
                TAG_KIND: KIND_SLOT_NETWORK}

    def _own_subnet(self, slot: int) -> Optional[dict]:
        """This site's subnet for `slot`, None when there is none. One that
        carries the identity but sits anywhere but the slot's exact
        VPC/CIDR/AZ is refused, never reused."""
        config = _config()
        found = self._tagged(_SUBNETS, self._slot_identity(slot))
        if not found:
            return None
        if len(found) > 1:
            raise BackendError(f'{len(found)} subnets claim slot {slot}')
        subnet = found[0]
        expected = (config.AWS_LAB_VPC_ID, cloud_slot_cidr(config.AWS_LAB_CIDR, slot),
                    config.AWS_AVAILABILITY_ZONE)
        actual = (subnet.get('VpcId'), subnet.get('CidrBlock'),
                  subnet.get('AvailabilityZone'))
        if actual != expected:
            raise BackendError(
                f'subnet {subnet["SubnetId"]} of slot {slot} is at {actual}, '
                f'not {expected}')
        return subnet

    def _create_subnet(self, slot: int) -> dict:
        config = _config()
        tags = dict(self._slot_identity(slot),
                    Name=slot_network_name(config.LAB_SITE_ID, slot),
                    **{TAG_CREATED: str(int(time.time()))})
        try:
            return self._ec2.create_subnet(
                VpcId=config.AWS_LAB_VPC_ID,
                CidrBlock=cloud_slot_cidr(config.AWS_LAB_CIDR, slot),
                AvailabilityZone=config.AWS_AVAILABILITY_ZONE,
                TagSpecifications=[{'ResourceType': 'subnet', 'Tags': _tags(tags)}],
            )['Subnet']
        except ClientError as error:
            if _error_code(error) != _SUBNET_CONFLICT:
                raise BackendError(f'could not create slot {slot} subnet: {error}') from error
            conflict = error
        except BotoCoreError as error:
            raise BackendError(f'could not create slot {slot} subnet: {error}') from error
        # A concurrent creator may have won the CIDR: only a subnet carrying
        # this slot's identity at its exact placement is acceptable.
        subnet = self._own_subnet(slot)
        if subnet is None:
            raise BackendError(
                f'the slot {slot} CIDR is held by a subnet without its identity: '
                f'{conflict}')
        return subnet

    def ensure_slot(self, slot: int) -> None:
        """Create or reuse the slot's private subnet. The caller holds
        vm_lifecycle_lock(slot_lock_key('aws', slot))."""
        subnet = self._own_subnet(slot) or self._create_subnet(slot)
        if subnet.get('MapPublicIpOnLaunch') is not False:
            try:
                self._ec2.modify_subnet_attribute(
                    SubnetId=subnet['SubnetId'], MapPublicIpOnLaunch={'Value': False})
            except _PROVIDER_ERRORS as error:
                raise BackendError(
                    f'could not make slot {slot} subnet private: {error}') from error

    def release_slot(self, slot: int) -> None:
        """Delete the slot's subnet; absent is done. The caller holds
        vm_lifecycle_lock(slot_lock_key('aws', slot))."""
        for subnet in self._tagged(_SUBNETS, self._slot_identity(slot)):
            self._delete_network(_SUBNETS, subnet['SubnetId'])

    def _group_identity(self, deployment_id: str) -> dict:
        return {TAG_SITE: _config().LAB_SITE_ID, TAG_DEPLOYMENT: deployment_id,
                TAG_KIND: KIND_DEPLOYMENT_NETWORK}

    def _own_group(self, deployment_id: str) -> Optional[dict]:
        """This site's group for the deployment, None when there is none;
        one outside the lab VPC or under another name is refused."""
        config = _config()
        found = self._tagged(_GROUPS, self._group_identity(deployment_id))
        if not found:
            return None
        if len(found) > 1:
            raise BackendError(
                f'{len(found)} security groups claim deployment {deployment_id}')
        group = found[0]
        name = deployment_network_name(config.LAB_SITE_ID, deployment_id)
        if group.get('VpcId') != config.AWS_LAB_VPC_ID or group.get('GroupName') != name:
            raise BackendError(
                f'security group {group["GroupId"]} of deployment {deployment_id} '
                f'is {group.get("GroupName")!r} in {group.get("VpcId")}, '
                f'not {name!r} in {config.AWS_LAB_VPC_ID}')
        return group

    def _create_group(self, deployment_id: str) -> str:
        config = _config()
        name = deployment_network_name(config.LAB_SITE_ID, deployment_id)
        tags = dict(self._group_identity(deployment_id), Name=name,
                    **{TAG_CREATED: str(int(time.time()))})
        try:
            return self._ec2.create_security_group(
                GroupName=name, VpcId=config.AWS_LAB_VPC_ID,
                Description='Lab Playground deployment isolation',
                TagSpecifications=[{'ResourceType': 'security-group',
                                    'Tags': _tags(tags)}],
            )['GroupId']
        except ClientError as error:
            if _error_code(error) != _GROUP_DUPLICATE:
                raise BackendError(f'could not create group {name}: {error}') from error
            duplicate = error
        except BotoCoreError as error:
            raise BackendError(f'could not create group {name}: {error}') from error
        # A concurrent creator may hold the name. The name carries only the
        # 16-character key, so only this deployment's full identity counts.
        group = self._own_group(deployment_id)
        if group is None:
            raise BackendError(
                f'group {name} is held without the identity of deployment '
                f'{deployment_id}: {duplicate}')
        return group['GroupId']

    def _converge_group_rules(self, group_id: str) -> None:
        """Make the group's rules exactly the isolation set, both ways.
        Egress first, and in each direction revoke before authorize, so
        the default allow-all egress is gone before anything is granted."""
        try:
            [group] = self._ec2.describe_security_groups(
                GroupIds=[group_id])['SecurityGroups']
        except (*_PROVIDER_ERRORS, ValueError) as error:
            raise BackendError(f'could not read the rules of {group_id}: {error}') from error
        wanted = _isolation_rules(group_id)
        for field, revoke, authorize in (
                ('IpPermissionsEgress', 'revoke_security_group_egress',
                 'authorize_security_group_egress'),
                ('IpPermissions', 'revoke_security_group_ingress',
                 'authorize_security_group_ingress')):
            current = _rule_set(group.get(field))
            # One rule per call: a batch fails whole on one duplicate, and a
            # concurrent creator converging the same group is not an error.
            for operation, rules, benign in ((revoke, current - wanted, _RULE_NOT_FOUND),
                                             (authorize, wanted - current, _RULE_DUPLICATE)):
                for rule in sorted(rules, key=str):
                    try:
                        getattr(self._ec2, operation)(
                            GroupId=group_id, IpPermissions=[_permission(rule)])
                    except ClientError as error:
                        if _error_code(error) != benign:
                            raise BackendError(
                                f'could not {operation} on {group_id}: {error}') from error
                    except BotoCoreError as error:
                        raise BackendError(
                            f'could not {operation} on {group_id}: {error}') from error

    def _deployment_group(self, deployment_id: str) -> str:
        group = self._own_group(deployment_id)
        group_id = group['GroupId'] if group else self._create_group(deployment_id)
        self._converge_group_rules(group_id)
        return group_id

    def _resolve_network(self, spec: VMSpec) -> Tuple[str, str]:
        """The production resolver: the slot subnet ensure_slot made, and
        the deployment's group, created or reused. A slot released since
        ensure_slot raises SlotNetworkMissing so create_vm re-ensures it (I3)."""
        subnet = self._own_subnet(spec.network_slot)
        if subnet is None:
            raise SlotNetworkMissing(
                f'slot {spec.network_slot} has no subnet for VM {spec.vmid}')
        return subnet['SubnetId'], self._deployment_group(spec.deployment_id)

    def cleanup_deployment(self, deployment_id: str) -> None:
        """Delete the deployment's security groups; absent is done. Run
        after its instances are terminated: EC2 refuses to delete a group
        an interface still uses, and that refusal propagates."""
        for group in self._tagged(_GROUPS, self._group_identity(deployment_id)):
            self._delete_network(_GROUPS, group['GroupId'])

    # Router. The caller holds vm_lifecycle_lock(router_lock_key('aws')).

    def _router(self) -> Tuple[str, str]:
        router_id = _config().AWS_ROUTER_INSTANCE_ID
        if not router_id:
            raise BackendError('AWS_ROUTER_INSTANCE_ID is not configured')
        try:
            reservations = self._ec2.describe_instances(
                InstanceIds=[router_id])['Reservations']
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'could not read router {router_id}: {error}') from error
        states = [_state(i) for r in reservations for i in r['Instances']
                  if i.get('InstanceId') == router_id]
        if not states:
            raise BackendError(f'router {router_id} does not exist')
        return router_id, states[0]

    def _router_call(self, router_id: str, operation: str) -> None:
        try:
            getattr(self._ec2, operation)(InstanceIds=[router_id])
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'{operation} of router {router_id} failed: {error}') from error

    def _wait_router(self, router_id: str, waiter: str, timeout_seconds: int) -> None:
        wait = _waiter_config(timeout_seconds)
        try:
            self._ec2.get_waiter(waiter).wait(InstanceIds=[router_id], WaiterConfig=wait)
        except (*_PROVIDER_ERRORS, WaiterError) as error:
            raise BackendError(f'router {router_id} {waiter} wait failed: {error}') from error

    def ensure_router_running(self) -> None:
        config = _config()
        router_id, state = self._router()
        if state == 'running':
            return
        if state == 'stopping':
            self._wait_router(router_id, 'instance_stopped',
                              config.AWS_TERMINATE_TIMEOUT_SECONDS)
            state = 'stopped'
        if state == 'stopped':
            self._router_call(router_id, 'start_instances')
        elif state != 'pending':
            raise BackendError(f'router {router_id} is {state}')
        self._wait_router(router_id, 'instance_running', config.AWS_LAUNCH_TIMEOUT_SECONDS)

    def stop_router(self) -> None:
        config = _config()
        router_id, state = self._router()
        if state == 'stopped':
            return
        if state == 'pending':
            # EC2 cannot stop an instance that is still starting.
            self._wait_router(router_id, 'instance_running',
                              config.AWS_LAUNCH_TIMEOUT_SECONDS)
            state = 'running'
        if state == 'running':
            self._router_call(router_id, 'stop_instances')
        elif state != 'stopping':
            raise BackendError(f'router {router_id} is {state}')
        self._wait_router(router_id, 'instance_stopped',
                          config.AWS_TERMINATE_TIMEOUT_SECONDS)

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
                TagSpecifications=[
                    {'ResourceType': 'instance', 'Tags': _tags(tags)},
                    {'ResourceType': 'volume', 'Tags': _tags(_volume_tags(tags))},
                ],
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

    def _expected_tags(self, ref: VMRef) -> dict:
        expected = dict(self._identity_tags(ref), **{TAG_KIND: KIND_INSTANCE})
        if ref.network_slot is not None:
            expected[TAG_SLOT] = str(ref.network_slot)
        return expected

    def _describe_id(self, provider_id: str, expected: dict) -> _Resolution:
        """A fresh describe of one instance id, judged against `expected`."""
        if not _INSTANCE_ID.fullmatch(provider_id):
            return _Resolution(GUEST_UNKNOWN,
                               reason=f'malformed instance id {provider_id!r}')
        try:
            reservations = self._ec2.describe_instances(
                InstanceIds=[provider_id])['Reservations']
        except ClientError as error:
            if _error_code(error) == _NOT_FOUND:
                return _Resolution(GUEST_ABSENT)
            return _Resolution(GUEST_UNKNOWN, reason=str(error))
        except BotoCoreError as error:
            return _Resolution(GUEST_UNKNOWN, reason=str(error))
        instances = [i for r in reservations for i in r['Instances']
                     if i.get('InstanceId') == provider_id]
        if not instances:
            return _Resolution(GUEST_ABSENT)
        return _Resolution(_verdict(instances[0], expected), instances[0])

    def _resolve(self, ref: VMRef) -> _Resolution:
        """A fresh describe of the instance `ref` names. Location is never
        consulted: a survivor may carry location='aws' (T8)."""
        if ref.provider_id is not None:
            return self._describe_id(ref.provider_id, self._expected_tags(ref))

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
        return _Resolution(_verdict(candidates[0], self._expected_tags(ref)), candidates[0])

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
        """Every non-terminated instance, deployment group and slot subnet
        this site created, from fresh reads. A failed read propagates; an
        empty list would mean "all gone"."""
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
        return resources + self._managed_networks()

    def _managed_networks(self) -> List[ManagedResource]:
        site = _config().LAB_SITE_ID
        resources = []
        for group in self._tagged(_GROUPS, {TAG_SITE: site, TAG_KIND: KIND_DEPLOYMENT_NETWORK}):
            tags = _tag_map(group)
            deployment = tags.get(TAG_DEPLOYMENT)
            resources.append(ManagedResource(
                backend=BACKEND_AWS, kind=KIND_DEPLOYMENT_NETWORK,
                provider_id=group['GroupId'], name=group.get('GroupName', ''),
                # The 16-character key on both clouds (ruling C1).
                deployment_id=deployment_key(deployment) if deployment else None,
                created_at_epoch=_created_epoch(tags)))
        for subnet in self._tagged(_SUBNETS, {TAG_SITE: site, TAG_KIND: KIND_SLOT_NETWORK}):
            tags = _tag_map(subnet)
            resources.append(ManagedResource(
                backend=BACKEND_AWS, kind=KIND_SLOT_NETWORK,
                provider_id=subnet['SubnetId'], name=tags.get('Name', ''),
                location=subnet.get('AvailabilityZone'),
                network_slot=_optional_int(tags.get(TAG_SLOT)),
                created_at_epoch=_created_epoch(tags)))
        return resources

    def delete_resource(self, resource: ManagedResource) -> None:
        """Delete one listed resource after re-proving, from a fresh
        describe, that it still carries the identity it was listed with:
        the listing may be minutes old. Absent is done; a changed identity
        raises DestroyRefused; an unverifiable one raises BackendError."""
        if resource.backend != self.name:
            raise BackendError(
                f'{resource.backend} {resource.kind} {resource.provider_id} '
                'is not an AWS resource')
        if resource.kind == KIND_INSTANCE:
            self._delete_instance(resource)
        elif resource.kind in _NETWORK_KINDS:
            self._delete_listed_network(_NETWORK_KINDS[resource.kind], resource)
        else:
            raise BackendError(
                f'AWS has no resource kind {resource.kind!r} ({resource.provider_id})')

    def _delete_instance(self, resource: ManagedResource) -> None:
        expected = {TAG_SITE: _config().LAB_SITE_ID, TAG_KIND: KIND_INSTANCE}
        for key, value in ((TAG_VMID, resource.vmid),
                           (TAG_DEPLOYMENT, resource.deployment_id),
                           (TAG_SLOT, resource.network_slot)):
            if value is not None:
                expected[key] = str(value)
        resolution = self._describe_id(resource.provider_id, expected)
        if resolution.verdict == GUEST_ABSENT:
            return
        if resolution.verdict == GUEST_MISMATCH:
            raise DestroyRefused(
                f'instance {resource.provider_id} no longer carries the identity '
                'it was listed with')
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'identity of instance {resource.provider_id} could not be '
                f'verified: {resolution.reason}')
        self._terminate(resource.provider_id)

    def _delete_listed_network(self, kind: _NetworkKind, resource: ManagedResource) -> None:
        provider_id = resource.provider_id
        # Checked locally, like instance ids: a malformed id must never
        # read as absent because of how a provider phrases the rejection.
        if not kind.id_pattern.fullmatch(provider_id or ''):
            raise BackendError(f'malformed {kind.label} id {provider_id!r}')
        try:
            items = getattr(self._ec2, kind.describe)(
                **{kind.ids_param: [provider_id]})[kind.key]
        except ClientError as error:
            if _error_code(error) == kind.not_found:
                return
            raise BackendError(
                f'identity of {kind.label} {provider_id} could not be verified: '
                f'{error}') from error
        except BotoCoreError as error:
            raise BackendError(
                f'identity of {kind.label} {provider_id} could not be verified: '
                f'{error}') from error
        items = [item for item in items if item.get(kind.id_field) == provider_id]
        if not items:
            return
        tags = _tag_map(items[0])
        deployment = tags.get(TAG_DEPLOYMENT)
        holds = (
            tags.get(TAG_SITE) == _config().LAB_SITE_ID
            and tags.get(TAG_KIND) == resource.kind
            and (resource.deployment_id is None
                 or (deployment is not None
                     and deployment_key(deployment) == resource.deployment_id))
            and (resource.network_slot is None
                 or tags.get(TAG_SLOT) == str(resource.network_slot)))
        if not holds:
            raise DestroyRefused(
                f'{kind.label} {provider_id} no longer carries the identity it '
                'was listed with')
        self._delete_network(kind, provider_id)
