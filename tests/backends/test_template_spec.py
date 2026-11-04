"""Backend resolution and validation for deployment-group templates."""

import copy

import pytest

from app.backends.template_spec import (
    backend_block, resolve_group_backend, validate_group_backends,
)
from app.template_manager import TemplateConfigError


AWS_BLOCK = {'image_id': 'ami-placeholder', 'instance_type': 'small'}
GCP_BLOCK = {'image': 'projects/example/global/images/test', 'machine_type': 'small'}


def valid_aws_group(**extra):
    group = {'display_name': 'Cloud Lab', 'backend': 'aws',
             'templates': [{'name': 'vm', 'aws': dict(AWS_BLOCK)}]}
    group.update(extra)
    return group


# Catches a machine override silently creating a mixed-provider deployment.
def test_machine_override_must_not_mix_group():
    group = {'backend': 'aws', 'templates': [
        {'name': 'a', 'aws': {'image_id': 'ami-placeholder', 'instance_type': 'small'}},
        {'name': 'b', 'backend': 'gcp',
         'gcp': {'image': 'projects/example/global/images/test', 'machine_type': 'small'}},
    ]}
    with pytest.raises(TemplateConfigError, match='mixed'):
        resolve_group_backend(group)


# Catches the machine-level override being ignored when it agrees for every machine.
def test_machine_override_to_the_same_backend_resolves():
    group = {'templates': [
        {'name': 'a', 'backend': 'gcp', 'gcp': dict(GCP_BLOCK)},
        {'name': 'b', 'backend': 'gcp', 'gcp': dict(GCP_BLOCK)},
    ]}
    assert resolve_group_backend(group) == 'gcp'
    assert validate_group_backends(group, ('proxmox', 'gcp')) == []


# Catches a machine override disagreeing with the group default going unnoticed.
def test_machine_override_against_group_default_is_mixed():
    group = {'backend': 'aws', 'templates': [
        {'name': 'a', 'aws': dict(AWS_BLOCK)},
        {'name': 'b', 'backend': 'proxmox', 'template_locations': {'node-a': 7001}},
    ]}
    with pytest.raises(TemplateConfigError, match='mixed'):
        resolve_group_backend(group)


# Catches existing catalogs (no backend key anywhere) moving off Proxmox.
def test_group_without_backend_key_resolves_to_proxmox():
    group = {'templates': [{'name': 'vm', 'template_locations': {'node-a': 7001}}]}
    assert resolve_group_backend(group) == 'proxmox'
    assert resolve_group_backend({'templates': []}) == 'proxmox'
    assert validate_group_backends(group, ('proxmox',)) == []


# Catches a typo in the backend name falling through to some default provider.
def test_unknown_backend_is_refused():
    group = {'display_name': 'Typo Lab', 'backend': 'azure',
             'templates': [{'name': 'vm'}]}
    with pytest.raises(TemplateConfigError, match='unknown backend'):
        resolve_group_backend(group)
    errors = validate_group_backends(group, ('proxmox',))
    assert any('Typo Lab' in e and "'azure'" in e and 'unknown backend' in e
               for e in errors)


# Catches a group offered to students on a backend the operator never enabled.
def test_disabled_backend_is_reported():
    errors = validate_group_backends(valid_aws_group(), ('proxmox',))
    assert any("'aws'" in e and 'not enabled' in e for e in errors)
    assert validate_group_backends(valid_aws_group(), ('proxmox', 'aws')) == []


# Catches a migration that breaks the existing flat Proxmox catalog.
def test_flat_proxmox_template_is_unchanged():
    template = {'type': 'qemu', 'memory': 2048,
                'template_locations': {'node-placeholder': 7001}}
    assert backend_block(template, 'proxmox') == template


