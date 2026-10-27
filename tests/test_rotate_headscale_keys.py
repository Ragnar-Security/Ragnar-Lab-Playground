"""Key rotation must never leave a student without a working Headscale key.

The rig stands in for both hosts: `ssh` runs the remote command locally,
`docker` forwards to a stateful fake `headscale` or to the real
`scripts/update_headscale_keys.py` against SQLite, and every fake appends to
one call log so the order of mutations is observable.
"""

import csv
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.models import db

ROOT = Path(__file__).resolve().parent.parent
FIELDNAMES = ["SIS User ID", "SIS Login ID", "email", "headscale_key"]
REQUIRED = [
    "PORTAL_HOST",
    "PORTAL_DIR",
    "PORTAL_COMPOSE",
    "HEADSCALE_HOST",
    "HEADSCALE_DIR",
    "HEADSCALE_KEEP_USER",
    "HEADSCALE_STAFF",
    "KEY_DAYS",
    "REMOTE_BACKUP_DIR",
]
FUTURE = int(time.time()) + 30 * 86400
PAST = int(time.time()) - 86400
OLD = {
    "infra": "hskey-auth-OLDINFRA0000000000000000000000000000000",
    "staffer": "hskey-auth-OLDSTAFF0000000000000000000000000000000",
    "alice": "hskey-auth-OLDALICE0000000000000000000000000000000",
    "alice-expired": "hskey-auth-EXPALICE0000000000000000000000000000000",
    "bob": "hskey-auth-OLDBOB000000000000000000000000000000000",
}

FAKE_HEADSCALE = r'''
import json, os, secrets, sys, time
state_path = os.environ["HS_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]

def log(line):
    with open(os.environ["CALL_LOG"], "a") as handle:
        handle.write(line + "\n")

def opt(*names):
    for name in names:
        if name in args:
            return args[args.index(name) + 1]

def user(uid):
    return next(u for u in state["users"] if u["id"] == uid)

def shown(k):
    out = {"user": user(k["user_id"]), "id": k["id"], "key": k["key"],
           "expiration": {"seconds": k["exp"]}, "created_at": {"seconds": 1}}
    if k["reusable"]:
        out["reusable"] = True
    return out

if args[:2] == ["users", "list"]:
    log("headscale users list")
    print(json.dumps(state["users"], indent="\t"))
elif args[:2] == ["preauthkeys", "list"]:
    uid = int(opt("-u", "--user"))
    log(f"headscale preauthkeys list {uid}")
    print(json.dumps([shown(k) for k in state["keys"] if k["user_id"] == uid], indent="\t"))
elif args[:2] == ["preauthkeys", "create"]:
    uid = int(opt("-u", "--user"))
    log(f"headscale preauthkeys create {uid} reusable={'--reusable' in args} expiration={opt('-e', '--expiration')}")
    key = {"id": max(k["id"] for k in state["keys"]) + 1, "user_id": uid,
           "key": "hskey-auth-" + secrets.token_hex(24),
           "reusable": "--reusable" in args, "exp": int(time.time()) + 86400}
    state["keys"].append(key)
    json.dump(state, open(state_path, "w"))
    print(json.dumps(shown(key), indent="\t"))
elif args[:2] == ["preauthkeys", "expire"]:
    uid = int(opt("-u", "--user"))
    value = args[-1]
    match = [k for k in state["keys"] if k["user_id"] == uid and k["key"] == value]
    if not match:
        print(f"Error expiring key {value}: not found", file=sys.stderr)
        sys.exit(1)
    match[0]["exp"] = int(time.time()) - 1
    json.dump(state, open(state_path, "w"))
    log(f"headscale preauthkeys expire {uid} {match[0]['id']}")
    print("Key expired")
else:
    print("fake headscale: unsupported " + " ".join(args), file=sys.stderr)
    sys.exit(2)
'''

FAKE_DOCKER = r'''
import os, sys
args = sys.argv[1:]

def log(line):
    with open(os.environ["CALL_LOG"], "a") as handle:
        handle.write(line + "\n")

assert args[0] == "compose", args
rest = args[1:]
if rest[0] == "-f":
    rest = rest[2:]
assert rest[:2] == ["exec", "-T"], args
service, command = rest[2], rest[3:]
if service == "headscale":
    os.execvp(command[0], command)
if service == "vm-automation":
    if command[:2] == ["test", "-f"]:
        sys.exit(1 if os.environ.get("NO_UPDATE_SCRIPT") else 0)
    if command[:2] == ["python", "scripts/update_headscale_keys.py"]:
        log("docker update_headscale_keys")
        if os.environ.get("FAIL_DB_UPDATE"):
            sys.stdin.read()
            print("db is down", file=sys.stderr)
            sys.exit(1)
        script = os.path.join(os.environ["REAL_SCRIPTS"], "update_headscale_keys.py")
        os.execv(sys.executable, [sys.executable, script] + command[2:])
print("fake docker: unsupported " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''

FAKE_SSH = r'''
import os, sys
args = sys.argv[1:]
i = 0
while args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-i", "-p", "-l") else 1
host, command = args[i], " ".join(args[i + 1:])
with open(os.environ["CALL_LOG"], "a") as handle:
    handle.write(f"ssh {host}\n")
