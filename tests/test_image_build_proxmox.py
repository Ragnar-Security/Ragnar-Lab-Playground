"""The Proxmox backend of the image builder, and sync_template.sh's fixed
target VMID. A fake `ssh` plays the Proxmox cluster: a command sent to a node
runs in a real `sh` whose PATH holds fake node tools (qm, pvesh, vzdump, ...)
acting on a JSON state file, so a remote script behaves as it would on the
node and every tool invocation is logged.

The VMID block is the point of most of these tests: build VMs only in
8000-8499 with the replica at +500, and no command ever names a VMID >= 9000
(the portal's allocator takes the cluster's highest VMID as a high-water mark)."""

import base64
import json
import os
import re
import shlex
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
NODE_TOOLS = ("qm", "pvesh", "pvesm", "vzdump", "qmrestore", "pct", "scp", "ls", "du", "rm")

# One program, three roles by the name it runs as:
#   ssh   root@<host in FAKE_PVE_HOSTS> runs the command line in sh, as a node
#         would, with the node tools first on PATH (raw line logged to
#         $PVE_LOG.remote). Any other target is a build VM: every command
#         succeeds, and FAKE_SSH_FAIL_ON fails one, as in the core's fake.
#   <tool> a node tool on node $FAKE_PVE_NODE: logged to $PVE_LOG as
#         "<node>\t<command>" and acted on in $PVE_STATE.
FAKE_PVE = r'''#!/usr/bin/env python3
import fnmatch, json, os, shlex, subprocess, sys

prog, argv = os.path.basename(sys.argv[0]), sys.argv[1:]
if prog == "ssh":
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
        # A build VM: it never reboots here, so its boot ID is constant.
        if "boot_id" in cmd or "LastBootUpTime" in cmd:
            print("boot-1")
        sys.exit(0)
    with open(os.environ["PVE_LOG"] + ".remote", "a") as f:
        f.write(nodes[host] + "\t" + cmd + "\0")
    env = dict(os.environ, FAKE_PVE_NODE=nodes[host],
               PATH=os.environ["FAKE_PVE_NODEBIN"] + ":" + os.environ["PATH"])
    sys.exit(subprocess.run(["sh", "-c", cmd], env=env, stdin=subprocess.DEVNULL).returncode)

node, w = os.environ["FAKE_PVE_NODE"], [prog] + argv
with open(os.environ["PVE_LOG"], "a") as f:
    f.write(node + "\t" + shlex.join(w) + "\n")
nodes = json.loads(os.environ["FAKE_PVE_HOSTS"])
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


def description(vmid):
    override = os.environ.get("FAKE_PVE_DESC_OVERRIDE", "")
    if override.startswith(vmid + "="):
        return override[len(vmid) + 1:]
    return guests[vmid]["description"]


def local_vm(vmid):
    g = guests.get(vmid)
    if not g or g["node"] != node or g["type"] != "qemu" or vmid == vanished:
        refuse("Configuration file 'nodes/%s/qemu-server/%s.conf' does not exist" % (node, vmid))
    return g


# FAKE_PVE_VANISHED=<vmid>: listed by pvesh, but gone for qm and the
# /etc/pve probe (it was destroyed in between).
vanished = os.environ.get("FAKE_PVE_VANISHED", "")
if prog == "ls":
    shown = []
    if w[1] == "-t":
        shown = [d for d in dumps if fnmatch.fnmatch(d, w[2])][-1:]
    elif w[1:] == ["-d", "/etc/pve/nodes"]:
        # FAKE_PVE_CLUSTERFS_DOWN: pmxcfs is not mounted.
        shown = [] if os.environ.get("FAKE_PVE_CLUSTERFS_DOWN") else ["/etc/pve/nodes"]
    else:
        for word in w[1:]:
            m = word.rsplit("/", 1)[-1].split(".")[0]
            if m in guests and m != vanished and ("/lxc/" in word) == (guests[m]["type"] == "lxc"):
                shown.append(word.replace("*", guests[m]["node"]))
    for s in shown:
        print(s)
    done(0 if shown else 2)
elif prog == "du":
    print("1.0G\t" + w[-1])
elif prog in ("rm", "pvesm"):
    pass
elif prog == "pct":
    refuse("no container " + w[2])
elif prog == "pvesh":
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
        # A guest's "scsi0" overrides its boot disk line; the default is 20G,
        # the recipe's DISK_GB in the shared fixture.
        cfg = {"name": guests[vmid]["name"], "description": description(vmid),
               "scsi0": guests[vmid].get("scsi0", "build-store:vm-%s-disk-0,discard=on,size=20G" % vmid)}
        if guests[vmid]["template"]:
            cfg["template"] = 1
        print(json.dumps(cfg))
    else:
        refuse("unexpected: " + shlex.join(w))
elif prog == "vzdump":
    local_vm(w[1])
    dumps.append("/var/lib/vz/dump/vzdump-qemu-" + w[1] + "-2026_11_08-00_00_00.vma.zst")
elif prog == "scp":
    dest_host, _, dest_path = w[2].partition(":")
    state["dumps"].setdefault(nodes[dest_host.split("@")[1]], []).append(dest_path)
elif prog == "qmrestore":
    if w[1] not in dumps:
        refuse("qmrestore: no dump " + w[1])
    if w[2] in guests:
        refuse("qmrestore: VMID " + w[2] + " exists")
    source = w[1].split("-")[2]
    guests[w[2]] = dict(guests[source], node=node, status="stopped",
                        storage=opts(w[3:]).get("storage"))
elif prog == "qm":
    sub = w[1]
    if sub == "guest":
        vmid = w[3]
        local_vm(vmid)
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
            refuse("unexpected: " + shlex.join(w))
        done()
    vmid = w[2]
    if sub == "config" and os.environ.get("FAKE_PVE_QM_CONFIG_ERROR"):
        # Any failure other than "does not exist", e.g. pmxcfs unreachable.
        print(os.environ["FAKE_PVE_QM_CONFIG_ERROR"], file=sys.stderr)
        done(255)
    g = local_vm(vmid)
    if sub == "clone":
        new = w[3]
        if new in guests:
            refuse("clone: VMID " + new + " exists")
        o = opts(w[4:])
        guests[new] = dict(g, node=node, name=o["name"], template=0, status="stopped",
                           description=o.get("description", ""))
    elif sub == "config":
        print("description: " + description(vmid))
        print("name: " + g["name"])
        if g["template"]:
            print("template: 1")
    elif sub == "set":
        o = opts(w[3:])
        if "memory" in o and os.environ.get("FAKE_PVE_SET_FAIL"):
            refuse("set failed")
        if o.get("description", "").startswith("image=") and os.environ.get("FAKE_PVE_DESC_FAIL"):
            refuse("set failed")
        for key in ("name", "description"):
            if key in o:
                g[key] = o[key]
    elif sub == "start":
        g["status"] = "running"
    elif sub == "shutdown":
        if os.environ.get("FAKE_PVE_SHUTDOWN_FAIL"):
            refuse("shutdown timed out")
        g["status"] = "stopped"
    elif sub == "stop":
        g["status"] = "stopped"
        if os.environ.get("FAKE_PVE_RETAG_ON_STOP"):
            g["description"] = os.environ["FAKE_PVE_RETAG_ON_STOP"]
    elif sub == "template":
        if os.environ.get("FAKE_PVE_TEMPLATE_FAIL"):
            refuse("template failed")
        g["template"] = 1
        done(int(os.environ.get("FAKE_PVE_TEMPLATE_RC", "0")))
    elif sub == "destroy":
        del guests[vmid]
    elif sub == "status":
        print("status: " + g["status"])
    else:
        refuse("unexpected: " + shlex.join(w))
else:
    refuse("unexpected: " + shlex.join(w))
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
    node_bin = bin_dir / "node"
    node_bin.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "ssh"
    fake.write_text(FAKE_PVE)
    fake.chmod(0o755)
    for tool in NODE_TOOLS:
        (node_bin / tool).symlink_to(fake)
    (tmp_path / "pve-state.json").write_text(json.dumps({"guests": guests}))
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        CALL_LOG=env.get("CALL_LOG", str(tmp_path / "calls.log")),
        PVE_STATE=str(tmp_path / "pve-state.json"),
        PVE_LOG=str(tmp_path / "pve.log"),
        FAKE_PVE_HOSTS=json.dumps(HOSTS),
        FAKE_PVE_NODEBIN=str(node_bin),
    )
    return env


def _pve_env(tmp_path: Path, guests: dict, os_family: str = "linux", **recipe):
    env, recipe_path = _build_env(tmp_path, os_family, **recipe)
    _fake_pve(tmp_path, env, guests)
    Path(env["IMAGE_BUILD_CONFIG"]).write_text(PVE_CONFIG)
    env["IMAGE_BUILD_BACKENDS_DIR"] = str(BACKENDS)
    return env, recipe_path


def _pve_cmds(tmp_path: Path, node=None) -> list:
    """Every node tool invocation, as a shell-quoted command line."""
    log = tmp_path / "pve.log"
    if not log.exists():
        return []
    rows = [line.split("\t", 1) for line in log.read_text().splitlines()]
    return [cmd for n, cmd in rows if node is None or n == node]


def _ssh_calls(tmp_path: Path) -> list:
    log = tmp_path / "calls.log"
    return [l for l in log.read_text().splitlines() if l.startswith("ssh ")] if log.exists() else []


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "pve-state.json").read_text())["guests"]


def _yaml(result) -> str:
    return result.stdout.rstrip("\n").splitlines()[-1]


def _clones(tmp_path: Path) -> list:
    return [c for c in _pve_cmds(tmp_path) if c.startswith("qm clone ")]


def _destroys(tmp_path: Path) -> list:
    return [c for c in _pve_cmds(tmp_path) if c.startswith(("qm stop", "qm destroy"))]


def test_proxmox_vmid_picks_lowest_free_pair(tmp_path):
    # 8000 is taken, and 8001's partner 8501 is taken: the first free pair is 8002/8502.
    guests = _cluster(**{"8000": _guest("pve", "taken"), "8501": _guest("pve2", "taken")})
    env, recipe = _pve_env(tmp_path, guests)
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    clones = _clones(tmp_path)
    assert len(clones) == 1 and clones[0].startswith(f"qm clone {BASE_VMID} 8002 --name ib-"), clones
    assert _yaml(result) == "template_locations: {pve: 8002, pve2: 8502}"


def test_proxmox_vmid_skips_lxc_occupants(tmp_path):
    # Containers hold VMIDs too: an LXC at 8000, and one at 8501 (8001's partner).
    guests = _cluster(**{"8000": _guest("pve", "ct", kind="lxc"),
                         "8501": _guest("pve2", "ct2", kind="lxc")})
    env, recipe = _pve_env(tmp_path, guests)
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert _clones(tmp_path)[0].startswith(f"qm clone {BASE_VMID} 8002 "), _clones(tmp_path)
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
    """Every VMID a node tool invocation names: numeric arguments of qm/
    qmrestore/vzdump/pct, and VMIDs in a pvesh, /etc/pve or dump path."""
    words = shlex.split(cmd)
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
    assert any(c.startswith("qm destroy 8000") for c in commands)
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


def _base_disk(scsi0: str) -> dict:
    guests = _cluster()
    guests[str(BASE_VMID)]["scsi0"] = scsi0
    return guests


def test_proxmox_disk_gb_equal_to_base_passes(tmp_path):
    env, recipe = _pve_env(tmp_path, _base_disk("build-store:vm-8100-disk-0,discard=on,size=20G"))
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert len(_clones(tmp_path)) == 1

    # A base measured in T: 1T is 1024G.
    env, recipe = _pve_env(tmp_path / "t", _base_disk("build-store:vm-8100-disk-0,size=1T"), DISK_GB="1024")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert len(_clones(tmp_path / "t")) == 1


def test_proxmox_disk_gb_differing_from_base_refused(tmp_path):
    # DISK_GB larger than the base, then smaller: either way the build stops
    # before any VM exists, and the message names both sizes and the way out.
    for name, disk_gb in (("larger", "40"), ("smaller", "10")):
        env, recipe = _pve_env(tmp_path / name, _cluster(), DISK_GB=disk_gb)
        result = _build(env, recipe, "proxmox")
        assert result.returncode == 1, result.stderr
        assert f"DISK_GB is {disk_gb}G but base template {BASE_VMID} has a 20G disk" in result.stderr, result.stderr
        assert "take the base template's disk size" in result.stderr, result.stderr
        assert "DISK_GB=20" in result.stderr, result.stderr
        assert _clones(tmp_path / name) == []
        assert _destroys(tmp_path / name) == []
        assert set(_state(tmp_path / name)) == set(_cluster())


def test_proxmox_disk_size_spellings_equal_to_disk_gb(tmp_path):
    # The same 20 GiB as Proxmox may write it, with options reordered and spaced.
    for n, scsi0 in enumerate(("build-store:vm-8100-disk-0,discard=on,size=20.0G",
                               "build-store:vm-8100-disk-0,size=20480M",
                               "build-store:vm-8100-disk-0,size=20971520K",
                               "size=20G,build-store:vm-8100-disk-0,discard=on",
                               "build-store:vm-8100-disk-0 , discard=on , size=20G ,ssd=1",
                               "build-store:vm-8100-disk-0,discard=on, size = 20G")):
        env, recipe = _pve_env(tmp_path / str(n), _base_disk(scsi0))
        result = _build(env, recipe, "proxmox")
        assert result.returncode == 0, (scsi0, result.stderr)
        assert len(_clones(tmp_path / str(n))) == 1, scsi0


def test_proxmox_disk_size_not_a_whole_gib_is_a_mismatch(tmp_path):
    # 20.5G and 3584M are not DISK_GB GiB; the message shows the size as written
    # and offers no DISK_GB value, since none matches.
    for n, written in enumerate(("20.5G", "3584M")):
        env, recipe = _pve_env(tmp_path / str(n),
                               _base_disk(f"build-store:vm-8100-disk-0,size={written}"))
        result = _build(env, recipe, "proxmox")
        assert result.returncode == 1, result.stderr
        assert f"DISK_GB is 20G but base template {BASE_VMID} has a {written} disk" in result.stderr, result.stderr
        assert "take the base template's disk size" in result.stderr, result.stderr
        assert "set DISK_GB=" not in result.stderr, result.stderr
        assert _clones(tmp_path / str(n)) == []

    # A whole GiB written in M names the GiB value to set.
    env, recipe = _pve_env(tmp_path / "m", _base_disk("build-store:vm-8100-disk-0,size=2048M"))
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert "has a 2048M disk" in result.stderr and "set DISK_GB=2 in the recipe" in result.stderr, result.stderr
    assert _clones(tmp_path / "m") == []


def test_proxmox_unreadable_base_disk_size_refused(tmp_path):
    for n, scsi0 in enumerate(("build-store:vm-8100-disk-0,discard=on",   # no size
                               "build-store:vm-8100-disk-0,size=20X",     # unknown unit
                               "build-store:vm-8100-disk-0,size=abcG",
                               "build-store:vm-8100-disk-0,size=G")):
        env, recipe = _pve_env(tmp_path / str(n), _base_disk(scsi0))
        result = _build(env, recipe, "proxmox")
        assert result.returncode == 1, (scsi0, result.stderr)
        assert "cannot read the size of the scsi0 disk of base template 8100" in result.stderr, result.stderr
        assert _clones(tmp_path / str(n)) == []

    # A base with no scsi0 at all.
    guests = _cluster()
    guests[str(BASE_VMID)]["scsi0"] = ""
    env, recipe = _pve_env(tmp_path / "none", guests)
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert _clones(tmp_path / "none") == []


def test_proxmox_save_replicates_to_plus_500(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    image_id, image_name = _build_log(tmp_path).read_text().splitlines()[1].split("\t")[2:4]
    assert image_id == "pve:8000,pve2:8500"
    assert _yaml(result) == "template_locations: {pve: 8000, pve2: 8500}"
    pve2 = _pve_cmds(tmp_path, "pve2")
    assert ("qmrestore /var/lib/vz/dump/vzdump-qemu-8000-2026_11_08-00_00_00.vma.zst 8500 "
            "--storage replica-store") in pve2, pve2
    assert not any("/cluster/nextid" in c for c in _pve_cmds(tmp_path))
    # The build VM became the template: the core destroys nothing.
    assert _destroys(tmp_path) == []
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


def test_proxmox_save_falls_back_to_stop(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_PVE_SHUTDOWN_FAIL"] = "1"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    pve = _pve_cmds(tmp_path, "pve")
    assert pve.index("qm stop 8000") < pve.index("qm template 8000"), pve


def test_proxmox_template_reported_failure_keeps_template(tmp_path):
    # qm template converted the VM, but the call reported failure: the build
    # fails, naming V, and the template is neither destroyed nor replicated.
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_PVE_TEMPLATE_RC"] = "255"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert "VM 8000 is now a template" in result.stderr, result.stderr
    assert _destroys(tmp_path) == []
    assert _state(tmp_path)["8000"]["template"] == 1
    assert not any(c.startswith(("vzdump", "qmrestore")) for c in _pve_cmds(tmp_path))
    assert not _build_log(tmp_path).exists()

    # A conversion that did not happen leaves a build VM, which is destroyed.
    env, recipe = _pve_env(tmp_path / "nottemplate", _cluster())
    env["FAKE_PVE_TEMPLATE_FAIL"] = "1"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert "8000" in result.stderr
    assert _destroys(tmp_path / "nottemplate") == ["qm stop 8000", "qm destroy 8000 --purge 1"]
    assert "8000" not in _state(tmp_path / "nottemplate")


def test_proxmox_leftovers_only_tagged_expired(tmp_path):
    guests = _cluster(**{
        "8003": _guest("pve", "expired", status="running", description=EXPIRED),
        "8004": _guest("pve", "unexpired", status="running", description=UNEXPIRED),
        "8005": _guest("pve", "untagged", description="image=enpm634-old-abc1234-202601010000"),
        "8006": _guest("pve", "template", template=1, description=EXPIRED),
    })
    env, _recipe = _pve_env(tmp_path, guests)
    result = _build(env, "--reap", "proxmox", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["would destroy 8003"], result.stdout
    assert _destroys(tmp_path) == []

    result = _build(env, "--reap", "proxmox")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["destroyed 8003"], result.stdout
    assert _destroys(tmp_path) == ["qm stop 8003", "qm destroy 8003 --purge 1"]
    state = _state(tmp_path)
    assert "8003" not in state
    assert {"8004", "8005", "8006", "9997"} <= set(state)


def test_proxmox_destroy_rechecks_tag(tmp_path):
    # Control: a failed build destroys its own, still-tagged VM.
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"
    assert _build(env, recipe, "proxmox").returncode == 1
    assert _destroys(tmp_path) == ["qm stop 8000", "qm destroy 8000 --purge 1"]
    assert "8000" not in _state(tmp_path)

    # The description no longer names this run: the VM is left alone.
    env, recipe = _pve_env(tmp_path / "changed", _cluster())
    env.update(FAKE_SSH_FAIL_ON="verify.sh", FAKE_PVE_DESC_OVERRIDE="8000=owner=someone-else")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert _destroys(tmp_path / "changed") == []
    assert "8000" in _state(tmp_path / "changed")
    assert "not destroyed" in result.stderr, result.stderr

    # The tag disappears between `qm stop` and `qm destroy` (the VMID was
    # freed and reused): the node-side re-check stops the destroy.
    env, recipe = _pve_env(tmp_path / "reused", _cluster())
    env.update(FAKE_SSH_FAIL_ON="verify.sh", FAKE_PVE_RETAG_ON_STOP="owner=someone-else")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert _destroys(tmp_path / "reused") == ["qm stop 8000"]
    assert "8000" in _state(tmp_path / "reused")
    assert "not destroyed" in result.stderr, result.stderr

    # The same for a reap.
    env, _recipe = _pve_env(tmp_path / "reap", _cluster(**{"8003": _guest("pve", "x", description=EXPIRED)}))
    env["FAKE_PVE_RETAG_ON_STOP"] = UNEXPIRED
    result = _build(env, "--reap", "proxmox")
    assert result.returncode == 1
    assert _destroys(tmp_path / "reap") == ["qm stop 8003"]
    assert "8003" in _state(tmp_path / "reap")


# Expired, but the image-build value is not a RUN_ID the core could have made.
HAND_TAGGED = {
    "8007": "image-build=x image-build-expires=202601010400",
    "8008": "image-build=ib-202601010000-ABCD image-build-expires=202601010400",
    "8009": "image-build=ib-2026010100-abcd image-build-expires=202601010400",
    "8010": EXPIRED + " image-build=ib-202601010000-ffff",
}


def test_proxmox_reap_requires_run_id_format(tmp_path):
    guests = _cluster(**{v: _guest("pve", "hand", status="running", description=d)
                         for v, d in HAND_TAGGED.items()})
    guests["8003"] = _guest("pve", "expired", status="running", description=EXPIRED)
    env, _recipe = _pve_env(tmp_path, guests)
    result = _build(env, "--reap", "proxmox")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["destroyed 8003"], result.stdout
    assert _destroys(tmp_path) == ["qm stop 8003", "qm destroy 8003 --purge 1"]
    assert set(HAND_TAGGED) <= set(_state(tmp_path))

    # The node-side check alone: the tag turns into a hand-made one between
    # `qm stop` and `qm destroy`.
    env, _recipe = _pve_env(tmp_path / "node", _cluster(**{"8003": _guest("pve", "x", status="running",
                                                                         description=EXPIRED)}))
    env["FAKE_PVE_RETAG_ON_STOP"] = HAND_TAGGED["8007"]
    result = _build(env, "--reap", "proxmox")
    assert result.returncode == 1
    assert _destroys(tmp_path / "node") == ["qm stop 8003"]
    assert "8003" in _state(tmp_path / "node")


def test_proxmox_clone_tagged_so_failed_set_is_destroyed(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_PVE_SET_FAIL"] = "1"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    clone = shlex.split(_clones(tmp_path)[0])
    desc = clone[clone.index("--description") + 1]
    assert re.fullmatch(r"image-build=ib-\d{12}-[0-9a-f]{4} image-build-expires=\d{12}", desc), desc
    assert _destroys(tmp_path) == ["qm stop 8000", "qm destroy 8000 --purge 1"]
    assert "8000" not in _state(tmp_path)


def test_proxmox_start_tags_and_configures(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"  # stop before save rewrites the description
    _build(env, recipe, "proxmox")
    pve = _pve_cmds(tmp_path, "pve")
    clone = shlex.split(_clones(tmp_path)[0])
    assert clone[:4] == ["qm", "clone", str(BASE_VMID), "8000"], clone
    opts = dict(zip(clone[4::2], clone[5::2]))
    run_id, expires = re.fullmatch(r"image-build=(ib-\d{12}-[0-9a-f]{4}) image-build-expires=(\d{12})",
                                   opts["--description"]).groups()
    assert opts == {"--name": run_id, "--full": "1", "--storage": "build-store",
                    "--description": f"image-build={run_id} image-build-expires={expires}"}
    assert "qm set 8000 --memory 2048 --cores 2 --net0 virtio,bridge=vmbr9,tag=999" in pve, pve
    assert "lab-playground-site" not in "".join(pve)
    assert pve.index("qm start 8000") > pve.index("qm set 8000 --memory 2048 --cores 2 "
                                                  "--net0 virtio,bridge=vmbr9,tag=999")


def _guest_exec(tmp_path: Path) -> list:
    return [shlex.split(c) for c in _pve_cmds(tmp_path) if c.startswith("qm guest exec 8000 ")]


def test_proxmox_linux_key_and_ssh(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    execs = _guest_exec(tmp_path)
    assert len(execs) == 1, execs
    assert execs[0][-3:-1] == ["/bin/sh", "-c"], execs
    script = execs[0][-1]
    assert "printf '%s\\n' 'ssh-ed25519 AAAAC3FAKEPUBLIC image-build' >> /home/builder/.ssh/authorized_keys" in script
    vm_ssh = [l for l in _ssh_calls(tmp_path) if " builder@192.0.2.10 " in l]
    assert vm_ssh and all("-J root@pve-host.invalid" in l for l in vm_ssh), vm_ssh


def test_proxmox_windows_key_acl_and_ssh(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster(), "windows")
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    execs = _guest_exec(tmp_path)
    assert len(execs) == 1, execs
    assert execs[0][-2] == "-EncodedCommand", execs
    script = base64.b64decode(execs[0][-1]).decode("utf-16-le")
    assert "Set-Content -Path $file -Value 'ssh-ed25519 AAAAC3FAKEPUBLIC image-build'" in script
    assert "$file = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'" in script
    # Administrators and SYSTEM full control, inheritance removed.
    assert "icacls.exe $file /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F'" in script
    vm_ssh = [l for l in _ssh_calls(tmp_path) if " Administrator@192.0.2.10 " in l]
    assert vm_ssh and all("-J root@pve-host.invalid" in l for l in vm_ssh), vm_ssh


def test_proxmox_failed_key_injection_fails_build(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_PVE_EXEC_EXITCODE"] = "1"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert not any(" builder@192.0.2.10 " in l for l in _ssh_calls(tmp_path))
    assert "8000" not in _state(tmp_path)


def test_proxmox_dry_run_touches_nothing(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    result = _build(env, "--dry-run", recipe, "proxmox")
    assert result.returncode == 0, result.stderr
    assert _pve_cmds(tmp_path) == []
    assert _ssh_calls(tmp_path) == []
    for printed in ("DRY-RUN: _pve_base_disk_gb ", "DRY-RUN: pve qm clone ", "DRY-RUN: pve qm template 8000",
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
    assert _ssh_calls(tmp_path) == []


def test_proxmox_config_unsafe_values_refused(tmp_path):
    # Every config value reaches a root shell on a node: shell syntax is refused
    # before any ssh call.
    for i, (key, value) in enumerate((("PVE_STORAGE", "local;id"), ("PVE_BUILD_BRIDGE", "vmbr0 x"),
                                      ("PVE_NODE", "pve$(id)"), ("PVE_REPLICA_STORAGE", "a'b"),
                                      ("PVE_HOST", "-oProxyCommand=x"),
                                      ("PVE_BUILD_VLAN", "4095"), ("PVE_BUILD_VLAN", "0"))):
        sub = tmp_path / str(i)
        env, recipe = _pve_env(sub, _cluster())
        config = re.sub(rf"^{key}=.*$", f"{key}={shlex.quote(value)}", PVE_CONFIG, flags=re.M)
        Path(env["IMAGE_BUILD_CONFIG"]).write_text(config)
        result = _build(env, recipe, "proxmox")
        assert result.returncode == 2, (key, value, result.stderr)
        assert key in result.stderr, (key, result.stderr)
        assert _ssh_calls(sub) == [], key


def test_proxmox_node_names_checked_at_config_time(tmp_path):
    # templates.yaml takes [a-z0-9-] node names: anything else is refused
    # before the build, not after the image is saved and logged.
    for i, (key, value) in enumerate((("PVE_NODE", "Pve"), ("PVE_NODE", "pve.lan"),
                                      ("PVE_REPLICA_NODE", "pve_2"))):
        sub = tmp_path / str(i)
        env, recipe = _pve_env(sub, _cluster())
        config = re.sub(rf"^{key}=.*$", f"{key}={value}", PVE_CONFIG, flags=re.M)
        Path(env["IMAGE_BUILD_CONFIG"]).write_text(config)
        result = _build(env, recipe, "proxmox")
        assert result.returncode == 2, (key, value, result.stderr)
        assert key in result.stderr, (key, result.stderr)
        assert _ssh_calls(sub) == [], key


def test_proxmox_failed_description_rewrite_names_the_template(tmp_path):
    env, recipe = _pve_env(tmp_path, _cluster())
    env["FAKE_PVE_DESC_FAIL"] = "1"
    result = _build(env, recipe, "proxmox")
    assert result.returncode == 1
    assert "template 8000" in result.stderr, result.stderr
    assert "build tags" in result.stderr, result.stderr
    state = _state(tmp_path)["8000"]
    assert state["template"] == 1 and state["description"].startswith("image-build=ib-")
    assert _destroys(tmp_path) == []
    assert not any(c.startswith(("vzdump", "qmrestore")) for c in _pve_cmds(tmp_path))
    assert not _build_log(tmp_path).exists()


def test_proxmox_node_destroy_absent_means_does_not_exist(tmp_path):
    def reap(sub, **extra):
        env, _recipe = _pve_env(tmp_path / sub, _cluster(**{"8003": _guest("pve", "x", description=EXPIRED)}))
        env.update(FAKE_PVE_VANISHED="8003", **extra)
        return _build(env, "--reap", "proxmox")

    # Really gone between the listing and the node-side destroy: nothing to do.
    result = reap("gone")
    assert result.returncode == 0, result.stderr
    assert "VM 8003 does not exist" in result.stderr
    assert _destroys(tmp_path / "gone") == []

    # qm config failed for another reason: not "absent", the destroy fails.
    result = reap("qmerror", FAKE_PVE_QM_CONFIG_ERROR="ipcc_send_rec[1] failed: Connection refused")
    assert result.returncode == 1, result.stderr
    assert "could not destroy 8003" in result.stderr
    assert "does not exist" not in result.stderr
    assert "Connection refused" in result.stderr
    assert _destroys(tmp_path / "qmerror") == []

    # "does not exist", but /etc/pve is not there to confirm it (pmxcfs down).
    result = reap("pmxcfs", FAKE_PVE_CLUSTERFS_DOWN="1")
    assert result.returncode == 1, result.stderr
    assert "could not destroy 8003" in result.stderr
    assert "/etc/pve" in result.stderr
    assert _destroys(tmp_path / "pmxcfs") == []


# --- sync_template.sh -------------------------------------------------------------


def _sync(tmp_path: Path, *args, **extra_env):
    env = _fake_pve(tmp_path, dict(os.environ), {"8002": _guest("pve", "tmpl", template=1),
                                                 "8502": _guest("pve2", "other")})
    env.update(extra_env)
    result = subprocess.run(["bash", str(SYNC), *args], env=env, capture_output=True, text=True,
                            timeout=60, stdin=subprocess.DEVNULL)
    return result, _pve_cmds(tmp_path, "pve2")


SYNC_ARGS = ("pve-host.invalid", "8002", "pve2-host.invalid", "replica-store")


def test_sync_template_fixed_target_vmid(tmp_path):
    for vmid in ("8500", "8999"):
        result, pve2 = _sync(tmp_path / vmid, *SYNC_ARGS, vmid, FAKE_PVE_NEXTID="8124")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not any("/cluster/nextid" in c for c in pve2), pve2
        restores = [c for c in pve2 if c.startswith("qmrestore ")]
        assert restores == ["qmrestore /var/lib/vz/dump/vzdump-qemu-8002-2026_11_08-00_00_00.vma.zst "
                            f"{vmid} --storage replica-store"], pve2
        assert f"pve2-host.invalid: {vmid}" in result.stdout
        assert _state(tmp_path / vmid)[vmid]["template"] == 1

    # Without the argument, the next free VMID is asked for, as before.
    result, pve2 = _sync(tmp_path / "next", *SYNC_ARGS, FAKE_PVE_NEXTID="8124")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pvesh get /cluster/nextid" in pve2
    assert [c for c in pve2 if c.startswith("qmrestore ")][0].endswith(" 8124 --storage replica-store")
    assert "Next available VMID: 8124" in result.stdout

    # Outside 8500-8999, not a number, or a sixth argument: refused before any ssh.
    for sub, extra in (("v9000", ["9000"]), ("v8499", ["8499"]), ("v99999", ["99999"]),
                       ("bad", ["85x2"]), ("empty", [""]), ("six", ["8501", "x"])):
        result, _pve2 = _sync(tmp_path / sub, *SYNC_ARGS, *extra)
        assert result.returncode == 1, (sub, result.stdout)
        assert _ssh_calls(tmp_path / sub) == [], sub
    assert "8500-8999" in _sync(tmp_path / "msg", *SYNC_ARGS, "9000")[0].stdout

    # A target VMID already in use is refused before anything is dumped.
    result, _pve2 = _sync(tmp_path / "used", *SYNC_ARGS, "8502")
    assert result.returncode == 1, result.stdout
    assert "already in use" in result.stdout
    assert not any(c.startswith(("vzdump", "qmrestore")) for c in _pve_cmds(tmp_path / "used"))
