"""Opt-in gate and fixtures for the live Proxmox contract run.

Every test marked ``live`` is skipped unless LIVE_PROXMOX=1 and the rest of
its environment is present, so ``pytest tests/`` and CI collect and skip it
without opening a connection. See tests/live/README.md.
"""

import os

import pytest

from tests.live.harness import ContractRun, LiveConfig, live_skip_reason


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'live: creates and destroys real Proxmox guests; runs only with '
        'LIVE_PROXMOX=1 (see tests/live/README.md)')


def pytest_collection_modifyitems(config, items):
    reason = live_skip_reason(os.environ)
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker('live'):
            item.add_marker(skip)


def pytest_generate_tests(metafunc):
    if 'live_node' in metafunc.fixturenames:
        # Collection must not fail while skipped, so an unset LIVE_NODE
        # still yields one (skipped) case per test.
        nodes = [n.strip() for n in os.environ.get('LIVE_NODE', '').split(',')
                 if n.strip()] or ['unconfigured']
        metafunc.parametrize('live_node', nodes)


@pytest.fixture(scope='session')
def live_config():
    return LiveConfig.from_env(os.environ)


@pytest.fixture(scope='session')
def live_proxmox(live_config):
    # Imported here so a skipped run never builds a connection.
    from app.proxmox_service import get_proxmox_connection
    return get_proxmox_connection()


@pytest.fixture
def contract_run(live_proxmox, live_config):
    """A ContractRun whose cleanup runs at teardown, pass or fail."""
    with ContractRun(live_proxmox, live_config.vmid_range) as run:
        yield run