os.execvp("sh", ["sh", "-c", command])
'''

FAKE_SCP = r'''
import hashlib, os, shutil, sys
args = [a for a in sys.argv[1:]]
paths = []
i = 0
while i < len(args):
    if args[i] in ("-o", "-i", "-P"):
        i += 2
        continue
    if not args[i].startswith("-"):
        paths.append(args[i])
    i += 1
src, dst = paths
local = os.environ["LOCAL_CSV"]
same = os.path.exists(local) and open(local, "rb").read() == open(src, "rb").read()
with open(os.environ["CALL_LOG"], "a") as handle:
    handle.write(f"scp local_written={int(same)}\n")
shutil.copyfile(src, dst.split(":", 1)[1])
'''

FAKE_SHA256SUM = r'''
import hashlib, sys
for path in sys.argv[1:]:
    print(hashlib.sha256(open(path, "rb").read()).hexdigest() + "  " + path)
'''


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _write_roster(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDNAMES)
        writer.writerows(rows)


class Rig:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "data").mkdir()
        for name in ("rotate_headscale_keys.sh", "rotate_headscale_keys.py", "site_env.sh"):
            shutil.copy(ROOT / "scripts" / name, self.repo / "scripts" / name)
        self.local_csv = self.repo / "data" / "students_merged.csv"

        self.portal = tmp_path / "portal"
        (self.portal / "data").mkdir(parents=True)
        self.portal_csv = self.portal / "data" / "students_merged.csv"
        # The keep user and a staff login appear on the roster only to prove
        # that exclusion comes from site.env, not from roster membership.
        _write_roster(
            self.portal_csv,
            [
                ["1001", "alice", "alice@example.edu", OLD["alice"]],
                ["1002", "bob", "bob@example.edu", OLD["bob"]],
                ["1003", "staffer", "staffer@example.edu", OLD["staffer"]],
                ["1004", "infra", "infra@example.edu", OLD["infra"]],
            ],
        )
        self.portal_csv_before = self.portal_csv.read_bytes()
        self.headscale_dir = tmp_path / "headscale"
        self.headscale_dir.mkdir()
        self.backups = tmp_path / "backups"

        self.hs_state = tmp_path / "hs_state.json"
        self.hs_state.write_text(
            json.dumps(
                {
                    "users": [
                        {"id": 1, "name": "infra"},
                        {"id": 2, "name": "staffer"},
                        {"id": 3, "name": "alice"},
                        {"id": 4, "name": "bob"},
                    ],
                    "keys": [
                        {"id": 1, "user_id": 1, "key": OLD["infra"], "reusable": True, "exp": FUTURE},
                        {"id": 2, "user_id": 2, "key": OLD["staffer"], "reusable": True, "exp": FUTURE},
                        {"id": 3, "user_id": 3, "key": OLD["alice-expired"], "reusable": True, "exp": PAST},
                        {"id": 4, "user_id": 3, "key": OLD["alice"], "reusable": True, "exp": FUTURE},
                        {"id": 5, "user_id": 4, "key": OLD["bob"], "reusable": True, "exp": FUTURE},
                    ],
                }
            )
        )

        self.db_path = tmp_path / "portal.db"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        db.Model.metadata.create_all(self.engine)
        now = datetime.now().isoformat(sep=" ")  # see test_update_headscale_keys.py
        with self.engine.begin() as conn:
            for login in ("alice", "bob", "staffer", "infra"):
                conn.execute(
                    text("INSERT INTO student_whitelist (login_id, headscale_key, added_at) VALUES (:l, :k, :t)"),
                    {"l": login, "k": OLD[login], "t": now},
                )
            conn.execute(
                text(
                    "INSERT INTO users (login_id, password_hash, is_admin, headscale_key, created_at, disabled, session_version) "
                    "VALUES ('alice', 'x', 0, :k, :t, 0, 0)"
                ),
                {"k": OLD["alice"], "t": now},
            )

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        _tool(self.bin, "headscale", FAKE_HEADSCALE)
        _tool(self.bin, "docker", FAKE_DOCKER)
        _tool(self.bin, "ssh", FAKE_SSH)
        _tool(self.bin, "scp", FAKE_SCP)
        _tool(self.bin, "sha256sum", FAKE_SHA256SUM)
        (self.bin / "python3").symlink_to(sys.executable)
        self.log = tmp_path / "calls.log"
        self.log.touch()

        self.site_env = tmp_path / "site.env"
        self.site_env.write_text(
            "\n".join(
                [
                    "PORTAL_HOST=portal-host",
                    f"PORTAL_DIR={self.portal}",
                    "PORTAL_COMPOSE=docker-compose.prod.yml",
                    "HEADSCALE_HOST=headscale-host",
                    f"HEADSCALE_DIR={self.headscale_dir}",
                    "HEADSCALE_KEEP_USER=infra",
                    "HEADSCALE_STAFF='staffer, ta2'",
                    "KEY_DAYS=150d",
                    f"REMOTE_BACKUP_DIR={self.backups}",
                ]
            )
            + "\n"
        )

    def env(self, **extra):
        env = dict(os.environ)
        for name in REQUIRED:
            env.pop(name, None)
        env.update(
            PATH=f"{self.bin}:{os.environ['PATH']}",
            SITE_ENV_FILE=str(self.site_env),
            CALL_LOG=str(self.log),
            HS_STATE=str(self.hs_state),
            REAL_SCRIPTS=str(ROOT / "scripts"),
            LOCAL_CSV=str(self.local_csv),
            DATABASE_URL=f"sqlite:///{self.db_path}",
        )
        env.update(extra)
        return env

    def run(self, *args, **extra):
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "rotate_headscale_keys.sh"), *args],
            env=self.env(**extra),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self):
        return self.log.read_text().splitlines()

    def mutations(self):
        return [
            c
            for c in self.calls()
            if " create " in c or " expire " in c or c.startswith("scp") or "update_headscale_keys" in c
        ]

    def hs_keys(self):
        return json.loads(self.hs_state.read_text())["keys"]

    def active_keys(self, user_id):
        now = time.time()
        return {k["key"] for k in self.hs_keys() if k["user_id"] == user_id and k["exp"] > now}

    def all_key_values(self):
        return [k["key"] for k in self.hs_keys()]

    def csv_keys(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            return {row["SIS Login ID"]: row["headscale_key"] for row in csv.DictReader(handle)}

    def db_keys(self, table):
        with self.engine.connect() as conn:
            return dict(conn.execute(text(f"SELECT login_id, headscale_key FROM {table}")).all())


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.engine.dispose()


def _assert_no_key_leaked(rig, result):
    output = result.stdout + result.stderr
    for value in rig.all_key_values():
        assert value not in output


@pytest.mark.parametrize("missing", REQUIRED)
def test_each_missing_site_variable_is_named_before_any_host_is_contacted(rig, missing):
    rig.site_env.write_text(
        "\n".join(line for line in rig.site_env.read_text().splitlines() if not line.startswith(missing + "="))
        + "\n"
    )

    result = rig.run()

    assert result.returncode != 0
    assert missing in result.stderr
    assert rig.calls() == []


def test_dry_run_reports_the_plan_and_changes_nothing(rig):
    result = rig.run()

    assert result.returncode == 0, result.stderr
    assert rig.mutations() == []
    assert rig.portal_csv.read_bytes() == rig.portal_csv_before
    assert not rig.local_csv.exists()
    assert rig.db_keys("student_whitelist")["alice"] == OLD["alice"]
    assert rig.active_keys(3) == {OLD["alice"]}
    assert "alice" in result.stdout and "bob" in result.stdout
    _assert_no_key_leaked(rig, result)


def test_apply_without_yes_refuses_when_nobody_can_confirm(rig):
    result = rig.run("--apply")

    assert result.returncode != 0
    assert "--yes" in result.stderr
    assert rig.mutations() == []
    assert rig.portal_csv.read_bytes() == rig.portal_csv_before


def test_apply_rotates_in_the_safe_order(rig):
    result = rig.run("--apply", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    events = rig.mutations()
    creates = [i for i, c in enumerate(events) if " create " in c]
    deliver = [i for i, c in enumerate(events) if c.startswith("scp")]
    update = [i for i, c in enumerate(events) if "update_headscale_keys" in c]
    expires = [i for i, c in enumerate(events) if " expire " in c]
    assert creates and deliver and update and expires
    assert max(creates) < min(deliver) < min(update) and max(update) < min(expires)
    # The local CSV already held the new keys when it was delivered.
    assert events[deliver[0]] == "scp local_written=1"
    assert all("reusable=True expiration=150d" in events[i] for i in creates)

    new_alice = rig.csv_keys(rig.local_csv)["alice"]
    new_bob = rig.csv_keys(rig.local_csv)["bob"]
    assert new_alice not in (OLD["alice"], "") and new_bob not in (OLD["bob"], "")
    assert stat.S_IMODE(rig.local_csv.stat().st_mode) == 0o600
    assert rig.csv_keys(rig.portal_csv) == rig.csv_keys(rig.local_csv)
    assert rig.db_keys("users")["alice"] == new_alice
    assert rig.db_keys("student_whitelist")["bob"] == new_bob
    assert rig.active_keys(3) == {new_alice}
    assert rig.active_keys(4) == {new_bob}
    # The old roster survives in the backup directory for a rollback.
    backups = list(rig.backups.iterdir())
    assert [b.read_bytes() for b in backups] == [rig.portal_csv_before]
    # A key that had already expired is left alone.
    assert "headscale preauthkeys expire 3 3" not in rig.calls()
    _assert_no_key_leaked(rig, result)


def test_failed_database_update_keeps_old_keys_working(rig):
    result = rig.run("--apply", "--yes", FAIL_DB_UPDATE="1")

    assert result.returncode != 0
    assert not [c for c in rig.calls() if " expire " in c]
    assert OLD["alice"] in rig.active_keys(3)
    assert OLD["bob"] in rig.active_keys(4)
    assert "not expired" in result.stderr.lower()
    _assert_no_key_leaked(rig, result)


def test_staff_and_keep_user_keep_their_keys(rig):
    result = rig.run("--apply", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    touched = {c.split()[3] for c in rig.calls() if " create " in c or " expire " in c}
    assert touched == {"3", "4"}
    assert rig.active_keys(1) == {OLD["infra"]}
    assert rig.active_keys(2) == {OLD["staffer"]}
    assert rig.csv_keys(rig.portal_csv)["staffer"] == OLD["staffer"]
    assert rig.csv_keys(rig.portal_csv)["infra"] == OLD["infra"]
    assert rig.db_keys("student_whitelist")["staffer"] == OLD["staffer"]


def test_only_limits_rotation_to_the_named_logins(rig):
    result = rig.run("--apply", "--yes", "--only", "alice")

    assert result.returncode == 0, result.stdout + result.stderr
    touched = {c.split()[3] for c in rig.calls() if " create " in c or " expire " in c}
    assert touched == {"3"}
    assert rig.active_keys(4) == {OLD["bob"]}
    assert rig.csv_keys(rig.portal_csv)["bob"] == OLD["bob"]
    assert rig.db_keys("student_whitelist")["bob"] == OLD["bob"]
    assert rig.csv_keys(rig.portal_csv)["alice"] != OLD["alice"]
    _assert_no_key_leaked(rig, result)


@pytest.mark.parametrize("login", ["nobody", "staffer", "infra"])
def test_only_refuses_logins_outside_the_rotatable_roster(rig, login):
    result = rig.run("--apply", "--yes", "--only", f"alice,{login}")

    assert result.returncode != 0
    assert login in result.stderr
    assert rig.mutations() == []


def test_student_missing_from_headscale_stops_apply_before_any_key_is_created(rig):
    state = json.loads(rig.hs_state.read_text())
    state["users"] = [u for u in state["users"] if u["name"] != "bob"]
    rig.hs_state.write_text(json.dumps(state))

    result = rig.run("--apply", "--yes")

    assert result.returncode != 0
    assert "bob" in result.stderr
    assert rig.mutations() == []


def test_portal_without_the_update_script_stops_apply_before_any_key_is_created(rig):
    result = rig.run("--apply", "--yes", NO_UPDATE_SCRIPT="1")

    assert result.returncode != 0
    assert "update_headscale_keys.py" in result.stderr
    assert rig.mutations() == []


def test_student_unknown_to_the_portal_database_keeps_the_old_key(rig):
    # Expiring a key the portal cannot replace would strand that student.
    with rig.engine.begin() as conn:
        conn.execute(text("DELETE FROM student_whitelist WHERE login_id = 'bob'"))

    result = rig.run("--apply", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert OLD["bob"] in rig.active_keys(4)
    assert OLD["alice"] not in rig.active_keys(3)
    assert "bob" in result.stdout + result.stderr
