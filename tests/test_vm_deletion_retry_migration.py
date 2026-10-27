"""Behavioral test for the VM deletion-retry schema migration."""

import importlib.util
from pathlib import Path
import re


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class _FakeConnection:
    def __init__(self):
        self.columns = set()
        self.alterations = []

    def execute(self, statement, params=None):
        if params is not None:
            return _ScalarResult(params["column_name"] in self.columns)
        sql = str(statement)
        match = re.search(r"ADD COLUMN\s+(\w+)", sql, re.IGNORECASE)
        assert match, f"unexpected migration statement: {sql}"
        column = match.group(1)
        if column in self.columns:
            raise AssertionError(f"duplicate ALTER for {column}")
        self.columns.add(column)
        self.alterations.append(column)
        return _ScalarResult(0)


class _BeginContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _FakeEngine:
    def __init__(self):
        self.connection = _FakeConnection()

    def begin(self):
        return _BeginContext(self.connection)


def test_vm_deletion_retry_migration_is_idempotent():
    script = Path(__file__).parents[1] / "scripts" / "migrate_vm_deletion_retry.py"
    assert script.exists(), "VM deletion retry schema migration must ship with the model change"

    spec = importlib.util.spec_from_file_location("migrate_vm_deletion_retry", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = _FakeEngine()

    module.run_migration(engine=engine)
    module.run_migration(engine=engine)

    assert engine.connection.alterations == [
        "deletion_last_attempt_at",
        "deletion_alert_sent",
    ]
