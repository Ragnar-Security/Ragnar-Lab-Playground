"""Fixtures shared by the backend-generic provisioning and lifecycle tests."""

import importlib

import pytest

from app.backends import register_backend_factory
from app.backends.proxmox import ProxmoxBackend
from tests.backends.fake_backend import make_memory_backend_case
from tests.test_live_contract_guards import FakeProxmox


@pytest.fixture
def memory_backend_case(app, monkeypatch):
    """A registered in-memory 'aws' case on a mixed Proxmox + AWS portal.

    Proxmox is served by an empty FakeProxmox: with Proxmox enabled the VMID
    allocator reads its inventory (ruling I19), and a test must never reach a
    real cluster through the default factory.
    """
    case = make_memory_backend_case(app, name='aws')
    monkeypatch.setattr(importlib.import_module('app.config'),
                        'ENABLED_BACKENDS', ('proxmox', 'aws'))
    register_backend_factory('aws', lambda: case.backend)
    register_backend_factory('proxmox', lambda: ProxmoxBackend(client=FakeProxmox()))
    yield case
    case.cleanup()
