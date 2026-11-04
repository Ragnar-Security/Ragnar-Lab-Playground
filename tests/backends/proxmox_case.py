"""ProxmoxBackend over the stateful FakeProxmox, for adapter and contract tests.

Imports nothing that reads Proxmox at import time, so the shared contract
suite can import make_proxmox_backend_case once T7 activates it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.backends.base import BACKEND_PROXMOX, VMRef, VMSpec
from app.backends.proxmox import ProxmoxBackend
from tests.backends.fake_backend import BackendContractCase
from tests.test_live_contract_guards import FakeProxmox

GUEST_VMID = 100
GUEST_NAME = 'student-test-vm'
GUEST_NODE = 'node-a'
GUEST_IP = '10.1.0.5'
TEMPLATE_VMID = 9000


@dataclass
class ProxmoxCase:
    backend: ProxmoxBackend
    api: FakeProxmox
    ref: VMRef
    expires_at: datetime


def forget_proxmox_caches() -> None:
    """Drop the process-wide inventory and IP caches the adapter reads.

    Both outlive a test: a cached index or address from one case would
    answer the next case's reads without a provider round trip.
    """
    from app.proxmox_service import reset_cluster_vm_index
    from app.routes import utils as route_utils
    reset_cluster_vm_index()
    with route_utils._IP_CACHE_LOCK:
        route_utils._IP_CACHE.clear()


def build_proxmox_case(*, vm_type: str = 'qemu',
                       corrupt_config: bool = False) -> ProxmoxCase:
    forget_proxmox_caches()
    expires_at = datetime.now() + timedelta(hours=1)
    api = FakeProxmox()
    api.add_guest(GUEST_VMID, GUEST_NAME, node=GUEST_NODE, status='running',
                  description=f'Expires: {expires_at.isoformat()}',
                  vm_type=vm_type, ips=[GUEST_IP])
    api.add_guest(TEMPLATE_VMID, 'template-base', node=GUEST_NODE, template=1)
    if corrupt_config:
        api.corrupt_config.add(GUEST_VMID)
    ref = VMRef(vmid=GUEST_VMID, vm_name=GUEST_NAME, vm_type=vm_type,
                location=GUEST_NODE, backend=BACKEND_PROXMOX)
    return ProxmoxCase(backend=ProxmoxBackend(client=api), api=api, ref=ref,
                       expires_at=expires_at)


def make_proxmox_backend_case(app) -> BackendContractCase:
    """The shared-contract case T7 appends once ProxmoxBackend can create."""
    from app.routes.vm_creation import extract_template_config
    forget_proxmox_caches()
    api = FakeProxmox()
    api.add_guest(TEMPLATE_VMID, 'template-base', node=GUEST_NODE, template=1)
    spec = VMSpec(
        vmid=GUEST_VMID, vm_name=GUEST_NAME, display_name='test-vm',
        backend=BACKEND_PROXMOX,
        template=extract_template_config({
            'name': 'test-vm', 'type': 'qemu', 'memory': 1024, 'cores': 1,
            'storage': 20, 'source_vmid': TEMPLATE_VMID, 'entry_point': True,
        }),
        memory_mb=1024, cores=1, storage_gb=20,
        is_entry_point=True, entry_slot=101, internal_slot=201,
        expires_at=datetime.now() + timedelta(hours=1),
        deployment_id='a' * 32, owner='student-a')
    # A fresh clone has no guest agent answer yet, so no addresses.
    return BackendContractCase(backend=ProxmoxBackend(client=api), spec=spec,
                               expected_ips=[], expected_status='running',
                               cleanup=forget_proxmox_caches,
                               inventory_reports_expiry=False)
