"""Opt-in gates and fixtures for the live Proxmox, AWS and GCP contract runs.

Each live item is skipped unless its own provider is opted in and fully
configured: ``live_aws`` items need LIVE_AWS=1, ``live_gcp`` items LIVE_GCP=1,
and plain ``live`` items (Proxmox) LIVE_PROXMOX=1. ``pytest tests/`` and CI
therefore collect and skip them without opening a connection or building a
cloud client. See tests/live/README.md.
"""

import importlib
import os
from unittest.mock import patch

import pytest

from tests.live.cloud_harness import (
    CloudContractRun, CloudLiveConfig, select_live_skip_reason)
from tests.live.harness import ContractRun, LiveConfig, read_portal_vmid_mark


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'live: creates and destroys real Proxmox guests; runs only with '
        'LIVE_PROXMOX=1 (see tests/live/README.md)')
    config.addinivalue_line(
        'markers',
        'live_aws: launches and deletes real EC2 resources in a dedicated live '
        'site; runs only with LIVE_AWS=1 (see tests/live/README.md)')
    config.addinivalue_line(
        'markers',
        'live_gcp: launches and deletes real Compute Engine resources in a '
        'dedicated live site; runs only with LIVE_GCP=1 (see tests/live/README.md)')


def pytest_collection_modifyitems(config, items):
    for item in items:
        reason = select_live_skip_reason(
            {mark.name for mark in item.iter_markers()}, os.environ)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


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
    # Read per run: the mark only rises, and a stale value could let a range
    # that is now above it through.
    mark = (read_portal_vmid_mark(live_config.portal_database_url)
            if live_config.portal_database_url else None)
    with ContractRun(live_proxmox, live_config.vmid_range,
                     portal_vmid_mark=mark,
                     separate_cluster=live_config.separate_cluster) as run:
        yield run


# --- cloud ----------------------------------------------------------------
# Fixtures only run for items that were not skipped, so nothing below imports
# a provider module or builds a client unless that provider's guard passed.


def _live_cloud_backend(cloud_config: CloudLiveConfig):
    """The app's own backend, built through the registry while app.config
    holds exactly the values the guard validated (a stray .env value can
    never widen the cap or change the site)."""
    from app.backends import get_backend, reset_backends_for_tests
    # Not `from app import config`: the package re-exports the config-class
    # mapping under that name, shadowing the module.
    config = importlib.import_module('app.config')
    with patch.multiple(config, **cloud_config.settings):
        reset_backends_for_tests()
        try:
            yield get_backend(cloud_config.backend)
        finally:
            reset_backends_for_tests()


@pytest.fixture(scope='session')
def aws_live_config():
    return CloudLiveConfig.from_env('aws', os.environ)


@pytest.fixture(scope='session')
def aws_backend(aws_live_config):
    yield from _live_cloud_backend(aws_live_config)


@pytest.fixture
def aws_run(aws_backend, aws_live_config):
    """A CloudContractRun whose teardown runs, pass or fail."""
    with CloudContractRun(aws_backend, aws_live_config) as run:
        yield run


@pytest.fixture(scope='session')
def gcp_live_config():
    return CloudLiveConfig.from_env('gcp', os.environ)


@pytest.fixture(scope='session')
def gcp_backend(gcp_live_config):
    yield from _live_cloud_backend(gcp_live_config)


@pytest.fixture
def gcp_run(gcp_backend, gcp_live_config):
    """A CloudContractRun whose teardown runs, pass or fail."""
    with CloudContractRun(gcp_backend, gcp_live_config) as run:
        yield run
