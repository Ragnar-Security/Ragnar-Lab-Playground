"""proxmox_base_ubuntu.sh, which builds the Proxmox base template. A fake `ssh`
plays the build node: a command sent to it runs in a real `sh` whose PATH holds
fake node tools (qm, pvesh, pvesm, hostname, ...) acting on a JSON state file,
so the script's remote shell lines behave as they would on the node and every
tool invocation is logged.

What these tests hold the script to: a run that fails or is interrupted after
`qm create` destroys the half-made VM, but only a VM that carries this run's
construction marker; and the first-boot user-data snippet is always removed,
loudly when it cannot be."""

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_image_build import IMAGE_BUILD

SCRIPT = IMAGE_BUILD / "proxmox_base_ubuntu.sh"
HOSTS = {"pve-host.invalid": "pve"}
PVE_CONFIG = (
    "PVE_HOST=pve-host.invalid\n"
    "PVE_NODE=pve\n"
    "PVE_STORAGE=build-store\n"
    "PVE_BUILD_BRIDGE=vmbr9\n"
    "PVE_BUILD_VLAN=999\n"
)
DATE = "20261113"
NAME = f"base-ubuntu-2404-{DATE}"
NODE_TOOLS = ("qm", "pvesh", "pvesm", "hostname", "sed", "ls", "rm")

