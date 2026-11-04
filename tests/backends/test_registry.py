"""Backend registry and the record-facing dataclasses."""

import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.backends import (
    backend_for, enabled_backends, get_backend, register_backend_factory,
    reset_backends_for_tests,
)
from app.backends.base import BACKEND_PROXMOX, BackendNotEnabled, VMRef, VMSpec
from tests.backends.fake_backend import FakeBackend

# The module, not `from app import config`: the package shadows that name
# with the config-class mapping.
config = importlib.import_module('app.config')


# Catches legacy rows becoming undispatchable after the schema migration.
def test_backend_for_legacy_record_defaults_to_proxmox(monkeypatch):
    fake = FakeBackend('proxmox')
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    register_backend_factory('proxmox', lambda: fake)
    assert backend_for(SimpleNamespace(backend=None)) is fake
    assert backend_for(SimpleNamespace(backend='')) is fake


# Catches constructing a new SDK/client wrapper on every route call.
def test_get_backend_is_process_singleton(monkeypatch):
    created = []
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    register_backend_factory('aws', lambda: created.append(FakeBackend('aws')) or created[-1])
    assert get_backend('aws') is get_backend('aws')
    assert len(created) == 1


# Catches disabled or misspelled backends being silently constructed.
@pytest.mark.parametrize('name', ['aws', 'unknown'])
def test_get_backend_refuses_disabled_or_unknown(monkeypatch, name):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    with pytest.raises(BackendNotEnabled):
        get_backend(name)


# Catches an unknown name dispatching just because someone listed it.
def test_get_backend_refuses_unknown_name_even_when_listed(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox', 'unknown'))
    register_backend_factory('unknown', lambda: FakeBackend('unknown'))
    with pytest.raises(BackendNotEnabled):
        get_backend('unknown')


# Catches an enabled backend with no implementation failing as a KeyError
# deep in a route instead of a refusal callers already handle.
def test_get_backend_refuses_enabled_backend_without_factory(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox', 'gcp'))
    with pytest.raises(BackendNotEnabled):
        get_backend('gcp')


# Catches the enabled set being frozen at import, so tests and operators
# patching config would talk to a stale list.
def test_enabled_backends_reads_config_at_call_time(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox', 'aws'))
    assert enabled_backends() == ('proxmox', 'aws')
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('gcp',))
    assert enabled_backends() == ('gcp',)


# Catches a test's stateful fake or patched client leaking into the next test.
def test_reset_drops_instances_and_registered_factories(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    # AWS has a production default factory; remove it so only the
    # registered one could answer after the reset.
    monkeypatch.delitem(importlib.import_module('app.backends')._DEFAULT_FACTORIES, 'aws')
    first = FakeBackend('aws')
    register_backend_factory('aws', lambda: first)
    assert get_backend('aws') is first
    reset_backends_for_tests()
    with pytest.raises(BackendNotEnabled):
        get_backend('aws')


# Catches a re-registered factory being ignored because an older instance is cached.
def test_register_replaces_cached_instance(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    first, second = FakeBackend('aws'), FakeBackend('aws')
    register_backend_factory('aws', lambda: first)
    assert get_backend('aws') is first
    register_backend_factory('aws', lambda: second)
    assert get_backend('aws') is second


def _spec(**overrides):
    values = dict(
        vmid=100, vm_name='vm-100', display_name='VM 100', backend='aws',
        template={}, memory_mb=1024, cores=1, storage_gb=20,
        is_entry_point=True, entry_slot=101, internal_slot=201,
        expires_at=datetime.now() + timedelta(hours=1),
        deployment_id='a' * 32, owner='student-a')
    values.update(overrides)
    return VMSpec(**values)


# Catches an entry point launched into the internal network, or the reverse.
def test_network_slot_is_entry_for_entry_points_internal_otherwise():
    assert _spec(is_entry_point=True).network_slot == 101
    assert _spec(is_entry_point=False, entry_slot=None).network_slot == 201


# Catches a cloud record losing its provider identity, or a node not
# becoming the location the backend inspects.
def test_vmref_from_record_copies_identity():
    record = SimpleNamespace(
        vmid=100, vm_name='vm-100', vm_type='aws', node='zone-a', backend='aws',
        provider_id='provider-100', deployment_id='a' * 32, network_slot=101)
    assert VMRef.from_record(record) == VMRef(
        vmid=100, vm_name='vm-100', vm_type='aws', location='zone-a',
        backend='aws', provider_id='provider-100', deployment_id='a' * 32,
        network_slot=101)


# Catches pre-migration test objects and legacy rows failing to build a ref.
@pytest.mark.parametrize('backend', [None, ''])
def test_vmref_from_legacy_record_defaults_to_proxmox(backend):
    legacy = SimpleNamespace(vmid=200, vm_name='vm-200', vm_type='qemu',
                             node='node-a', backend=backend)
    ref = VMRef.from_record(legacy)
    assert ref.backend == BACKEND_PROXMOX
    assert ref.location == 'node-a'
    assert (ref.provider_id, ref.deployment_id, ref.network_slot) == (None, None, None)
    bare = SimpleNamespace(vmid=200, vm_name='vm-200', vm_type='qemu', node='node-a')
    assert VMRef.from_record(bare).backend == BACKEND_PROXMOX
