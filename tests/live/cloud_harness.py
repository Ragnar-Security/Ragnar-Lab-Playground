"""Safety harness for the opt-in live AWS and GCP contract runs.

The cloud contract runs launch, inspect and delete real (billed) cloud
resources through the app's own VMBackend. Everything destructive goes
through ``CloudContractRun``, which enforces these rules:

* the run uses a dedicated ``LAB_SITE_ID=live-<name>`` and a machine cap of
  exactly one, so it can neither see nor reach a portal site's resources;
* it refuses to start while that site already owns any resource, so its
  teardown can only ever touch what this run created;
* every instance it launches carries the app's expiry, 30 minutes out;
* each VMRef is claimed before ``create`` is called, so a launch that
  raises half-way is still destroyed;
* teardown runs on exit even when the body raised, in a fixed order
  (instances with positive absence, deployment networks, slots, admission,
  router), then requires a fresh inventory of the site to be empty and the
  site router to read stopped. A leftover is deleted from that fresh
  inventory only, and a second empty inventory is required. Any cleanup
  failure fails the run with provider ids and copy-paste inventory
  commands; none is swallowed.

This module imports no provider SDK: a skipped run must not build a client.
The guards are unit-tested against the in-memory backend in
``tests/test_live_cloud_contract_guards.py``.
"""

import secrets
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import AbstractSet, Callable, Dict, List, Mapping, Optional, Tuple

from app.backends.base import (
    BackendError, DestroyPolicy, DestroyRefused, ManagedResource, VMBackend, VMRef,
    VMSpec,
)
from app.backends.cloud_common import (
    TAG_SITE, deployment_network_name, instance_name, site_prefix,
    slot_network_name, validate_site_id,
)
from tests.live.harness import live_skip_reason


LIVE_SITE_PREFIX = 'live-'
EXPIRY = timedelta(minutes=30)
NAME_PREFIX = 'lab-contract-test-'
# The slot the lifecycle uses. Any slot is safe: preflight proves the
# dedicated site owns no network at all.
LIVE_SLOT = 1
# Networks free up asynchronously: EC2 refuses to delete a security group or
# subnet (DependencyViolation) until a terminated instance's interface is
# gone, which can take a minute or two after the termination waiter returns.
NETWORK_SETTLE_SECONDS = 300
NETWORK_POLL_SECONDS = 10
# EC2 can answer a DescribeInstances straight after StartInstances with the
# old 'stopped' (read-after-write lag; measured live: the read right after
# the start said 'stopped' and the router came up anyway). A stop or a
# final check that believes that read leaves the router billing, so after a
# direct start teardown accepts 'stopped' only once it has seen the start:
# it polls for a non-stopped read for up to this long, then fails.
ROUTER_READ_LAG_SECONDS = 60
ROUTER_POLL_SECONDS = 2
# Fallback deletion order: an instance holds its networks.
_KIND_ORDER = {'instance': 0, 'deployment-network': 1, 'slot-network': 2}

CLOUD_MARKERS = {'aws': 'live_aws', 'gcp': 'live_gcp'}


@dataclass(frozen=True)
class _Provider:
    label: str
    flag: str
    cap: str
    settings: Tuple[str, ...]  # app.config names the run pins, besides site/cap
    image: str
    machine: str
    template_keys: Tuple[str, str]  # (image key, machine-size key)


_PROVIDERS = {
    'aws': _Provider(
        'AWS', 'LIVE_AWS', 'AWS_MAX_VMS',
        ('AWS_REGION', 'AWS_AVAILABILITY_ZONE', 'AWS_LAB_VPC_ID', 'AWS_LAB_CIDR',
         'AWS_ROUTER_INSTANCE_ID'),
        'LIVE_AWS_IMAGE_ID', 'LIVE_AWS_INSTANCE_TYPE', ('image_id', 'instance_type')),
    'gcp': _Provider(
        'GCP', 'LIVE_GCP', 'GCP_MAX_VMS',
        ('GCP_PROJECT', 'GCP_REGION', 'GCP_ZONE', 'GCP_NETWORK', 'GCP_LAB_CIDR',
         'GCP_ROUTER_INSTANCE'),
        'LIVE_GCP_IMAGE', 'LIVE_GCP_MACHINE_TYPE', ('image', 'machine_type')),
}


class UnsafeLiveRun(RuntimeError):
    """A guard refused to run: the live site could hold resources this run
    did not create."""