# One program, several roles by the name it runs as:
#   ssh    root@<host in FAKE_PVE_HOSTS> runs the command line in sh, as the
#          node would, with the node tools first on PATH; stdin passes through
#          unless -n is given. Any other target fails.
#   <tool> a node tool: logged to $PVE_LOG as "<command>" and acted on in
#          $PVE_STATE. sed, ls and rm fall through to the real tools except
#          for the node paths they fake (/proc/meminfo, /etc/pve, FAKE_RM_FAIL).
# Injected faults:
#   FAKE_QM_CREATE=fail     qm create makes the VM, then fails (an SSH drop)
#   FAKE_QM_CREATE=hang     qm create makes the VM, touches $FAKE_READY, sleeps
#   FAKE_QM_CREATE=foreign  qm create finds the VMID taken by someone else's VM
#                           (it appears there) and fails
#   FAKE_QM_FAIL_ON=<sub>   that qm subcommand fails; "template" converts first
#   FAKE_RM_FAIL=1          rm of a snippet fails and leaves the file
FAKE_PVE = r'''#!/usr/bin/env python3
import json, os, shlex, shutil, subprocess, sys, time

prog, argv = os.path.basename(sys.argv[0]), sys.argv[1:]
nodebin = os.environ["FAKE_PVE_NODEBIN"]


def real(name):
    path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                           if os.path.abspath(p) != os.path.abspath(nodebin))
    return shutil.which(name, path=path)


if prog == "ssh":
    with open(os.environ["CALL_LOG"], "a") as f:
        f.write("ssh " + " ".join(argv) + "\n")
    with_arg = {"-o", "-i", "-J", "-p", "-l", "-F"}
    i, stdin = 0, None
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "-n":
            stdin = subprocess.DEVNULL
        i += 2 if argv[i] in with_arg else 1
    target, cmd = argv[i], " ".join(argv[i + 1:])
    user, _, host = target.rpartition("@")
    nodes = json.loads(os.environ["FAKE_PVE_HOSTS"])
    if user != "root" or host not in nodes:
        print("ssh: connect to host %s: unexpected target" % host, file=sys.stderr)
        sys.exit(255)
    env = dict(os.environ, FAKE_PVE_NODE=nodes[host], PATH=nodebin + os.pathsep + os.environ["PATH"])
    sys.exit(subprocess.run(["sh", "-c", cmd], env=env, stdin=stdin).returncode)

w = [prog] + argv
with open(os.environ["PVE_LOG"], "a") as f:
    f.write(shlex.join(w) + "\n")
node = os.environ["FAKE_PVE_NODE"]

if prog == "sed" and argv and argv[-1] == "/proc/meminfo":
    print(os.environ.get("FAKE_MEMAVAIL_KIB", str(32 * 1024 * 1024)))
    sys.exit(0)
if prog == "rm":
    if os.environ.get("FAKE_RM_FAIL") and any(a.endswith("-user.yaml") for a in argv):
        print("rm: cannot remove %r: Permission denied" % argv[-1], file=sys.stderr)
        sys.exit(1)
    os.execv(real("rm"), ["rm"] + argv)
if prog == "sed":
    os.execv(real("sed"), ["sed"] + argv)
if prog == "hostname":
    print(node)
    sys.exit(0)

path = os.environ["PVE_STATE"]
with open(path) as f:
    state = json.load(f)
guests = state["guests"]


def done(code=0):
    with open(path, "w") as f:
        json.dump(state, f)
    sys.exit(code)


def refuse(msg):
    print(msg, file=sys.stderr)
    done(255)


def opts(words):
    out, k = {}, 0
    while k < len(words):
        if words[k].startswith("--"):
            out[words[k][2:]] = words[k + 1]
            k += 2
        else:
            k += 1
    return out


if prog == "ls":
    shown = []
    if w[1:] == ["-d", "/etc/pve/nodes"]:
        shown = ["/etc/pve/nodes"]
    else:
        for word in w[1:]:
            m = word.rsplit("/", 1)[-1].split(".")[0]
            if m in guests and ("/lxc/" in word) == (guests[m]["type"] == "lxc"):
                shown.append(word.replace("*", guests[m]["node"]))
    for s in shown:
        print(s)
    done(0 if shown else 2)
elif prog == "pvesm":
    if w[1] != "path":
        refuse("unexpected: " + shlex.join(w))
    print(os.path.join(os.environ["FAKE_SNIPPET_DIR"], w[2].rsplit("/", 1)[-1]))
elif prog == "pvesh":
    if w[1:3] != ["get", "/cluster/resources"]:
        refuse("unexpected: " + shlex.join(w))
    print(json.dumps([{"id": g["type"] + "/" + v, "vmid": int(v), "node": g["node"],
                       "type": g["type"], "name": g["name"], "template": g["template"],
                       "status": g["status"]} for v, g in guests.items()]))
elif prog == "qm":
    sub = w[1]
    vmid = w[3] if sub == "guest" else w[2]
    fail_on = os.environ.get("FAKE_QM_FAIL_ON", "")
    if sub == "create":
        mode = os.environ.get("FAKE_QM_CREATE", "")
        if vmid in guests:
            refuse("unable to create VM %s: config file already exists" % vmid)
        o = opts(w[3:])
        if mode == "foreign":
            guests[vmid] = {"node": node, "type": "qemu", "name": "someone-else", "template": 0,
                            "status": "running", "description": "not yours", "config": {}}
            refuse("unable to create VM %s: config file already exists" % vmid)
        cfg = {k: v for k, v in o.items() if k not in ("name", "description")}
        guests[vmid] = {"node": node, "type": "qemu", "name": o["name"], "template": 0,
                        "status": "stopped", "description": o.get("description", ""), "config": cfg}
        if mode == "fail":
            done(255)
        if mode == "hang":
            with open(path, "w") as f:
                json.dump(state, f)
            open(os.environ["FAKE_READY"], "w").close()
            time.sleep(60)
        done()
    g = guests.get(vmid)
    if not g or g["node"] != node:
        refuse("Configuration file 'nodes/%s/qemu-server/%s.conf' does not exist" % (node, vmid))
    if sub == fail_on and sub != "template":
        refuse("fake qm: %s failed" % sub)
    if sub == "set":
        o = opts(w[3:])
        for key in o.pop("delete", "").split(","):
            g["config"].pop(key, None)
        for key, value in o.items():
            if key in ("name", "description"):
                g[key] = value
            else:
                g["config"][key] = value
    elif sub == "resize":
        g["config"]["size"] = w[4]
    elif sub == "start":
        g["status"] = "running"
    elif sub in ("shutdown", "stop"):
        g["status"] = "stopped"
    elif sub == "status":
        print("status: " + g["status"])
    elif sub == "config":
        print("description: " + g["description"])
        print("name: " + g["name"])
        for key, value in g["config"].items():
            print("%s: %s" % (key, value))
        if g["template"]:
            print("template: 1")
    elif sub == "template":
        g["template"] = 1
        if fail_on == "template":
            refuse("fake qm: template reported failure")
    elif sub == "destroy":
        del guests[vmid]
    elif sub == "guest":
        if w[2] == "exec":
            if "--pass-stdin" in w:
                sys.stdin.read()
            out = "status: done\n" if "cloud-init" in w and "status" in w else "finalized\n"
            print(json.dumps({"exitcode": 0, "exited": 1, "out-data": out}))
        elif w[2:5:2] != ["cmd", "ping"]:
            refuse("unexpected: " + shlex.join(w))
    else:
        refuse("unexpected: " + shlex.join(w))
else:
    refuse("unexpected: " + shlex.join(w))
done()
'''


