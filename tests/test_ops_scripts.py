"""Operator scripts must reject incomplete site config before touching hosts."""

import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TERM_CHANGEOVER = ROOT / "scripts" / "term_changeover.sh"
HEADSCALE_CHANGEOVER = ROOT / "scripts" / "headscale_term_changeover.sh"
JUMPBOX = ROOT / "scripts" / "configure_jumpbox.sh"
STUDENT_ACCOUNTS = ROOT / "scripts" / "create_student_accounts.sh"
JUMPBOX_LOCAL = ROOT / "scripts" / "create_jumpbox_local.sh"

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


def _clean_env(tmp_path: Path, **values: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in set(TERM_REQUIRED + HEADSCALE_REQUIRED):
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
    for name in ("term_changeover.sh", "site_env.sh", "build_students_merged.py", "purge_roster.sql"):
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
    assert "644" not in delivery
    assert delivery.index("chmod 640") < delivery.index("mv '")



def test_jumpbox_accounts_get_unique_random_passwords_in_private_file(tmp_path):
    config = tmp_path / "site.env"
    config.write_text("HEADSCALE_URL=https://vpn.example.edu\n")
    students = tmp_path / "students.txt"
    students.write_text("alice\nbob\n")
    creds = tmp_path / "creds.csv"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_log = tmp_path / "ssh.log"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "printf 'ARGS: %s\\n' \"$*\" >> \"$SSH_LOG\"\n"
        "for last; do :; done\n"
        "case \"$last\" in *@192.0.2.50) script_on_stdin=1 ;; esac\n"  # no remote command
        "if [ -n \"$script_on_stdin\" ]; then\n"
        "  input=$(cat)\n"
        "  printf '%s\\n' \"$input\" >> \"$SSH_LOG\"\n"
        "  case \"$input\" in *useradd*) printf 'CREATED\\n' ;; esac\n"
        "fi\n"
        "exit 0\n"
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)

    env = _clean_env(
        tmp_path,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SITE_ENV_FILE=str(config),
        SSH_LOG=str(ssh_log),
        STUDENTS_FILE=str(students),
        JUMPBOX_CREDENTIALS_FILE=str(creds),
    )
    result = subprocess.run(
        ["bash", str(JUMPBOX), "192.0.2.50"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        input="\n",  # answers the manual Headscale-registration pause
    )

    sent = ssh_log.read_text()
    legacy_password = "change" + "me123"  # split so the literal never appears in the repo
    assert legacy_password not in sent + result.stdout + result.stderr
    assert "chage -d 0" in sent
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    rows = [line.split(",") for line in creds.read_text().splitlines()]
    assert [r[0] for r in rows] == ["alice", "bob"]
    passwords = [r[1] for r in rows]
    assert len(set(passwords)) == 2 and all(len(p) >= 12 for p in passwords)
    for password in passwords:
        assert password in sent  # delivered to the remote side via stdin
        assert password not in result.stdout + result.stderr
        assert not any(
            password in line for line in sent.splitlines() if line.startswith("ARGS:")
        )
    assert str(creds) in result.stdout


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


def test_create_student_accounts_uses_unique_random_passwords_via_stdin(tmp_path):
    csv = tmp_path / "students.csv"
    csv.write_text("SIS User ID,SIS Login ID\n100000001,alice\n100000002,bob\n")
    creds = tmp_path / "creds.csv"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    stdin_log = tmp_path / "chpasswd.stdin"
    fakes = {
        "id": 'if [ "$1" = "-u" ]; then echo 0; exit 0; fi\nexit 1\n',
        "useradd": 'printf "useradd %s\\n" "$*" >> "$CALL_LOG"\n',
        "chage": 'printf "chage %s\\n" "$*" >> "$CALL_LOG"\n',
        "chpasswd": (
            'printf "chpasswd %s\\n" "$*" >> "$CALL_LOG"\n'
            'cat >> "$STDIN_LOG"\n'
        ),
    }
    for name, body in fakes.items():
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\n" + body)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CALL_LOG=str(log),
        STDIN_LOG=str(stdin_log),
        JUMPBOX_CREDENTIALS_FILE=str(creds),
    )
    result = subprocess.run(
        ["bash", str(STUDENT_ACCOUNTS), str(csv)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    legacy = "Change" + "Me2026!"  # split so the literal never appears in the repo
    calls = log.read_text()
    assert legacy not in calls + result.stdout + result.stderr
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    rows = [line.split(",") for line in creds.read_text().splitlines()]
    assert [r[0] for r in rows] == ["alice", "bob"]
    passwords = [r[1] for r in rows]
    assert len(set(passwords)) == 2 and all(len(p) >= 12 for p in passwords)
    sent = stdin_log.read_text().splitlines()
    assert sent == [f"alice:{passwords[0]}", f"bob:{passwords[1]}"]
    for password in passwords:
        assert password not in calls  # never on argv
        assert password not in result.stdout + result.stderr
    assert "chage -d 0 alice" in calls and "chage -d 0 bob" in calls
    assert str(creds) in result.stdout


def test_legacy_shared_student_password_is_gone_from_repo():
    pattern = "Change" + "Me2026"
    found = subprocess.run(
        ["git", "grep", "-l", "-i", pattern, "--", ".", ":!tests/test_ops_scripts.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert found.stdout == ""


def test_jumpbox_local_does_not_advertise_default_ubuntu_login():
    text = JUMPBOX_LOCAL.read_text()
    assert "ubuntu/ubuntu" not in text
