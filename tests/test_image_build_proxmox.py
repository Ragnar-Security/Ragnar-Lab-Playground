"""The Proxmox backend of the image builder, and sync_template.sh's fixed
target VMID. A fake `ssh` plays the Proxmox cluster from a JSON state file, so
every pvesh/qm/vzdump command the backend (and sync_template.sh) sends is
logged and acted on.

The VMID block is the point of most of these tests: build VMs only in
8000-8499 with the replica at +500, and no command ever names a VMID >= 9000
(the portal's allocator takes the cluster's highest VMID as a high-water mark)."""

import base64
import json
import os
import re
import subprocess
from pathlib import Path

from tests.test_image_build import ROOT, IMAGE_BUILD, _build, _build_env, _build_log

BACKENDS = IMAGE_BUILD / "backends"
SYNC = ROOT / "scripts" / "sync_template.sh"
HOSTS = {"pve-host.invalid": "pve", "pve2-host.invalid": "pve2"}
PVE_CONFIG = (
    "PVE_HOST=pve-host.invalid\n"
    "PVE_NODE=pve\n"
    "PVE_REPLICA_NODE=pve2\n"
    "PVE_REPLICA_HOST=pve2-host.invalid\n"
    "PVE_STORAGE=build-store\n"
    "PVE_REPLICA_STORAGE=replica-store\n"
    "PVE_BUILD_BRIDGE=vmbr9\n"
    "PVE_BUILD_VLAN=999\n"
)
BASE_VMID = 8100
EXPIRED = "image-build=ib-202601010000-abcd image-build-expires=202601010400"
UNEXPIRED = "image-build=ib-209912310000-abcd image-build-expires=209912312359"

