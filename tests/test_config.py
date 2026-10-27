"""
Unit tests for configuration validation
Tests validate_config() behavior with various configurations
"""

import os
import unittest
import logging
from unittest.mock import patch
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


if __name__ == '__main__':
    unittest.main()
