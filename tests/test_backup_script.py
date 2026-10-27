"""scripts/backup-database.sh must produce a usable MySQL dump or fail loudly.

An earlier version of the script copied a stale SQLite file while production ran
MySQL, so a cron job built from the docs "succeeded" without backing anything
up. These tests run the real script against a fake `docker` on PATH.
"""

import gzip
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup-database.sh"

GOOD_DUMP = "CREATE TABLE users (id int);\n-- Dump completed on 2026-10-23\n"


def _fake_docker(bin_dir: Path, body: str) -> None:
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\n" + body)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, docker_body: str, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_docker(bin_dir, docker_body)
    backup_dir = tmp_path / "backups"
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BACKUP_DIR": str(backup_dir),
        "COMPOSE_DIR": str(tmp_path),
        **env,
    }
    result = subprocess.run(["bash", str(SCRIPT)], env=full_env,
                            capture_output=True, text=True, timeout=30)
    return result, backup_dir


def test_writes_gzipped_mysql_dump(tmp_path):
    result, backup_dir = _run(tmp_path, f"printf '%s' '{GOOD_DUMP}'\n")
    assert result.returncode == 0, result.stderr
    dumps = list(backup_dir.glob("vmdatabase-*.sql.gz"))
    assert len(dumps) == 1
    assert gzip.decompress(dumps[0].read_bytes()).decode() == GOOD_DUMP


def test_failed_dump_leaves_nothing_and_exits_nonzero(tmp_path):
    result, backup_dir = _run(tmp_path, "echo 'Access denied' >&2; exit 2\n")
    assert result.returncode != 0
    assert list(backup_dir.glob("*")) == []


def test_truncated_dump_is_rejected(tmp_path):
    """mysqldump killed mid-stream exits 0 through some wrappers; the
    completion trailer is the only reliable proof the dump is whole."""
    result, backup_dir = _run(tmp_path, "printf 'CREATE TABLE users (id int);\\n'\n")
    assert result.returncode != 0
    assert list(backup_dir.glob("*")) == []


def test_prunes_dumps_older_than_retention(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old = backup_dir / "vmdatabase-20260101-000000.sql.gz"
    recent = backup_dir / "vmdatabase-20261020-000000.sql.gz"
    manual = backup_dir / "vmdatabase-20261004-150359.sql"  # hand-made, never pruned
    for f in (old, recent, manual):
        f.write_bytes(b"x")
    month_ago = time.time() - 30 * 86400
    os.utime(old, (month_ago, month_ago))
    os.utime(manual, (month_ago, month_ago))

    result, _ = _run(tmp_path, f"printf '%s' '{GOOD_DUMP}'\n", RETENTION_DAYS="14")
    assert result.returncode == 0, result.stderr
    assert not old.exists()
    assert recent.exists()
    assert manual.exists()