# A Proxmox cluster in a JSON file. root@<host in FAKE_PVE_HOSTS> is a node:
# each command is logged to $PVE_LOG as "<node>\t<command>" and acted on.
# Any other target is a build VM, where every command succeeds (and
# FAKE_SSH_FAIL_ON fails one, as in the core's fake).
FAKE_PVE_SSH = r'''#!/usr/bin/env python3
import fnmatch, json, os, shlex, sys

argv = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as f:
    f.write("ssh " + " ".join(argv) + "\n")
with_arg = {"-o", "-i", "-J", "-p", "-l", "-F"}
i = 0
while i < len(argv) and argv[i].startswith("-"):
    i += 2 if argv[i] in with_arg else 1
target, cmd = argv[i], " ".join(argv[i + 1:])
fail_on = os.environ.get("FAKE_SSH_FAIL_ON")
if fail_on and fail_on in " ".join(argv):
    sys.exit(1)
user, _, host = target.rpartition("@")
nodes = json.loads(os.environ["FAKE_PVE_HOSTS"])
if user != "root" or host not in nodes:
    sys.exit(0)
node = nodes[host]
with open(os.environ["PVE_LOG"], "a") as f:
    f.write(node + "\t" + cmd + "\n")
path = os.environ["PVE_STATE"]
with open(path) as f:
    state = json.load(f)
guests = state["guests"]
dumps = state.setdefault("dumps", {}).setdefault(node, [])


def done(code=0):
    with open(path, "w") as f:
        json.dump(state, f)
    sys.exit(code)


def refuse(msg):
    print("fake pve: " + msg, file=sys.stderr)
    done(2)


def opts(words):
    out, k = {}, 0
    while k < len(words):
        if words[k].startswith("--"):
            out[words[k][2:]] = words[k + 1]
            k += 2
        else:
            k += 1
    return out


def description(vmid):
    override = os.environ.get("FAKE_PVE_DESC_OVERRIDE", "")
    if override.startswith(vmid + "="):
        return override[len(vmid) + 1:]
    return guests[vmid]["description"]


w = shlex.split(cmd)
if w == ["echo", "OK"] or w == ["true"]:
    print("OK")
elif cmd.startswith("ls -t "):
    hits = [d for d in dumps if fnmatch.fnmatch(d, w[2])]
    if hits:
        print(hits[-1])
elif cmd.startswith("ls /etc/pve/nodes/"):
    for word in w[1:]:
        vmid = word.rsplit("/", 1)[-1].split(".")[0]
        if vmid.isdigit() and vmid in guests and ("/lxc/" in word) == (guests[vmid]["type"] == "lxc"):
            print(word.replace("*", guests[vmid]["node"]))
elif cmd.startswith("du -h "):
    print("1.0G")
elif cmd.startswith("qm config "):
    print(1 if guests[w[2]]["template"] else 0)
elif w[:2] == ["pvesh", "get"]:
    if w[2] == "/cluster/resources":
        print(json.dumps([{"id": g["type"] + "/" + v, "vmid": int(v), "node": g["node"],
                           "type": g["type"], "name": g["name"], "template": g["template"],
                           "status": g["status"]} for v, g in guests.items()]))
    elif w[2] == "/cluster/nextid":
        print(os.environ.get("FAKE_PVE_NEXTID", "100"))
    elif w[2].startswith("/nodes/") and w[2].endswith("/config"):
        _, _, n, kind, vmid, _ = w[2].split("/")
        if vmid not in guests or guests[vmid]["node"] != n:
            refuse("no VM " + vmid + " on " + n)
        cfg = {"name": guests[vmid]["name"], "description": description(vmid)}
        if guests[vmid]["template"]:
            cfg["template"] = 1
        print(json.dumps(cfg))
    else:
        refuse("unexpected: " + cmd)
elif w[0] == "pvesm":
    pass
elif w[0] == "vzdump":
    if w[1] not in guests or guests[w[1]]["node"] != node:
        refuse("vzdump: no VM " + w[1])
    dumps.append("/var/lib/vz/dump/vzdump-qemu-" + w[1] + "-2026_11_08-00_00_00.vma.zst")
elif w[0] == "scp":
    dest_host, _, dest_path = w[2].partition(":")
    state["dumps"].setdefault(nodes[dest_host.split("@")[1]], []).append(dest_path)
elif w[0] == "qmrestore":
    if w[1] not in dumps:
        refuse("qmrestore: no dump " + w[1])
    if w[2] in guests:
        refuse("qmrestore: VMID " + w[2] + " exists")
    source = w[1].split("-")[2]
    guests[w[2]] = dict(guests[source], node=node, status="stopped",
                        storage=opts(w[3:]).get("storage"))
elif w[0] == "rm":
    pass
elif w[0] == "qm":
    sub, vmid = w[1], (w[2] if len(w) > 2 else "")
    if sub == "guest":
        vmid = w[3]
        if w[2] == "cmd" and w[4] == "ping":
            pass
        elif w[2] == "cmd" and w[4] == "network-get-interfaces":
            print(json.dumps([
                {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
                {"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv6", "ip-address": "fe80::1"},
                                                  {"ip-address-type": "ipv4", "ip-address": "192.0.2.10"}]}]))
        elif w[2] == "exec":
            print(json.dumps({"exitcode": int(os.environ.get("FAKE_PVE_EXEC_EXITCODE", "0")),
                              "exited": 1, "out-data": ""}))
        else:
            refuse("unexpected: " + cmd)
    elif sub == "clone":
        new = w[3]
        if new in guests:
            refuse("clone: VMID " + new + " exists")
        guests[new] = dict(guests[vmid], node=node, name=opts(w[4:])["name"], template=0,
                           status="stopped")
    elif vmid not in guests:
        refuse(sub + ": no VM " + vmid)
    elif sub == "set":
        o = opts(w[3:])
        for key in ("name", "description"):
            if key in o:
                guests[vmid][key] = o[key]
    elif sub == "start":
        guests[vmid]["status"] = "running"
    elif sub == "shutdown":
        if os.environ.get("FAKE_PVE_SHUTDOWN_FAIL"):
            refuse("shutdown timed out")
        guests[vmid]["status"] = "stopped"
    elif sub == "stop":
        guests[vmid]["status"] = "stopped"
    elif sub == "template":
        guests[vmid]["template"] = 1
    elif sub == "destroy":
        del guests[vmid]
    elif sub == "status":
        print("status: " + guests[vmid]["status"])
    else:
        refuse("unexpected: " + cmd)
else:
    refuse("unexpected: " + cmd)
done()
'''


