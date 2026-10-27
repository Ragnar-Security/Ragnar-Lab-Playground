"""Contract checks against a real Proxmox cluster.

The unit suite mocks proxmoxer; these tests confirm the real API still has
the shapes those mocks assume. Opt-in only: see tests/live/README.md. All
guests are created and destroyed through ContractRun (tests/live/harness.py).
"""

import os
import re
import time
import warnings
from datetime import datetime, timedelta

import pytest
from proxmoxer.core import ResourceException

from app.orphan_reaper import _parse_expiration
from app.proxmox_service import (
    VM_GUEST_MATCH,
    confirm_proxmox_absence,
    get_cluster_vm_index,
    get_node_resources,
    inspect_vm_guest,
    is_proxmox_not_found,
)
from tests.live.harness import ContractRun


pytestmark = pytest.mark.live

_DISK_KEY = re.compile(r'(scsi|virtio|sata|ide)\d+')


class InjectedFailure(Exception):
    pass


def _fresh_index(proxmox):
    return get_cluster_vm_index(proxmox, max_age=0, allow_stale=False)


def _agent_enabled(config):
    first = str(config.get('agent', '0')).split(',')[0].strip()
    return first in ('1', 'enabled=1')


def _assert_absent_with_app_semantics(proxmox, guest):
    """A destroyed VMID reads the way the app's absence checks expect."""
    assert confirm_proxmox_absence(proxmox, guest.vmid)
    endpoint = proxmox.nodes(guest.node).qemu(guest.vmid)
    with pytest.raises(ResourceException) as missing:
        endpoint.status.current.get()
    error = missing.value
    assert isinstance(error.status_code, int)
    # The app classifies a missing guest by HTTP 404 or by the "does not
    # exist" text that PVE sends with HTTP 500, then confirms with inventory.
    assert error.status_code in (404, 500), error
    assert is_proxmox_not_found(error), error
    assert confirm_proxmox_absence(proxmox, guest.vmid, error)


def test_cluster_resources_list_the_fields_inventory_reads(live_proxmox, live_config):
    rows = live_proxmox.cluster.resources.get(type='vm')

    assert isinstance(rows, list)
    for row in rows:
        assert {'vmid', 'node', 'type', 'status'} <= row.keys(), row
        assert row['type'] in ('qemu', 'lxc'), row
    index = _fresh_index(live_proxmox)
    for node, template_vmid in live_config.templates.items():
        entry = index.get(template_vmid)
        assert entry is not None, f'template {template_vmid} is not in inventory'
        assert entry['node'] == node, (
            f'template {template_vmid} lives on {entry["node"]}, not {node}; '
            'the app clones on the template node')
        assert entry['type'] == 'qemu'
        raw = next(r for r in rows if int(r['vmid']) == template_vmid)
        # The orphan reaper skips templates by this field.
        assert raw.get('template') in (1, '1', True), raw


def test_node_status_carries_the_memory_fields_admission_reads(live_proxmox, live_node):
    status = live_proxmox.nodes(live_node).status.get()

    memory = status['memory']
    for key in ('total', 'used', 'free', 'available'):
        assert isinstance(memory[key], int) and memory[key] >= 0, (key, memory)
    # Admission uses MemAvailable because MemFree excludes reclaimable
    # cache and ZFS ARC (docs/LOAD_BALANCING.md).
    assert memory['free'] <= memory['available'] <= memory['total'], memory
    assert isinstance(status['cpu'], (int, float))
    assert status['uptime'] > 0
    assert status['cpuinfo']['cpus'] >= 1

    resources = get_node_resources(live_proxmox, live_node)
    assert resources['online'] is True
    assert resources['memory_total'] == memory['total']
    assert resources['cpu_count'] == status['cpuinfo']['cpus']
    assert 0 < resources['memory_available'] <= resources['memory_total']
    assert 0 <= resources['memory_linux_free'] <= resources['memory_total']


