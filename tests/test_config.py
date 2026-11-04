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


class TestGroupVlanConfig(unittest.TestCase):
    """Tests for the group VLAN pool env vars."""

    def tearDown(self):
        """Restore app.config module state after tests that reload it with
        patched env. Without this, a reloaded module carries its last-loaded
        state (e.g. inverted VLAN ranges) into subsequent test files in the
        same pytest run, corrupting unrelated tests."""
        import sys, importlib
        cfg_module = sys.modules.get('app.config')
        if cfg_module is not None:
            importlib.reload(cfg_module)

    def test_defaults_when_unset(self):
        """When GROUP_*_VLAN_* env vars are unset, defaults must be used."""
        import sys
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('GROUP_') or not k.endswith('_VLAN_MIN')
               and not k.endswith('_VLAN_MAX')}
        with patch.dict(os.environ, env, clear=True):
            # Re-import module so it re-reads env
            # Use sys.modules key to avoid the name collision with the module-level
            # `config = ProductionConfig` variable that shadows `app.config` attribute access.
            import importlib
            cfg_module = sys.modules['app.config']
            importlib.reload(cfg_module)
            self.assertEqual(cfg_module.GROUP_ENTRY_VLAN_MIN, 300)
            self.assertEqual(cfg_module.GROUP_ENTRY_VLAN_MAX, 400)
            self.assertEqual(cfg_module.GROUP_INTERNAL_VLAN_MIN, 400)
            self.assertEqual(cfg_module.GROUP_INTERNAL_VLAN_MAX, 500)

    def test_get_group_vlan_ranges_returns_tuple_pair(self):
        """get_group_vlan_ranges() returns ((entry_min, entry_max), (internal_min, internal_max))."""
        import sys
        cfg = sys.modules['app.config']
        entry, internal = cfg.get_group_vlan_ranges()
        self.assertEqual(entry, (cfg.GROUP_ENTRY_VLAN_MIN, cfg.GROUP_ENTRY_VLAN_MAX))
        self.assertEqual(internal, (cfg.GROUP_INTERNAL_VLAN_MIN, cfg.GROUP_INTERNAL_VLAN_MAX))

    def test_inverted_entry_range_raises(self):
        """When GROUP_ENTRY_VLAN_MIN >= GROUP_ENTRY_VLAN_MAX, validate_config raises."""
        env = dict(os.environ)
        env.update({
            'GROUP_ENTRY_VLAN_MIN': '400',
            'GROUP_ENTRY_VLAN_MAX': '300',
            # Keep Proxmox config satisfied so we reach the group validation block
            'PROXMOX_HOST': 'example.test',
            'PROXMOX_PASSWORD': 'x',
        })
        import sys, importlib
        with patch.dict(os.environ, env, clear=True):
            cfg = sys.modules['app.config']
            importlib.reload(cfg)
            with self.assertRaises(ValueError) as ctx:
                cfg.validate_config()
            self.assertIn('GROUP_ENTRY_VLAN_MAX', str(ctx.exception))

    def test_inverted_internal_range_raises(self):
        """When GROUP_INTERNAL_VLAN_MIN >= GROUP_INTERNAL_VLAN_MAX, validate_config raises."""
        env = dict(os.environ)
        env.update({
            'GROUP_INTERNAL_VLAN_MIN': '500',
            'GROUP_INTERNAL_VLAN_MAX': '400',
            'PROXMOX_HOST': 'example.test',
            'PROXMOX_PASSWORD': 'x',
        })
        import sys, importlib
        with patch.dict(os.environ, env, clear=True):
            cfg = sys.modules['app.config']
            importlib.reload(cfg)
            with self.assertRaises(ValueError) as ctx:
                cfg.validate_config()
            self.assertIn('GROUP_INTERNAL_VLAN_MAX', str(ctx.exception))

    def test_defaults_produce_no_validation_errors(self):
        """Default group ranges are valid and raise nothing."""
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith('GROUP_') and (k.endswith('_VLAN_MIN') or k.endswith('_VLAN_MAX')))}
        env.update({'PROXMOX_HOST': 'example.test', 'PROXMOX_PASSWORD': 'x',
                    'FLASK_ENV': 'development'})
        import sys, importlib
        with patch.dict(os.environ, env, clear=True):
            cfg = sys.modules['app.config']
            importlib.reload(cfg)
            cfg.validate_config()  # must not raise

    def test_entry_vlan_overlap_warns_entry_only(self):
        """A group-entry pool straddling the user entry pool [100,200) emits entry warning only."""
        env = dict(os.environ)
        env.update({
            'GROUP_ENTRY_VLAN_MIN': '150',
            'GROUP_ENTRY_VLAN_MAX': '250',
            'GROUP_INTERNAL_VLAN_MIN': '400',
            'GROUP_INTERNAL_VLAN_MAX': '500',
            'PROXMOX_HOST': 'example.test',
            'PROXMOX_PASSWORD': 'x',
            'FLASK_ENV': 'development',
        })
        import sys, importlib
        with patch.dict(os.environ, env, clear=True):
            cfg = sys.modules['app.config']
            importlib.reload(cfg)
            with self.assertLogs('app.config', level=logging.WARNING) as cm:
                cfg.validate_config()
        self.assertTrue(any('GROUP_ENTRY_VLAN' in m for m in cm.output),
                        f"expected an entry-pool warning; got: {cm.output}")
        self.assertFalse(any('GROUP_INTERNAL_VLAN' in m for m in cm.output),
                         f"unexpected internal-pool warning; got: {cm.output}")

    def test_internal_vlan_overlap_warns_internal_only(self):
        """A group-internal pool straddling the user internal pool [200,300) emits internal warning only."""
        env = dict(os.environ)
        env.update({
            'GROUP_ENTRY_VLAN_MIN': '300',
            'GROUP_ENTRY_VLAN_MAX': '400',
            'GROUP_INTERNAL_VLAN_MIN': '250',
            'GROUP_INTERNAL_VLAN_MAX': '350',
            'PROXMOX_HOST': 'example.test',
            'PROXMOX_PASSWORD': 'x',
            'FLASK_ENV': 'development',
        })
        import sys, importlib
        with patch.dict(os.environ, env, clear=True):
            cfg = sys.modules['app.config']
            importlib.reload(cfg)
            with self.assertLogs('app.config', level=logging.WARNING) as cm:
                cfg.validate_config()
        self.assertFalse(any('GROUP_ENTRY_VLAN' in m for m in cm.output),
                         f"unexpected entry-pool warning; got: {cm.output}")
        self.assertTrue(any('GROUP_INTERNAL_VLAN' in m for m in cm.output),
                        f"expected an internal-pool warning; got: {cm.output}")

    def test_defaults_produce_no_overlap_warnings(self):
        """Default group ranges must emit zero overlap warnings."""
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith('GROUP_') and
                       (k.endswith('_VLAN_MIN') or k.endswith('_VLAN_MAX')))}
        env.update({
            'PROXMOX_HOST': 'example.test',
            'PROXMOX_PASSWORD': 'x',
            'FLASK_ENV': 'development',
            'SECRET_KEY': 'test-secret-key',
        })
        import sys, importlib
        with patch.dict(os.environ, env, clear=True):
            cfg = sys.modules['app.config']
            importlib.reload(cfg)
            with self.assertNoLogs('app.config', level=logging.WARNING):
                cfg.validate_config()