def _guest(node, name, template=0, status="stopped", description="", kind="qemu"):
    return {"node": node, "type": kind, "name": name, "template": template, "status": status,
            "description": description}


def _cluster(**extra):
    """A base template, and the guests above the build block that must never be
    touched, one of them a newer template whose name matches the base glob."""
    guests = {
        str(BASE_VMID): _guest("pve", "base-ubuntu-2404-20261101", template=1),
        "9997": _guest("pve", "portal-a", status="running", description=EXPIRED),
        "9998": _guest("pve", "base-ubuntu-2404-20991231", template=1),
        "9999": _guest("pve2", "portal-c"),
        "9706": _guest("pve", "keep-9706"),
        "9806": _guest("pve2", "keep-9806"),
    }
    guests.update(extra)
    return guests


def _fake_pve(tmp_path: Path, env: dict, guests: dict) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_PVE_SSH)
    ssh.chmod(0o755)
    (tmp_path / "pve-state.json").write_text(json.dumps({"guests": guests}))
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        CALL_LOG=env.get("CALL_LOG", str(tmp_path / "calls.log")),
        PVE_STATE=str(tmp_path / "pve-state.json"),
        PVE_LOG=str(tmp_path / "pve.log"),
        FAKE_PVE_HOSTS=json.dumps(HOSTS),
    )
    return env


def _pve_env(tmp_path: Path, guests: dict, os_family: str = "linux", **recipe):
    env, recipe_path = _build_env(tmp_path, os_family, **recipe)
    _fake_pve(tmp_path, env, guests)
    Path(env["IMAGE_BUILD_CONFIG"]).write_text(PVE_CONFIG)
    env["IMAGE_BUILD_BACKENDS_DIR"] = str(BACKENDS)
    return env, recipe_path


def _pve_cmds(tmp_path: Path, node=None) -> list:
    log = tmp_path / "pve.log"
    if not log.exists():
        return []
    rows = [line.split("\t", 1) for line in log.read_text().splitlines()]
    return [cmd for n, cmd in rows if node is None or n == node]


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "pve-state.json").read_text())["guests"]


def _yaml(result) -> str:
    return result.stdout.rstrip("\n").splitlines()[-1]


def _clones(tmp_path: Path) -> list:
    return [c for c in _pve_cmds(tmp_path) if c.startswith("qm clone ")]


def test_proxmox_vmid_picks_lowest_free_pair(tmp_path):
    # 8000 is taken, and 8001's partner 8501 is taken: the first free pair is 8002/8502.
    guests = _cluster(**{"8000": _guest("pve", "taken"), "8501": _guest("pve2", "taken")})
    env, recipe = _pve_env(tmp_path, guests)
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    clones = _clones(tmp_path)
    assert len(clones) == 1 and clones[0].startswith(f"qm clone {BASE_VMID} 8002 --name ib-"), clones
    assert clones[0].endswith(" --full 1 --storage build-store"), clones
    assert _yaml(result) == "template_locations: {pve: 8002, pve2: 8502}"


def test_proxmox_vmid_block_exhausted_refused(tmp_path):
    # The base template at 8100 fills its own slot.
    taken = {str(v): _guest("pve", f"g{v}") for v in range(8000, 8500) if v != BASE_VMID}
    env, recipe = _pve_env(tmp_path, _cluster(**taken))
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert "no free VMID pair" in result.stderr, result.stderr
    assert _clones(tmp_path) == []
    # Every replica slot taken refuses the same way: a pair needs both halves.
    partners = {str(v): _guest("pve2", f"g{v}") for v in range(8500, 9000)}
    env, recipe = _pve_env(tmp_path / "replicas", _cluster(**partners))
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert "no free VMID pair" in result.stderr, result.stderr
    assert _clones(tmp_path / "replicas") == []


def _vmids_named(cmd: str) -> list:
    """Every VMID a Proxmox command names: numeric arguments of qm/qmrestore/
    vzdump/pct, and VMIDs in a pvesh or /etc/pve path."""
    words = cmd.split()
    found = [int(n) for n in re.findall(r"/(?:qemu|lxc|qemu-server)/(\d+)", cmd)]
    found += [int(n) for n in re.findall(r"vzdump-(?:qemu|lxc)-(\d+)-", cmd)]
    if words[0] in ("qm", "qmrestore", "vzdump", "pct"):
        found += [int(w) for w in words if w.isdigit()]
    return found


