"""Behavioral test for the VM backend-identity schema migration."""

import importlib.util
from pathlib import Path
import re


SCRIPTS = Path(__file__).parents[1] / "scripts"


def load_migration(filename):
    path = SCRIPTS / filename
    assert path.exists(), f"{filename} must ship with the VMRecord backend columns"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class FakeInformationSchemaConnection:
    """Answers information_schema probes and records DDL, refusing repeats."""

    def __init__(self):
        self.columns = set()
        self.indexes = set()
        self.column_ddl = {}
        self.ddl_count = 0

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if "information_schema.COLUMNS" in sql:
            assert params["table_name"] == "vm_records", params
            return _ScalarResult(int(params["column_name"] in self.columns))
        if "information_schema.STATISTICS" in sql:
            assert params["table_name"] == "vm_records", params
            return _ScalarResult(int(params["index_name"] in self.indexes))

        add_column = re.fullmatch(
            r"ALTER TABLE vm_records ADD COLUMN (\w+) (.+)", sql, re.IGNORECASE)
        if add_column:
            column, ddl = add_column.groups()
            if column in self.columns:
                raise AssertionError(f"duplicate ALTER for {column}")
            self.columns.add(column)
            self.column_ddl[column] = ddl
            self.ddl_count += 1
            return _ScalarResult(0)

        create_index = re.fullmatch(
            r"CREATE INDEX (\w+) ON vm_records \((\w+)\)", sql, re.IGNORECASE)
        if create_index:
            index, column = create_index.groups()
            if index in self.indexes:
                raise AssertionError(f"duplicate CREATE INDEX for {index}")
            assert column in self.columns, f"index on missing column {column}"
            self.indexes.add(index)
            self.ddl_count += 1
            return _ScalarResult(0)

        raise AssertionError(f"unexpected migration statement: {sql}")


class _BeginContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class FakeInformationSchemaEngine:
    def __init__(self):
        self.connection = FakeInformationSchemaConnection()

    def begin(self):
        return _BeginContext(self.connection)


# Catches a production restart that re-runs ALTER TABLE or CREATE INDEX.
def test_vm_backend_migration_is_idempotent():
    module = load_migration('migrate_add_vm_backend.py')
    engine = FakeInformationSchemaEngine()
    module.run_migration(engine=engine)
    module.run_migration(engine=engine)
    assert engine.connection.columns == {
        'backend', 'provider_id', 'deployment_id', 'network_slot'}
    assert engine.connection.indexes == {'ix_vm_records_deployment_id'}
    assert engine.connection.ddl_count == 5


# Catches a migration that leaves existing rows backend-less or mistypes a column.
def test_vm_backend_migration_column_ddl():
    module = load_migration('migrate_add_vm_backend.py')
    engine = FakeInformationSchemaEngine()
    module.run_migration(engine=engine)
    ddl = engine.connection.column_ddl
    assert ddl['backend'] == "VARCHAR(32) NOT NULL DEFAULT 'proxmox'"
    assert ddl['provider_id'] == "VARCHAR(255) NULL"
    assert ddl['deployment_id'] == "VARCHAR(64) NULL"
    assert ddl['network_slot'] == "INT NULL"
