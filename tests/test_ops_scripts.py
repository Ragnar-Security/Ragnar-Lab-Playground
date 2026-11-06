"""Operator scripts must reject incomplete site config before touching hosts."""

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.headscale_policy import canonical, policy_inputs, render

ROOT = Path(__file__).resolve().parent.parent
TERM_CHANGEOVER = ROOT / "scripts" / "term_changeover.sh"
HEADSCALE_CHANGEOVER = ROOT / "scripts" / "headscale_term_changeover.sh"
APPLY_POLICY = ROOT / "scripts" / "apply_headscale_policy.sh"

TERM_REQUIRED = [
    "PORTAL_HOST",
    "PORTAL_DIR",
    "PORTAL_COMPOSE",
    "PORTAL_APP_CONTAINER",
    "HEADSCALE_HOST",
    "HEADSCALE_DIR",
    "HEADSCALE_KEEP_USER",
    "HEADSCALE_STAFF",
    "EMAIL_DOMAIN",
    "REMOTE_BACKUP_DIR",
    "KEY_DAYS",
]
HEADSCALE_REQUIRED = [
    "HEADSCALE_KEEP_USER",
    "HEADSCALE_STAFF",
    "EMAIL_DOMAIN",
    "KEY_DAYS",
]
POLICY_REQUIRED = [
    "PORTAL_HOST",
    "PORTAL_APP_CONTAINER",
    "HEADSCALE_HOST",
    "HEADSCALE_DIR",
]
POLICY_INPUTS = ["HEADSCALE_BASE_POLICY", "HEADSCALE_POLICY_FILE", "POLICY_VIEW", "CHECK_FAIL",
                 "RELOAD_FAIL"]
# term_changeover.sh reads this exact line from the policy stage's dry run.
POLICY_FILE_CURRENT = "  nothing to do: the policy file Headscale serves matches the rendered one."