def test_proxmox_never_touches_9000_plus(tmp_path):
    # A successful build, a failed one, and a reap, with tagged-expired guest
    # 9997 and a newer matching template at 9998 as lures.
    env, recipe = _pve_env(tmp_path, _cluster())
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert _clones(tmp_path)[0].startswith(f"qm clone {BASE_VMID} 8000 "), _clones(tmp_path)

    env, recipe = _pve_env(tmp_path / "fail", _cluster())
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"
    assert _build(env, recipe, "proxmox").returncode == 1

    env, _recipe = _pve_env(tmp_path / "reap", _cluster(**{"8003": _guest("pve", "x", description=EXPIRED)}))
    result = _build(env, "--reap", "proxmox")
    assert result.returncode == 0, result.stderr

    commands = _pve_cmds(tmp_path) + _pve_cmds(tmp_path / "fail") + _pve_cmds(tmp_path / "reap")
    assert any(c.startswith("qm destroy 8003") for c in commands)
    named = [(c, v) for c in commands for v in _vmids_named(c) if v >= 9000]
    assert named == [], named
    for sub in ("", "fail", "reap"):
        for vmid in ("9997", "9998", "9999", "9706", "9806"):
            assert vmid in _state(tmp_path / sub if sub else tmp_path)


def test_proxmox_resolve_base_newest(tmp_path):
    # Newest by the last field, not by VMID; templates only, on PVE_NODE only.
    guests = _cluster(**{
        "8100": _guest("pve", "base-ubuntu-2404-20261108", template=1),
        "8101": _guest("pve", "base-ubuntu-2404-20261101", template=1),
        "8102": _guest("pve", "base-ubuntu-2404-20261231"),  # not a template
        "8602": _guest("pve2", "base-ubuntu-2404-20261230", template=1),  # other node
    })
    env, recipe = _pve_env(tmp_path, guests)
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert _clones(tmp_path)[0].startswith("qm clone 8100 8000 "), _clones(tmp_path)

    # The SHA sorts the other way; the timestamp decides.
    guests = _cluster(**{
        "8110": _guest("pve", "enpm634-docker-host-ffff000-202611010000", template=1),
        "8111": _guest("pve", "enpm634-docker-host-0000aaa-202611080000", template=1),
    })
    env, recipe = _pve_env(tmp_path / "sha", guests, BASE_PROXMOX="enpm634-docker-host-*")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert _clones(tmp_path / "sha")[0].startswith("qm clone 8111 8000 "), _clones(tmp_path / "sha")

    env, recipe = _pve_env(tmp_path / "none", _cluster(), BASE_PROXMOX="base-nothing-*")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert "base-nothing-*" in result.stderr, result.stderr
    assert _clones(tmp_path / "none") == []


