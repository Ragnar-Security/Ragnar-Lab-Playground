"""
Unit tests for configuration validation
Tests validate_config() behavior with various configurations
"""

import importlib
import os
import sys
import unittest
import logging
from unittest.mock import patch

import pytest

from app.config import validate_config


class TestConfig(unittest.TestCase):
    """Test cases for configuration validation"""

    def setUp(self):
        # These cases exercise the Proxmox checks. An unset FLASK_ENV means
        # production, whose SECRET_KEY/database checks have their own tests
        # in test_security_settings.py.
        env = patch.dict(os.environ, {'FLASK_ENV': 'development'})
        env.start()
        self.addCleanup(env.stop)

    def test_validate_config_missing_proxmox_host(self):
        """validate_config raises ValueError when PROXMOX_HOST is empty"""
        with patch('app.config.PROXMOX_HOST', ''):
            with patch('app.config.PROXMOX_TOKEN_ID', 'some-token'):
                with patch('app.config.PROXMOX_TOKEN_SECRET', 'some-secret'):
                    with self.assertRaises(ValueError) as ctx:
                        validate_config()
                    self.assertIn('PROXMOX_HOST', str(ctx.exception))

    def test_validate_config_missing_auth(self):
        """validate_config raises ValueError when no auth method configured"""
        with patch('app.config.PROXMOX_HOST', 'proxmox.example.com'):
            with patch('app.config.PROXMOX_TOKEN_ID', ''):
                with patch('app.config.PROXMOX_TOKEN_SECRET', ''):
                    with patch('app.config.PROXMOX_PASSWORD', ''):
                        with self.assertRaises(ValueError) as ctx:
                            validate_config()
                        self.assertIn('authentication', str(ctx.exception).lower())

    def test_validate_config_valid_with_token(self):
        """validate_config passes with valid token-based config"""
        with patch('app.config.PROXMOX_HOST', 'proxmox.example.com'):
            with patch('app.config.PROXMOX_TOKEN_ID', 'user@pam!mytoken'):
                with patch('app.config.PROXMOX_TOKEN_SECRET', 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'):
                    with patch('app.config.PROXMOX_PASSWORD', ''):
                        # Should not raise
                        validate_config()

    def test_validate_config_valid_with_password(self):
        """validate_config passes with valid password-based config"""
        with patch('app.config.PROXMOX_HOST', 'proxmox.example.com'):
            with patch('app.config.PROXMOX_TOKEN_ID', ''):
                with patch('app.config.PROXMOX_TOKEN_SECRET', ''):
                    with patch('app.config.PROXMOX_PASSWORD', 'mysecretpassword'):
                        # Should not raise
                        validate_config()

    def test_validate_config_missing_host_and_auth(self):
        """validate_config raises ValueError listing both issues when host and auth missing"""
        with patch('app.config.PROXMOX_HOST', ''):
            with patch('app.config.PROXMOX_TOKEN_ID', ''):
                with patch('app.config.PROXMOX_TOKEN_SECRET', ''):
                    with patch('app.config.PROXMOX_PASSWORD', ''):
                        with self.assertRaises(ValueError) as ctx:
                            validate_config()
                        error_msg = str(ctx.exception)
                        self.assertIn('PROXMOX_HOST', error_msg)
                        self.assertIn('authentication', error_msg.lower())

    def test_koth_defaults_present(self):
        from app.config import (
            KOTH_DEFAULT_SCAN_INTERVAL_SEC,
            KOTH_DEFAULT_CLAIM_COOLDOWN_SEC,
            KOTH_DEFAULT_BOX_OWNERSHIP_SCORE,
            KOTH_DEFAULT_PORT_OPEN_SCORE,
            KOTH_DEFAULT_EXP_SCORING,
            KOTH_DEFAULT_EXP_VAL,
            KOTH_TEAM_COLORS,
            get_koth_defaults,
        )
        self.assertEqual(KOTH_DEFAULT_SCAN_INTERVAL_SEC, 12)
        self.assertEqual(KOTH_DEFAULT_CLAIM_COOLDOWN_SEC, 30)
        self.assertEqual(KOTH_DEFAULT_BOX_OWNERSHIP_SCORE, 1)
        self.assertEqual(KOTH_DEFAULT_PORT_OPEN_SCORE, 3)
        self.assertTrue(KOTH_DEFAULT_EXP_SCORING)
        self.assertEqual(KOTH_DEFAULT_EXP_VAL, 60)
        self.assertIn("red", KOTH_TEAM_COLORS)
        self.assertIn("blue", KOTH_TEAM_COLORS)
        d = get_koth_defaults()
        self.assertEqual(d["scan_interval_sec"], 12)


# ============ VM backend selection ============

# Every key whose value decides which backends run and what they need. A
# key is set to '' rather than deleted so an operator's .env cannot refill
# it when the reloaded module calls load_dotenv().
BACKEND_ENV_KEYS = (
    'ENABLED_BACKENDS', 'LAB_SITE_ID', 'VLAN_BANDS',
    # Removed settings, blanked so a stale .env value cannot add a warning.
    'JUMPBOX_VLAN_BANDS', 'GROUP_ENTRY_VLAN_MIN', 'GROUP_ENTRY_VLAN_MAX',
    'GROUP_INTERNAL_VLAN_MIN', 'GROUP_INTERNAL_VLAN_MAX',
    'PROXMOX_HOST', 'PROXMOX_USER', 'PROXMOX_PASSWORD', 'PROXMOX_TOKEN_ID',
    'PROXMOX_TOKEN_SECRET', 'PROXMOX_VERIFY_SSL', 'PROXMOX_CA_BUNDLE',
    'ENABLE_LOAD_BALANCING', 'FIXED_NODE', 'LAB_BRIDGE', 'VMID_RANGE',
    'AWS_REGION', 'AWS_AVAILABILITY_ZONE', 'AWS_LAB_VPC_ID', 'AWS_LAB_CIDR',
    'AWS_ROUTER_INSTANCE_ID', 'AWS_MAX_VMS', 'AWS_ROUTER_IDLE_MINUTES',
    'AWS_CLIENT_CIDRS', 'VPN_MODE', 'VPN_NAME', 'VPN_INSTRUCTIONS_URL',
    'VPN_INSTRUCTIONS',
    'AWS_LAUNCH_TIMEOUT_SECONDS', 'AWS_TERMINATE_TIMEOUT_SECONDS',
    'GCP_PROJECT', 'GCP_REGION', 'GCP_ZONE', 'GCP_NETWORK', 'GCP_LAB_CIDR',
    'GCP_ROUTER_INSTANCE', 'GCP_MAX_VMS', 'GCP_ROUTER_IDLE_MINUTES',
    'GCP_OPERATION_TIMEOUT_SECONDS',
)

AWS_VALUES = {
    'LAB_SITE_ID': 'site-a', 'AWS_REGION': 'us-test-1',
    'AWS_AVAILABILITY_ZONE': 'us-test-1a', 'AWS_LAB_VPC_ID': 'vpc-placeholder',
    'AWS_ROUTER_INSTANCE_ID': 'i-placeholder', 'AWS_MAX_VMS': '3',
}
GCP_VALUES = {
    'GCP_PROJECT': 'project-placeholder', 'GCP_REGION': 'region-test1',
    'GCP_ZONE': 'region-test1-a', 'GCP_NETWORK': 'network-placeholder',
    'GCP_ROUTER_INSTANCE': 'router-placeholder', 'GCP_MAX_VMS': '2',
}


@pytest.fixture(autouse=True)
def _restore_reloaded_config():
    """Reload app.config after each test, so a module reloaded under
    patched env does not carry that state into later test files.

    The conftest autouse fixtures request monkeypatch before this one is
    set up, so monkeypatch is torn down after it and cannot be relied on
    to have restored the environment. Restore a snapshot of os.environ
    here, then reload against it."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    importlib.reload(sys.modules['app.config'])


def reload_config(monkeypatch, **overrides):
    # Unset FLASK_ENV means production, whose secret checks are not under
    # test here; a test that wants production says so in overrides.
    monkeypatch.setenv('FLASK_ENV', 'development')
    for key in BACKEND_ENV_KEYS:
        monkeypatch.setenv(key, '')
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(sys.modules['app.config'])


def reload_valid_aws_config(monkeypatch, **overrides):
    return reload_config(monkeypatch, **{
        'ENABLED_BACKENDS': 'aws', **AWS_VALUES, **overrides})


def reload_valid_cloud_config(monkeypatch, **overrides):
    return reload_config(monkeypatch, **{
        'ENABLED_BACKENDS': 'aws,gcp', **AWS_VALUES, **GCP_VALUES, **overrides})


def _netgen():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scripts" / "jumpbox_netgen.py"
    spec = importlib.util.spec_from_file_location("jumpbox_netgen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Catches the contract's defaults drifting, or an unset ENABLED_BACKENDS
# no longer meaning the Proxmox-only portal every existing site runs.
def test_backend_defaults_match_the_contract(monkeypatch):
    cfg = reload_config(monkeypatch)
    assert cfg.ENABLED_BACKENDS == ('proxmox',)
    assert cfg.LAB_SITE_ID == ''
    assert (cfg.AWS_LAB_CIDR, cfg.GCP_LAB_CIDR) == ('172.20.0.0/16', '172.21.0.0/16')
    assert (cfg.AWS_MAX_VMS, cfg.GCP_MAX_VMS) == (None, None)
    assert (cfg.AWS_ROUTER_IDLE_MINUTES, cfg.GCP_ROUTER_IDLE_MINUTES) == (30, 30)
    assert (cfg.AWS_LAUNCH_TIMEOUT_SECONDS, cfg.AWS_TERMINATE_TIMEOUT_SECONDS,
            cfg.GCP_OPERATION_TIMEOUT_SECONDS) == (600, 600, 600)
    assert cfg.VLAN_BANDS.split() == _netgen().DEFAULT_BANDS.split() == [
        "entry:100-199:0:0", "internal:200-299:100:1",
        "group-entry:300-399:200:2", "group-internal:400-500:300:4"]


# Catches a list that keeps case, whitespace or duplicates, which every
# `name in ENABLED_BACKENDS` check downstream would then mis-answer.
def test_enabled_backends_are_normalized_in_declaration_order(monkeypatch):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS=' GCP, proxmox ,gcp,AWS ')
    assert cfg.ENABLED_BACKENDS == ('gcp', 'proxmox', 'aws')
    assert cfg.AWS_MAX_VMS is None


# Catches cloud-only startup still requiring an unrelated Proxmox cluster.
def test_cloud_only_config_does_not_require_proxmox(monkeypatch):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS='aws', LAB_SITE_ID='site-a',
        AWS_REGION='us-test-1', AWS_AVAILABILITY_ZONE='us-test-1a',
        AWS_LAB_VPC_ID='vpc-placeholder', AWS_ROUTER_INSTANCE_ID='i-placeholder',
        AWS_MAX_VMS='3', PROXMOX_HOST='', PROXMOX_PASSWORD='')
    cfg.validate_config()
    assert cfg.AWS_MAX_VMS == 3


# Catches accidentally enabling an unimplemented or misspelled provider.
@pytest.mark.parametrize('value', ['azure', 'proxmox,azure', ','])
def test_unknown_or_empty_enabled_backend_is_rejected(monkeypatch, value):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS=value,
                        FLASK_ENV='development')
    with pytest.raises(ValueError, match='ENABLED_BACKENDS'):
        cfg.validate_config()


# Catches an ownership tag that cloud naming APIs reject.
@pytest.mark.parametrize('site', ['', '-site', 'site-', 'UPPER', 'x' * 21])
def test_cloud_site_id_is_validated(monkeypatch, site):
    cfg = reload_valid_aws_config(monkeypatch, LAB_SITE_ID=site)
    with pytest.raises(ValueError, match='LAB_SITE_ID'):
        cfg.validate_config()


# Catches an enabled cloud starting without a setting it needs to place a
# VM, or with a cap that admits nothing (or everything). The routers are
# optional (router-less sites); see the tests below.
@pytest.mark.parametrize('key,value', [
    *[(key, '') for key in AWS_VALUES
      if key not in ('LAB_SITE_ID', 'AWS_ROUTER_INSTANCE_ID')],
    *[(key, '') for key in GCP_VALUES if key != 'GCP_ROUTER_INSTANCE'],
    ('AWS_MAX_VMS', '0'), ('AWS_MAX_VMS', '-1'), ('AWS_MAX_VMS', 'three'),
    ('GCP_MAX_VMS', '0'),
])
def test_enabled_cloud_requires_each_setting(monkeypatch, key, value):
    cfg = reload_valid_cloud_config(monkeypatch, **{key: value})
    with pytest.raises(ValueError, match=key):
        cfg.validate_config()


# Catches cross-backend routing collisions and slot 500 escaping the base range.
@pytest.mark.parametrize('aws_cidr,gcp_cidr,message', [
    ('10.20.0.0/16', '172.21.0.0/16', '10.0.0.0/8'),
    ('172.17.0.0/16', '172.21.0.0/16', '172.17.0.0/16'),
    ('172.20.0.0/24', '172.21.0.0/16', 'slot 500'),
    ('172.20.0.0/16', '172.20.128.0/17', 'overlap'),
])
def test_lab_cidrs_fail_closed(monkeypatch, aws_cidr, gcp_cidr, message):
    cfg = reload_valid_cloud_config(
        monkeypatch, AWS_LAB_CIDR=aws_cidr, GCP_LAB_CIDR=gcp_cidr)
    with pytest.raises(ValueError, match=message):
        cfg.validate_config()


# Catches the tailnet range, a malformed value, an IPv6 network or a base
# with host bits set reaching the slot arithmetic of either cloud.
@pytest.mark.parametrize('cidr,message', [
    ('100.64.0.0/16', 'overlaps reserved range 100.64.0.0/10'),
    ('100.0.0.0/8', 'overlaps reserved range 100.64.0.0/10'),
    ('not-a-cidr', 'is not an IPv4 network address/prefix'),
    ('172.20.0.0/33', 'is not an IPv4 network address/prefix'),
    ('fd00:20::/48', 'is not an IPv4 network address/prefix'),
    # Outside both clouds' bases, so only the host bits can refuse it.
    ('172.22.1.0/16', 'is not an IPv4 network address/prefix'),
], ids=['inside-cgnat', 'contains-cgnat', 'malformed', 'bad-prefix', 'ipv6', 'host-bits'])
@pytest.mark.parametrize('key', ['AWS_LAB_CIDR', 'GCP_LAB_CIDR'])
def test_lab_cidr_rejects_cgnat_malformed_ipv6_and_host_bits(monkeypatch, key, cidr, message):
    cfg = reload_valid_cloud_config(monkeypatch, **{key: cidr})
    with pytest.raises(ValueError) as exc:
        cfg.validate_config()
    assert f'{key} ' in str(exc.value) and message in str(exc.value)


# Catches validate_config() drifting from the shared validator's exact peer message.
def test_lab_cidr_peer_overlap_message_is_the_shared_validators(monkeypatch):
    cfg = reload_valid_cloud_config(
        monkeypatch, AWS_LAB_CIDR='172.20.0.0/16', GCP_LAB_CIDR='172.20.128.0/17')
    with pytest.raises(ValueError) as exc:
        cfg.validate_config()
    assert 'AWS_LAB_CIDR 172.20.0.0/16 overlaps gcp lab CIDR 172.20.128.0/17' in str(exc.value)


# Catches the boundary drifting: a /17 holds slot 500 (32,064 addresses).
def test_smallest_base_that_holds_slot_500_is_accepted(monkeypatch):
    cfg = reload_valid_cloud_config(
        monkeypatch, AWS_LAB_CIDR='172.20.0.0/17', GCP_LAB_CIDR='172.20.128.0/17')
    cfg.validate_config()


# Catches cloud admission reservations silently consuming a Proxmox node's capacity.
@pytest.mark.parametrize('node', ['aws', 'GCP'])
def test_proxmox_fixed_node_named_like_a_backend_is_rejected(monkeypatch, node):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS='proxmox', FIXED_NODE=node,
                        FLASK_ENV='development')
    with pytest.raises(ValueError, match='FIXED_NODE'):
        cfg.validate_config()


# ============ VPN mode, client networks, optional routers ============

# Catches the defaults drifting from today's behaviour: the tailnet and a
# Headscale dashboard.
def test_vpn_defaults_keep_the_tailnet(monkeypatch):
    cfg = reload_valid_aws_config(monkeypatch)
    cfg.validate_config()
    assert cfg.VPN_MODE == 'headscale'
    assert cfg.VPN_NAME == 'the course VPN'
    assert cfg.AWS_CLIENT_CIDRS == ''
    assert cfg.aws_client_cidrs() == ('100.64.0.0/10',)
    assert cfg.Config.VPN_MODE == 'headscale'


# Catches a router-less site (AWS Client VPN, site-to-site) refused at startup.
@pytest.mark.parametrize('reload', [reload_valid_aws_config, reload_valid_cloud_config])
def test_routers_are_optional(monkeypatch, reload):
    cfg = reload(monkeypatch, AWS_ROUTER_INSTANCE_ID='', GCP_ROUTER_INSTANCE='')
    cfg.validate_config()
    assert (cfg.AWS_ROUTER_INSTANCE_ID, cfg.GCP_ROUTER_INSTANCE) == ('', '')


# Catches a typo or an instruction-less external VPN reaching students, and
# a link that is not plainly http(s).
@pytest.mark.parametrize('overrides,message', [
    ({'VPN_MODE': 'wireguard'}, 'VPN_MODE must be one of headscale, external, none'),
    ({'VPN_MODE': 'external'}, 'external needs VPN_INSTRUCTIONS_URL or VPN_INSTRUCTIONS'),
    ({'VPN_MODE': 'external', 'VPN_INSTRUCTIONS_URL': 'javascript:alert(1)'},
     'VPN_INSTRUCTIONS_URL must be an http:// or https:// URL'),
    ({'VPN_MODE': 'external', 'VPN_INSTRUCTIONS_URL': 'https://'},
     'VPN_INSTRUCTIONS_URL must be an http:// or https:// URL'),
    ({'VPN_MODE': 'external', 'VPN_INSTRUCTIONS_URL': 'https://user@evil.example/x'},
     'VPN_INSTRUCTIONS_URL must be an http:// or https:// URL'),
])
def test_vpn_settings_are_validated(monkeypatch, overrides, message):
    cfg = reload_config(monkeypatch, PROXMOX_HOST='fake-host',
                        PROXMOX_PASSWORD='fake-password', **overrides)
    with pytest.raises(ValueError) as exc:
        cfg.validate_config()
    assert message in str(exc.value)
    assert 'javascript' not in str(exc.value) and 'evil' not in str(exc.value)


# Catches a valid external or internal-network site being refused.
@pytest.mark.parametrize('overrides', [
    {'VPN_MODE': 'external', 'VPN_INSTRUCTIONS_URL': 'https://vpn.example.edu/guide'},
    {'VPN_MODE': 'EXTERNAL', 'VPN_INSTRUCTIONS': 'Use the campus VPN client.'},
    {'VPN_MODE': 'none'},
])
def test_valid_vpn_modes_start(monkeypatch, overrides):
    cfg = reload_config(monkeypatch, PROXMOX_HOST='fake-host',
                        PROXMOX_PASSWORD='fake-password', **overrides)
    cfg.validate_config()
    assert cfg.VPN_MODE == overrides['VPN_MODE'].lower()


# Catches an external or internal-network site silently admitting the tailnet
# (a range nobody uses there) because AWS_CLIENT_CIDRS was left unset.
@pytest.mark.parametrize('mode', ['external', 'none'])
def test_aws_client_cidrs_are_required_outside_headscale(monkeypatch, mode):
    cfg = reload_valid_aws_config(monkeypatch, VPN_MODE=mode,
                                  VPN_INSTRUCTIONS='Connect first.')
    with pytest.raises(ValueError,
                       match=f'AWS_CLIENT_CIDRS is required when VPN_MODE={mode}'):
        cfg.validate_config()
    with pytest.raises(ValueError):
        cfg.aws_client_cidrs()
    cfg = reload_valid_aws_config(monkeypatch, VPN_MODE=mode,
                                  VPN_INSTRUCTIONS='Connect first.',
                                  AWS_CLIENT_CIDRS='192.0.2.0/24, 10.8.0.0/16',
                                  AWS_ROUTER_INSTANCE_ID='')
    cfg.validate_config()
    assert cfg.aws_client_cidrs() == ('192.0.2.0/24', '10.8.0.0/16')


# Catches an allow-everyone, too-broad, malformed or self-overlapping client
# list reaching every deployment's security group.
@pytest.mark.parametrize('cidrs,message', [
    ('0.0.0.0/0', 'entry 1 admits every IPv4 address'),
    ('192.0.2.0/24,0.0.0.0/0', 'entry 2 admits every IPv4 address'),
    ('64.0.0.0/2', 'broader than a /8'),
    ('::/0', 'not an IPv4 network'),
    ('192.0.2.1/24', 'host bits'),
    ('192.0.2.0/24,192.0.2.0/25', 'overlap'),
    (','.join(f'192.0.{i}.0/24' for i in range(9)), 'at most 8'),
])
def test_invalid_aws_client_cidrs_are_refused(monkeypatch, cidrs, message):
    cfg = reload_valid_aws_config(monkeypatch, AWS_CLIENT_CIDRS=cidrs)
    with pytest.raises(ValueError) as exc:
        cfg.validate_config()
    assert 'AWS_CLIENT_CIDRS' in str(exc.value) and message in str(exc.value)


# Catches a lab base that overlaps the networks its own labs admit.
def test_aws_client_cidrs_overlapping_the_lab_cidr_are_refused(monkeypatch):
    cfg = reload_valid_aws_config(monkeypatch, AWS_CLIENT_CIDRS='172.20.128.0/24')
    with pytest.raises(ValueError, match='AWS_LAB_CIDR 172.20.0.0/16 overlaps '
                                         'AWS_CLIENT_CIDRS network 172.20.128.0/24'):
        cfg.validate_config()


# Catches the tailnet range staying off-limits for a site without a tailnet,
# or released for a site that has one.
def test_tailnet_range_is_reserved_only_under_headscale(monkeypatch):
    cloud = dict(AWS_LAB_CIDR='100.64.0.0/16', GCP_LAB_CIDR='100.65.0.0/16',
                 AWS_CLIENT_CIDRS='192.0.2.0/24')
    cfg = reload_valid_cloud_config(monkeypatch, **cloud)
    with pytest.raises(ValueError, match='overlaps reserved range 100.64.0.0/10'):
        cfg.validate_config()
    cfg = reload_valid_cloud_config(monkeypatch, VPN_MODE='none', **cloud)
    cfg.validate_config()


# Catches a staging-only setting changing what production does when unset.
def test_lab_bridge_and_vmid_range_defaults_are_todays_behaviour(monkeypatch):
    cfg = reload_config(monkeypatch)
    assert (cfg.LAB_BRIDGE, cfg.VMID_RANGE, cfg.VMID_RANGE_ERROR) == ('vmbr0', None, None)


@pytest.mark.parametrize('text, parsed', [('', None), ('4000-4099', (4000, 4099)), (' 100-100 ', (100, 100))])
def test_parse_vmid_range_accepts_min_dash_max(monkeypatch, text, parsed):
    assert reload_config(monkeypatch).parse_vmid_range(text) == parsed


@pytest.mark.parametrize('text', ['4000', '4099-4000', '99-200', '4000-1000000000', 'a-b', '4000-4099,4100-4199'])
def test_parse_vmid_range_rejects_anything_else(monkeypatch, text):
    with pytest.raises(ValueError):
        reload_config(monkeypatch).parse_vmid_range(text)


def test_invalid_vmid_range_is_refused_at_startup(monkeypatch):
    cfg = reload_config(monkeypatch, VMID_RANGE='4099-4000')
    assert cfg.VMID_RANGE is None and 'VMID_RANGE' in cfg.VMID_RANGE_ERROR
    with pytest.raises(ValueError, match='VMID_RANGE'):
        cfg.validate_config()


# Catches a bridge value that injects options into a Proxmox net string.
@pytest.mark.parametrize('bridge', ['vmbrtest,firewall=0', 'vmbr 7', '7vmbr', 'a' * 16])
def test_invalid_lab_bridge_is_refused_at_startup(monkeypatch, bridge):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS='proxmox', LAB_BRIDGE=bridge)
    with pytest.raises(ValueError, match='LAB_BRIDGE'):
        cfg.validate_config()


if __name__ == '__main__':
    unittest.main()