class CloudCleanupError(RuntimeError):
    """Teardown could not prove the live site empty."""


def _provider(backend: str) -> _Provider:
    try:
        return _PROVIDERS[backend]
    except KeyError:
        raise ValueError(f'no live cloud contract for backend {backend!r}') from None


def _enabled(environ: Mapping[str, str]) -> Tuple[str, ...]:
    # The same reading as app.config's ENABLED_BACKENDS.
    names = []
    for part in (environ.get('ENABLED_BACKENDS') or '').split(','):
        name = part.strip().lower()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def required_env(backend: str) -> Tuple[str, ...]:
    """Every variable a live run of `backend` needs, the flag included."""
    provider = _provider(backend)
    return (provider.flag, 'ENABLED_BACKENDS', 'LAB_SITE_ID', provider.cap,
            *provider.settings, provider.image, provider.machine)


def cloud_live_skip_reason(backend: str, environ: Mapping[str, str]) -> Optional[str]:
    """Why the live `backend` run must be skipped, or None when it is opted
    in and every variable is present. Values are validated later, loudly,
    by CloudLiveConfig.from_env."""
    provider = _provider(backend)
    if environ.get(provider.flag) != '1':
        return (f'live {provider.label} contract run is opt-in: '
                f'set {provider.flag}=1')
    missing = [name for name in required_env(backend)[2:]
               if not (environ.get(name) or '').strip()]
    if backend not in _enabled(environ):
        missing.insert(0, f'ENABLED_BACKENDS containing {backend}')
    if missing:
        return f'live {provider.label} contract run is missing: ' + ', '.join(missing)
    return None


def select_live_skip_reason(marker_names: AbstractSet[str],
                            environ: Mapping[str, str]) -> Optional[str]:
    """The one skip decision for a collected item. A cloud marker selects
    only that provider's guard; the Proxmox guard applies only to `live`
    items that carry no cloud marker (ruling I15)."""
    clouds = [backend for backend, marker in CLOUD_MARKERS.items()
              if marker in marker_names]
    for backend in clouds:
        reason = cloud_live_skip_reason(backend, environ)
        if reason is not None:
            return reason
    if clouds:
        return None
    if 'live' in marker_names:
        return live_skip_reason(environ)
    return None


@dataclass(frozen=True)
class CloudLiveConfig:
    backend: str
    site_id: str
    settings: Mapping[str, object]  # app.config values the run pins
    image: str
    machine_type: str
    expiry: timedelta = EXPIRY

    @classmethod
    def from_env(cls, backend: str, environ: Mapping[str, str]) -> 'CloudLiveConfig':
        provider = _provider(backend)
        reason = cloud_live_skip_reason(backend, environ)
        if reason is not None:
            raise ValueError(reason)
        site = environ['LAB_SITE_ID'].strip()
        if not site.startswith(LIVE_SITE_PREFIX) or site == LIVE_SITE_PREFIX:
            raise ValueError(
                f'LAB_SITE_ID must be a dedicated {LIVE_SITE_PREFIX}<name> site that no '
                f'portal uses, got {site!r}')
        validate_site_id(site)
        cap = environ[provider.cap].strip()
        if cap != '1':
            raise ValueError(
                f'{provider.cap}=1 is required for a live run (one machine at a time), '
                f'got {cap!r}')
        settings = {name: environ[name].strip() for name in provider.settings}
        settings.update({'ENABLED_BACKENDS': _enabled(environ), 'LAB_SITE_ID': site,
                         provider.cap: 1})
        return cls(backend, site, settings, environ[provider.image].strip(),
                   environ[provider.machine].strip())

    def template(self) -> dict:
        """The backend's resolved template block, through the app's resolver."""
        from app.backends.template_spec import backend_block
        image_key, machine_key = _provider(self.backend).template_keys
        return dict(backend_block(
            {'name': 'live-contract',
             self.backend: {image_key: self.image, machine_key: self.machine_type}},
            self.backend))

    def manual_inventory_commands(self) -> str:
        site, s = self.site_id, self.settings
        if self.backend == 'aws':
            region = s['AWS_REGION']
            tag = f'Name=tag:{TAG_SITE},Values={site}'
            return (
                f'  aws ec2 describe-instances --region {region} --filters {tag} '
                'Name=instance-state-name,Values=pending,running,stopping,stopped\n'
                f'  aws ec2 describe-security-groups --region {region} --filters {tag}\n'
                f'  aws ec2 describe-subnets --region {region} --filters {tag}\n'
                f'  remove: aws ec2 terminate-instances --region {region} --instance-ids <id>; '
                f'aws ec2 delete-security-group --region {region} --group-id <sg-id>; '
                f'aws ec2 delete-subnet --region {region} --subnet-id <subnet-id>')
        project, region, prefix = s['GCP_PROJECT'], s['GCP_REGION'], site_prefix(site)
        return (
            f'  gcloud compute instances list --project {project} '
            f"--filter='labels.{TAG_SITE}={site}'\n"
            f'  gcloud compute firewall-rules list --project {project} '
            f"--filter='name ~ ^{prefix}d-'\n"
            f'  gcloud compute networks subnets list --project {project} '
            f"--filter='name ~ ^{prefix}s-'\n"
            f'  remove: gcloud compute instances delete <name> --project {project} '
            f'--zone <zone>; gcloud compute firewall-rules delete <name> --project {project}; '
            f'gcloud compute networks subnets delete <name> --project {project} '
            f'--region {region}')

    def manual_router_stop_command(self) -> str:
        s = self.settings
        if self.backend == 'aws':
            return (f"  aws ec2 stop-instances --region {s['AWS_REGION']} "
                    f"--instance-ids {s['AWS_ROUTER_INSTANCE_ID']}")
        return (f"  gcloud compute instances stop {s['GCP_ROUTER_INSTANCE']} "
                f"--project {s['GCP_PROJECT']} --zone <router zone>")


