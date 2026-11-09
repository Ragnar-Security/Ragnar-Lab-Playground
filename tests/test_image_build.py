"""The image builder uploads a payload (setup, verify, tarballs) into a build VM
and seals it before the image is saved. A sealed image must carry none of the
payload, no usable build key, and no identity shared with the build VM."""

import os
import re
import shlex
import signal
import subprocess
import tarfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_BUILD = ROOT / "scripts" / "image_build"
SEAL_LINUX = IMAGE_BUILD / "seal_linux.sh"

LOG_CALL = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$CALL_LOG"\n'


# systemctl <verb> <unit>: logged. A unit in FAKE_SYSTEMCTL_MISSING does not
# exist (disable fails as systemd 255 does); one in FAKE_SYSTEMCTL_FAIL fails
# for another reason.
FAKE_SYSTEMCTL = LOG_CALL + (
    'case " ${FAKE_SYSTEMCTL_MISSING:-} " in *" $2 "*)\n'
    '  [ "$1" = disable ] && { echo "Failed to disable unit: Unit file $2 does not exist." >&2; exit 1; } ;;\n'
    'esac\n'
    'case " ${FAKE_SYSTEMCTL_FAIL:-} " in *" $2 "*)\n'
    '  echo "Failed to $1 unit: Access denied" >&2; exit 1 ;;\n'
    'esac\n'
)


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _seal_env(tmp_path: Path) -> dict:
    """A temp root laid out like a freshly built VM, plus logging fakes for the
    commands that act on the live system."""
    root = tmp_path / "root"
    for rel in ("tmp/image-build/sub", "home/builder/.ssh", "home/other", "root", "etc/ssh"):
        (root / rel).mkdir(parents=True)
    (root / "tmp/image-build/images.tar").write_text("tarball")
    (root / "tmp/image-build/sub/verify.sh").write_text("solve steps")
    (root / "home/builder/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA build-key\n")
    (root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    (root / "etc/ssh/ssh_host_ed25519_key").write_text("private")
    (root / "etc/ssh/ssh_host_ed25519_key.pub").write_text("public")
    (root / "etc/ssh/sshd_config").write_text("keep me")
    (root / "root/.bash_history").write_text("secret command\n")
    (root / "home/other/.bash_history").write_text("another secret\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("passwd", "cloud-init"):
        _tool(bin_dir, name, LOG_CALL)
    _tool(bin_dir, "systemctl", FAKE_SYSTEMCTL)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CALL_LOG=str(tmp_path / "calls.log"),
        SEAL_ROOT=str(root),
    )
    return env


def _seal(tmp_path: Path, env: dict, keep_ssh: str):
    return subprocess.run(
        ["bash", str(SEAL_LINUX), keep_ssh] if keep_ssh is not None else ["bash", str(SEAL_LINUX)],
        env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )


def _calls(tmp_path: Path) -> str:
    log = tmp_path / "calls.log"
    return log.read_text() if log.exists() else ""


# --- seal_linux.sh ----------------------------------------------------------

def test_seal_linux_removes_payload_and_key(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    assert not (root / "tmp/image-build").exists()
    assert (root / "home/builder/.ssh/authorized_keys").read_text() == ""
    assert (root / "etc/machine-id").read_text() == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))
    assert (root / "etc/ssh/sshd_config").read_text() == "keep me"
    assert (root / "root/.bash_history").read_text() == ""
    assert (root / "home/other/.bash_history").read_text() == ""
    calls = _calls(tmp_path)
    assert "passwd -l builder" in calls
    assert "cloud-init clean --logs" in calls


def test_seal_linux_ssh_off_includes_socket(tmp_path):
    # Ubuntu >= 22.10: ssh.socket owns port 22, so disabling ssh.service alone
    # leaves sshd reachable. Both units are disabled and masked, and the
    # marker both Debian/Ubuntu units honour is written.
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path).splitlines()
    for unit in ("ssh.service", "ssh.socket"):
        assert f"systemctl disable {unit}" in calls, calls
        assert f"systemctl mask {unit}" in calls, calls
    assert (root / "etc/ssh/sshd_not_to_be_run").is_file()


def test_seal_linux_ssh_off_tolerates_missing_unit_only(tmp_path):
    # A base without ssh.socket (pre-22.10) still seals.
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    env["FAKE_SYSTEMCTL_MISSING"] = "ssh.socket"
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path).splitlines()
    assert "systemctl mask ssh.service" in calls, calls
    assert "systemctl mask ssh.socket" not in calls, calls
    assert (root / "etc/ssh/sshd_not_to_be_run").is_file()
    # Any other failure fails the seal, so the image is not saved.
    for unit in ("ssh.socket", "ssh.service"):
        env = _seal_env(tmp_path / unit)
        env["FAKE_SYSTEMCTL_FAIL"] = unit
        result = _seal(tmp_path / unit, env, "no")
        assert result.returncode != 0, unit
        assert unit in result.stderr, (unit, result.stderr)


def test_seal_linux_keep_ssh(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "yes")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert "systemctl" not in calls
    assert not (root / "etc/ssh/sshd_not_to_be_run").exists()
    # The account is locked either way: key login still works under UsePAM.
    assert "passwd -l builder" in calls
    assert (root / "home/builder/.ssh/authorized_keys").read_text() == ""
    assert not (root / "tmp/image-build").exists()
    assert (root / "etc/machine-id").read_text() == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))


