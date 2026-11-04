"""Which backend a deployment group runs on, and that backend's machine spec.

A machine's `backend` overrides its group's, which defaults to Proxmox, but
one deployment is one provider: every machine in a group must resolve to the
same backend. Existing flat Proxmox templates are the Proxmox spec unchanged;
a nested `proxmox:` mapping overlays those top-level keys. Cloud machines
carry their spec in an `aws:` or `gcp:` mapping.

Everything here is side-effect free. resolve_group_backend() and
backend_block() raise TemplateConfigError for a selection a caller cannot
use; validate_group_backends() collects every problem for the admin instead.
"""

from typing import Any, List, Mapping, Sequence

from app.backends.base import BACKEND_AWS, BACKEND_GCP, BACKEND_PROXMOX, KNOWN_BACKENDS
from app.template_manager import TemplateConfigError

CLOUD_REQUIRED_KEYS = {
    BACKEND_AWS: ('image_id', 'instance_type'),
    BACKEND_GCP: ('image', 'machine_type'),
}
CLOUD_DEFAULT_DISK_GB = 20


def _group_name(group: Mapping) -> str:
    return group.get('display_name', '<unnamed>')


def _templates(group: Mapping) -> list:
    return list(group.get('templates') or [])


def _machine_backends(group: Mapping) -> List[Any]:
    """Each machine's backend, machine override first, in template order."""
    group_backend = group.get('backend') or BACKEND_PROXMOX
    return [template.get('backend') or group_backend for template in _templates(group)]


def _distinct(values: Sequence[Any]) -> List[Any]:
    # Not a set: a malformed (unhashable) YAML value must still be reported.
    distinct = []
    for value in values:
        if value not in distinct:
            distinct.append(value)
    return distinct


def _unknown_backend_message(backend: Any) -> str:
    return f"unknown backend {backend!r} (known: {', '.join(KNOWN_BACKENDS)})"


def _mixed_message(backends: Sequence[Any]) -> str:
    return (f"mixed backends ({', '.join(map(str, backends))}); every machine "
            "in a deployment group must use the same backend")


def resolve_group_backend(group: Mapping) -> str:
    name = _group_name(group)
    group_backend = group.get('backend') or BACKEND_PROXMOX
    backends = _distinct(_machine_backends(group)) or [group_backend]
    if len(backends) > 1:
        raise TemplateConfigError(f"deployment_group {name!r}: {_mixed_message(backends)}")
    backend = backends[0]
    if backend not in KNOWN_BACKENDS:
        raise TemplateConfigError(f"deployment_group {name!r}: {_unknown_backend_message(backend)}")
    return backend


def backend_block(template: Mapping, backend: str) -> Mapping:
    name = template.get('name', '<unnamed>')
    if backend == BACKEND_PROXMOX:
        nested = template.get(BACKEND_PROXMOX)
        if nested is None:
            return dict(template)
        if not isinstance(nested, Mapping):
            raise TemplateConfigError(f"machine {name!r}: the 'proxmox' block must be a mapping")
        resolved = {key: value for key, value in template.items() if key != BACKEND_PROXMOX}
        resolved.update(nested)
        return resolved

    required = CLOUD_REQUIRED_KEYS.get(backend)
    if required is None:
        raise TemplateConfigError(f"machine {name!r}: {_unknown_backend_message(backend)}")
    block = template.get(backend)
    if not isinstance(block, Mapping):
        raise TemplateConfigError(
            f"machine {name!r}: backend {backend!r} requires its {backend!r} block "
            f"with {' and '.join(required)}")
    missing = [key for key in required if not block.get(key)]
    if missing:
        raise TemplateConfigError(
            f"machine {name!r}: the {backend!r} block is missing {', '.join(missing)}")
    resolved = dict(block)
    resolved.setdefault('disk_gb', CLOUD_DEFAULT_DISK_GB)
    disk_gb = resolved['disk_gb']
    # type() rather than isinstance(): YAML `true` is a bool, which is an int.
    if type(disk_gb) is not int or disk_gb <= 0:
        raise TemplateConfigError(
            f"machine {name!r}: {backend!r} disk_gb must be a positive integer, got {disk_gb!r}")
    return resolved


def validate_group_backends(group: Mapping, enabled: Sequence[str]) -> List[str]:
    prefix = f"deployment_group {_group_name(group)!r}: "
    errors = []
    machine_backends = _machine_backends(group)
    backends = _distinct(machine_backends) or [group.get('backend') or BACKEND_PROXMOX]

    unknown = [backend for backend in backends if backend not in KNOWN_BACKENDS]
    known = [backend for backend in backends if backend in KNOWN_BACKENDS]
    errors.extend(prefix + _unknown_backend_message(backend) for backend in unknown)
    if len(backends) > 1:
        errors.append(prefix + _mixed_message(backends))
    for backend in known:
        if backend not in enabled:
            errors.append(prefix + f"backend {backend!r} is not enabled "
                          f"(ENABLED_BACKENDS: {', '.join(enabled) or 'none'})")
    cloud = [backend for backend in known if backend != BACKEND_PROXMOX]
    if group.get('koth_mode') and cloud:
        errors.append(prefix + f"koth_mode requires the proxmox backend, not "
                      f"{', '.join(cloud)}")

    for template, backend in zip(_templates(group), machine_backends):
        if backend not in KNOWN_BACKENDS:
            continue
        try:
            backend_block(template, backend)
        except TemplateConfigError as exc:
            errors.append(prefix + str(exc))
    return errors