# Catches nested Proxmox values failing to override legacy top-level defaults.
def test_nested_proxmox_block_overlays_top_level_without_mutation():
    template = {'type': 'qemu', 'memory': 2048,
                'proxmox': {'memory': 4096, 'cores': 4}}
    original = copy.deepcopy(template)
    resolved = backend_block(template, 'proxmox')
    assert resolved['type'] == 'qemu'
    assert resolved['memory'] == 4096
    assert resolved['cores'] == 4
    assert template['memory'] == 2048
    assert template == original


# Catches cloud deployment reaching an SDK with an incomplete image spec.
@pytest.mark.parametrize('backend,block,missing', [
    ('aws', {'instance_type': 'small'}, 'image_id'),
    ('aws', {'image_id': 'ami-placeholder'}, 'instance_type'),
    ('gcp', {'image': 'projects/example/global/images/test'}, 'machine_type'),
    ('gcp', {'machine_type': 'small'}, 'image'),
])
def test_cloud_block_requires_pinned_keys(backend, block, missing):
    with pytest.raises(TemplateConfigError, match=missing):
        backend_block({'name': 'vm', backend: block}, backend)


# Catches a cloud machine with no provider block at all being deployable.
@pytest.mark.parametrize('backend', ['aws', 'gcp'])
def test_cloud_block_must_exist(backend):
    with pytest.raises(TemplateConfigError, match=f"'{backend}' block"):
        backend_block({'name': 'vm'}, backend)


# Catches the documented 20 GB disk default missing from the resolved spec.
@pytest.mark.parametrize('backend,block', [('aws', AWS_BLOCK), ('gcp', GCP_BLOCK)])
def test_cloud_disk_defaults_to_20(backend, block):
    template = {'name': 'vm', backend: dict(block)}
    resolved = backend_block(template, backend)
    assert resolved['disk_gb'] == 20
    assert 'disk_gb' not in template[backend]
    explicit = {'name': 'vm', backend: dict(block, disk_gb=50)}
    assert backend_block(explicit, backend)['disk_gb'] == 50


# Catches a non-numeric disk size reaching the SDK.
@pytest.mark.parametrize('disk_gb', [0, -5, 'big', True])
def test_cloud_disk_must_be_a_positive_integer(disk_gb):
    with pytest.raises(TemplateConfigError, match='disk_gb'):
        backend_block({'name': 'vm', 'aws': dict(AWS_BLOCK, disk_gb=disk_gb)}, 'aws')


# Catches missing cloud keys never reaching the admin validation list.
def test_validation_reports_every_missing_cloud_key():
    group = {'display_name': 'Cloud Lab', 'backend': 'gcp', 'templates': [
        {'name': 'a', 'gcp': {'machine_type': 'small'}},
        {'name': 'b'},
    ]}
    errors = validate_group_backends(group, ('proxmox', 'gcp'))
    assert any("'a'" in e and 'image' in e for e in errors)
    assert any("'b'" in e and "'gcp' block" in e for e in errors)
    assert all(e.startswith("deployment_group 'Cloud Lab'") for e in errors)


# Catches a mixed group being reported only by an exception, never as a list error.
def test_validation_reports_mixed_group():
    group = valid_aws_group()
    group['templates'].append({'name': 'b', 'backend': 'gcp', 'gcp': dict(GCP_BLOCK)})
    errors = validate_group_backends(group, ('proxmox', 'aws', 'gcp'))
    assert any('mixed' in e for e in errors)


# Catches KOTH reaching a backend with no guest-agent claim-file semantics.
def test_koth_cloud_group_is_rejected():
    errors = validate_group_backends(valid_aws_group(koth_mode=True), ('proxmox', 'aws'))
    assert any('koth_mode' in error and 'proxmox' in error for error in errors)


# Catches the KOTH rule refusing ordinary Proxmox KOTH groups.
def test_koth_proxmox_group_is_accepted():
    group = {'display_name': 'KOTH', 'koth_mode': True,
             'templates': [{'name': 'vm', 'template_locations': {'node-a': 7001}}]}
    assert validate_group_backends(group, ('proxmox',)) == []