def test_proxmox_save_replicates_to_plus_500(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    image_id, image_name = _build_log(tmp_path).read_text().splitlines()[1].split("\t")[2:4]
    assert image_id == "pve:8000,pve2:8500"
    assert _yaml(result) == "template_locations: {pve: 8000, pve2: 8500}"
    pve2 = _pve_cmds(tmp_path, "pve2")
    assert any(c.startswith("qmrestore '/var/lib/vz/dump/vzdump-qemu-8000-")
               and c.endswith("' 8500 --storage replica-store") for c in pve2), pve2
    assert not any("/cluster/nextid" in c for c in _pve_cmds(tmp_path))
    # The build VM became the template: the core destroys nothing.
    assert not any(c.startswith("qm destroy") for c in _pve_cmds(tmp_path))
    state = _state(tmp_path)
    for vmid, node in (("8000", "pve"), ("8500", "pve2")):
        assert state[vmid]["node"] == node
        assert state[vmid]["template"] == 1
        assert state[vmid]["name"] == image_name
        assert state[vmid]["description"] == f"image={image_name}"
    assert state["8500"]["storage"] == "replica-store"
    # Shut down, then template, then replicate.
    pve = _pve_cmds(tmp_path, "pve")
    order = [next(i for i, c in enumerate(pve) if c.startswith(p))
             for p in ("qm shutdown 8000 --timeout 300", "qm template 8000", "vzdump 8000")]
    assert order == sorted(order), pve
    assert not any(c.startswith("qm stop") for c in pve)


def test_proxmox_save_falls_back_to_stop(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_PVE_SHUTDOWN_FAIL"] = "1"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    pve = _pve_cmds(tmp_path, "pve")
    assert pve.index("qm stop 8000") < pve.index("qm template 8000"), pve


def test_proxmox_leftovers_only_tagged_expired(tmp_path):
    guests = _cluster(**{
        "8003": _guest("pve", "expired", status="running", description=EXPIRED),
        "8004": _guest("pve", "unexpired", status="running", description=UNEXPIRED),
        "8005": _guest("pve", "untagged", description="image=enpm634-old-abc1234-202601010000"),
    })
    env, _recipe = _pve_env(tmp_path, guests)
    result = _build(env, "--reap", "proxmox", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["would destroy 8003"], result.stdout
    assert not any(c.startswith(("qm stop", "qm destroy")) for c in _pve_cmds(tmp_path))

    result = _build(env, "--reap", "proxmox")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["destroyed 8003"], result.stdout
    destroys = [c for c in _pve_cmds(tmp_path) if c.startswith(("qm stop", "qm destroy"))]
    assert destroys == ["qm stop 8003", "qm destroy 8003 --purge 1"], destroys
    state = _state(tmp_path)
    assert "8003" not in state
    assert {"8004", "8005", "9997"} <= set(state)


def test_proxmox_destroy_rechecks_tag(tmp_path):
    # Control: a failed build destroys its own, still-tagged VM.
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"
    assert _build(env, recipe, "proxmox").returncode == 1
    destroys = [c for c in _pve_cmds(tmp_path) if c.startswith(("qm stop", "qm destroy"))]
    assert destroys == ["qm stop 8000", "qm destroy 8000 --purge 1"], destroys
    assert "8000" not in _state(tmp_path)

    # The description no longer names this run: the VM is left alone.
    env, recipe = _pve_env(tmp_path / "changed", _cluster())
    env.update(FAKE_SSH_FAIL_ON="verify.sh", FAKE_PVE_DESC_OVERRIDE="8000=owner=someone-else")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert not any(c.startswith(("qm stop", "qm destroy")) for c in _pve_cmds(tmp_path / "changed"))
    assert "8000" in _state(tmp_path / "changed")
    assert "not destroyed" in result.stderr, result.stderr


def test_proxmox_start_tags_and_configures(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"  # stop before save rewrites the description
    _build(env, recipe, "proxmox")
    pve = _pve_cmds(tmp_path, "pve")
    sets = [c for c in pve if c.startswith("qm set 8000 --memory")]
    assert len(sets) == 1, pve
    assert sets[0].startswith("qm set 8000 --memory 2048 --cores 2 --net0 virtio,bridge=vmbr9,tag=999 "
                              "--description 'image-build=ib-"), sets
    desc = re.search(r"--description '([^']*)'", sets[0]).group(1)
    run_id, expires = re.fullmatch(r"image-build=(ib-\d{12}-[0-9a-f]{4}) image-build-expires=(\d{12})",
                                   desc).groups()
    assert "lab-playground-site" not in "".join(pve)
    assert pve.index("qm start 8000") > pve.index(sets[0])
    clone = _clones(tmp_path)[0]
    assert f"--name {run_id} " in clone


def _guest_exec(tmp_path: Path) -> list:
    return [c for c in _pve_cmds(tmp_path) if c.startswith("qm guest exec 8000 ")]


def test_proxmox_linux_key_and_ssh(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    execs = _guest_exec(tmp_path)
    assert len(execs) == 1, execs
    assert "ssh-ed25519 AAAAC3FAKEPUBLIC image-build" in execs[0]
    assert ">> /home/builder/.ssh/authorized_keys" in execs[0]
    vm_ssh = [l for l in (tmp_path / "calls.log").read_text().splitlines() if " builder@192.0.2.10 " in l]
    assert vm_ssh and all("-J root@pve-host.invalid" in l for l in vm_ssh), vm_ssh


def test_proxmox_windows_key_acl_and_ssh(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster(), "windows")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    execs = _guest_exec(tmp_path)
    assert len(execs) == 1, execs
    encoded = re.search(r"-EncodedCommand (\S+)$", execs[0]).group(1)
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert "Set-Content -Path $file -Value 'ssh-ed25519 AAAAC3FAKEPUBLIC image-build'" in script
    assert "$file = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'" in script
    # Administrators and SYSTEM full control, inheritance removed.
    assert "icacls.exe $file /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F'" in script
    vm_ssh = [l for l in (tmp_path / "calls.log").read_text().splitlines()
              if " Administrator@192.0.2.10 " in l]
    assert vm_ssh and all("-J root@pve-host.invalid" in l for l in vm_ssh), vm_ssh


def test_proxmox_failed_key_injection_fails_build(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_PVE_EXEC_EXITCODE"] = "1"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert not any(" builder@192.0.2.10 " in l for l in (tmp_path / "calls.log").read_text().splitlines())
    assert "8000" not in _state(tmp_path)


def test_proxmox_dry_run_touches_nothing(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    result = _build(env, "--dry-run", recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert _pve_cmds(tmp_path) == []
    for printed in ("DRY-RUN: pve qm clone ", "DRY-RUN: pve qm template 8000",
                    f"DRY-RUN: bash {ROOT / 'scripts' / 'sync_template.sh'} pve-host.invalid 8000 "
                    "pve2-host.invalid replica-store 8500"):
        assert printed in result.stderr, printed
    assert _yaml(result) == "template_locations: {pve: 8000, pve2: 8500}"


def test_proxmox_config_missing_refused(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    Path(env["IMAGE_BUILD_CONFIG"]).write_text(PVE_CONFIG.replace("PVE_REPLICA_HOST=pve2-host.invalid\n", ""))
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 2
    assert "PVE_REPLICA_HOST" in result.stderr
    assert _pve_cmds(tmp_path) == []


# --- sync_template.sh -------------------------------------------------------------


def _sync(tmp_path: Path, *args, **extra_env):
    env = _fake_pve(tmp_path, dict(os.environ), {"8002": _guest("pve", "tmpl", template=1),
                                                 "8123": _guest("pve2", "other")})
    env.update(extra_env)
    result = subprocess.run(["bash", str(SYNC), *args], env=env, capture_output=True, text=True,
                            timeout=60, stdin=subprocess.DEVNULL)
    return result, _pve_cmds(tmp_path, "pve2")


def test_sync_template_fixed_target_vmid(tmp_path):
    result, pve2 = _sync(tmp_path / "fixed", "pve-host.invalid", "8002", "pve2-host.invalid",
                         "replica-store", "8502", FAKE_PVE_NEXTID="8124")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("/cluster/nextid" in c for c in pve2), pve2
    restores = [c for c in pve2 if c.startswith("qmrestore ")]
    assert restores == ["qmrestore '/var/lib/vz/dump/vzdump-qemu-8002-2026_11_08-00_00_00.vma.zst' 8502 "
                        "--storage replica-store"], pve2
    assert "pve2-host.invalid: 8502" in result.stdout

    # Without the argument, the next free VMID is asked for, as before.
    result, pve2 = _sync(tmp_path / "next", "pve-host.invalid", "8002", "pve2-host.invalid",
                         "replica-store", FAKE_PVE_NEXTID="8124")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pvesh get /cluster/nextid" in pve2
    assert [c for c in pve2 if c.startswith("qmrestore ")][0].endswith("' 8124 --storage replica-store")
    assert "Next available VMID: 8124" in result.stdout

    # A target VMID in use, or not a number, is refused before anything is dumped.
    for sub, vmid in (("used", "8123"), ("bad", "85x2")):
        result, pve2 = _sync(tmp_path / sub, "pve-host.invalid", "8002", "pve2-host.invalid",
                             "replica-store", vmid)
        assert result.returncode == 1, (sub, result.stdout)
        assert not any(c.startswith(("vzdump", "qmrestore")) for c in _pve_cmds(tmp_path / sub)), sub