def test_clone_start_stop_destroy_round_trip(contract_run, live_proxmox,
                                             live_config, live_node):
    guest = contract_run.clone_template(
        live_node, live_config.template_for(live_node))
    endpoint = live_proxmox.nodes(live_node).qemu(guest.vmid)

    entry = _fresh_index(live_proxmox)[guest.vmid]
    assert (entry['name'], entry['node'], entry['type']) == (
        guest.name, live_node, 'qemu')
    assert inspect_vm_guest(live_proxmox, guest.vmid, node=live_node,
                            vm_name=guest.name, vm_type='qemu') == VM_GUEST_MATCH
    config = endpoint.config.get()
    assert config['name'] == guest.name
    expires = _parse_expiration(config.get('description'))
    assert expires is not None, config.get('description')
    assert timedelta(minutes=20) < expires - datetime.now() <= timedelta(minutes=30)

    started = contract_run.start(guest)
    assert (started['success'], started['status']) == (True, 'stopped'), started
    assert endpoint.status.current.get()['status'] == 'running'

    if _agent_enabled(config):
        _check_guest_agent_if_it_answers(endpoint)

    stopped = contract_run.stop(guest)
    upid = stopped['upid']
    assert isinstance(upid, str) and upid.startswith('UPID:'), upid
    assert stopped['success'], stopped
    # The raw task status wait_for_proxmox_task maps to success.
    task = live_proxmox.nodes(live_node).tasks(upid).status.get()
    assert task['status'] == 'stopped' and task['exitstatus'] == 'OK', task
    assert endpoint.status.current.get()['status'] == 'stopped'

    assert contract_run.destroy(guest.vmid) == {'success': True, 'status': 'stopped'}
    _assert_absent_with_app_semantics(live_proxmox, guest)


def _check_guest_agent_if_it_answers(endpoint):
    """The agent is optional; when it answers, its reply must parse as the app reads it."""
    deadline = time.monotonic() + int(os.environ.get('LIVE_AGENT_TIMEOUT', '90'))
    while True:
        try:
            reply = endpoint.agent('network-get-interfaces').get()
            break
        except ResourceException:
            if time.monotonic() >= deadline:
                warnings.warn('guest agent is enabled but never answered; '
                              'agent shape not checked')
                return
            time.sleep(3)
    assert isinstance(reply.get('result'), list), reply
    for iface in reply['result']:
        assert 'name' in iface, iface
        for address in iface.get('ip-addresses', []):
            assert {'ip-address', 'ip-address-type'} <= address.keys(), address


def test_full_clone_lands_on_the_configured_storage(contract_run, live_proxmox,
                                                    live_config, live_node):
    storage = live_config.storage_for(live_node)
    if not storage:
        pytest.skip(f'LIVE_CLONE_STORAGE names no storage for {live_node}')

    guest = contract_run.clone_template(
        live_node, live_config.template_for(live_node), storage=storage)

    config = live_proxmox.nodes(live_node).qemu(guest.vmid).config.get()
    disks = {key: value for key, value in config.items()
             if _DISK_KEY.fullmatch(key) and 'media=cdrom' not in str(value)}
    assert disks, config
    for key, value in disks.items():
        assert str(value).startswith(f'{storage}:'), (key, value)
    contract_run.destroy(guest.vmid)
    _assert_absent_with_app_semantics(live_proxmox, guest)


def test_cleanup_removes_guests_after_an_injected_failure(request, live_proxmox,
                                                          live_config, live_node):
    run = ContractRun(live_proxmox, live_config.vmid_range)
    # Backstop if the context manager's own cleanup fails part-way; cleanup
    # is a no-op once nothing is owned.
    request.addfinalizer(run.cleanup)
    created = []

    with pytest.raises(InjectedFailure):
        with run:
            guest = run.clone_template(
                live_node, live_config.template_for(live_node))
            created.append(guest)
            assert run.start(guest)['success']
            raise InjectedFailure('injected after start')

    assert run.owned_vmids() == []
    assert created
    for guest in created:
        _assert_absent_with_app_semantics(live_proxmox, guest)
