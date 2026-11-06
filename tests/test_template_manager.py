"""
Unit tests for template_manager module
Tests YAML template loading and management functions
"""

import importlib
import pytest
import unittest
import os
import yaml
import tempfile
from app import template_manager
from app.template_manager import (
    load_template_config,
    get_enabled_deployment_groups,
    get_deployment_group_by_name,
    get_template_by_name,
    get_enabled_templates,
    get_template_for_vm,
    TemplateConfigError,
)
from app.template_manager import validate_template_config

# The module, not `from app import config`: that name is the config-class mapping.
config = importlib.import_module('app.config')


class TestTemplateManager(unittest.TestCase):
    """Test cases for template manager functions"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        self.original_templates_file = getattr(
            template_manager, 'TEMPLATES_FILE', None
        )
        template_manager.TEMPLATES_FILE = os.path.join(
            self.temp_dir, 'templates.yaml'
        )
        os.chdir(self.temp_dir)
        
        # Create test template configuration
        self.test_config = {
            'deployment_groups': [
                {
                    'display_name': 'homework1',
                    'description': 'Test Homework 1',
                    'enabled': True,
                    'uptime_minutes': 120,
                    'extend_time_minutes': 60,
                    'templates': [
                        {
                            'name': 'webserver',
                            'type': 'lxc',
                            'memory': 2048,
                            'cores': 2,
                            'storage': 20
                        },
                        {
                            'name': 'database',
                            'type': 'lxc',
                            'memory': 4096,
                            'cores': 4,
                            'storage': 40
                        }
                    ]
                },
                {
                    'display_name': 'homework2',
                    'description': 'Test Homework 2',
                    'enabled': False,
                    'uptime_minutes': 180,
                    'extend_time_minutes': 90,
                    'templates': [
                        {
                            'name': 'router',
                            'type': 'qemu',
                            'memory': 1024,
                            'cores': 1,
                            'storage': 10
                        }
                    ]
                }
            ],
            'templates': [
                {
                    'display_name': 'legacy-template',
                    'enabled': True,
                    'type': 'lxc'
                }
            ]
        }
        
        # Write test config to file
        with open('templates.yaml', 'w') as f:
            yaml.dump(self.test_config, f)
    
    def tearDown(self):
        """Clean up test fixtures"""
        os.chdir(self.original_cwd)
        templates_path = os.path.join(self.temp_dir, 'templates.yaml')
        if os.path.exists(templates_path):
            os.remove(templates_path)
        os.rmdir(self.temp_dir)
        template_manager.TEMPLATES_FILE = self.original_templates_file
    
    def test_load_template_config(self):
        """Test loading template configuration from YAML"""
        config = load_template_config()
        
        self.assertIsNotNone(config)
        self.assertIn('deployment_groups', config)
        self.assertIn('templates', config)
        self.assertEqual(len(config['deployment_groups']), 2)
        self.assertEqual(len(config['templates']), 1)
    
    def test_load_template_config_missing_file(self):
        """A missing operator catalog must stop startup-facing consumers clearly."""
        os.remove('templates.yaml')

        with self.assertRaisesRegex(TemplateConfigError, 'templates.yaml'):
            load_template_config()

    def test_load_template_config_empty_file(self):
        """An empty operator catalog must not pass the startup check."""
        with open('templates.yaml', 'w'):
            pass

        with self.assertRaisesRegex(TemplateConfigError, 'empty'):
            load_template_config()
    
    def test_get_enabled_deployment_groups(self):
        """Test filtering enabled deployment groups"""
        config = load_template_config()
        enabled_groups = get_enabled_deployment_groups(config)
        
        self.assertEqual(len(enabled_groups), 1)
        self.assertEqual(enabled_groups[0]['display_name'], 'homework1')
        self.assertTrue(enabled_groups[0]['enabled'])
    
    def test_get_enabled_deployment_groups_empty(self):
        """Test getting enabled groups when none exist"""
        empty_config = {'deployment_groups': []}
        enabled_groups = get_enabled_deployment_groups(empty_config)
        
        self.assertEqual(len(enabled_groups), 0)
    
    def test_get_deployment_group_by_name_found(self):
        """Test finding deployment group by name"""
        config = load_template_config()
        group = get_deployment_group_by_name(config, 'homework1')
        
        self.assertIsNotNone(group)
        self.assertEqual(group['display_name'], 'homework1')
        self.assertEqual(len(group['templates']), 2)
        self.assertEqual(group['uptime_minutes'], 120)
    
    def test_get_deployment_group_by_name_not_found(self):
        """Test finding non-existent deployment group"""
        config = load_template_config()
        group = get_deployment_group_by_name(config, 'nonexistent')
        
        self.assertIsNone(group)
    
    def test_get_deployment_group_templates(self):
        """Test accessing templates within a deployment group"""
        config = load_template_config()
        group = get_deployment_group_by_name(config, 'homework1')
        
        templates = group['templates']
        self.assertEqual(len(templates), 2)
        
        # Check first template
        self.assertEqual(templates[0]['name'], 'webserver')
        self.assertEqual(templates[0]['type'], 'lxc')
        self.assertEqual(templates[0]['memory'], 2048)
        self.assertEqual(templates[0]['cores'], 2)
        
        # Check second template
        self.assertEqual(templates[1]['name'], 'database')
        self.assertEqual(templates[1]['memory'], 4096)
    
    def test_get_template_by_name_legacy(self):
        """Test finding legacy single template by name"""
        config = load_template_config()
        templates = config['templates']
        
        template = get_template_by_name(templates, 'legacy-template')
        
        self.assertIsNotNone(template)
        self.assertEqual(template['display_name'], 'legacy-template')
        self.assertTrue(template['enabled'])
    
    def test_get_enabled_templates_legacy(self):
        """Test filtering enabled legacy templates"""
        templates = [
            {'display_name': 'template1', 'enabled': True},
            {'display_name': 'template2', 'enabled': False},
            {'display_name': 'template3', 'enabled': True}
        ]
        
        enabled = get_enabled_templates(templates)

        self.assertEqual(len(enabled), 2)
        self.assertEqual(enabled[0]['display_name'], 'template1')
        self.assertEqual(enabled[1]['display_name'], 'template3')

    def test_get_koth_groups_filters_by_koth_mode(self):
        from app.template_manager import get_koth_groups
        config = {'deployment_groups': [
            {'display_name': 'Regular', 'enabled': True, 'templates': []},
            {'display_name': 'KOTH', 'enabled': True, 'koth_mode': True, 'templates': []},
        ]}
        out = get_koth_groups(config)
        self.assertEqual([g['display_name'] for g in out], ['KOTH'])

    def test_get_koth_services_default_empty(self):
        from app.template_manager import get_koth_services
        group = {'templates': [{'name': 'a'}]}
        self.assertEqual(get_koth_services(group, 'a'), [])

    def test_get_koth_services_returns_list(self):
        from app.template_manager import get_koth_services
        group = {'templates': [{'name': 'a', 'koth_services': [
            {'name': 'SSH', 'port': 22}]}]}
        self.assertEqual(get_koth_services(group, 'a'),
                         [{'name': 'SSH', 'port': 22}])


class GroupModeTestCase(unittest.TestCase):
    def test_group_mode_defaults_false(self):
        from app.template_manager import get_group_mode
        self.assertFalse(get_group_mode({'display_name': 'x'}))

    def test_group_mode_reads_flag(self):
        from app.template_manager import get_group_mode
        self.assertTrue(get_group_mode({'display_name': 'x', 'group_mode': True}))
        self.assertFalse(get_group_mode({'display_name': 'x', 'group_mode': False}))

    def test_validate_accepts_individual_mode_config(self):
        from app.template_manager import validate_template_config
        cfg = {'deployment_groups': [
            {'display_name': 'HW1', 'enabled': True,
             'templates': [{'name': 'vm', 'entry_point': True}]},
        ]}
        errors, warnings = validate_template_config(cfg)
        self.assertEqual(errors, [])

    def test_validate_rejects_group_mode_without_entry_point(self):
        from app.template_manager import validate_template_config
        cfg = {'deployment_groups': [
            {'display_name': 'Team 1', 'group_mode': True, 'enabled': True,
             'templates': [{'name': 'vm', 'entry_point': False}]},
        ]}
        errors, warnings = validate_template_config(cfg)
        self.assertTrue(any('Team 1' in e and 'entry_point' in e for e in errors))

    def test_validate_warns_on_unknown_top_level_key(self):
        from app.template_manager import validate_template_config
        cfg = {'deployment_groups': [
            {'display_name': 'HW1', 'enabled': True, 'unknown_flag': True,
             'templates': [{'name': 'vm', 'entry_point': True}]},
        ]}
        errors, warnings = validate_template_config(cfg)
        self.assertTrue(any('unknown_flag' in w for w in warnings))


class GetTemplateForVMTestCase(unittest.TestCase):
    """Tests for get_template_for_vm lookup helper."""

    def setUp(self):
        self.config = {
            'deployment_groups': [
                {
                    'display_name': 'homework1',
                    'templates': [
                        {'name': 'webserver', 'mask_ip': True},
                        {'name': 'database'},
                    ],
                },
                {
                    'display_name': 'homework2',
                    'templates': [{'name': 'router'}],
                },
            ]
        }

    def test_finds_template_with_flag(self):
        tpl = get_template_for_vm(self.config, 'homework1/webserver')
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl['name'], 'webserver')
        self.assertTrue(tpl.get('mask_ip'))

    def test_finds_template_without_flag(self):
        tpl = get_template_for_vm(self.config, 'homework1/database')
        self.assertIsNotNone(tpl)
        self.assertFalse(tpl.get('mask_ip', False))

    def test_returns_none_for_unknown_group(self):
        self.assertIsNone(get_template_for_vm(self.config, 'nope/webserver'))

    def test_returns_none_for_unknown_vm(self):
        self.assertIsNone(get_template_for_vm(self.config, 'homework1/missing'))

    def test_returns_none_for_malformed_template_name(self):
        self.assertIsNone(get_template_for_vm(self.config, 'no-slash'))
        self.assertIsNone(get_template_for_vm(self.config, ''))
        self.assertIsNone(get_template_for_vm(self.config, None))

    def test_returns_none_when_group_has_no_templates(self):
        config = {'deployment_groups': [{'display_name': 'empty'}]}
        self.assertIsNone(get_template_for_vm(config, 'empty/anything'))


# Catches a Proxmox node named like a cloud backend sharing its admission reservations (M14).
@pytest.mark.parametrize('group', [
    {'display_name': 'LB Lab', 'load_balancing': {'enabled': False, 'fixed_node': 'AWS'},
     'templates': [{'name': 'vm', 'template_locations': {'node-a': 7001}}]},
    {'display_name': 'LB Lab', 'templates': [
        {'name': 'vm', 'template_locations': {'gcp': 7001}}]},
    {'display_name': 'LB Lab', 'templates': [
        {'name': 'vm', 'proxmox': {'storage_locations': {'aws': 'local-lvm'}}}]},
])
def test_proxmox_node_named_like_a_backend_is_rejected(group, monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    errors, _warnings = validate_template_config({'deployment_groups': [group]})
    assert any("'LB Lab'" in e and 'reserved for the' in e for e in errors)


# Catches the reserved-name error losing the operator's spelling or naming the wrong backend.
def test_reserved_node_name_error_keeps_operator_spelling(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    group = {'display_name': 'LB Lab', 'load_balancing': {'fixed_node': 'AWS'},
             'templates': [{'name': 'vm', 'template_locations': {'Gcp': 7001},
                            'storage_locations': {'Gcp': 'local-lvm'}}]}
    errors, _warnings = validate_template_config({'deployment_groups': [group]})
    reserved = [e for e in errors if 'reserved for the' in e]
    assert reserved == [
        "deployment_group 'LB Lab': Proxmox node name 'AWS' is reserved for the aws backend",
        "deployment_group 'LB Lab': Proxmox node name 'Gcp' is reserved for the gcp backend",
    ]


def test_ordinary_proxmox_node_names_are_not_flagged(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    group = {'display_name': 'LB Lab', 'load_balancing': {'fixed_node': 'node-a'},
             'templates': [{'name': 'vm', 'template_locations': {'node-a': 7001},
                            'storage_locations': {'node-a': 'local-lvm'}}]}
    errors, _warnings = validate_template_config({'deployment_groups': [group]})
    assert not [e for e in errors if 'reserved for the' in e]


# Catches backend errors never reaching validate_template_config, the admin's error list.
def test_validate_template_config_includes_backend_errors(monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    group = {'display_name': 'Cloud Lab', 'backend': 'aws', 'koth_mode': True,
             'templates': [{'name': 'vm'}]}
    errors, warnings = validate_template_config({'deployment_groups': [group]})
    assert any("'Cloud Lab'" in e and 'not enabled' in e for e in errors)
    assert any("'Cloud Lab'" in e and "'aws' block" in e for e in errors)
    assert any("'Cloud Lab'" in e and 'koth_mode' in e for e in errors)
    # backend and koth_mode are schema keys, not typos.
    assert warnings == []


# Catches the enabled set being read once at import instead of at validation time.
def test_validate_template_config_reads_enabled_backends_at_call_time(monkeypatch):
    group = {'display_name': 'Cloud Lab', 'backend': 'gcp', 'templates': [
        {'name': 'vm', 'gcp': {'image': 'projects/example/global/images/test',
                               'machine_type': 'small'}}]}
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox', 'gcp'))
    assert validate_template_config({'deployment_groups': [group]}) == ([], [])
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    errors, _warnings = validate_template_config({'deployment_groups': [group]})
    assert any('not enabled' in e for e in errors)


if __name__ == '__main__':
    unittest.main()