def _guest(name, template=0, status="stopped", description=""):
    return {"node": "pve", "type": "qemu", "name": name, "template": template, "status": status,
            "description": description, "config": {}}


def _env(tmp_path: Path, guests=None, **extra) -> dict:
    bin_dir = tmp_path / "bin"
    node_bin = bin_dir / "node"
    node_bin.mkdir(parents=True)
    fake = bin_dir / "ssh"
    fake.write_text(FAKE_PVE)
    fake.chmod(0o755)
    for tool in NODE_TOOLS:
        (node_bin / tool).symlink_to(fake)
    (tmp_path / "snippets").mkdir()
    (tmp_path / "noble.img").write_bytes(b"cloud image")
    (tmp_path / "pve-state.json").write_text(json.dumps({"guests": guests or {}}))
    (tmp_path / "config.env").write_text(PVE_CONFIG)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        CALL_LOG=str(tmp_path / "calls.log"),
        PVE_STATE=str(tmp_path / "pve-state.json"),
        PVE_LOG=str(tmp_path / "pve.log"),
        FAKE_PVE_HOSTS=json.dumps(HOSTS),
        FAKE_PVE_NODEBIN=str(node_bin),
        FAKE_SNIPPET_DIR=str(tmp_path / "snippets"),
        FAKE_READY=str(tmp_path / "ready"),
        IMAGE_BUILD_CONFIG=str(tmp_path / "config.env"),
        **extra,
    )
    return env


def _args(tmp_path: Path, *extra):
    return ["bash", str(SCRIPT), "--image", str(tmp_path / "noble.img"), "--snippets", "snipstore",
            "--date", DATE, *extra]


def _run(tmp_path: Path, *extra, guests=None, **env_extra):
    env = _env(tmp_path, guests, **env_extra)
    return subprocess.run(_args(tmp_path, *extra), env=env, capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "pve-state.json").read_text())["guests"]


def _cmds(tmp_path: Path) -> list:
    log = tmp_path / "pve.log"
    return log.read_text().splitlines() if log.exists() else []


def _snippet(tmp_path: Path) -> Path:
    return tmp_path / "snippets" / f"{NAME}-user.yaml"


def _marker(tmp_path: Path) -> str:
    """The construction marker the run's `qm create` put in the description."""
    create = [c for c in _cmds(tmp_path) if c.startswith("qm create ")]
    assert len(create) == 1, _cmds(tmp_path)
    found = re.findall(r"proxmox-base-build=[0-9]{14}-[0-9a-f]{8}", create[0])
    assert len(found) == 1, create[0]
    return found[0]


def _assert_cleaned_up(tmp_path: Path, result):
    assert result.returncode != 0, result.stderr
    assert "8000" not in _state(tmp_path), result.stderr
    marker = _marker(tmp_path)
    destroys = [c for c in _cmds(tmp_path) if c.startswith("qm destroy")]
    assert destroys == ["qm destroy 8000 --purge 1"], _cmds(tmp_path)
    assert "VM 8000" in result.stderr and "destroyed" in result.stderr, result.stderr
    assert marker in result.stderr, result.stderr
    assert not _snippet(tmp_path).exists(), result.stderr


# --- the normal paths -------------------------------------------------------------