# ============ VM backend selection ============

# Every key whose value decides which backends run and what they need. A
# key is set to '' rather than deleted so an operator's .env cannot refill
# it when the reloaded module calls load_dotenv().
BACKEND_ENV_KEYS = (
    'ENABLED_BACKENDS', 'LAB_SITE_ID', 'JUMPBOX_VLAN_BANDS',
    'PROXMOX_HOST', 'PROXMOX_USER', 'PROXMOX_PASSWORD', 'PROXMOX_TOKEN_ID',
    'PROXMOX_TOKEN_SECRET', 'PROXMOX_VERIFY_SSL', 'PROXMOX_CA_BUNDLE',
    'ENABLE_LOAD_BALANCING', 'FIXED_NODE',
    'AWS_REGION', 'AWS_AVAILABILITY_ZONE', 'AWS_LAB_VPC_ID', 'AWS_LAB_CIDR',
    'AWS_ROUTER_INSTANCE_ID', 'AWS_MAX_VMS', 'AWS_ROUTER_IDLE_MINUTES',
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

    Autouse and independent of monkeypatch, so it is set up before and torn
    down after monkeypatch: the reload sees the restored environment."""
    yield
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
    assert cfg.JUMPBOX_VLAN_BANDS == (
        'entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4')


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
# VM, or with a cap that admits nothing (or everything).
@pytest.mark.parametrize('key,value', [
    *[(key, '') for key in AWS_VALUES if key != 'LAB_SITE_ID'],
    *[(key, '') for key in GCP_VALUES],
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


# Catches the boundary drifting: a /17 holds slot 500 (32,064 addresses).
def test_smallest_base_that_holds_slot_500_is_accepted(monkeypatch):
    cfg = reload_valid_cloud_config(
        monkeypatch, AWS_LAB_CIDR='172.20.0.0/17', GCP_LAB_CIDR='172.20.128.0/17')
    cfg.validate_config()


# Catches cloud admission reservations silently consuming a Proxmox node's capacity (M14).
@pytest.mark.parametrize('node', ['aws', 'GCP'])
def test_proxmox_fixed_node_named_like_a_backend_is_rejected(monkeypatch, node):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS='proxmox', FIXED_NODE=node,
                        FLASK_ENV='development')
    with pytest.raises(ValueError, match='FIXED_NODE'):
        cfg.validate_config()


if __name__ == '__main__':
    unittest.main()
