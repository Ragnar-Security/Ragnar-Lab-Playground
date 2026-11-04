"""Backend registry: one VMBackend instance per backend name per process.

Factories are zero-argument callables. Production defaults must be lazy
functions that import their backend module inside the body, so importing this
package never imports a provider module or, through it, app.config — that
cycle bites as soon as app.config itself imports from app.backends.
"""

import importlib
import threading
from typing import Callable, Dict, Tuple

from app.backends.base import (
    BACKEND_PROXMOX, KNOWN_BACKENDS, BackendNotEnabled, VMBackend,
)

_DEFAULT_FACTORIES: Dict[str, Callable[[], VMBackend]] = {}

_backend_factories: Dict[str, Callable[[], VMBackend]] = dict(_DEFAULT_FACTORIES)
_backend_instances: Dict[str, VMBackend] = {}
# Concurrent first requests must not each construct a client wrapper.
# Reentrant so a factory that resolves another backend cannot self-deadlock.
_registry_lock = threading.RLock()


def enabled_backends() -> Tuple[str, ...]:
    # Read at call time so a patched or reloaded config is what dispatches.
    # Not `from app import config`: the package re-exports the config-class
    # mapping under that name, shadowing the module.
    return tuple(importlib.import_module('app.config').ENABLED_BACKENDS)


def get_backend(name: str) -> VMBackend:
    if name not in KNOWN_BACKENDS:
        raise BackendNotEnabled(f'unknown backend {name!r}')
    if name not in enabled_backends():
        raise BackendNotEnabled(f'backend {name!r} is not in ENABLED_BACKENDS')
    with _registry_lock:
        backend = _backend_instances.get(name)
        if backend is None:
            factory = _backend_factories.get(name)
            if factory is None:
                raise BackendNotEnabled(f'backend {name!r} has no implementation')
            backend = factory()
            _backend_instances[name] = backend
        return backend


def backend_for(ref_or_record) -> VMBackend:
    # Rows written before the backend column existed are Proxmox guests.
    return get_backend(getattr(ref_or_record, 'backend', None) or BACKEND_PROXMOX)


def register_backend_factory(name: str, factory: Callable[[], VMBackend]) -> None:
    with _registry_lock:
        _backend_factories[name] = factory
        # A cached instance from the previous factory would shadow this one.
        _backend_instances.pop(name, None)


def reset_backends_for_tests() -> None:
    with _registry_lock:
        _backend_instances.clear()
        _backend_factories.clear()
        _backend_factories.update(_DEFAULT_FACTORIES)