def test_dry_run_changes_nothing(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "would run: qm create 8000 " in result.stderr
    assert _state(tmp_path) == {}
    assert not _snippet(tmp_path).exists()
    changes = [c for c in _cmds(tmp_path)
               if c.startswith(("qm create", "qm set", "qm destroy", "qm template", "rm "))]
    assert changes == [], changes


def test_apply_builds_template_and_removes_snippet(tmp_path):
    result = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "8000\n"
    g = _state(tmp_path)["8000"]
    assert g["template"] == 1 and g["name"] == NAME
    marker = _marker(tmp_path)
    # The finished template keeps no construction marker and no cloud-init drive.
    assert marker not in g["description"] and "under construction" not in g["description"]
    assert g["description"].startswith(f"{NAME}: Ubuntu 24.04 cloud image noble.img sha256 ")
    assert not {"ide2", "cicustom", "ipconfig0"} & set(g["config"]), g["config"]
    assert not _snippet(tmp_path).exists()
    assert not any(c.startswith(("qm destroy", "qm stop")) for c in _cmds(tmp_path))


def test_vmid_outside_block_or_in_use_is_refused(tmp_path):
    for sub, vmid in (("v9000", "9000"), ("v7999", "7999"), ("v8500", "8500")):
        result = _run(tmp_path / sub, "--apply", "--vmid", vmid)
        assert result.returncode == 1, (sub, result.stderr)
        assert "8000-8499" in result.stderr
        assert not (tmp_path / sub / "calls.log").exists(), sub
    result = _run(tmp_path / "used", "--apply", "--vmid", "8003", guests={"8503": _guest("other")})
    assert result.returncode == 1 and "in use" in result.stderr, result.stderr
    assert not any(c.startswith("qm ") for c in _cmds(tmp_path / "used"))


# --- finding 1: a half-made VM is destroyed, and only when it carries the marker ---


def test_create_that_fails_after_making_the_vm_destroys_it(tmp_path):
    # qm create made the VM but the call failed (an SSH drop, say): no flag set
    # after the call returns can know about it; the marker does.
    result = _run(tmp_path, "--apply", FAKE_QM_CREATE="fail")
    _assert_cleaned_up(tmp_path, result)


def test_failure_midway_destroys_vm_and_removes_snippet(tmp_path):
    result = _run(tmp_path, "--apply", FAKE_QM_FAIL_ON="resize")
    _assert_cleaned_up(tmp_path, result)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_signal_during_create_destroys_the_vm(tmp_path, sig):
    env = _env(tmp_path, FAKE_QM_CREATE="hang")
    proc = subprocess.Popen(_args(tmp_path, "--apply"), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                            start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "ready").exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "qm create never ran"
            time.sleep(0.05)
        # As a terminal or a dropped session does: the whole process group.
        os.killpg(proc.pid, sig)
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    _assert_cleaned_up(tmp_path, result)


def test_vm_without_this_runs_marker_is_never_destroyed(tmp_path):
    result = _run(tmp_path, "--apply", FAKE_QM_CREATE="foreign")
    assert result.returncode != 0
    g = _state(tmp_path)["8000"]
    assert g["name"] == "someone-else" and g["status"] == "running", g
    assert not any(c.startswith(("qm stop", "qm destroy")) for c in _cmds(tmp_path)), _cmds(tmp_path)
    assert "VM 8000" in result.stderr and "NOT destroyed" in result.stderr, result.stderr
    assert not _snippet(tmp_path).exists()


def test_vm_that_became_a_template_is_never_destroyed(tmp_path):
    # qm template converted the VM but reported failure.
    result = _run(tmp_path, "--apply", FAKE_QM_FAIL_ON="template")
    assert result.returncode != 0
    assert _state(tmp_path)["8000"]["template"] == 1
    assert not any(c.startswith(("qm stop", "qm destroy")) for c in _cmds(tmp_path)), _cmds(tmp_path)
    assert "template" in result.stderr and "8000" in result.stderr, result.stderr
    assert not _snippet(tmp_path).exists()


# --- finding 2: the snippet is always removed, loudly when it cannot be ------------


def test_snippet_that_cannot_be_removed_after_success_is_reported(tmp_path):
    result = _run(tmp_path, "--apply", FAKE_RM_FAIL="1")
    assert result.returncode != 0, result.stderr
    assert _snippet(tmp_path).exists()
    assert _state(tmp_path)["8000"]["template"] == 1
    assert "ERROR" in result.stderr and str(_snippet(tmp_path)) in result.stderr, result.stderr
    assert "NOT deleted" in result.stderr, result.stderr


def test_snippet_that_cannot_be_removed_after_failure_is_reported(tmp_path):
    result = _run(tmp_path, "--apply", FAKE_QM_CREATE="fail", FAKE_RM_FAIL="1")
    assert result.returncode != 0
    assert "8000" not in _state(tmp_path), result.stderr
    assert _snippet(tmp_path).exists()
    assert "ERROR" in result.stderr and str(_snippet(tmp_path)) in result.stderr, result.stderr
    assert "NOT deleted" in result.stderr, result.stderr