def _describe(resource: ManagedResource) -> str:
    where = f' in {resource.location}' if resource.location else ''
    return f'{resource.kind} {resource.provider_id} ({resource.name!r}{where})'


# A synchronous Compute refusal of a request naming a resource that does not
# exist: 404 notFound, or 400 invalid when the reference is judged malformed.
MISSING_RESOURCE_STATUSES = (400, 404)


def is_missing_resource_refusal(status_code: int, payload, resource: str) -> bool:
    """Whether a Compute error response refuses a request because `resource`
    (a name or partial URL) does not exist, as opposed to an auth, quota,
    rate-limit, server or any other failure. `payload` is the decoded body."""
    if status_code not in MISSING_RESOURCE_STATUSES:
        return False
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    entries = error.get('errors') if isinstance(error.get('errors'), list) else []
    messages = [error.get('message')] + [e.get('message') for e in entries
                                         if isinstance(e, dict)]
    name = resource.rstrip('/').rsplit('/', 1)[-1]
    return any(isinstance(m, str) and name in m for m in messages)



class CloudContractRun:
    """Owns every resource the cloud contract run creates; use as a context
    manager. Only backend methods touch the provider."""

    def __init__(self, backend: VMBackend, config: CloudLiveConfig, *,
                 now: Callable[[], datetime] = datetime.now,
                 settle_seconds: float = NETWORK_SETTLE_SECONDS,
                 poll_seconds: float = NETWORK_POLL_SECONDS,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.backend = backend
        self.config = config
        self.now = now
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self.clock = clock
        self._refs: Dict[int, VMRef] = {}
        self._deployments: List[str] = []
        self._slots: List[int] = []
        self._reservations: List[str] = []
        self._next_vmid = 1
        self._router_started_at: Optional[float] = None

    def __enter__(self) -> 'CloudContractRun':
        self.preflight()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        failures = self.teardown()
        if failures:
            # Raised even when the body failed: the body's exception stays
            # attached as __context__, and a leak is never hidden behind it.
            raise CloudCleanupError('\n\n'.join(failures))
        return False

    # --- guards ----------------------------------------------------------

    def preflight(self) -> None:
        existing = self.backend.list_managed()
        if existing:
            raise UnsafeLiveRun(
                f'refusing to run: live site {self.config.site_id!r} already owns '
                f'{len(existing)} resource(s), which this run did not create and will '
                'not touch: ' + ', '.join(_describe(r) for r in existing)
                + '\nInspect them by hand:\n' + self.config.manual_inventory_commands())

    # --- lifecycle -------------------------------------------------------

    def new_spec(self, *, slot: int = LIVE_SLOT) -> VMSpec:
        vmid = self._next_vmid
        self._next_vmid += 1
        template = self.config.template()
        return VMSpec(
            vmid=vmid, vm_name=f'{NAME_PREFIX}{secrets.token_hex(6)}',
            display_name=f'live contract {vmid}', backend=self.config.backend,
            template=template, memory_mb=1024, cores=1,
            storage_gb=template['disk_gb'],
            is_entry_point=True, entry_slot=slot, internal_slot=slot,
            expires_at=self.now() + self.config.expiry,
            deployment_id=secrets.token_hex(16), owner='live-contract')

    def admit(self, spec: VMSpec) -> str:
        reservation_id = f'live-contract-{secrets.token_hex(8)}'
        # Claimed first: release is idempotent, a leaked lease is not.
        self._reservations.append(reservation_id)
        return self.backend.admit(spec, reservation_id=reservation_id,
                                  load_balancing_config=None)

    def ensure_router(self) -> None:
        self.backend.ensure_router_running()

    def note_router_started(self) -> None:
        """Record a router start made outside the backend.

        ensure_router_running() waits until the provider reports the router
        running, so its reads are settled. A test that starts the router
        directly calls this; teardown then requires a non-stopped read
        before it stops the router or trusts a 'stopped' read."""
        self._router_started_at = self.clock()

    def stop_router(self) -> bool:
        """Stop the router through the backend, as a test step. A stop this
        call issued proves the start was visible, which settles a recorded
        direct start; the backend's answer is returned."""
        issued = self.backend.stop_router()
        if issued:
            self._router_started_at = None
        return issued

    def ensure_slot(self, slot: int) -> None:
        if slot not in self._slots:
            self._slots.append(slot)
        self.backend.ensure_slot(slot)

    def claim(self, spec: VMSpec, location: str) -> VMRef:
        """Record the instance `spec` may become, before anything launches
        it: teardown then destroys whatever exists under that identity."""
        ref = VMRef(vmid=spec.vmid, vm_name=spec.vm_name, vm_type=spec.backend,
                    location=location, backend=spec.backend, provider_id=None,
                    deployment_id=spec.deployment_id, network_slot=spec.network_slot)
        self._refs[spec.vmid] = ref
        if spec.deployment_id not in self._deployments:
            self._deployments.append(spec.deployment_id)
        return ref

    def _heartbeat(self) -> None:
        for reservation_id in self._reservations:
            self.backend.renew_admission(reservation_id)

    def create(self, spec: VMSpec, location: str, *,
               via: Optional[VMBackend] = None) -> VMRef:
        """Launch `spec` through the backend (or `via`, another client of the
        same provider and site); return the ref a record would hold."""
        ref = self.claim(spec, location)
        launched = (via or self.backend).create(spec, location, heartbeat=self._heartbeat)
        ref = replace(ref, location=launched.location, provider_id=launched.provider_id,
                      network_slot=launched.network_slot, vm_type=launched.vm_type)
        self._refs[spec.vmid] = ref
        return ref

    def launch_one(self) -> VMRef:
        """Admission, router, slot and one instance, in the app's order."""
        spec = self.new_spec()
        location = self.admit(spec)
        self.ensure_router()
        self.ensure_slot(spec.network_slot)
        return self.create(spec, location)

    # --- teardown --------------------------------------------------------

    def _failure(self, what: str, error: BaseException) -> str:
        return (f'{what} was NOT cleaned up: {type(error).__name__}: {error}\n'
                'Inventory the live site by hand:\n' + self.config.manual_inventory_commands())

    def _settled(self, action: Callable[[], None]) -> None:
        """Run a network delete, retrying a provider failure (EC2 reports a
        network still in use) until the settle window ends. An identity
        refusal and any non-provider error are never retried."""
        deadline = self.clock() + self.settle_seconds
        while True:
            try:
                action()
                return
            except DestroyRefused:
                raise
            except BackendError:
                if self.clock() >= deadline:
                    raise
            self.sleep(self.poll_seconds)

    def _instance_label(self, ref: VMRef) -> str:
        provider_id = ref.provider_id
        if provider_id is None and ref.backend == 'gcp':
            provider_id = instance_name(self.config.site_id, ref.vmid)
        return (f'instance of VM {ref.vmid} '
                f'({provider_id or "no provider id; resolved by tags"}, {ref.location})')

    def teardown(self) -> List[str]:
        """Remove everything this run made; return one report per failure."""
        failures: List[str] = []
        site = self.config.site_id

        for vmid in sorted(self._refs):
            ref = self._refs[vmid]
            try:
                self.backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
                if not self.backend.confirm_absent(ref):
                    raise CloudCleanupError('no positive absence after destroy')
                del self._refs[vmid]
            except Exception as error:
                failures.append(self._failure(self._instance_label(ref), error))

        for deployment_id in list(self._deployments):
            try:
                self._settled(lambda: self.backend.cleanup_deployment(deployment_id))
                self._deployments.remove(deployment_id)
            except Exception as error:
                failures.append(self._failure(
                    f'deployment network {deployment_network_name(site, deployment_id)} '
                    f'(deployment {deployment_id})', error))

        for slot in list(self._slots):
            try:
                self._settled(lambda: self.backend.release_slot(slot))
                self._slots.remove(slot)
            except Exception as error:
                failures.append(self._failure(
                    f'slot network {slot_network_name(site, slot)} (slot {slot})', error))

        for reservation_id in list(self._reservations):
            try:
                self.backend.release_admission(reservation_id)
                self._reservations.remove(reservation_id)
            except Exception as error:
                failures.append(self._failure(f'admission {reservation_id}', error))

        if self._router_started_at is not None:
            unseen = self._await_router_start()
            if unseen is not None:
                failures.append(unseen)
        try:
            self.backend.stop_router()
        except Exception as error:
            failures.append(self._failure('the live site router (stop)', error))

        failures.extend(self._prove_empty())
        failures.extend(self._prove_router_stopped())
        for failure in failures:
            print(f'[live cloud contract cleanup] {failure}', file=sys.stderr, flush=True)
        return failures

    def _prove_empty(self) -> List[str]:
        """A fresh site inventory must be empty. Leftovers are deleted from
        that inventory alone, then a second inventory must be empty."""
        try:
            remaining = self.backend.list_managed()
        except Exception as error:
            return [self._failure('the post-teardown inventory (unreadable)', error)]
        if not remaining:
            return []
        failures: List[str] = []
        print('[live cloud contract cleanup] normal cleanup left '
              + ', '.join(_describe(r) for r in remaining) + '; deleting them',
              file=sys.stderr, flush=True)
        for resource in sorted(remaining, key=lambda r: _KIND_ORDER.get(r.kind, 99)):
            try:
                self._settled(lambda: self.backend.delete_resource(resource))
            except Exception as error:
                failures.append(self._failure(_describe(resource), error))
        try:
            still = self.backend.list_managed()
        except Exception as error:
            return failures + [self._failure('the second post-teardown inventory', error)]
        if still:
            failures.append(
                f'live site {self.config.site_id!r} still holds '
                + ', '.join(_describe(r) for r in still)
                + '\nInventory and remove them by hand:\n'
                + self.config.manual_inventory_commands())
        return failures

    def _await_router_start(self) -> Optional[str]:
        """Poll until a fresh read shows the directly started router as not
        stopped; None once it does, else a failure report after
        ROUTER_READ_LAG_SECONDS from the start. Until the start is visible
        every 'stopped' read may be the pre-start answer, so neither the stop
        nor the final check could be trusted."""
        deadline = self._router_started_at + ROUTER_READ_LAG_SECONDS
        while True:
            try:
                if not self.backend.router_stopped():
                    return None
            except Exception as error:
                return self._failure('the live site router (state unreadable)', error)
            if self.clock() >= deadline:
                return (f'the live site {self.config.site_id!r} router still read stopped '
                        f'{ROUTER_READ_LAG_SECONDS}s after this run started it, so no '
                        "stopped reading can be trusted; it may be running\nCheck it and "
                        'stop it (never terminate it) by hand:\n'
                        + self.config.manual_router_stop_command())
            self.sleep(ROUTER_POLL_SECONDS)

    def _prove_router_stopped(self) -> List[str]:
        """The site router must read stopped after teardown. The inventory
        lists only what a run creates, never the operator's router, so an
        empty inventory says nothing about a router left billing."""
        try:
            if self.backend.router_stopped():
                return []
        except Exception as error:
            return [self._failure('the live site router (state unreadable)', error)]
        return [f'the live site {self.config.site_id!r} router is not stopped after '
                'teardown\nStop it (never terminate it) by hand:\n'
                + self.config.manual_router_stop_command()]