def test_seal_linux_installs_hostkey_regen_dropin(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 0, result.stderr
    dropin = root / "etc/systemd/system/ssh.service.d/10-regen-hostkeys.conf"
    assert dropin.is_file()
    # The unit's own `ExecStartPre=/usr/sbin/sshd -t` fails without host keys,
    # so the list is reset and the keys are made before sshd checks them.
    assert dropin.read_text().splitlines() == [
        "[Service]",
        "ExecStartPre=",
        "ExecStartPre=/usr/bin/ssh-keygen -A",
        "ExecStartPre=/usr/sbin/sshd -t",
    ]


def test_seal_linux_fails_if_payload_survives(tmp_path):
    env = _seal_env(tmp_path)
    _tool(Path(env["PATH"].split(":")[0]), "rm", "exit 0\n")
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 1
    assert "image-build" in result.stderr


def test_seal_linux_fails_if_stray_payload_file_survives(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    (root / "home/other/setup.sh").write_text("stray")
    result = _seal(tmp_path, env, "no")
    assert result.returncode == 1
    assert "setup.sh" in result.stderr


def test_seal_linux_rejects_bad_keep_ssh(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    for value in ("maybe", "YES", "true", ""):
        result = _seal(tmp_path, env, value)
        assert result.returncode == 2, value
        assert "keep_ssh" in result.stderr
    result = _seal(tmp_path, env, None)
    assert result.returncode == 2
    # Nothing was touched.
    assert (root / "tmp/image-build").exists()
    assert _calls(tmp_path) == ""


# --- build_image.sh ---------------------------------------------------------

BUILD = IMAGE_BUILD / "build_image.sh"
FAKE_BACKENDS = ROOT / "tests" / "fixtures" / "image_build" / "backends"
KEY_MATERIAL = "FAKE-PRIVATE-KEY-MATERIAL"
FULL_SHA = "abc1234def5678901234567890abcdef12345678"

BASE_RECIPE = {
    "IMAGE_PREFIX": "enpm634-privesc-linux",
    "OS_FAMILY": "linux",
    "PAYLOAD_DIR": "payload",
    "SETUP": "setup.sh",
    "VERIFY": "verify.sh",
    "BASE_PROXMOX": "base-ubuntu-2404-*",
    "BASE_AWS": "self:base-ubuntu-2404-*",
    "BASE_GCP": "ubuntu-os-cloud/ubuntu-2404-lts-amd64",
    "BASE_FAKE": "fake-base-*",  # the recipe's BASE_<BACKEND>
    "DISK_GB": "20",
    "AWS_INSTANCE_TYPE": "t3.small",
    "GCP_MACHINE_TYPE": "e2-small",
    "PVE_MEMORY_MB": "2048",
    "PVE_CORES": "2",
}

# The build VM. Every remote command line is kept intact (NUL-separated) and,
# with the boot it ran in, in $CALL_LOG.timeline. A boot-ID read prints
# boot-<n>. A reboot command (or one matching FAKE_SSH_REBOOT_ON) makes the
# boot ID change, but only after FAKE_BOOT_FLIP_AFTER more reads (default 0):
# a VM still going down answers with its old boot.
FAKE_SSH = LOG_CALL + (
    'for a in "$@"; do last="$a"; done; printf "%s\\0" "$last" >> "$CALL_LOG.remote"\n'
    'boot="$CALL_LOG.boot"; pending="$CALL_LOG.reboot-pending"\n'
    '[ -f "$boot" ] || echo 1 > "$boot"\n'
    'case "$last" in *boot_id*|*LastBootUpTime*)\n'
    '  if [ -f "$pending" ]; then\n'
    '    n=$(cat "$pending")\n'
    '    if [ "$n" -le 0 ]; then echo $(( $(cat "$boot") + 1 )) > "$boot"; rm -f "$pending"\n'
    '    else echo $((n - 1)) > "$pending"; fi\n'
    '  fi ;;\n'
    'esac\n'
    'printf "boot-%s\\t%s\\n" "$(cat "$boot")" "$last" >> "$CALL_LOG.timeline"\n'
    'case "$last" in *"systemctl reboot"*|*Restart-Computer*|*"${FAKE_SSH_REBOOT_ON:-@none@}"*)\n'
    '  echo "${FAKE_BOOT_FLIP_AFTER:-0}" > "$pending" ;;\n'
    'esac\n'
    'case "$last" in *boot_id*|*LastBootUpTime*) echo "boot-$(cat "$boot")"; exit 0 ;; esac\n'
    'case "$*" in *"${FAKE_SSH_FAIL_ON:-@none@}"*) exit "${FAKE_SSH_FAIL_RC:-1}" ;; esac\n'
    'case "$*" in *"${FAKE_SSH_SLEEP_ON:-@none@}"*) exec sleep 30 ;; esac\n'
    "exit 0\n"
)
# Copies the uploaded file (second-to-last argument) into $SCP_SINK.
FAKE_SCP = LOG_CALL + (
    'n=0; for a in "$@"; do n=$((n + 1)); [ "$n" -eq $(($# - 1)) ] && src="$a"; done\n'
    'mkdir -p "$SCP_SINK" && cp "$src" "$SCP_SINK/"\n'
)
FAKE_KEYGEN = LOG_CALL + (
    'while [ $# -gt 0 ]; do [ "$1" = -f ] && f="$2"; shift; done\n'
    f"printf '{KEY_MATERIAL}\\n' > \"$f\"\n"
    "printf 'ssh-ed25519 AAAAC3FAKEPUBLIC image-build\\n' > \"$f.pub\"\n"
)
FAKE_GIT = LOG_CALL + (
    '[ "$1" = -C ] && shift 2\n'
    'case "$*" in\n'
    '  "rev-parse --show-toplevel") echo "$FAKE_GIT_TOPLEVEL" ;;\n'
    '  "rev-parse --show-prefix") echo "$FAKE_GIT_PREFIX" ;;\n'
    f'  "rev-parse HEAD") echo {FULL_SHA} ;;\n'
    '  "status --porcelain"*) [ -n "${FAKE_GIT_STATUS:-}" ] && printf "%s\\n" "$FAKE_GIT_STATUS"; exit 0 ;;\n'
    '  *) echo "fake git: unexpected: $*" >&2; exit 1 ;;\n'
    "esac\n"
)
FAKE_CLOUD = LOG_CALL + (
    'case "$1" in\n'
    "  resolve) echo base-1234 ;;\n"
    '  leftovers) for id in ${FAKE_LEFTOVERS:-}; do echo "$id"; done ;;\n'
    # The tag a reaper re-reads: anything in FAKE_UNTAGGED is not a build VM.
    '  tag) case " ${FAKE_UNTAGGED:-} " in *" $2 "*) echo "owner=someone-else" ;;\n'
    '       *) echo "image-build=ib-202601010000-abcd" ;; esac ;;\n'
    "  fail) exit 1 ;;\n"
    "esac\n"
)


def _build_env(tmp_path: Path, os_family: str = "linux", drop=(), **overrides):
    """A course repo holding one recipe and its payload, the fake backend, and
    logging fakes for every command that reaches a build VM or a cloud."""
    repo = tmp_path / "course"
    lab = repo / "labs" / "demo"
    payload = lab / "payload"
    (payload / "packer").mkdir(parents=True)
    (payload / "packer" / "template.pkr.hcl").write_text("packer")
    (payload / "disk.iso").write_text("iso")
    recipe = dict(BASE_RECIPE)
    if os_family == "windows":
        recipe.update(OS_FAMILY="windows", SETUP="setup.ps1", VERIFY="verify.ps1",
                      IMAGE_PREFIX="enpm634-privesc-windows")
    recipe.update(overrides)
    for key in drop:
        recipe.pop(key)
    # The payload holds the OS's default scripts; an overridden SETUP/VERIFY
    # names a file that does not exist.
    ext = "ps1" if os_family == "windows" else "sh"
    for name in (f"setup.{ext}", f"verify.{ext}"):
        (payload / name).write_text("echo step\n")
    recipe_path = lab / "recipe.env"
    recipe_path.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in recipe.items()))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", FAKE_SSH), ("scp", FAKE_SCP), ("ssh-keygen", FAKE_KEYGEN),
                       ("git", FAKE_GIT), ("fake-cloud", FAKE_CLOUD)):
        _tool(bin_dir, name, body)
    config = tmp_path / "config.env"
    config.write_text("FAKE_REGION='test-1'\n")
    (tmp_path / "tmp").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IMAGE_BUILD_", "FAKE_"))}
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CALL_LOG=str(tmp_path / "calls.log"),
        IMAGE_BUILD_CONFIG=str(config),
        IMAGE_BUILD_BACKENDS_DIR=str(FAKE_BACKENDS),
        FAKE_GIT_TOPLEVEL=str(repo),
        FAKE_GIT_PREFIX="labs/demo/",
        TMPDIR=str(tmp_path / "tmp"),
        SCP_SINK=str(tmp_path / "sink"),
    )
    return env, recipe_path


