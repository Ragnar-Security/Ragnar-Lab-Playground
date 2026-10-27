"""
Template management module
Handles loading and managing VM templates from YAML configuration
Supports both deployment groups (multiple VMs per assignment) and legacy single templates
"""

import yaml
import logging
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
    'group_mode',
}


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

    Warnings currently include:
        - unknown top-level keys on a deployment_group (typo detection)
    """
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

