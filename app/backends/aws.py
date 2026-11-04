"""Amazon EC2 behind the VMBackend contract: instance lifecycle, capped
admission, slot subnets, per-deployment security groups and the router.

Identity is the tag set written at launch, never the DB row: an instance is
this record's only when every contract tag agrees and, before anything acts
on it, it sits in the lab VPC and its slot's subnet; a ref without a
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
# An instance's EBS volume. Its own kind, never 'instance': inventory and
# identity match kind='instance', so a volume can never be mistaken for a
# machine, while a volume a failed DeleteOnTermination leaves behind still
# carries its owner's site/VMID/deployment/slot.
KIND_INSTANCE_VOLUME = 'instance-volume'
# Instance tags the volumes do not carry: its expiry lives on the instance
# (set_expiry retags only that).
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
# The operator-provisioned shared router's kind tag.
KIND_ROUTER = 'router'
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
        DependencyViolation included, raises so cleanup and the sweeper retry.
        Only ever called right after _owned_network proved ownership."""
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

    def _owned_network(self, kind: _NetworkKind, provider_id: str, pairs: dict,
                       key: Optional[str] = None) -> Tuple[Optional[dict], List[str]]:
        """A fresh by-id describe of one network resource, judged against
        the identity a caller is about to act on: the exact tag `pairs`, the
        deployment `key` when given, the lab VPC, and (for a group) the
        deterministic name of the deployment it is tagged with.

        Returns (None, []) on provider-confirmed absence, (item, []) when
        it is still owned, (item, problems) when it is not. A malformed id
        or an unverifiable describe raises BackendError: neither is absence.
        """
        config = _config()
        # Checked locally, like instance ids: a malformed id must never
        # read as absent because of how a provider phrases the rejection.
        if not kind.id_pattern.fullmatch(provider_id or ''):
            raise BackendError(f'malformed {kind.label} id {provider_id!r}')
        try:
            items = getattr(self._ec2, kind.describe)(
                **{kind.ids_param: [provider_id]})[kind.key]
        except ClientError as error:
            if _error_code(error) == kind.not_found:
                return None, []
            raise BackendError(
                f'identity of {kind.label} {provider_id} could not be verified: '
                f'{error}') from error
        except BotoCoreError as error:
            raise BackendError(
                f'identity of {kind.label} {provider_id} could not be verified: '
                f'{error}') from error
        items = [item for item in items if item.get(kind.id_field) == provider_id]
        if not items:
            return None, []
        item = items[0]
        tags = _tag_map(item)
        problems = [f'{tag}={tags.get(tag)!r}' for tag, value in pairs.items()
                    if tags.get(tag) != value]
        deployment = tags.get(TAG_DEPLOYMENT)
        if key is not None and (deployment is None or deployment_key(deployment) != key):
            problems.append(f'{TAG_DEPLOYMENT}={deployment!r}')
        if item.get('VpcId') != config.AWS_LAB_VPC_ID:
            problems.append(f'VpcId={item.get("VpcId")!r}')
        if kind is _GROUPS:
            try:
                name = deployment_network_name(config.LAB_SITE_ID, deployment or '')
            except ValueError:
                name = None
            if name is None or item.get('GroupName') != name:
                problems.append(f'GroupName={item.get("GroupName")!r}')
        return item, problems

    def _delete_owned_network(self, kind: _NetworkKind, provider_id: str,
                              pairs: dict, key: Optional[str] = None) -> None:
        """Delete one network resource only if a fresh read proves it still
        carries `pairs` (and `key`); absent is done; otherwise DestroyRefused
        and nothing is mutated."""
        item, problems = self._owned_network(kind, provider_id, pairs, key)
        if item is None:
            return
        if problems:
            raise DestroyRefused(
                f'{kind.label} {provider_id} no longer carries the identity to be '
                f'deleted: {", ".join(problems)}')
        self._delete_network(kind, provider_id)

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
        identity = self._slot_identity(slot)
        for subnet in self._tagged(_SUBNETS, identity):
            # The enumeration may be stale by now: re-prove before deleting.
            self._delete_owned_network(_SUBNETS, subnet['SubnetId'], identity)

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

    def _require_own_group(self, group_id: str, deployment_id: str) -> dict:
        """A fresh read of the group, refused unless it is still this
        deployment's."""
        group, problems = self._owned_network(
            _GROUPS, group_id, self._group_identity(deployment_id))
        if group is None:
            raise BackendError(f'security group {group_id} vanished before its rules were set')
        if problems:
            raise BackendError(
                f'security group {group_id} is no longer deployment {deployment_id}\'s: '
                f'{", ".join(problems)}')
        return group

    def _converge_group_rules(self, group_id: str, deployment_id: str) -> None:
        """Make the group's rules exactly the isolation set, both ways.
        Egress first, and in each direction revoke before authorize, so
        the default allow-all egress is gone before anything is granted.
        A fresh read proves the group is this deployment's before every
        mutation: one retagged after the first change gets no further one."""
        group = self._require_own_group(group_id, deployment_id)
        wanted = _isolation_rules(group_id)
        mutations = []
        for field, revoke, authorize in (
                ('IpPermissionsEgress', 'revoke_security_group_egress',
                 'authorize_security_group_egress'),
                ('IpPermissions', 'revoke_security_group_ingress',
                 'authorize_security_group_ingress')):
            current = _rule_set(group.get(field))
            mutations += [(revoke, rule, _RULE_NOT_FOUND)
                          for rule in sorted(current - wanted, key=str)]
            mutations += [(authorize, rule, _RULE_DUPLICATE)
                          for rule in sorted(wanted - current, key=str)]
        for index, (operation, rule, benign) in enumerate(mutations):
            if index:
                # The first mutation follows the read above.
                self._require_own_group(group_id, deployment_id)
            # One rule per call: a batch fails whole on one duplicate, and a
            # concurrent creator converging the same group is not an error.
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
        self._converge_group_rules(group_id, deployment_id)
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
        identity = self._group_identity(deployment_id)
        for group in self._tagged(_GROUPS, identity):
            # The enumeration may be stale by now: re-prove before deleting.
            self._delete_owned_network(_GROUPS, group['GroupId'], identity)

    # Router. The caller holds vm_lifecycle_lock(router_lock_key('aws')).
    # The operator provisions it tagged TAG_SITE=<site> and TAG_KIND=router;
    # the configured id alone never proves which machine it is.

    def _router(self) -> Tuple[str, str]:
        config = _config()
        router_id = config.AWS_ROUTER_INSTANCE_ID
        if not router_id:
            raise BackendError('AWS_ROUTER_INSTANCE_ID is not configured')
        try:
            reservations = self._ec2.describe_instances(
                InstanceIds=[router_id])['Reservations']
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'could not read router {router_id}: {error}') from error
        instances = [i for r in reservations for i in r['Instances']
                     if i.get('InstanceId') == router_id]
        if not instances:
            raise BackendError(f'router {router_id} does not exist')
        identity = {TAG_SITE: config.LAB_SITE_ID, TAG_KIND: KIND_ROUTER}
        if not _carries(instances[0], identity):
            tags = _tag_map(instances[0])
            raise BackendError(
                f'instance {router_id} is not this site\'s router: '
                f'{TAG_SITE}={tags.get(TAG_SITE)!r}, {TAG_KIND}={tags.get(TAG_KIND)!r}')
        return router_id, _state(instances[0])

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
        if state == 'stopping':
            self._wait_router(router_id, 'instance_stopped',
                              config.AWS_TERMINATE_TIMEOUT_SECONDS)
            # The waiter reads state, never tags: re-prove the router.
            router_id, state = self._router()
        if state == 'running':
            return
        if state == 'stopped':
            self._router_call(router_id, 'start_instances')
        elif state != 'pending':
            raise BackendError(f'router {router_id} is {state}')
        self._wait_router(router_id, 'instance_running', config.AWS_LAUNCH_TIMEOUT_SECONDS)

    def stop_router(self) -> None:
        config = _config()
        router_id, state = self._router()
        if state == 'pending':
            # EC2 cannot stop an instance that is still starting.
            self._wait_router(router_id, 'instance_running',
                              config.AWS_LAUNCH_TIMEOUT_SECONDS)
            # The waiter reads state, never tags: re-prove the router.
            router_id, state = self._router()
        if state == 'stopped':
            return
        if state == 'running':
            self._router_call(router_id, 'stop_instances')
        elif state != 'stopping':
            raise BackendError(f'router {router_id} is {state}')
        self._wait_router(router_id, 'instance_stopped',
                          config.AWS_TERMINATE_TIMEOUT_SECONDS)

    # Create

    def _block_device_mappings(self, image_id: str, disk_gb: int) -> List[dict]:
        """The launch's BlockDeviceMappings: every EBS disk the AMI adds,
        each deleted on termination, the root resized to `disk_gb`.

        A mapping the request leaves out keeps the AMI's own flag, and EC2's
        non-root default is false: an inherited data disk would outlive the
        instance and bill with no cleanup path (no inventory lists volumes).
        Instance-store and suppressed (NoDevice) mappings never outlive it.
        An AMI mapping that cannot be restated as a disposable disk is refused.
        A data disk is restated field-for-field (type, IOPS, throughput,
        encryption, key), so only its deletion flag differs from the AMI's.
        """
        ebs_fields = self._ec2.meta.service_model.operation_model(
            'RunInstances').input_shape.members['BlockDeviceMappings'] \
            .member.members['Ebs'].members
        try:
            images = self._ec2.describe_images(ImageIds=[image_id])['Images']
        except _PROVIDER_ERRORS as error:
            raise BackendError(f'could not describe image {image_id}: {error}') from error
        if len(images) != 1 or not images[0].get('RootDeviceName'):
            raise BackendError(f'image {image_id} has no single root device')
        root = images[0]['RootDeviceName']
        mappings = []
        for mapping in images[0].get('BlockDeviceMappings') or []:
            device = mapping.get('DeviceName')
            ebs = mapping.get('Ebs')
            if not device:
                raise BackendError(f'image {image_id} maps a disk to no device name')
            if 'VirtualName' in mapping or 'NoDevice' in mapping:
                continue
            if not isinstance(ebs, dict):
                raise BackendError(
                    f'image {image_id} maps {device} to neither EBS nor an instance store')
            if device == root:
                pinned = {'VolumeSize': disk_gb}
            else:
                # Only fields RunInstances accepts, as this client's own
                # model defines them: DescribeImages and RunInstances share
                # the EbsBlockDevice shape, so none of the AMI's is lost.
                pinned = {key: value for key, value in ebs.items()
                          if key in ebs_fields and value is not None}
                # An AMI reports Encrypted=False for an unencrypted disk; sent
                # back, it would be an explicit request that can conflict
                # with the account's EBS encryption-by-default. Left out, the
                # snapshot's state and that default decide, as for any disk.
                if pinned.get('Encrypted') is False:
                    del pinned['Encrypted']
                # EC2 merges an override with neither, but moto refuses it.
                if 'SnapshotId' not in pinned and 'VolumeSize' not in pinned:
                    raise BackendError(
                        f'image {image_id} maps {device} to EBS with no snapshot or size')
            mappings.append({'DeviceName': device,
                             'Ebs': dict(pinned, DeleteOnTermination=True)})
        if root not in {mapping['DeviceName'] for mapping in mappings}:
            raise BackendError(f'image {image_id} has no EBS root device {root}')
        return mappings

    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None]) -> LaunchedVM:
        config = _config()
        template = spec.template
        subnet_id, group_id = self._network_resolver(spec)
        block_devices = self._block_device_mappings(template['image_id'],
                                                    template['disk_gb'])
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
                BlockDeviceMappings=block_devices,
                TagSpecifications=[
                    {'ResourceType': 'instance', 'Tags': _tags(tags)},
                    # Every launch-created EBS volume, not only the root.
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

    def _placement_problems(self, instance, slot_tag: Optional[str]) -> List[str]:
        """Why the instance is not in the lab VPC and, when the caller knows
        its slot, that slot's subnet. Tags alone never place an instance:
        anything may carry a copy of them. A slot whose subnet cannot be
        identified raises BackendError, which is neither match nor absence."""
        config = _config()
        vpc_id = instance.get('VpcId')
        if not vpc_id or vpc_id != config.AWS_LAB_VPC_ID:
            return [f'VpcId={vpc_id!r}']
        if slot_tag is None:
            return []
        subnet = self._own_subnet(int(slot_tag))
        if subnet is None or instance.get('SubnetId') != subnet['SubnetId']:
            return [f'SubnetId={instance.get("SubnetId")!r}']
        return []

    def _judge(self, instance, expected: dict, *, placement: bool) -> _Resolution:
        """Judge one fresh instance against `expected` and, when the caller
        may act on the verdict, its actual placement. A terminated instance
        is absent; a misplaced one is a mismatch, never absent."""
        if _state(instance) == 'terminated':
            return _Resolution(GUEST_ABSENT)
        tags = _tag_map(instance)
        problems = [f'{key}={tags.get(key)!r}' for key, value in expected.items()
                    if tags.get(key) != value]
        if placement and not problems:
            try:
                problems = self._placement_problems(instance, expected.get(TAG_SLOT))
            except BackendError as error:
                return _Resolution(GUEST_UNKNOWN, instance, str(error))
        if problems:
            return _Resolution(GUEST_MISMATCH, instance, ', '.join(problems))
        return _Resolution(GUEST_MATCH, instance)

    def _describe_id(self, provider_id: str, expected: dict, *,
                     placement: bool = True) -> _Resolution:
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
        return self._judge(instances[0], expected, placement=placement)

    def _resolve(self, ref: VMRef, *, placement: bool = True) -> _Resolution:
        """A fresh describe of the instance `ref` names. Location is never
        consulted: a survivor may carry location='aws' (T8). Placement is
        judged unless the caller only displays the result."""
        if ref.provider_id is not None:
            return self._describe_id(ref.provider_id, self._expected_tags(ref),
                                     placement=placement)

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
        return self._judge(candidates[0], self._expected_tags(ref), placement=placement)

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
                f'the identity of VM {ref.vmid}: {resolution.reason}')
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'identity of VM {ref.vmid} could not be verified: {resolution.reason}')
        self._terminate(resolution.instance['InstanceId'])

    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        if _error_code(error) == _NOT_FOUND:
            return True
        # Absence is decided before placement, which only costs a read here.
        return self._resolve(ref, placement=False).verdict == GUEST_ABSENT

    # Display reads: judged on tags alone, from the fresh describe; nothing
    # here mutates, so the slot-subnet read placement needs is skipped.

    def _live_match(self, ref: VMRef):
        resolution = self._resolve(ref, placement=False)
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
                f'it was listed with: {resolution.reason}')
        if resolution.verdict != GUEST_MATCH:
            raise BackendError(
                f'identity of instance {resource.provider_id} could not be '
                f'verified: {resolution.reason}')
        self._terminate(resource.provider_id)

    def _delete_listed_network(self, kind: _NetworkKind, resource: ManagedResource) -> None:
        pairs = {TAG_SITE: _config().LAB_SITE_ID, TAG_KIND: resource.kind}
        if resource.network_slot is not None:
            pairs[TAG_SLOT] = str(resource.network_slot)
        # A listed group carries the 16-character key (ruling C1).
        self._delete_owned_network(kind, resource.provider_id, pairs,
                                   key=resource.deployment_id)