def _clean_env(tmp_path: Path, **values: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in set(TERM_REQUIRED + HEADSCALE_REQUIRED + POLICY_REQUIRED + POLICY_INPUTS):
        env.pop(name, None)
    env.update(values)
    env.setdefault("SITE_ENV_FILE", str(tmp_path / "missing-site.env"))
    return env


@pytest.mark.parametrize("missing_index", range(len(TERM_REQUIRED)))
def test_term_changeover_names_each_missing_site_variable(tmp_path, missing_index):
    supplied = {name: "configured" for name in TERM_REQUIRED[:missing_index]}
    result = subprocess.run(
        ["bash", str(TERM_CHANGEOVER)],
        env=_clean_env(tmp_path, **supplied),
        capture_output=True,
        text=True,
        timeout=10,
    )

    missing = TERM_REQUIRED[missing_index]
    assert result.returncode != 0
    assert missing in result.stderr


@pytest.mark.parametrize("missing_index", range(len(HEADSCALE_REQUIRED)))
def test_headscale_changeover_names_each_missing_site_variable(tmp_path, missing_index):
    supplied = {name: "configured" for name in HEADSCALE_REQUIRED[:missing_index]}
    result = subprocess.run(
        ["bash", str(HEADSCALE_CHANGEOVER)],
        env=_clean_env(tmp_path, **supplied),
        capture_output=True,
        text=True,
        timeout=10,
    )

    missing = HEADSCALE_REQUIRED[missing_index]
    assert result.returncode != 0
    assert missing in result.stderr


def test_term_changeover_forwards_headscale_config_to_remote_helper(tmp_path):
    config = tmp_path / "site.env"
    config.write_text(
        "\n".join(
            [
                "PORTAL_HOST=portal-host",
                "PORTAL_DIR=/opt/lab-playground",
                "PORTAL_COMPOSE=docker-compose.prod.yml",
                "PORTAL_APP_CONTAINER=lab-playground",
                "HEADSCALE_HOST=headscale-host",
                "HEADSCALE_DIR=/opt/headscale",
                "HEADSCALE_KEEP_USER=keep-user",
                "HEADSCALE_STAFF='staff-one, staff-two'",
                "EMAIL_DOMAIN=example.edu",
                "REMOTE_BACKUP_DIR=/var/backups/lab-playground",
                "KEY_DAYS=45d",
            ]
        )
        + "\n"
    )
    roster = tmp_path / "roster.csv"
    roster.write_text("SIS User ID,SIS Login ID\n1001,student1\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_log = tmp_path / "ssh.log"
    users = base64.b64encode(b'[{"name":"keep-user"},{"name":"staff-two"}]').decode()
    keys = base64.b64encode(b"student1,student1@example.edu,fake-key\n").decode()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SSH_LOG\"\n"
        "case \"$*\" in\n"
        "  *\"python3 -c\"*) printf 'to remove: 0 users\\n' ;;\n"
        f"  *\"base64 -w0 && printf\"*) printf '%s\\n%s\\n' '{users}' '{keys}' ;;\n"
        f"  *\"base64 -w0 students_with_keys.csv\"*) printf '%s' '{keys}' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)

    env = _clean_env(
        tmp_path,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SITE_ENV_FILE=str(config),
        SSH_LOG=str(ssh_log),
    )
    result = subprocess.run(
        ["bash", str(TERM_CHANGEOVER), str(roster)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert "Headscale counts disagree" not in result.stderr
    helper_call = next(
        line for line in ssh_log.read_text().splitlines() if "python3 -c" in line
    )
    forwarded = helper_call.replace("\\,", ",").replace("\\ ", " ")
    assert "HEADSCALE_KEEP_USER=keep-user" in forwarded
    assert "HEADSCALE_STAFF=staff-one, staff-two" in forwarded
    assert "EMAIL_DOMAIN=example.edu" in forwarded
    assert "KEY_DAYS=45d" in forwarded


def test_term_changeover_delivers_roster_readable_only_by_owner_and_app_group(tmp_path):
    # The live roster holds reusable VPN keys. The app container runs as
    # uid/gid 1000 (Dockerfile.prod), so group 1000 + 0640 is enough for it
    # and keeps the file away from every other account on the portal host.
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "data").mkdir()
    for name in ("term_changeover.sh", "site_env.sh", "build_students_merged.py", "purge_roster.sql",
                 "apply_headscale_policy.sh"):
        shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
    config = tmp_path / "site.env"
    config.write_text(
        "\n".join(
            [
                "PORTAL_HOST=portal-host",
                "PORTAL_DIR=/opt/lab-playground",
                "PORTAL_COMPOSE=docker-compose.prod.yml",
                "PORTAL_APP_CONTAINER=lab-playground",
                "HEADSCALE_HOST=headscale-host",
                "HEADSCALE_DIR=/opt/headscale",
                "HEADSCALE_KEEP_USER=keep-user",
                "HEADSCALE_STAFF=staff-one",
                "EMAIL_DOMAIN=example.edu",
                f"REMOTE_BACKUP_DIR={tmp_path / 'backups'}",
                "KEY_DAYS=45d",
            ]
        )
        + "\n"
    )
    roster = tmp_path / "roster.csv"
    roster.write_text("SIS User ID,SIS Login ID\n1001,student1\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_log = tmp_path / "ssh.log"
    key_line = "student1,student1@example.edu,fake-key\n"
    users = base64.b64encode(b'[{"name":"keep-user"},{"name":"student1"}]').decode()
    keys = base64.b64encode(key_line.encode()).decode()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n=====\\n' \"$*\" >> \"$SSH_LOG\"\n"
        "case \"$*\" in\n"
        "  *\"python3 -c\"*) printf 'to remove: 0 users\\n' ;;\n"
        f"  *\"base64 -w0 && printf\"*) printf '%s\\n%s\\n' '{users}' '{keys}' ;;\n"
        f"  *\"base64 -w0 students_with_keys.csv\"*) printf '%s' '{keys}' ;;\n"
        f"  *\"cat '\"*) printf '%s' '{key_line}' ;;\n"
        "  *sha256sum*) printf 'missing' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    scp = bin_dir / "scp"
    scp.write_text("#!/bin/sh\nexit 0\n")
    scp.chmod(scp.stat().st_mode | stat.S_IEXEC)

    env = _clean_env(
        tmp_path,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SITE_ENV_FILE=str(config),
        SSH_LOG=str(ssh_log),
    )
    subprocess.run(
        ["bash", str(repo / "scripts" / "term_changeover.sh"), "--apply", str(roster)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    delivery = next(
        call
        for call in ssh_log.read_text().split("\n=====\n")
        if "mv '" in call and "students_merged.csv" in call
    )
    tmp = next(word for word in delivery.split() if word.endswith(".tmp'"))
    assert f"chgrp 1000 {tmp}" in delivery
    assert f"chmod 640 {tmp}" in delivery
    # Match the mode, not the digits: temp names carry a timestamp and PID.
    assert not re.search(r"chmod\s+0?644\b", delivery), delivery
    assert delivery.index("chmod 640") < delivery.index("mv '")


def test_setup_production_passes_proxmox_credentials_via_environment(tmp_path):
    """Credentials with quotes/backslashes must reach proxmoxer intact and never be code."""
    lines = (ROOT / "setup-production.sh").read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if "Testing Proxmox connection" in l)
    end = next(i for i, l in enumerate(lines) if l.startswith("# Check templates.yaml"))
    snippet = "\n".join(lines[start:end])

    stub = tmp_path / "stub"
    stub.mkdir()
    out = tmp_path / "seen.txt"
    (stub / "proxmoxer.py").write_text(
        "import os\n"
        "class _V:\n"
        "    def get(self):\n"
        "        return {'version': '8'}\n"
        "class ProxmoxAPI:\n"
        "    def __init__(self, host, user=None, password=None, verify_ssl=True):\n"
        f"        open({str(out)!r}, 'w').write(repr((host, user, password)))\n"
        "        self.version = _V()\n"
    )
    evil = "pa'ss\\w\"); import os #"
    marker = tmp_path / "pwned"
    evil_user = f"u'\"); open({str(marker)!r}, 'w') #@pam"
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(stub),
        PROXMOX_HOST="h'ost",
        PROXMOX_USER=evil_user,
        PROXMOX_PASSWORD=evil,
    )
    result = subprocess.run(
        ["bash", "-c", snippet], env=env, capture_output=True, text=True, cwd=tmp_path
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.read_text() == repr(("h'ost", evil_user, evil))
    assert not marker.exists()


def test_legacy_shared_student_password_is_gone_from_repo():
    pattern = "Change" + "Me2026"
    found = subprocess.run(
        ["git", "grep", "-l", "-i", pattern, "--", ".", ":!tests/test_ops_scripts.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert found.stdout == ""


# ---- scripts/apply_headscale_policy.sh --------------------------------------
#
# ssh runs the remote command locally, scp copies, and docker emulates the two
# containers against files under tmp_path, so every assertion is about the
# resulting files and the order of events, not about mock calls.

POLICY_SLOTS = {
    "config": {
        "enabled_backends": ["proxmox", "aws"],
        "aws_lab_cidr": "172.20.0.0/16",
        "gcp_lab_cidr": "172.21.0.0/16",
        "jumpbox_vlan_bands": "entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4",
    },
    "users": [
        {"login": "alice", "entry_vlan": 100, "internal_vlan": 200},
        {"login": "bob", "entry_vlan": 101, "internal_vlan": 201},
    ],
    "groups": [{"slug": "red-team", "members": ["alice", "bob"],
                "entry_vlan": 300, "internal_vlan": 400}],
    "koth": [],
}
EXAMPLE_POLICY = json.loads((ROOT / "config" / "headscale-acl.example.json").read_text())

SSH_STUB = """#!/bin/bash
while [ "$1" = "-o" ]; do shift 2; done
host="$1"; shift
printf 'ssh %s %s\\n=====\\n' "$host" "$*" >> "$SSH_LOG"
exec /bin/sh -c "$*"
"""
SCP_STUB = """#!/bin/bash
while [ "$1" = "-o" ]; do shift 2; done
printf 'scp %s\\n=====\\n' "$*" >> "$SSH_LOG"
cp "$1" "${2#*:}"
"""
DOCKER_STUB = """#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
args = sys.argv[1:]
policy = os.environ["HOST_POLICY"]
loaded = os.environ["LOADED_POLICY"]
view = os.environ.get("POLICY_VIEW", "live")
def event(text):
    with open(os.environ["DOCKER_LOG"], "a") as log:
        log.write(text + "\\n")
def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()
if args[:1] == ["exec"]:
    event("exec " + " ".join(args[1:]))
    sys.stdout.write(open(os.environ["SLOTS_FILE"]).read())
elif args == ["inspect", "headscale-cid"]:
    print(json.dumps([{"Mounts": [
        {"Type": "bind", "Source": os.environ["HOST_DATA"], "Destination": "/var/lib/headscale"},
        {"Type": "bind", "Source": os.path.dirname(policy) + "/", "Destination": "/etc/headscale"},
    ]}]))
elif args == ["compose", "ps", "-q", "headscale"]:
    print("headscale-cid")
elif args == ["compose", "exec", "-T", "headscale", "headscale", "policy", "get"]:
    event("policy get")
    sys.stdout.write(open(policy if view == "live" else loaded).read())
elif args == ["compose", "exec", "-T", "headscale", "headscale", "policy", "check", "--file", "/dev/stdin"]:
    data = sys.stdin.read()
    event("policy check while-installed=" + digest(policy))
    if os.environ.get("CHECK_FAIL"):
        sys.exit("policy check failed")
    json.loads(data)
elif args == ["compose", "kill", "-s", "HUP", "headscale"]:
    event("reload headscale")
    if os.environ.get("RELOAD_FAIL"):
        sys.exit("cannot kill container")
    # `policy get` reads the file; what Headscale enforces changes only here.
    # A single-file bind mount ("stale") still shows the replaced inode.
    if view == "live":
        shutil.copy(policy, loaded)
elif args == ["compose", "restart", "headscale"]:
    event("restart headscale")
    if view == "stale":
        shutil.copy(policy, loaded)
else:
    event("unexpected " + " ".join(args))
    sys.exit(97)
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _policy_rig(tmp_path: Path, **extra: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "ssh", SSH_STUB)
    _executable(bin_dir / "scp", SCP_STUB)
    _executable(bin_dir / "docker", DOCKER_STUB)
    headscale_dir = tmp_path / "headscale"
    config_dir = headscale_dir / "config"
    config_dir.mkdir(parents=True)
    (headscale_dir / "data").mkdir()
    policy = config_dir / "acl.json"
    original = json.dumps(EXAMPLE_POLICY, indent=4)  # not canonical on purpose
    policy.write_text(original)
    policy.chmod(0o644)
    loaded = tmp_path / "loaded.json"
    loaded.write_text(original)
    base = tmp_path / "base.json"
    base.write_text(json.dumps(EXAMPLE_POLICY))
    slots = tmp_path / "slots.json"
    slots.write_text(json.dumps(POLICY_SLOTS, separators=(",", ":")) + "\n")
    env = _clean_env(
        tmp_path,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        PORTAL_HOST="portal-host",
        PORTAL_APP_CONTAINER="lab-playground",
        HEADSCALE_HOST="headscale-host",
        HEADSCALE_DIR=str(headscale_dir),
        HEADSCALE_BASE_POLICY=str(base),
        HEADSCALE_POLICY_FILE="/etc/headscale/acl.json",
        HOST_POLICY=str(policy),
        HOST_DATA=str(headscale_dir / "data"),
        LOADED_POLICY=str(loaded),
        SLOTS_FILE=str(slots),
        SSH_LOG=str(tmp_path / "ssh.log"),
        DOCKER_LOG=str(tmp_path / "docker.log"),
        **extra,
    )
    desired = canonical(render(EXAMPLE_POLICY, POLICY_SLOTS, *policy_inputs(POLICY_SLOTS)))
    return env, policy, original, desired


def _apply_policy(env, *args):
    return subprocess.run(["bash", str(APPLY_POLICY), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def _log(env, name):
    path = Path(env[name])
    return path.read_text() if path.exists() else ""


def _backups(policy: Path):
    return sorted(p for p in policy.parent.iterdir()
                  if re.fullmatch(r"acl\.json\.\d{8}T\d{6}Z", p.name))


@pytest.mark.parametrize("missing", POLICY_REQUIRED)
def test_apply_policy_names_a_missing_site_variable_before_connecting(tmp_path, missing):
    env, _, _, _ = _policy_rig(tmp_path)
    env.pop(missing)
    result = _apply_policy(env)
    assert result.returncode != 0
    assert missing in result.stderr
    assert _log(env, "SSH_LOG") == ""


# Catches a dry run that writes, restarts, or hides the change it would make.
def test_apply_policy_dry_run_prints_the_diff_and_changes_nothing(tmp_path):
    env, policy, original, _ = _policy_rig(tmp_path)
    before = sorted(p.name for p in policy.parent.iterdir())
    result = _apply_policy(env)

    assert result.returncode == 0, result.stderr
    assert "--- current\n+++ desired\n" in result.stdout
    assert re.search(r'^-\s+"autogroup:member"$', result.stdout, re.M)
    assert re.search(r'^\+\s+"alice@"$', result.stdout, re.M)
    assert policy.read_text() == original
    assert sorted(p.name for p in policy.parent.iterdir()) == before
    docker = _log(env, "DOCKER_LOG")
    assert "exec lab-playground python scripts/export_network_slots.py" in docker
    assert "policy get" in docker
    assert "policy check" not in docker
    assert "restart" not in docker and "reload" not in docker
    assert "scp " not in _log(env, "SSH_LOG")


# Catches an unchecked, non-atomic or unbacked-up install, and a needless restart.
def test_apply_policy_checks_backs_up_and_installs_atomically(tmp_path):
    env, policy, original, desired = _policy_rig(tmp_path)
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    original_inode = policy.stat().st_ino
    result = _apply_policy(env, "--apply")

    assert result.returncode == 0, result.stderr
    assert policy.read_text() == desired
    assert policy.stat().st_ino != original_inode  # renamed into place, not rewritten
    assert stat.S_IMODE(policy.stat().st_mode) == 0o644
    backups = _backups(policy)
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert sorted(p.name for p in policy.parent.iterdir()) == ["acl.json", backups[0].name]
    docker = _log(env, "DOCKER_LOG").splitlines()
    check = docker.index(f"policy check while-installed={original_digest}")
    assert check < docker.index("reload headscale") < len(docker) - 1
    assert docker[-1] == "policy get"
    assert not any("restart" in line for line in docker)
    assert "verified: the policy file Headscale serves matches the rendered one." in result.stdout
    assert Path(env["LOADED_POLICY"]).read_text() == desired

    # A matching file is not proof of what Headscale loaded, so --apply still
    # checks and signals, and installs nothing.
    again = _apply_policy(env, "--apply")
    assert again.returncode == 0, again.stderr
    assert len(_backups(policy)) == 1
    docker = _log(env, "DOCKER_LOG").splitlines()
    assert docker.count("reload headscale") == 2
    rerun = docker[check + 1:]
    assert not any("restart" in line for line in rerun)
    rechecks = [i for i, line in enumerate(rerun) if line.startswith("policy check")]
    assert len(rechecks) == 1 and rechecks[0] < len(rerun) - 1
    assert rerun[-1] == "reload headscale"

    dry = _apply_policy(env)
    assert dry.returncode == 0, dry.stderr
    assert POLICY_FILE_CURRENT in dry.stdout.splitlines()
    assert _log(env, "DOCKER_LOG").splitlines().count("reload headscale") == 2


# Catches a re-run trusting `policy get` (the file) after the reload failed:
# the file matches, but Headscale still enforces the outgoing policy, which can
# hold the blanket member rule that lets every student into every lab.
def test_apply_policy_rerun_after_a_failed_reload_makes_headscale_load_the_policy(tmp_path):
    env, policy, original, desired = _policy_rig(tmp_path, RELOAD_FAIL="1")
    first = _apply_policy(env, "--apply")
    assert first.returncode != 0
    assert "reload" in first.stderr
    assert policy.read_text() == desired
    assert Path(env["LOADED_POLICY"]).read_text() == original

    del env["RELOAD_FAIL"]
    again = _apply_policy(env, "--apply")
    assert again.returncode == 0, again.stderr
    assert Path(env["LOADED_POLICY"]).read_text() == desired
    assert len(_backups(policy)) == 1
    assert "effective" not in again.stdout


# Catches a matching-file --apply signalling a reload of a policy Headscale
# rejects (an upgrade, or a hand-installed file), then reporting success.
def test_apply_policy_matching_file_is_checked_before_the_reload(tmp_path):
    env, policy, _, desired = _policy_rig(tmp_path)
    policy.write_text(desired)
    Path(env["LOADED_POLICY"]).write_text(desired)
    env["CHECK_FAIL"] = "1"
    result = _apply_policy(env, "--apply")

    assert result.returncode != 0
    assert "rejects" in result.stderr
    docker = _log(env, "DOCKER_LOG")
    assert "policy check" in docker
    assert "reload headscale" not in docker
    assert policy.read_text() == desired
    assert _backups(policy) == []


# Catches a stale read-back being accepted, or answered with more than one restart.
def test_apply_policy_restarts_headscale_once_when_the_read_back_is_stale(tmp_path):
    env, policy, _, desired = _policy_rig(tmp_path, POLICY_VIEW="stale")
    result = _apply_policy(env, "--apply")

    assert result.returncode == 0, result.stderr
    docker = _log(env, "DOCKER_LOG").splitlines()
    assert docker.count("reload headscale") == 1
    assert docker.count("restart headscale") == 1
    assert docker.index("reload headscale") < docker.index("restart headscale")
    assert docker[-1] == "policy get"
    assert Path(env["LOADED_POLICY"]).read_text() == desired
    assert policy.read_text() == desired


def test_apply_policy_fails_when_the_read_back_still_differs_after_one_restart(tmp_path):
    env, _, _, _ = _policy_rig(tmp_path, POLICY_VIEW="stuck")
    result = _apply_policy(env, "--apply")

    assert result.returncode != 0
    assert "still differs" in result.stderr
    assert _log(env, "DOCKER_LOG").splitlines().count("restart headscale") == 1


# Catches a policy Headscale rejects being installed anyway.
def test_apply_policy_failed_check_leaves_the_policy_and_backups_unchanged(tmp_path):
    env, policy, original, _ = _policy_rig(tmp_path, CHECK_FAIL="1")
    result = _apply_policy(env, "--apply")

    assert result.returncode != 0
    assert "rejected" in result.stderr
    assert policy.read_text() == original
    assert sorted(p.name for p in policy.parent.iterdir()) == ["acl.json"]
    assert "restart" not in _log(env, "DOCKER_LOG")


# ---- scripts/term_changeover.sh, fourth stage -------------------------------

TERM_SSH_STUB = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
while args and args[0] == "-o":
    args = args[2:]
command = " ".join(args[1:])
state = os.environ["TERM_STATE"]
def event(text):
    with open(os.environ["EVENT_LOG"], "a") as log:
        log.write(text + "\\n")
def flag(name):
    return os.path.exists(os.path.join(state, name))
def set_flag(name):
    open(os.path.join(state, name), "w").close()
reads_sql = "exec mysql " in command
sql = sys.stdin.read() if reads_sql else ""
out = sys.stdout.write
if "python3 -c" in command:
    out("to remove: 0 users\\n")
elif "base64 -w0 && printf" in command:
    out(os.environ["USERS_B64"] + "\\n" + os.environ["KEYS_B64"] + "\\n")
elif "base64 -w0 students_with_keys.csv" in command:
    out(os.environ["KEYS_B64"])
elif "sha256sum" in command:
    out(os.environ["DESIRED_HASH"])
elif "mysqldump" in command:
    event("DB BACKUP")
elif "restart vm-automation" in command:
    event("PORTAL RESTART")
    if flag("purged"):
        set_flag("seeded")
elif "docker inspect --format" in command:
    out("healthy")
elif "DELETE FROM" in sql:
    event("PURGE")
    set_flag("purged")
elif "SELECT login_id, sis_user_id" in sql:
    event("WHITELIST READ")
    out("student1\\t1001\\tfake-key\\n" if flag("seeded") else "olduser\\t999\\told-key\\n")
elif "SELECT (SELECT COUNT(*)" in sql:
    out("0\\t0\\t1\\t0\\t0\\t0\\t0\\n")
"""
POLICY_STAGE_STUB = f"""#!/usr/bin/env bash
printf 'STAGE INPUTS base=%s file=%s\\n' "${{HEADSCALE_BASE_POLICY-unset}}" \\
    "${{HEADSCALE_POLICY_FILE-unset}}" >> "$EVENT_LOG"
printf 'POLICY [%s] SITE_ENV_FILE=%s\\n' "$*" "$SITE_ENV_FILE" >> "$EVENT_LOG"
printf 'Headscale policy: stub\\n{POLICY_FILE_CURRENT}\\n'
"""


def _term_rig(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "data").mkdir()
    for name in ("term_changeover.sh", "site_env.sh", "build_students_merged.py", "purge_roster.sql"):
        shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
    _executable(repo / "scripts" / "apply_headscale_policy.sh", POLICY_STAGE_STUB)
    config = tmp_path / "site.env"
    config.write_text("\n".join([
        "PORTAL_HOST=portal-host",
        "PORTAL_DIR=/opt/lab-playground",
        "PORTAL_COMPOSE=docker-compose.prod.yml",
        "PORTAL_APP_CONTAINER=lab-playground",
        "HEADSCALE_HOST=headscale-host",
        "HEADSCALE_DIR=/opt/headscale",
        "HEADSCALE_KEEP_USER=keep-user",
        "HEADSCALE_STAFF=staff-one",
        "EMAIL_DOMAIN=example.edu",
        "REMOTE_BACKUP_DIR=/var/backups/lab-playground",
        "KEY_DAYS=45d",
        "HEADSCALE_BASE_POLICY=config/site-acl.json",
        "HEADSCALE_POLICY_FILE=/etc/headscale/site-acl.json",
    ]) + "\n")
    roster = tmp_path / "roster.csv"
    roster.write_text("SIS User ID,SIS Login ID\n1001,student1\n")
    # The merged roster both hosts already hold, so stages 1-2 have nothing to do.
    merged = io.StringIO(newline="")
    writer = csv.writer(merged)
    writer.writerow(["SIS User ID", "SIS Login ID", "email", "headscale_key"])
    writer.writerow(["1001", "student1", "student1@example.edu", "fake-key"])
    (repo / "data" / "students_merged.csv").write_bytes(merged.getvalue().encode())

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "ssh", TERM_SSH_STUB)
    # GNU `base64 -w0` semantics (no trailing newline), which the script assumes
    # and the macOS base64 does not always honour.
    _executable(bin_dir / "base64", "#!/usr/bin/env python3\n"
                "import base64, sys\n"
                "assert sys.argv[1:] == ['-w0'], sys.argv\n"
                "sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())\n")
    (tmp_path / "state").mkdir()
    env = _clean_env(
        tmp_path,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SITE_ENV_FILE=str(config),
        TERM_STATE=str(tmp_path / "state"),
        EVENT_LOG=str(tmp_path / "events.log"),
        USERS_B64=base64.b64encode(b'[{"name":"keep-user"},{"name":"student1"}]').decode(),
        KEYS_B64=base64.b64encode(b"student1,student1@example.edu,fake-key\n").decode(),
        DESIRED_HASH=hashlib.sha256(merged.getvalue().encode()).hexdigest(),
    )
    return repo, env, config, roster


def _events(env):
    return Path(env["EVENT_LOG"]).read_text().splitlines()


# Catches the policy stage running before the database stage it depends on.
def test_term_changeover_dry_run_ends_with_the_policy_dry_run(tmp_path):
    repo, env, config, roster = _term_rig(tmp_path)
    result = subprocess.run(["bash", str(repo / "scripts" / "term_changeover.sh"), str(roster)],
                            env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    events = _events(env)
    assert events[-1] == f"POLICY [] SITE_ENV_FILE={config}"
    assert events.count(events[-1]) == 1
    assert "WHITELIST READ" in events[:-1]
    assert "PURGE" not in events
    stages = [line for line in result.stdout.splitlines() if re.match(r"\[\d/\d\]", line)]
    assert stages == ["[1/4] Headscale roster", "[2/4] Mounted roster file",
                      "[3/4] Production database", "[4/4] Headscale policy"]
    assert POLICY_FILE_CURRENT in result.stdout.splitlines()
    assert "No changes were made" in result.stdout


# Catches the orchestrator no longer recognising the policy stage's
# "file already matches" line, so a finished changeover never reads as done.
def test_term_changeover_dry_run_of_a_finished_changeover_reports_complete(tmp_path):
    repo, env, config, roster = _term_rig(tmp_path)
    (Path(env["TERM_STATE"]) / "seeded").touch()
    result = subprocess.run(["bash", str(repo / "scripts" / "term_changeover.sh"), str(roster)],
                            env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert "Changeover complete. Every stage has nothing to do." in result.stdout


# Catches the policy being applied from the outgoing roster, before the purge and reseed.
def test_term_changeover_applies_the_policy_after_the_new_roster_is_seeded(tmp_path):
    repo, env, config, roster = _term_rig(tmp_path)
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "term_changeover.sh"), "--apply", str(roster)],
        env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    events = _events(env)
    policy = [i for i, line in enumerate(events) if line.startswith("POLICY")]
    assert [events[i] for i in policy] == [f"POLICY [--apply] SITE_ENV_FILE={config}"]
    assert events.index("PURGE") < events.index("PORTAL RESTART") < policy[0]
    last_read = max(i for i, line in enumerate(events) if line == "WHITELIST READ")
    assert last_read < policy[0]
    assert "verified: the whitelist has the exact 1 rows." in result.stdout
    assert result.stdout.index("[3/4] Production database") < result.stdout.index("[4/4] Headscale policy")


# Catches site.env overwriting the operator's exported policy paths, so the
# changeover installs the policy somewhere the operator did not choose.
@pytest.mark.parametrize("exported", [True, False])
def test_term_changeover_policy_stage_keeps_exported_policy_paths(tmp_path, exported):
    repo, env, config, roster = _term_rig(tmp_path)
    if exported:
        env.update(HEADSCALE_BASE_POLICY="/operator/base.json",
                   HEADSCALE_POLICY_FILE="/etc/headscale/operator.json")
        expected = "STAGE INPUTS base=/operator/base.json file=/etc/headscale/operator.json"
    else:
        expected = "STAGE INPUTS base=config/site-acl.json file=/etc/headscale/site-acl.json"
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "term_changeover.sh"), "--apply", str(roster)],
        env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert [line for line in _events(env) if line.startswith("STAGE INPUTS")] == [expected]
