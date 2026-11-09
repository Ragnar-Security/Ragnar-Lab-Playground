"""The image builder uploads a payload (setup, verify, tarballs) into a build VM
and seals it before the image is saved. A sealed image must carry none of the
payload, no usable build key, and no identity shared with the build VM."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_BUILD = ROOT / "scripts" / "image_build"
SEAL_LINUX = IMAGE_BUILD / "seal_linux.sh"

LOG_CALL = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$CALL_LOG"\n'


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
    for name in ("systemctl", "passwd", "cloud-init"):
        _tool(bin_dir, name, LOG_CALL)
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
    assert "systemctl disable ssh" in calls
    assert "passwd -l builder" in calls
    assert "cloud-init clean --logs" in calls


def test_seal_linux_keep_ssh(tmp_path):
    env = _seal_env(tmp_path)
    root = Path(env["SEAL_ROOT"])
    result = _seal(tmp_path, env, "yes")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert "systemctl disable ssh" not in calls
    # A build base is logged into as builder by the next build.
    assert "passwd -l builder" not in calls
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
    text = dropin.read_text()
    assert "[Service]" in text
    assert "ExecStartPre=/usr/bin/ssh-keygen -A" in text.splitlines()


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
