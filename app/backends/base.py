"""The provider-neutral VM lifecycle contract.

Every backend (Proxmox, AWS, GCP) implements VMBackend; routes and tasks hold
only VMRef/VMSpec values and never a provider client. Semantics every
backend shares:

- inspect/destroy/confirm_absent accept a VMRef whose provider_id is None and
  resolve the guest from site/VMID identity: a create that raised after
  launch leaves a record without a provider id, and that guest must still be
  findable or it bills forever.
- destroy verifies identity first and never deletes a mismatching guest.
- set_expiry raises BackendError on failure; callers that want best effort
  catch it, so a failed write can never be counted as a success.

This module imports nothing from the app, so any module may import it.
"""

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Mapping, Optional, Protocol

BACKEND_PROXMOX = 'proxmox'
BACKEND_AWS = 'aws'
BACKEND_GCP = 'gcp'
KNOWN_BACKENDS = (BACKEND_PROXMOX, BACKEND_AWS, BACKEND_GCP)

# The same strings as proxmox_service.VM_GUEST_*, so either set compares equal.
GUEST_MATCH = 'match'
GUEST_ABSENT = 'absent'
GUEST_MISMATCH = 'mismatch'
GUEST_UNKNOWN = 'unknown'


class DestroyPolicy(enum.Enum):
    STRICT = 'strict'                # expiry: refuse delete when stop fails
    BEST_EFFORT = 'best_effort'      # admin bulk, failed deploy: delete despite stop error
    FORCE_CORRUPT = 'force_corrupt'  # student destroy: force delete of corrupt-config guest


class BackendError(Exception):
    """Base of every backend failure."""


class BackendNotEnabled(BackendError):
    """The backend is unknown, disabled, or has no implementation."""


class AdmissionRefused(BackendError):
    """Capacity cap reached or no node can take the guest."""


class DestroyRefused(BackendError):
    """The policy or an identity check forbade the delete."""


class SlotNetworkMissing(BackendError):
    """A cloud create found its slot network gone (released after ensure_slot).

    Distinct so the caller can re-ensure the slot and retry once.
    """


@dataclass(frozen=True)
class VMRef:
    vmid: int
    vm_name: str
    vm_type: str
    location: str
    backend: str
    provider_id: Optional[str] = None
    deployment_id: Optional[str] = None
    network_slot: Optional[int] = None

    @classmethod
    def from_record(cls, record) -> 'VMRef':
        """Build from a VMRecord or any object with its attributes.

        getattr defaults keep pre-migration rows and legacy test objects
        working: they lack the backend columns and are Proxmox guests.
        """
        return cls(
            vmid=record.vmid,
            vm_name=record.vm_name,
            vm_type=record.vm_type,
            location=record.node,
            backend=getattr(record, 'backend', None) or BACKEND_PROXMOX,
            provider_id=getattr(record, 'provider_id', None),
            deployment_id=getattr(record, 'deployment_id', None),
            network_slot=getattr(record, 'network_slot', None),
        )


@dataclass(frozen=True)
class VMSpec:
    vmid: int
    vm_name: str
    display_name: str
    backend: str
    template: Mapping[str, Any]  # the backend's resolved template block
    memory_mb: int
    cores: int
    storage_gb: int
    is_entry_point: bool
    entry_slot: Optional[int]    # owner's entry VLAN tag; None when not an entry point
    internal_slot: int           # owner's internal VLAN tag
    expires_at: datetime
    deployment_id: str           # the bundle's vmid_reservation_id
    owner: str                   # student login / group slug / koth

    @property
    def network_slot(self) -> int:
        return self.entry_slot if self.is_entry_point else self.internal_slot


@dataclass(frozen=True)
class LaunchedVM:
    vmid: int
    vm_type: str
    location: str
    network_slot: int
    provider_id: Optional[str] = None


@dataclass(frozen=True)
class ManagedResource:
    backend: str
    kind: str  # 'instance' | 'deployment-network' | 'slot-network'
    provider_id: str
    name: str
    vmid: Optional[int] = None
    location: Optional[str] = None
    deployment_id: Optional[str] = None
    network_slot: Optional[int] = None
    expires_at_epoch: Optional[float] = None
    created_at_epoch: Optional[float] = None
    status: Optional[str] = None
    is_template: bool = False
    # A Proxmox guest's 'qemu'/'lxc', which its identity check and a
    # survivor record need; None on the clouds, whose instances' vm_type is
    # the backend name.
    vm_type: Optional[str] = None


class VMBackend(Protocol):
    name: str

    def admit(self, spec: VMSpec, *, reservation_id: str,
              load_balancing_config: Optional[Mapping]) -> str:
        """Reserve capacity; return the location. Raises AdmissionRefused."""
        ...

    def renew_admission(self, reservation_id: str) -> bool:
        ...

    def release_admission(self, reservation_id: Optional[str]) -> bool:
        ...

    def create(self, spec: VMSpec, location: str, *,
               heartbeat: Callable[[], None]) -> LaunchedVM:
        ...

    def inspect(self, ref: VMRef) -> str:
        """One of GUEST_MATCH / GUEST_ABSENT / GUEST_MISMATCH / GUEST_UNKNOWN."""
        ...

    def destroy(self, ref: VMRef, policy: DestroyPolicy) -> None:
        """Raises on failure, DestroyRefused when policy or identity forbids it."""
        ...

    def confirm_absent(self, ref: VMRef, error: Optional[Exception] = None) -> bool:
        ...

    def ip_addresses(self, ref: VMRef) -> List[str]:
        """[] when unknown; never ['N/A'] (the UI maps that)."""
        ...

    def status(self, ref: VMRef) -> str:
        """'running' | 'stopped' | 'starting' | 'unknown' | a provider string."""
        ...

    def set_expiry(self, ref: VMRef, expires_at: datetime) -> None:
        """Raises BackendError on failure."""
        ...

    def list_managed(self) -> List[ManagedResource]:
        ...

    def delete_resource(self, resource: ManagedResource) -> None:
        ...

    def cleanup_deployment(self, deployment_id: str) -> None:
        ...

    def ensure_slot(self, slot: int) -> None:
        ...

    def release_slot(self, slot: int) -> None:
        ...

    def ensure_router_running(self) -> None:
        ...

    def stop_router(self) -> None:
        ...
