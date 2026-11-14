"""Import-order guarantees of the GCP backend, each in a clean interpreter."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(program: str):
    return subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def test_gcp_backend_imports_after_gevent_patch_without_grpc():
    # Catches GCPBackend pulling in a gRPC client or an import that breaks
    # under run_prod.py's gevent order.
    result = _run('''
from gevent import monkey
monkey.patch_all()
from app.backends.gcp import GCPBackend
from google.auth.transport.requests import AuthorizedSession
import requests, sys
assert issubclass(AuthorizedSession, requests.Session)
assert 'grpc' not in sys.modules
''')
    assert result.returncode == 0, result.stderr


def test_importing_backends_does_not_import_gcp_or_google_auth():
    # Catches app.backends eagerly importing gcp.py (ruling I8's import
    # cycle) or the registry discovering credentials at import.
    result = _run('''
import sys
import app.backends
assert 'app.backends.gcp' not in sys.modules, 'gcp.py imported eagerly'
assert 'google.auth' not in sys.modules, 'google.auth imported eagerly'
''')
    assert result.returncode == 0, result.stderr


def test_importing_gcp_module_does_not_import_google_auth():
    # Catches the module discovering credentials or importing google.auth at
    # import time, before any backend is constructed.
    result = _run('''
import sys
import app.backends.gcp
assert 'google.auth' not in sys.modules, 'google.auth imported by gcp.py'
''')
    assert result.returncode == 0, result.stderr