def _build(env: dict, *args):
    return subprocess.run(
        ["bash", str(BUILD), *[str(a) for a in args]],
        env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )


def _build_log(tmp_path: Path) -> Path:
    return tmp_path / "course" / "coursework" / "deploy" / "image-build" / "BUILD-LOG.tsv"


def _events(calls: str, setup: str, verify: str, seal: str) -> list:
    """Reduce CALL_LOG to the build's steps: backend calls, the payload upload,
    and the remote setup/verify/seal commands."""
    steps = []
    for line in calls.splitlines():
        word = line.split(" ", 1)[0]
        if word in ("backend_check_config", "backend_resolve_base", "backend_start",
                    "backend_save", "backend_destroy"):
            steps.append(word[len("backend_"):])
        elif word == "scp" and "payload.tgz" in line:
            steps.append("upload")
        elif word == "ssh" and seal in line:
            steps.append("seal")
        elif word == "ssh" and setup in line:
            steps.append("setup")
        elif word == "ssh" and verify in line:
            steps.append("verify")
    return steps


def _remote_commands(calls: str, tool: str = "ssh") -> list:
    return [line for line in calls.splitlines() if line.startswith(tool + " ")]


def test_dry_run_runs_no_mutation(tmp_path):
    env, recipe = _build_env(tmp_path)
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert _remote_commands(calls, "ssh") == []
    assert _remote_commands(calls, "scp") == []
    assert _remote_commands(calls, "fake-cloud") == []
    for printed in ("DRY-RUN: ssh ", "DRY-RUN: scp ", "DRY-RUN: tar ",
                    "DRY-RUN: fake-cloud start", "DRY-RUN: fake-cloud save",
                    "DRY-RUN: fake-cloud destroy", "DRY-RUN: fake-cloud resolve"):
        assert printed in result.stderr, printed
    # The same steps as a real build, in the same order.
    assert _events(calls, "setup.sh", "verify.sh", "seal_linux.sh") == [
        "check_config", "resolve_base", "start", "save", "destroy"]
    assert "sudo bash /tmp/image-build/seal_linux.sh no" in result.stderr
    # The boot-ID read and the reboot a setup exit 100 would trigger are shown.
    assert "DRY-RUN: ssh " in result.stderr and "cat /proc/sys/kernel/random/boot_id" in result.stderr
    assert "nohup sudo systemctl reboot" in result.stderr
    assert not _build_log(tmp_path).exists()
    assert result.stdout.rstrip("\n").splitlines()[-1].startswith("fake: {image: ")


def test_order_of_operations(tmp_path):
    env, recipe = _build_env(tmp_path, SETUP_PASSES="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert _events(calls, "setup.sh", "verify.sh", "seal_linux.sh") == [
        "check_config", "resolve_base", "start", "upload", "setup", "setup",
        "verify", "seal", "save", "destroy"]
    # Setup exited 0: no reboot.
    assert "reboot" not in "".join(_remote_lines(tmp_path))
    lines = _build_log(tmp_path).read_text().splitlines()
    assert lines[0] == "date\tbackend\timage_id\timage_name\tbase_id\trecipe\tcommit\tverify"
    assert len(lines) == 2
    date, backend, image_id, image_name, base_id, recipe_rel, commit, verify = lines[1].split("\t")
    assert backend == "fake"
    assert image_id == f"img-{image_name}"
    assert image_name.startswith("enpm634-privesc-linux-abc1234-")
    assert base_id == "base-1234"
    assert "backend_resolve_base fake-base-*" in calls
    assert "backend_start ib-" in calls and " base-1234 " in calls
    assert recipe_rel == "labs/demo/recipe.env"
    assert commit == "abc1234"
    assert verify == "pass"
    assert date.endswith("Z")
    # Stdout ends with the templates.yaml lines.
    assert result.stdout.rstrip("\n").splitlines()[-1] == f"fake: {{image: {image_id}, disk_gb: 20}}"
    # The per-run key's path may appear; its material never does.
    for text in (result.stdout, result.stderr, calls, _build_log(tmp_path).read_text()):
        assert KEY_MATERIAL not in text


