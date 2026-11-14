"""
Template management module
Handles loading and managing VM templates from YAML configuration
Supports both deployment groups (multiple VMs per assignment) and legacy single templates
"""

import yaml
import logging
from collections.abc import Mapping
from .backends import enabled_backends
from .backends.base import BACKEND_AWS, BACKEND_GCP
from .config import TEMPLATES_FILE

logger = logging.getLogger(__name__)


class TemplateConfigError(Exception):
    """Raised when the operator-supplied template catalog cannot be used."""


def load_template_config():
    """Load the operator-supplied VM template catalog.

    A missing or unreadable catalog is a deployment error, not an empty site:
    silently returning no groups made a bad bind mount look like a valid app.
    """
    try:
        with open(TEMPLATES_FILE, 'r', encoding='utf-8') as catalog:
            config = yaml.safe_load(catalog)
        if config is None:
            raise TemplateConfigError(
                f"Template catalog {TEMPLATES_FILE} is empty"
            )
        if not isinstance(config, dict):
            raise TemplateConfigError(
                f"Template catalog {TEMPLATES_FILE} must contain a YAML mapping"
            )
        return config
    except (OSError, yaml.YAMLError) as exc:
        message = f"Unable to load template catalog {TEMPLATES_FILE}: {exc}"
        logger.error(message)
        raise TemplateConfigError(message) from exc


def get_enabled_deployment_groups(config):
    """
    Get list of enabled deployment groups
    
    Args:
        config (dict): Full configuration from templates.yaml
        
    Returns:
        list: List of enabled deployment group dictionaries
    """
    deployment_groups = config.get('deployment_groups', [])
    return [g for g in deployment_groups if g.get('enabled', False)]


def get_deployment_group_by_name(config, display_name):
    """
    Find a deployment group by display name

    Args:
        config (dict): Full configuration from templates.yaml
        display_name (str): Display name to search for

    Returns:
        dict: Deployment group configuration or None if not found
    """
    for group in config.get('deployment_groups', []):
        if group.get('display_name') == display_name:
            return group
    return None


def get_template_for_vm(config, template_name):
    """Look up a per-VM template dict by its '<group>/<vm>' template_name.

    template_name follows the format stored on VMRecord, e.g. 'homework1/webserver'.
    Returns None if the format is wrong, the group is missing, or no VM in
    that group has the matching name.
    """
    if not template_name or '/' not in template_name:
        return None
    group_name, vm_name = template_name.split('/', 1)
    group = get_deployment_group_by_name(config, group_name)
    if not group:
        return None
    for tpl in group.get('templates', []) or []:
        if tpl.get('name') == vm_name:
            return tpl
    return None


# Legacy functions for backward compatibility
def get_template_by_name(templates, display_name):
    """
    Find a template by display name (legacy single template format)
    
    Args:
        templates (list): List of template configurations
        display_name (str): Display name to search for
        
    Returns:
        dict: Template configuration or None if not found
    """
    for template in templates:
        if template.get('display_name') == display_name:
            return template
    return None


def get_enabled_templates(templates):
    """
    Filter templates to only enabled ones (legacy single template format)

    Args:
        templates (list): List of template configurations

    Returns:
        list: Filtered list of enabled templates
    """
    return [t for t in templates if t.get('enabled', False)]


# Known top-level keys on a deployment_group. Unknown keys become warnings in
# validate_template_config() so typos don't silently get ignored.
KNOWN_DEPLOYMENT_GROUP_KEYS = {
    'display_name', 'description', 'enabled', 'uptime_minutes',
    'extend_time_minutes', 'load_balancing', 'templates',
    'group_mode', 'backend', 'koth_mode',
}

# Cloud admission reserves capacity under these node names, so a Proxmox node
# spelled the same would share (and exhaust) the cloud backend's reservations.
RESERVED_NODE_NAMES = (BACKEND_AWS, BACKEND_GCP)


def _reserved_node_name_errors(group, name):
    """One error per distinct Proxmox node name in `group` that is a cloud
    backend name: load_balancing.fixed_node and the keys of every machine's
    template_locations/storage_locations, top-level or in a nested proxmox:."""
    candidates = []
    load_balancing = group.get('load_balancing')
    if isinstance(load_balancing, Mapping):
        candidates.append(load_balancing.get('fixed_node'))
    for template in group.get('templates') or []:
        nested = template.get('proxmox')
        sources = [template] + ([nested] if isinstance(nested, Mapping) else [])
        for source in sources:
            for key in ('template_locations', 'storage_locations'):
                locations = source.get(key)
                if isinstance(locations, Mapping):
                    candidates.extend(locations)
    errors = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        backend = value.strip().lower()
        if backend in RESERVED_NODE_NAMES:
            message = (f"deployment_group {name!r}: Proxmox node name {value!r} "
                       f"is reserved for the {backend} backend")
            if message not in errors:
                errors.append(message)
    return errors


def get_group_mode(deployment_group):
    """Return True if this deployment_group should be deployed once per student
    group (shared), False otherwise. Defaults to False (individual mode)."""
    return bool(deployment_group.get('group_mode', False))


def validate_template_config(config):
    """
    Scan a loaded templates.yaml dict for problems.

    Returns (errors, warnings) — both lists of str. Callers decide whether to
    hard-fail on errors. Errors currently include:
        - group_mode: true with no template having entry_point: true
        - backend problems (mixed, unknown, not enabled, koth_mode on cloud,
          incomplete aws/gcp blocks), against ENABLED_BACKENDS read now
        - Proxmox node names that are reserved cloud backend names

    Warnings currently include:
        - unknown top-level keys on a deployment_group (typo detection)
    """
    # Lazy: template_spec imports TemplateConfigError from this module.
    from .backends.template_spec import validate_group_backends
    enabled = enabled_backends()
    errors = []
    warnings = []
    for group in config.get('deployment_groups', []) or []:
        name = group.get('display_name', '<unnamed>')

        unknown = set(group.keys()) - KNOWN_DEPLOYMENT_GROUP_KEYS
        for key in sorted(unknown):
            warnings.append(f"deployment_group {name!r}: unknown top-level key {key!r}")

        if get_group_mode(group):
            templates = group.get('templates') or []
            if not any(t.get('entry_point') for t in templates):
                errors.append(
                    f"deployment_group {name!r}: group_mode=true requires exactly one "
                    "template with entry_point: true"
                )

        errors.extend(validate_group_backends(group, enabled))
        errors.extend(_reserved_node_name_errors(group, name))
    return errors, warnings


def get_koth_groups(config):
    """Return only deployment_groups marked koth_mode: true and enabled."""
    return [g for g in (config.get('deployment_groups') or [])
            if g.get('koth_mode') is True and g.get('enabled', True)]


def get_koth_services(group, template_name):
    """Return koth_services list for the named template, or []."""
    for tmpl in (group.get('templates') or []):
        if tmpl.get('name') == template_name:
            return list(tmpl.get('koth_services') or [])
    return []


# Default path the KOTH claim poller reads on each box. Linux templates
# inherit this; Windows templates override via templates.yaml.
KOTH_DEFAULT_CLAIM_FILE = '/koth/claim'


def get_koth_claim_file(group, template_name):
    """Return the per-template KOTH claim-file path.

    Falls back to `/koth/claim` when the template doesn't set its own.
    Admins set `koth_claim_file: 'C:\\koth\\claim'` on Windows templates.
    """
    for tmpl in (group.get('templates') or []):
        if tmpl.get('name') == template_name:
            override = tmpl.get('koth_claim_file')
            if override:
                return str(override)
            break
    return KOTH_DEFAULT_CLAIM_FILE