def test_ssh_options_and_jump(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_JUMP"] = "root@203.0.113.5"
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    for line in _remote_commands(calls, "ssh") + _remote_commands(calls, "scp"):
        for opt in ("-o StrictHostKeyChecking=no", "-o UserKnownHostsFile=/dev/null",
                    "-o ConnectTimeout=10", "-J root@203.0.113.5", "-i "):
            assert opt in line, (opt, line)
    env2, recipe2 = _build_env(tmp_path / "nojump")
    result = _build(env2, recipe2, "fake")
    assert result.returncode == 0, result.stderr
    assert all(" -J " not in line for line in _remote_commands(_calls(tmp_path / "nojump")))


def test_failed_verify_destroys_and_saves_nothing(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"
    result = _build(env, recipe, "fake")
    assert result.returncode == 1
    steps = _events(_calls(tmp_path), "setup.sh", "verify.sh", "seal_linux.sh")
    assert "destroy" in steps
    assert "save" not in steps
    assert "seal" not in steps
    assert not _build_log(tmp_path).exists()


def test_failed_seal_saves_nothing(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_SSH_FAIL_ON"] = "seal_linux.sh"
    result = _build(env, recipe, "fake")
    assert result.returncode == 1
    steps = _events(_calls(tmp_path), "setup.sh", "verify.sh", "seal_linux.sh")
    assert steps[-1] == "destroy"
    assert "save" not in steps
    assert not _build_log(tmp_path).exists()


def test_failed_save_destroys(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_FAIL"] = "backend_save"
    result = _build(env, recipe, "fake")
    assert result.returncode == 1
    assert "backend_destroy vm-" in _calls(tmp_path)
    assert not _build_log(tmp_path).exists()


def _fast_sleep(tmp_path: Path) -> None:
    """The core's 10 s poll sleep, shortened for the reboot-wait tests."""
    _tool(tmp_path / "bin", "sleep", LOG_CALL + "PATH=/usr/bin:/bin exec sleep 0.1\n")


def _timeline(tmp_path: Path) -> list:
    """(boot, remote command line) for every SSH command, in order."""
    log = tmp_path / "calls.log.timeline"
    return [tuple(l.split("\t", 1)) for l in log.read_text().splitlines()] if log.exists() else []


def _is_boot_read(command: str) -> bool:
    return "boot_id" in command or "LastBootUpTime" in command


def test_setup_exit_100_core_reboots_and_waits_for_new_boot(tmp_path):
    for os_family, setup, verify, reboot in (
            ("linux", "setup.sh", "verify.sh", "nohup sudo systemctl reboot"),
            ("windows", "setup.ps1", "verify.ps1", "Restart-Computer -Force")):
        sub = tmp_path / os_family
        env, recipe = _build_env(sub, os_family)
        _fast_sleep(sub)
        # The VM keeps answering with its old boot ID for three polls.
        env.update(FAKE_SSH_FAIL_ON=setup, FAKE_SSH_FAIL_RC="100", FAKE_BOOT_FLIP_AFTER="3")
        result = _build(env, recipe, "fake")
        assert result.returncode == 0, result.stderr
        timeline = _timeline(sub)
        commands = [c for _b, c in timeline]
        i_setup = next(i for i, c in enumerate(commands) if setup in c)
        i_reboot = next(i for i, c in enumerate(commands) if reboot in c)
        i_verify = next(i for i, c in enumerate(commands) if verify in c)
        assert i_setup < i_reboot < i_verify, commands
        # The setup did not reboot; the core did, once.
        assert sum(reboot in c for c in commands) == 1, commands
        # Verify ran only in the new boot, after the polls that still saw the old one.
        assert timeline[i_verify][0] == "boot-2", timeline
        polls = [b for b, c in timeline[i_reboot + 1:i_verify] if _is_boot_read(c)]
        assert polls[:3] == ["boot-1"] * 3 and "boot-2" in polls, timeline
        assert all(b == "boot-1" for b, _c in timeline[:i_reboot + 1]), timeline


def test_setup_session_drop_requires_a_new_boot(tmp_path):
    # Exit 255 (session dropped) and the VM rebooted: the build goes on.
    env, recipe = _build_env(tmp_path / "rebooted")
    _fast_sleep(tmp_path / "rebooted")
    env.update(FAKE_SSH_FAIL_ON="setup.sh", FAKE_SSH_FAIL_RC="255", FAKE_SSH_REBOOT_ON="setup.sh",
               FAKE_BOOT_FLIP_AFTER="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    timeline = _timeline(tmp_path / "rebooted")
    verify = [b for b, c in timeline if "verify.sh" in c]
    assert verify == ["boot-2"], timeline
    assert not any("systemctl reboot" in c for _b, c in timeline)

    # Exit 255 but the boot ID never changes: the session dropped without a
    # reboot, so the build stops when the wait runs out.
    env, recipe = _build_env(tmp_path / "same")
    _fast_sleep(tmp_path / "same")
    env.update(FAKE_SSH_FAIL_ON="setup.sh", FAKE_SSH_FAIL_RC="255", IMAGE_BUILD_SSH_WAIT_SECONDS="2")
    result = _build(env, recipe, "fake")
    assert result.returncode == 1, result.stderr
    assert "SSH dropped during setup but the VM did not reboot" in result.stderr
    steps = _events(_calls(tmp_path / "same"), "setup.sh", "verify.sh", "seal_linux.sh")
    assert "verify" not in steps and "save" not in steps
    assert steps[-1] == "destroy"

    # Any other exit code fails the build at once.
    env2, recipe2 = _build_env(tmp_path / "fail")
    env2.update(FAKE_SSH_FAIL_ON="setup.sh", FAKE_SSH_FAIL_RC="3")
    result = _build(env2, recipe2, "fake")
    assert result.returncode == 1
    steps = _events(_calls(tmp_path / "fail"), "setup.sh", "verify.sh", "seal_linux.sh")
    assert "verify" not in steps and "save" not in steps
    assert steps[-1] == "destroy"


def test_ssh_wait_seconds_checked(tmp_path):
    for i, bad in enumerate(("0", "abc", "-5", "08")):
        env, recipe = _build_env(tmp_path / str(i))
        env["IMAGE_BUILD_SSH_WAIT_SECONDS"] = bad
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, bad
        assert "IMAGE_BUILD_SSH_WAIT_SECONDS" in result.stderr


def test_exit_2_only_before_any_vm_started(tmp_path):
    # A backend step that exits 2 once the build is under way is a build
    # failure (1), not a rejected input.
    for fn in ("backend_resolve_base", "backend_start", "backend_save"):
        env, recipe = _build_env(tmp_path / fn)
        env.update(FAKE_FAIL=fn, FAKE_FAIL_RC="2")
        result = _build(env, recipe, "fake")
        assert result.returncode == 1, (fn, result.stderr)
    # A config the backend rejects is still 2.
    env, recipe = _build_env(tmp_path / "check")
    env.update(FAKE_FAIL="backend_check_config", FAKE_FAIL_RC="2")
    assert _build(env, recipe, "fake").returncode == 2


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text() + text)


def test_recipe_cannot_touch_core_state(tmp_path):
    # DRY_RUN=0 in a recipe does not turn a dry run live.
    env, recipe = _build_env(tmp_path / "dry")
    _append(recipe, "DRY_RUN=0\nQUERY_LIVE=1\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path / "dry")
    assert _remote_commands(calls, "fake-cloud") == []
    assert _remote_commands(calls, "ssh") == [] and _remote_commands(calls, "scp") == []
    assert "DRY-RUN: fake-cloud start" in result.stderr

    # STARTED/BUILD_VM_ID from a recipe do not make the trap destroy anything.
    env, recipe = _build_env(tmp_path / "started")
    _append(recipe, "STARTED=1\nBUILD_VM_ID=vm-victim\n")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("# nothing configured\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 2, result.stderr
    calls = _calls(tmp_path / "started")
    assert "backend_destroy" not in calls and "vm-victim" not in calls

    # A recipe cannot replace the core's helpers.
    env, recipe = _build_env(tmp_path / "func")
    _append(recipe, "log() { echo HIJACKED >&2; }\nmutate() { echo HIJACKED >&2; }\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert "HIJACKED" not in result.stderr
    assert "[image-build] run ib-" in result.stderr
    assert "fake-cloud start" in _calls(tmp_path / "func")


def test_recipe_values_round_trip(tmp_path):
    # Spaces, quotes, $ and a glob survive exactly; nothing is split or expanded.
    value = """fake base-* 'q' "dq" $HOME \\ *"""
    env, recipe = _build_env(tmp_path, BASE_FAKE=value, PAYLOAD_EXCLUDE="packer *.iso")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert f"backend_resolve_base {value}\n" in _calls(tmp_path)
    assert f"DRY-RUN: fake-cloud resolve {value}\n" in result.stderr
    tar_line = [l for l in result.stderr.splitlines() if l.startswith("DRY-RUN: tar ")][0]
    assert "--exclude=packer" in tar_line.split() and "--exclude=*.iso" in tar_line.split()


def test_config_cannot_touch_core_state_or_recipe_keys(tmp_path):
    config_ok = "FAKE_REGION='test-1'\n"
    # DRY_RUN=0 in the config does not turn a dry run live.
    env, recipe = _build_env(tmp_path / "dry")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text(config_ok + "DRY_RUN=0\nQUERY_LIVE=1\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path / "dry")
    assert _remote_commands(calls, "fake-cloud") == []
    assert _remote_commands(calls, "ssh") == [] and _remote_commands(calls, "scp") == []

    # STARTED/BUILD_VM_ID from the config do not make the trap destroy anything.
    env, recipe = _build_env(tmp_path / "started")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("STARTED=1\nBUILD_VM_ID=vm-victim\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 2, result.stderr
    calls = _calls(tmp_path / "started")
    assert "backend_destroy" not in calls and "vm-victim" not in calls

    # Only the recipe sets recipe keys: a config that sets one is refused.
    for key, value in (("DISK_GB", "999"), ("KEEP_SSH", "yes"), ("BASE_FAKE", "other-*"),
                       ("AWS_INSTANCE_TYPE", "t3.large")):
        env, recipe = _build_env(tmp_path / key)
        Path(env["IMAGE_BUILD_CONFIG"]).write_text(config_ok + f"{key}={value}\n")
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, (key, result.stderr)
        assert key in result.stderr, (key, result.stderr)
        assert "backend_start" not in _calls(tmp_path / key)

    # The backend's own keys still arrive, exactly.
    env, recipe = _build_env(tmp_path / "value")
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("FAKE_REGION='a b * \"c\"'\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert 'backend_check_config region=a b * "c"' in _calls(tmp_path / "value")


def test_sigterm_destroys(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_SSH_SLEEP_ON"] = "setup.sh"
    out = (tmp_path / "out.txt").open("w")
    proc = subprocess.Popen(["bash", str(BUILD), str(recipe), "fake"], env=env,
                            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while "setup.sh" not in "".join(_remote_commands(_calls(tmp_path))):
            assert proc.poll() is None, (tmp_path / "out.txt").read_text()
            assert time.time() < deadline
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
        out.close()
    assert rc != 0
    steps = _events(_calls(tmp_path), "setup.sh", "verify.sh", "seal_linux.sh")
    assert steps[-1] == "destroy"
    assert "save" not in steps
    assert list((tmp_path / "tmp").iterdir()) == []


def test_dirty_tree_refused(tmp_path):
    env, recipe = _build_env(tmp_path)
    env["FAKE_GIT_STATUS"] = " M labs/demo/payload/setup.sh"
    result = _build(env, recipe, "fake")
    assert result.returncode == 2
    assert "dirty" in result.stderr
    assert "backend_" not in _calls(tmp_path)
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 2
    result = _build(env, "--allow-dirty", recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert _build_log(tmp_path).read_text().splitlines()[1].split("\t")[6] == "abc1234-dirty"


def _git(repo: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        check=True, capture_output=True, text=True).stdout


def test_build_log_alone_does_not_dirty_the_repo(tmp_path):
    # One recipe is built on several backends before the log is committed: the
    # log a build appends must not make the next build's repo "dirty". Real
    # git, so the pathspec is proven against git itself (and from a recipe in
    # a subdirectory, where a cwd-relative pathspec would miss).
    env, recipe = _build_env(tmp_path)
    (tmp_path / "bin" / "git").unlink()
    repo = tmp_path / "course"
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "recipe")
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    log = _build_log(tmp_path)

    def build_ok():
        result = _build(env, recipe, "fake")
        assert result.returncode == 0, result.stderr
        assert log.read_text().splitlines()[-1].split("\t")[6] == sha7

    build_ok()                       # creates the log (untracked)
    build_ok()                       # an untracked log alone is clean
    _git(repo, "add", "coursework")
    _git(repo, "commit", "-q", "-m", "log")
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    build_ok()                       # appends: the log is now modified
    build_ok()                       # a modified log alone is clean

    # Anything else still refuses, even next to the log.
    for other in ("labs/demo/payload/setup.sh", "coursework/deploy/image-build/notes.txt"):
        path = repo / other
        before = path.read_text() if path.exists() else None
        path.write_text("changed\n")
        result = _build(env, recipe, "fake")
        assert result.returncode == 2, (other, result.stderr)
        assert "dirty" in result.stderr
        if before is None:
            path.unlink()
        else:
            path.write_text(before)


def test_recipe_missing_key_refused(tmp_path):
    env, recipe = _build_env(tmp_path, drop=("VERIFY",))
    env["VERIFY"] = "verify.sh"  # an inherited value must not stand in for the recipe's
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 2
    assert "VERIFY" in result.stderr
    assert "backend_" not in _calls(tmp_path)


def test_recipe_bad_values_refused(tmp_path):
    for i, (key, value) in enumerate((("OS_FAMILY", "bsd"), ("KEEP_SSH", "maybe"),
                                      ("SETUP_PASSES", "0"), ("DISK_GB", "big"),
                                      ("SETUP", "missing.sh"), ("PAYLOAD_DIR", "nowhere"),
                                      ("PAYLOAD_DIR", "/etc"), ("SETUP", "../setup.sh"),
                                      ("SETUP", "it's.sh"), ("VERIFY", "/tmp/verify.sh"))):
        env, recipe = _build_env(tmp_path / str(i), **{key: value})
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, (key, result.stderr)
        assert key in result.stderr, (key, result.stderr)


def test_usage_errors(tmp_path):
    env, recipe = _build_env(tmp_path)
    assert _build(env).returncode == 2
    assert _build(env, recipe).returncode == 2
    assert _build(env, "--bogus", recipe, "fake").returncode == 2
    result = _build(env, "--dry-run", recipe, "nosuch")
    assert result.returncode == 2
    assert "nosuch" in result.stderr
    assert _build(env, "--dry-run", recipe, "../fake").returncode == 2


def test_backend_config_missing_refused(tmp_path):
    env, recipe = _build_env(tmp_path)
    Path(env["IMAGE_BUILD_CONFIG"]).write_text("# nothing configured\n")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 2
    assert "FAKE_REGION" in result.stderr
    assert "backend_start" not in _calls(tmp_path)
    env["IMAGE_BUILD_CONFIG"] = str(tmp_path / "absent.env")
    assert _build(env, "--dry-run", recipe, "fake").returncode == 2


def test_image_name_shape(tmp_path):
    env, recipe = _build_env(tmp_path)
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    save = [l for l in _calls(tmp_path).splitlines() if l.startswith("backend_save ")]
    name = save[0].split(" ", 1)[1]
    assert re.fullmatch(r"enpm634-privesc-linux-[0-9a-f]{7}-[0-9]{12}", name), name
    assert len(name) <= 63
    for i, prefix in enumerate(("a" * 43, "Enpm634-upper", "enpm634_underscore")):
        env, recipe = _build_env(tmp_path / str(i), IMAGE_PREFIX=prefix)
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 2, prefix
        assert "backend_start" not in _calls(tmp_path / str(i))
    env, recipe = _build_env(tmp_path / "max", IMAGE_PREFIX="a" * 42)
    assert _build(env, "--dry-run", recipe, "fake").returncode == 0


def test_ttl_changes_expiry(tmp_path):
    def run_and_minutes(sub, ttl):
        env, recipe = _build_env(tmp_path / sub)
        if ttl is not None:
            env["IMAGE_BUILD_TTL_MINUTES"] = ttl
        result = _build(env, "--dry-run", recipe, "fake")
        assert result.returncode == 0, result.stderr
        start = [l for l in _calls(tmp_path / sub).splitlines() if l.startswith("backend_start ")][0]
        run_id, _base, _pub, expires = start.split(" ")[1:5]
        m = re.fullmatch(r"ib-([0-9]{12})-[0-9a-f]{4}", run_id)
        assert m, run_id
        began = datetime.strptime(m.group(1), "%Y%m%d%H%M")
        ends = datetime.strptime(expires.removeprefix("expires="), "%Y%m%d%H%M")
        return (ends - began).total_seconds() / 60
    assert run_and_minutes("default", None) == 240
    assert run_and_minutes("short", "60") == 60
    for i, bad in enumerate(("0", "abc", "-5", "08", "0060")):
        env, recipe = _build_env(tmp_path / f"bad{i}")
        env["IMAGE_BUILD_TTL_MINUTES"] = bad
        assert _build(env, "--dry-run", recipe, "fake").returncode == 2, bad


def test_windows_remote_commands(tmp_path):
    env, recipe = _build_env(tmp_path, "windows")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    ssh = _remote_commands(calls, "ssh")
    assert any(l.endswith(' powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\setup.ps1"')
               for l in ssh), ssh
    assert any(l.endswith(' powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\verify.ps1"')
               for l in ssh), ssh
    assert any(l.endswith(" powershell -NoProfile -ExecutionPolicy Bypass -File C:\\image-build\\seal_windows.ps1")
               for l in ssh), ssh
    scp = _remote_commands(calls, "scp")
    assert any(l.endswith(" Administrator@192.0.2.10:C:/image-build/payload.tgz") for l in scp), scp
    assert any(l.endswith(" Administrator@192.0.2.10:C:/image-build/seal_windows.ps1") for l in scp), scp
    assert _events(calls, "setup.ps1", "verify.ps1", "seal_windows.ps1") == [
        "check_config", "resolve_base", "start", "upload", "setup", "verify", "seal", "save", "destroy"]
    assert "sudo" not in "".join(ssh)

    env, recipe = _build_env(tmp_path / "keep", "windows", KEEP_SSH="yes")
    assert _build(env, recipe, "fake").returncode == 0
    ssh = _remote_commands(_calls(tmp_path / "keep"))
    assert any(l.endswith(" -File C:\\image-build\\seal_windows.ps1 -KeepSsh") for l in ssh), ssh


def test_linux_remote_commands(tmp_path):
    env, recipe = _build_env(tmp_path)
    assert _build(env, recipe, "fake").returncode == 0
    calls = _calls(tmp_path)
    ssh = _remote_commands(calls)
    assert any(l.endswith(" builder@192.0.2.10 sudo bash -c 'cd /tmp/image-build && bash '\\''./setup.sh'\\'''")
               for l in ssh), ssh
    assert any(l.endswith(" builder@192.0.2.10 sudo bash /tmp/image-build/seal_linux.sh no") for l in ssh), ssh
    scp = _remote_commands(calls, "scp")
    assert any(l.endswith(" builder@192.0.2.10:/tmp/image-build/payload.tgz") for l in scp), scp
    assert any(f"{IMAGE_BUILD / 'seal_linux.sh'} builder@192.0.2.10:/tmp/image-build/seal_linux.sh" in l
               for l in scp), scp
    env, recipe = _build_env(tmp_path / "keep", KEEP_SSH="yes")
    assert _build(env, recipe, "fake").returncode == 0
    assert any(l.endswith(" sudo bash /tmp/image-build/seal_linux.sh yes")
               for l in _remote_commands(_calls(tmp_path / "keep")))


def test_payload_exclude(tmp_path):
    env, recipe = _build_env(tmp_path, PAYLOAD_EXCLUDE="packer *.iso")
    result = _build(env, "--dry-run", recipe, "fake")
    assert result.returncode == 0, result.stderr
    tar_line = [l for l in result.stderr.splitlines() if l.startswith("DRY-RUN: tar ")][0]
    assert "--exclude=packer" in tar_line.split()
    assert "--exclude=*.iso" in tar_line.split()
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "sink" / "payload.tgz") as tar:
        names = {n.removeprefix("./") for n in tar.getnames()}
    assert "setup.sh" in names and "verify.sh" in names
    assert not any(n.startswith("packer") or n.endswith(".iso") for n in names), names


def test_per_run_key_removed(tmp_path):
    for sub, fail_on in (("pass", None), ("fail", "verify.sh")):
        env, recipe = _build_env(tmp_path / sub)
        if fail_on:
            env["FAKE_SSH_FAIL_ON"] = fail_on
        result = _build(env, recipe, "fake")
        assert result.returncode == (1 if fail_on else 0), result.stderr
        keygen = [l for l in _calls(tmp_path / sub).splitlines() if l.startswith("ssh-keygen ")]
        assert len(keygen) == 1 and "-t ed25519" in keygen[0] and "-N " in keygen[0]
        key = Path(keygen[0].split(" -f ")[1].split()[0])
        assert key.is_relative_to(tmp_path / sub / "tmp")
        assert not key.parent.exists()
        assert list((tmp_path / sub / "tmp").iterdir()) == []


def test_reap_destroys_each_leftover(tmp_path):
    env, _recipe = _build_env(tmp_path)
    env["FAKE_LEFTOVERS"] = "vm-old-1 vm-old-2"
    result = _build(env, "--reap", "fake")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert "backend_destroy_id vm-old-1" in calls
    assert "backend_destroy_id vm-old-2" in calls
    assert "fake-cloud destroy vm-old-1" in calls
    assert "fake-cloud destroy vm-old-2" in calls
    assert "backend_start" not in calls


def test_reap_dry_run_destroys_nothing(tmp_path):
    env, _recipe = _build_env(tmp_path)
    env["FAKE_LEFTOVERS"] = "vm-old-1 vm-old-2"
    result = _build(env, "--reap", "fake", "--dry-run")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    # The leftover listing is read live; nothing is destroyed.
    assert "fake-cloud leftovers" in calls
    assert "fake-cloud destroy" not in calls
    assert "vm-old-1" in result.stdout and "vm-old-2" in result.stdout
    assert "DRY-RUN: fake-cloud destroy vm-old-1" in result.stderr
    env["FAKE_LEFTOVERS"] = ""
    result = _build(env, "--dry-run", "--reap", "fake")
    assert result.returncode == 0, result.stderr
    assert "nothing to reap" in result.stderr


def test_payload_uploaded_again_when_reboot_cleared_tmp(tmp_path):
    # Ubuntu empties /tmp at boot: a setup that rebooted took the payload with it.
    env, recipe = _build_env(tmp_path, SETUP_PASSES="2")
    env.update(FAKE_SSH_FAIL_ON="test -d /tmp/image-build", FAKE_SSH_FAIL_RC="1")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    assert _events(_calls(tmp_path), "setup.sh", "verify.sh", "seal_linux.sh") == [
        "check_config", "resolve_base", "start", "upload", "setup", "upload", "setup",
        "upload", "verify", "seal", "save", "destroy"]
    # A probe that cannot connect is a failure, not a missing payload.
    env2, recipe2 = _build_env(tmp_path / "down", SETUP_PASSES="2")
    env2.update(FAKE_SSH_FAIL_ON="test -d /tmp/image-build", FAKE_SSH_FAIL_RC="255")
    result = _build(env2, recipe2, "fake")
    assert result.returncode == 1
    assert "save" not in _events(_calls(tmp_path / "down"), "setup.sh", "verify.sh", "seal_linux.sh")


def _remote_lines(tmp_path: Path) -> list:
    """Each remote command line exactly as ssh received it."""
    log = tmp_path / "calls.log.remote"
    return log.read_text().split("\0")[:-1] if log.exists() else []


def test_script_paths_with_spaces_linux(tmp_path):
    env, recipe = _build_env(tmp_path, SETUP="my steps/set up.sh", VERIFY="check it.sh")
    payload = recipe.parent / "payload"
    (payload / "my steps").mkdir()
    (payload / "my steps" / "set up.sh").write_text('echo setup > "$MARKER_DIR/setup ran"\n')
    (payload / "check it.sh").write_text('echo verify > "$MARKER_DIR/verify ran"\n')
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    remote = _remote_lines(tmp_path)
    commands = [c for c in remote if "set up.sh" in c] + [c for c in remote if "check it.sh" in c]
    assert len(commands) == 2, remote
    # Run each line as the VM's login shell would, against the local payload.
    markers = tmp_path / "markers"
    markers.mkdir()
    for command in commands:
        assert command.startswith("sudo bash -c "), command
        local = command.removeprefix("sudo ").replace("/tmp/image-build", str(payload))
        ran = subprocess.run(["sh", "-c", local], env={**os.environ, "MARKER_DIR": str(markers)},
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode == 0, (command, ran.stderr)
    assert (markers / "setup ran").read_text() == "setup\n"
    assert (markers / "verify ran").read_text() == "verify\n"


def test_script_paths_with_spaces_windows(tmp_path):
    env, recipe = _build_env(tmp_path, "windows", SETUP="my steps/set up.ps1", VERIFY="check it.ps1")
    payload = recipe.parent / "payload"
    (payload / "my steps").mkdir()
    (payload / "my steps" / "set up.ps1").write_text("Write-Output setup\n")
    (payload / "check it.ps1").write_text("Write-Output verify\n")
    result = _build(env, recipe, "fake")
    assert result.returncode == 0, result.stderr
    remote = _remote_lines(tmp_path)
    assert ('powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\my steps\\set up.ps1"'
            in remote), remote
    assert ('powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\image-build\\check it.ps1"'
            in remote), remote


def test_reap_refuses_mismatched_tag(tmp_path):
    env, _recipe = _build_env(tmp_path)
    env.update(FAKE_LEFTOVERS="vm-old-1 vm-stranger", FAKE_UNTAGGED="vm-stranger")
    result = _build(env, "--reap", "fake")
    assert result.returncode == 1
    calls = _calls(tmp_path)
    assert "fake-cloud tag vm-stranger" in calls
    assert "fake-cloud destroy vm-stranger" not in calls
    assert "fake-cloud destroy vm-old-1" in calls
    assert "vm-stranger" in result.stderr
    result = _build(env, "--reap", "fake", "--dry-run")
    assert result.returncode == 1
    assert "DRY-RUN: fake-cloud destroy vm-old-1" in result.stderr
    assert "DRY-RUN: fake-cloud destroy vm-stranger" not in result.stderr


def test_backend_midway_failure_not_masked(tmp_path):
    # A backend step that fails, followed by steps that succeed, must still fail
    # the call: the core never runs a backend function where set -e is ignored.
    def run(sub, fn, *args):
        env, recipe = _build_env(tmp_path / sub)
        env.update(FAKE_MIDFAIL=fn, FAKE_LEFTOVERS="vm-old-1")
        result = _build(env, *[recipe if a == "RECIPE" else a for a in args])
        return result, _calls(tmp_path / sub)

    result, calls = run("check", "backend_check_config", "--dry-run", "RECIPE", "fake")
    assert result.returncode == 2
    assert "backend_resolve_base" not in calls

    result, calls = run("resolve", "backend_resolve_base", "RECIPE", "fake")
    assert result.returncode == 1
    assert "backend_start" not in calls

    result, calls = run("start", "backend_start", "RECIPE", "fake")
    assert result.returncode == 1
    steps = _events(calls, "setup.sh", "verify.sh", "seal_linux.sh")
    assert "upload" not in steps and "save" not in steps
    assert steps[-1] == "destroy"

    result, calls = run("save", "backend_save", "RECIPE", "fake")
    assert result.returncode == 1
    assert "fake-cloud save" not in calls
    assert _events(calls, "setup.sh", "verify.sh", "seal_linux.sh")[-1] == "destroy"
    assert not _build_log(tmp_path / "save").exists()
    assert result.stdout == ""

    result, calls = run("leftovers", "backend_leftovers", "--reap", "fake")
    assert result.returncode == 1
    assert "backend_destroy_id" not in calls

    result, calls = run("destroy_id", "backend_destroy_id", "--reap", "fake")
    assert result.returncode == 1
    assert "fake-cloud destroy" not in calls
