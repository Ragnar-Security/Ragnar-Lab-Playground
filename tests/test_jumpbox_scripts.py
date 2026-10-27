"""The jumpbox is the router for every lab VLAN. Its tooling must rebuild that
router exactly, never rewrite live network config by surprise, keep the
Tailscale key off every command line, and never lock the operator out."""

import importlib.util
import os
import pty
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
NETGEN = SCRIPTS / "jumpbox_netgen.py"
CONFIGURE = SCRIPTS / "jumpbox_configure.sh"
PROVISION = SCRIPTS / "jumpbox_provision.sh"
ACCOUNTS = SCRIPTS / "jumpbox_accounts.sh"

LOG_CALL = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$CALL_LOG"\n'
MUTATING = (
    "apt-get", "snap", "lvextend", "netplan apply", "netplan try",
    "systemctl restart", "systemctl reload", "systemctl enable", "systemctl try", "sysctl -p",
    "qm create", "qm set", "qm resize", "qm start", "qm destroy", "qm stop",
    "curl", "passwd -l", "passwd -u", "chage",
)


def _netgen():
    spec = importlib.util.spec_from_file_location("jumpbox_netgen", NETGEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _base_env(tmp_path: Path, site_text: str) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    site = tmp_path / "site.env"
    site.write_text(site_text)
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("JUMPBOX_") and k not in ("HEADSCALE_URL", "SUDO_USER")
    }
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CALL_LOG=str(tmp_path / "calls.log"),
        SITE_ENV_FILE=str(site),
    )
    _tool(bin_dir, "id", 'if [ "$1" = "-u" ]; then echo 0; exit 0; fi\nexec /usr/bin/id "$@"\n')
    return env


def _calls(tmp_path: Path) -> str:
    log = tmp_path / "calls.log"
    return log.read_text() if log.exists() else ""


def _mutations(calls: str) -> list:
    return [line for line in calls.splitlines() if line.startswith(MUTATING)]


def _tree(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _run(script: Path, args, env, input=None, stdin=None):
    kwargs = {"input": input} if input is not None else {"stdin": stdin if stdin is not None else subprocess.DEVNULL}
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True,
        timeout=60, **kwargs,
    )


# --- address plan -----------------------------------------------------------

@pytest.mark.parametrize("tag, gateway, first, last", [
    (100, "10.100.0.1/24", "10.100.0.50", "10.100.0.254"),
    (200, "10.200.0.1/24", "10.200.0.50", "10.200.0.254"),
    (300, "10.100.2.1/24", "10.100.2.50", "10.100.2.254"),
    (399, "10.199.2.1/24", "10.199.2.50", "10.199.2.254"),
    (400, "10.100.4.1/24", "10.100.4.50", "10.100.4.254"),
    (499, "10.199.4.1/24", "10.199.4.50", "10.199.4.254"),
])
def test_vlan_gateway_and_dhcp_pool_follow_the_lab_address_plan(tag, gateway, first, last):
    ng = _netgen()
    bands = ng.parse_bands(ng.DEFAULT_BANDS)
    assert ng.gateway(tag, bands) == gateway
    assert ng.dhcp_pool(tag, bands) == (first, last)


@pytest.mark.parametrize("tag", [99, 201, 250, 299, 501])
def test_vlans_outside_the_routed_bands_get_no_gateway(tag):
    ng = _netgen()
    bands = ng.parse_bands(ng.DEFAULT_BANDS)
    assert tag not in ng.vlan_tags(bands)
    with pytest.raises(ValueError):
        ng.gateway(tag, bands)


def test_jumpbox_routes_every_vlan_the_portal_hands_out_for_entry_and_group_networks():
    # A VLAN the portal assigns but the jumpbox does not serve leaves that
    # student's VMs without DHCP or a route; the allocator bounds are inclusive.
    from app import vlan_allocator

    ng = _netgen()
    bands = ng.parse_bands(ng.DEFAULT_BANDS)
    (user_lo, user_hi), _user_internal = vlan_allocator._pool_for("user")
    (ge_lo, ge_hi), (gi_lo, gi_hi) = vlan_allocator._pool_for("group")
    portal = (set(range(user_lo, user_hi + 1)) | set(range(ge_lo, ge_hi + 1))
              | set(range(gi_lo, gi_hi + 1)))
    served = set(ng.vlan_tags(bands))
    assert sorted(portal - served) == []
    gateways = [ng.gateway(t, bands) for t in served]
    assert len(set(gateways)) == len(gateways)


def test_overlapping_or_out_of_range_bands_are_rejected():
    ng = _netgen()
    with pytest.raises(ValueError):
        ng.parse_bands("a:100-200:0:0 b:150-250:0:1")
    with pytest.raises(ValueError):
        ng.parse_bands("a:100-300:0:0")  # 10.256.0.0 is not an address


# --- generated files ----------------------------------------------------------

def test_generated_netplan_is_valid_yaml_with_a_gateway_on_every_lab_vlan():
    ng = _netgen()
    bands = ng.parse_bands(ng.DEFAULT_BANDS)
    doc = yaml.safe_load(ng.render_netplan("ens18", "ens19", bands))
    net = doc["network"]
    assert net["version"] == 2
    assert net["ethernets"]["ens18"] == {"dhcp4": True}
    assert net["ethernets"]["ens19"] == {"dhcp4": False}
    vlans = net["vlans"]
    assert set(vlans) == {f"vlan{t}" for t in ng.vlan_tags(bands)}
    assert vlans["vlan100"] == {"id": 100, "link": "ens19", "addresses": ["10.100.0.1/24"]}
    assert vlans["vlan399"] == {"id": 399, "link": "ens19", "addresses": ["10.199.2.1/24"]}
    assert vlans["vlan499"] == {"id": 499, "link": "ens19", "addresses": ["10.199.4.1/24"]}
    with pytest.raises(ValueError):
        ng.render_netplan("ens18", "ens19\n  evil: true", bands)


def test_dnsmasq_serves_twelve_hour_leases_on_lab_vlans_and_never_on_management():
    ng = _netgen()
    bands = ng.parse_bands(ng.DEFAULT_BANDS)
    lines = ng.render_dnsmasq(
        "ens18", bands, upstreams=["192.0.2.53"],
        reservations=["group-internal=02:00:00:00:00:01,dc,10"],
    ).splitlines()
    for required in ("no-resolv", "server=192.0.2.53", "no-dhcp-interface=ens18",
                     "interface=vlan*", "bind-dynamic"):
        assert required in lines
    ranges = [l for l in lines if l.startswith("dhcp-range=")]
    assert len(ranges) == len(ng.vlan_tags(bands))
    assert "dhcp-range=10.100.0.50,10.100.0.254,255.255.255.0,12h" in ranges
    assert "dhcp-range=10.100.2.50,10.100.2.254,255.255.255.0,12h" in ranges
    assert "dhcp-range=10.199.4.50,10.199.4.254,255.255.255.0,12h" in ranges
    hosts = [l for l in lines if l.startswith("dhcp-host=")]
    internal = [t for t in ng.vlan_tags(bands) if 400 <= t]
    assert len(hosts) == len(internal)
    assert "dhcp-host=02:00:00:00:00:01,dc,10.100.4.10,infinite" in hosts
    assert all(re.search(r"10\.\d+\.4\.10,", h) for h in hosts)


def test_zeek_watches_the_trunk_and_treats_lab_routes_as_local():
    ng = _netgen()
    assert "interface=ens19" in ng.render_zeek_node("ens19").splitlines()
    assert ng.render_zeek_networks(["10.0.0.0/8"]).splitlines()[-1].startswith("10.0.0.0/8")


# --- configure (runs on the jumpbox) ----------------------------------------

SITE = (
    "HEADSCALE_URL=https://vpn.example.edu\n"
    "JUMPBOX_MGMT_IF=ens18\n"
    "JUMPBOX_TRUNK_IF=ens19\n"
)


def _configure_env(tmp_path: Path, site_extra: str = ""):
    sysroot = tmp_path / "root"
    for d in ("etc/netplan", "sys/class/net/ens18", "sys/class/net/ens19"):
        (sysroot / d).mkdir(parents=True)
    env = _base_env(tmp_path, SITE + site_extra)
    bin_dir = tmp_path / "bin"
    for name in ("netplan", "systemctl", "sysctl", "apt-get", "snap", "dnsmasq", "lvextend"):
        _tool(bin_dir, name, LOG_CALL)
    _tool(bin_dir, "qemu-ga", "exit 0\n")  # guest agent already installed
    _tool(bin_dir, "findmnt", "echo /dev/vda1\n")
    env["JUMPBOX_SYSROOT"] = str(sysroot)
    return env, sysroot


NETPLAN = "etc/netplan/60-jumpbox-vlans.yaml"
DNSMASQ = "etc/dnsmasq.d/jumpbox.conf"
SYSCTL = "etc/sysctl.d/90-jumpbox-router.conf"


def test_configure_dry_run_shows_the_change_and_writes_nothing(tmp_path):
    env, sysroot = _configure_env(tmp_path)
    before = _tree(sysroot)
    result = _run(CONFIGURE, [], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(sysroot) == before
    assert _mutations(_calls(tmp_path)) == []
    assert "+    vlan100:" in result.stdout
    assert "+dhcp-range=10.199.4.50,10.199.4.254,255.255.255.0,12h" in result.stdout
    assert "+net.ipv4.ip_forward = 1" in result.stdout
    assert "--apply" in result.stdout


def test_configure_apply_converges_and_a_second_run_changes_nothing(tmp_path):
    env, sysroot = _configure_env(tmp_path)
    first = _run(CONFIGURE, ["--apply", "--no-try"], env)
    assert first.returncode == 0, first.stdout + first.stderr
    netplan = sysroot / NETPLAN
    assert yaml.safe_load(netplan.read_text())["network"]["vlans"]["vlan300"]["addresses"] == ["10.100.2.1/24"]
    assert stat.S_IMODE(netplan.stat().st_mode) == 0o600
    assert "dhcp-range=10.100.0.50,10.100.0.254,255.255.255.0,12h" in (sysroot / DNSMASQ).read_text()
    sysctl = (sysroot / SYSCTL).read_text().splitlines()
    for line in ("net.ipv4.ip_forward = 1", "net.ipv6.conf.all.forwarding = 1",
                 "net.ipv4.conf.all.rp_filter = 2", "net.ipv4.conf.default.rp_filter = 2"):
        assert line in sysctl
    calls = _calls(tmp_path)
    assert "netplan apply" in calls
    assert "systemctl restart dnsmasq" in calls
    assert "sysctl -p" in calls

    converged = _tree(sysroot)
    (tmp_path / "calls.log").unlink()
    second = _run(CONFIGURE, ["--apply", "--no-try"], env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _tree(sysroot) == converged
    assert _mutations(_calls(tmp_path)) == []


def test_configure_uses_netplan_try_so_a_bad_change_reverts_itself(tmp_path):
    env, sysroot = _configure_env(tmp_path)
    controller, terminal = pty.openpty()
    try:
        result = _run(CONFIGURE, ["--apply"], env, stdin=terminal)
    finally:
        os.close(terminal)
        os.close(controller)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(tmp_path)
    assert "netplan try --timeout 120" in calls
    assert "netplan apply" not in calls


def test_configure_will_not_apply_netplan_without_a_terminal_to_confirm(tmp_path):
    env, sysroot = _configure_env(tmp_path)
    before = _tree(sysroot)
    result = _run(CONFIGURE, ["--apply"], env)
    assert result.returncode != 0
    assert "netplan try" in result.stderr
    assert _tree(sysroot) == before
    assert _mutations(_calls(tmp_path)) == []


@pytest.mark.parametrize("path, text", [
    ("etc/netplan/70-group-vlans.yaml",
     "network:\n  version: 2\n  vlans:\n    vlan300:\n      id: 300\n      link: ens19\n"
     "      addresses: [10.100.2.1/24]\n"),
    ("etc/dnsmasq.conf",
     "no-resolv\ndhcp-range=ens19.100,10.100.0.50,10.100.0.255,255.255.255.0,12h\n"),
])
def test_configure_refuses_to_apply_over_a_hand_built_config_it_would_duplicate(tmp_path, path, text):
    env, sysroot = _configure_env(tmp_path)
    (sysroot / path).parent.mkdir(parents=True, exist_ok=True)
    (sysroot / path).write_text(text)
    before = _tree(sysroot)
    result = _run(CONFIGURE, ["--apply", "--no-try"], env)
    assert result.returncode != 0
    assert Path(path).name in result.stderr
    assert _tree(sysroot) == before
    assert _mutations(_calls(tmp_path)) == []


def test_configure_grows_an_lvm_root_into_the_whole_volume_group(tmp_path):
    # The Ubuntu installer gives the root LV half the VG; the jumpbox filled
    # its disk and dnsmasq could no longer write its lease file.
    env, sysroot = _configure_env(tmp_path)
    bin_dir = tmp_path / "bin"
    free = tmp_path / "vg_free"
    free.write_text("3839\n")
    env["VG_FREE_FILE"] = str(free)
    _tool(bin_dir, "findmnt", "echo /dev/mapper/ubuntu--vg-ubuntu--lv\n")
    _tool(bin_dir, "lvs", "echo '  ubuntu-vg'\n")
    _tool(bin_dir, "vgs", 'printf "  %s\\n" "$(cat "$VG_FREE_FILE")"\n')
    _tool(bin_dir, "lvextend", LOG_CALL + 'echo 0 > "$VG_FREE_FILE"\n')
    grow = "lvextend -r -l +100%FREE /dev/mapper/ubuntu--vg-ubuntu--lv"

    dry = _run(CONFIGURE, [], env)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert grow in dry.stdout
    assert grow not in _calls(tmp_path)

    applied = _run(CONFIGURE, ["--apply", "--no-try"], env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert _calls(tmp_path).count(grow) == 1
    again = _run(CONFIGURE, ["--apply", "--no-try"], env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _calls(tmp_path).count(grow) == 1


def _fake_tailscale(tmp_path: Path, state: str) -> Path:
    log = tmp_path / "tailscale.log"
    _tool(tmp_path / "bin", "tailscale",
          'printf "ARGV %s\\n" "$*" >> "$TS_LOG"\n'
          'env > "$TS_LOG.env"\n'
          'case "$1" in\n'
          f"  status) echo '{{\"BackendState\": \"{state}\"}}' ;;\n"
          '  up) for a; do case "$a" in --auth-key=file:*)\n'
          '      f="${a#--auth-key=file:}"\n'
          '      printf "KEYFILE %s\\n" "$(cat "$f")" >> "$TS_LOG"\n'
          '      printf "KEYMODE %s\\n" "$(ls -l "$f" | cut -c1-10)" >> "$TS_LOG"\n'
          '      printf "%s" "$f" > "$TS_LOG.path" ;; esac; done ;;\n'
          "esac\n")
    return log


@pytest.mark.parametrize("via", ["env", "stdin"])
def test_tailscale_auth_key_reaches_tailscale_without_ever_being_on_a_command_line(tmp_path, via):
    env, sysroot = _configure_env(tmp_path)
    log = _fake_tailscale(tmp_path, "NeedsLogin")
    env["TS_LOG"] = str(log)
    key = "hskey-auth-" + "Zq8sExampleOnly0123456789"
    args = ["--apply", "--no-try", "--tailscale"]
    if via == "env":
        env["JUMPBOX_TS_AUTHKEY"] = key
        result = _run(CONFIGURE, args, env)
    else:
        result = _run(CONFIGURE, args + ["--authkey-stdin"], env, input=key + "\n")
    assert result.returncode == 0, result.stdout + result.stderr

    text = log.read_text()
    argv = [l for l in text.splitlines() if l.startswith("ARGV")]
    up = next(l for l in argv if l.startswith("ARGV up")).split()
    for flag in ("--login-server=https://vpn.example.edu", "--advertise-routes=10.0.0.0/8",
                 "--advertise-tags=tag:jumpbox", "--snat-subnet-routes=false",
                 "--stateful-filtering=false"):
        assert flag in up
    assert key not in "\n".join(argv)
    assert f"KEYFILE {key}" in text
    assert "KEYMODE -rw-------" in text
    assert not Path((tmp_path / "tailscale.log.path").read_text()).exists()
    assert key not in Path(str(log) + ".env").read_text()
    assert key not in result.stdout + result.stderr + _calls(tmp_path)


def test_tailscale_on_a_joined_jumpbox_reasserts_routing_without_a_key(tmp_path):
    env, sysroot = _configure_env(tmp_path)
    log = _fake_tailscale(tmp_path, "Running")
    env["TS_LOG"] = str(log)
    result = _run(CONFIGURE, ["--apply", "--no-try", "--tailscale"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    argv = [l for l in log.read_text().splitlines() if l.startswith("ARGV")]
    assert not any(l.startswith("ARGV up") for l in argv)
    set_line = next(l for l in argv if l.startswith("ARGV set")).split()
    for flag in ("--advertise-routes=10.0.0.0/8", "--snat-subnet-routes=false",
                 "--stateful-filtering=false"):
        assert flag in set_line


def _ssh_env(tmp_path: Path, authorized_keys: str):
    env, sysroot = _configure_env(tmp_path, "JUMPBOX_ADMIN_USER=labadmin\n")
    home = tmp_path / "home" / "labadmin"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "authorized_keys").write_text(authorized_keys)
    bin_dir = tmp_path / "bin"
    _tool(bin_dir, "getent", f'[ "$2" = labadmin ] && echo "labadmin:x:1000:1000::{home}:/bin/bash"\n')
    _tool(bin_dir, "sshd", LOG_CALL + '[ "$1" = -T ] && echo "passwordauthentication no"\nexit 0\n')
    return env, sysroot


def test_ssh_keys_only_refuses_when_the_admin_has_no_authorized_key(tmp_path):
    # A commented-out key logs nobody in.
    env, sysroot = _ssh_env(
        tmp_path, "# ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyKeyMaterial old@example\n")
    before = _tree(sysroot)
    result = _run(CONFIGURE, ["--apply", "--no-try", "--ssh-keys-only"], env)
    assert result.returncode != 0
    assert "authorized" in result.stderr and "labadmin" in result.stderr
    assert _tree(sysroot) == before
    assert _mutations(_calls(tmp_path)) == []


def test_ssh_keys_only_disables_password_logins_once_the_admin_has_a_key(tmp_path):
    env, sysroot = _ssh_env(
        tmp_path, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyKeyMaterial admin@example\n")
    result = _run(CONFIGURE, ["--apply", "--no-try", "--ssh-keys-only"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    conf = (sysroot / "etc/ssh/sshd_config.d/10-jumpbox-keys-only.conf").read_text().splitlines()
    assert "PasswordAuthentication no" in conf
    assert "KbdInteractiveAuthentication no" in conf
    calls = _calls(tmp_path)
    assert "sshd -t" in calls
    assert "systemctl try-reload-or-restart ssh" in calls


def test_monitoring_refuses_a_root_disk_too_small_for_zeek_and_rita(tmp_path):
    # Zeek logs and RITA's ClickHouse filled the reference jumpbox's disk,
    # and a full disk stopped dnsmasq from recording leases.
    env, sysroot = _configure_env(tmp_path, "JUMPBOX_MONITORING_MIN_GB=1000000\n")
    before = _tree(sysroot)
    result = _run(CONFIGURE, ["--apply", "--no-try", "--monitoring"], env)
    assert result.returncode != 0
    assert "JUMPBOX_MONITORING_MIN_GB" in result.stderr
    assert _tree(sysroot) == before
    assert _mutations(_calls(tmp_path)) == []


# --- provision (runs on the Proxmox node) -----------------------------------

def _provision_env(tmp_path: Path, node: str = "node1", exists: bool = False):
    pubkey = tmp_path / "admin.pub"
    pubkey.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyKeyMaterial admin@example\n")
    image = tmp_path / "noble-server-cloudimg-amd64.img"
    image.write_bytes(b"qcow2")
    env = _base_env(tmp_path, (
        "JUMPBOX_NODE=node1\nJUMPBOX_STORAGE=local-zfs\nJUMPBOX_VMID=4000\n"
        f"JUMPBOX_SSH_PUBKEY={pubkey}\nJUMPBOX_CLOUD_IMAGE={image}\n"
    ))
    bin_dir = tmp_path / "bin"
    _tool(bin_dir, "hostname", f"echo {node}\n")
    _tool(bin_dir, "curl", LOG_CALL)
    _tool(bin_dir, "qm", LOG_CALL + ('[ "$1" = status ] && exit 0\n' if exists
                                     else '[ "$1" = status ] && exit 2\n') + "exit 0\n")
    return env, pubkey, image


def test_provision_dry_run_prints_the_plan_and_creates_nothing(tmp_path):
    env, _pubkey, _image = _provision_env(tmp_path)
    result = _run(PROVISION, [], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "qm create 4000" in result.stdout
    assert _mutations(_calls(tmp_path)) == []


def test_provision_builds_a_two_nic_full_disk_cloud_init_vm(tmp_path):
    env, pubkey, image = _provision_env(tmp_path)
    result = _run(PROVISION, ["--apply"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [l for l in _calls(tmp_path).splitlines() if l.startswith("qm ") and not l.startswith("qm status")]
    create = calls[0]
    assert create.startswith("qm create 4000 ")
    for option in ("--cores 2", "--memory 4096", "--onboot 1", "--agent enabled=1",
                   "--net0 virtio,bridge=vmbr0,tag=1", "--net1 virtio,bridge=vmbr0,firewall=1"):
        assert option in create
    joined = "\n".join(calls)
    assert f"--scsi0 local-zfs:0,import-from={image}" in joined
    assert "--ide2 local-zfs:cloudinit" in joined
    assert "--ciuser ubuntu" in joined
    assert f"--sshkeys {pubkey}" in joined
    assert "--ipconfig0 ip=dhcp" in joined
    disk = next(i for i, l in enumerate(calls) if "import-from=" in l)
    resize = calls.index("qm resize 4000 scsi0 32G")
    assert disk < resize < len(calls) - 1
    assert calls[-1] == "qm start 4000"


@pytest.mark.parametrize("node, exists", [("node2", False), ("node1", True)])
def test_provision_refuses_the_wrong_node_or_an_existing_vmid(tmp_path, node, exists):
    env, _pubkey, _image = _provision_env(tmp_path, node=node, exists=exists)
    result = _run(PROVISION, ["--apply"], env)
    assert result.returncode != 0
    assert _mutations(_calls(tmp_path)) == []


# --- accounts (optional; run on the jumpbox) --------------------------------

def test_create_accounts_uses_unique_random_passwords_via_stdin(tmp_path):
    csv = tmp_path / "students.csv"
    csv.write_text("SIS User ID,SIS Login ID\n100000001,alice\n100000002,bob\n")
    creds = tmp_path / "creds.csv"
    stdin_log = tmp_path / "chpasswd.stdin"
    env = _base_env(tmp_path, "")
    bin_dir = tmp_path / "bin"
    _tool(bin_dir, "id", 'if [ "$1" = "-u" ]; then echo 0; exit 0; fi\nexit 1\n')
    _tool(bin_dir, "useradd", LOG_CALL)
    _tool(bin_dir, "chage", LOG_CALL)
    _tool(bin_dir, "chpasswd", LOG_CALL + 'cat >> "$STDIN_LOG"\n')
    env.update(STDIN_LOG=str(stdin_log), JUMPBOX_CREDENTIALS_FILE=str(creds))

    result = _run(ACCOUNTS, ["create", str(csv)], env)

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    rows = [line.split(",") for line in creds.read_text().splitlines()]
    assert [r[0] for r in rows] == ["alice", "bob"]
    passwords = [r[1] for r in rows]
    assert len(set(passwords)) == 2 and all(len(p) >= 12 for p in passwords)
    assert stdin_log.read_text().splitlines() == [f"alice:{passwords[0]}", f"bob:{passwords[1]}"]
    for password in passwords:
        assert password not in calls
        assert password not in result.stdout + result.stderr
    assert "chage -d 0 alice" in calls and "chage -d 0 bob" in calls
    assert str(creds) in result.stdout


PASSWD = """root:x:0:0:root:/root:/bin/bash
syslog:x:104:110::/home/syslog:/usr/sbin/nologin
labadmin:x:1000:1000::/home/labadmin:/bin/bash
alice:x:1001:1001::/home/alice:/bin/bash
bob:x:1002:1002::/home/bob:/bin/bash
carol:x:1003:1003::/home/carol:/bin/bash
dave:x:1004:1004::/home/dave:/bin/bash
erin:x:1005:1005::/home/erin:/usr/sbin/nologin
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
"""


def _accounts_env(tmp_path: Path):
    env = _base_env(tmp_path, "")
    passwd = tmp_path / "passwd"
    passwd.write_text(PASSWD)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    bin_dir = tmp_path / "bin"
    _tool(bin_dir, "getent",
          '[ "$1" = passwd ] || exit 2\n'
          'if [ -n "$2" ]; then grep "^$2:" "$PASSWD_FILE"; else cat "$PASSWD_FILE"; fi\n')
    _tool(bin_dir, "lastlog",
          'echo "Username         Port     From             Latest"\n'
          'case "$2" in\n'
          '  labadmin|bob) printf "%-16s pts/0    192.0.2.7        Mon Oct 26 10:00:00 -0400 2026\\n" "$2" ;;\n'
          '  *) printf "%-16s                           **Never logged in**\\n" "$2" ;;\n'
          "esac\n")
    _tool(bin_dir, "passwd", LOG_CALL +
          'if [ "$1" = -S ]; then\n'
          '  if [ "$2" = carol ]; then echo "carol L 02/08/2026 0 99999 7 -1";\n'
          '  else echo "$2 P 02/08/2026 0 99999 7 -1"; fi\nfi\n')
    _tool(bin_dir, "chage", LOG_CALL)
    env.update(PASSWD_FILE=str(passwd), JUMPBOX_LOCK_LIST_DIR=str(lock_dir))
    return env, lock_dir


def test_lock_stale_locks_only_accounts_that_never_logged_in_and_records_them_for_undo(tmp_path):
    env, lock_dir = _accounts_env(tmp_path)

    dry = _run(ACCOUNTS, ["lock-stale"], env)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "alice" in dry.stdout and "dave" in dry.stdout
    for kept in ("bob", "carol", "labadmin", "erin"):
        assert kept not in dry.stdout
    assert _mutations(_calls(tmp_path)) == []
    assert list(lock_dir.iterdir()) == []

    applied = _run(ACCOUNTS, ["lock-stale", "--apply"], env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert sorted(_mutations(_calls(tmp_path))) == [
        "chage -E 0 alice", "chage -E 0 dave", "passwd -l alice", "passwd -l dave"]
    [undo] = list(lock_dir.iterdir())
    assert stat.S_IMODE(undo.stat().st_mode) == 0o600
    assert undo.read_text().splitlines() == ["alice", "dave"]
    assert str(undo) in applied.stdout

    (tmp_path / "calls.log").unlink()
    dry_unlock = _run(ACCOUNTS, ["unlock", str(undo)], env)
    assert dry_unlock.returncode == 0, dry_unlock.stdout + dry_unlock.stderr
    assert _mutations(_calls(tmp_path)) == []
    unlocked = _run(ACCOUNTS, ["unlock", str(undo), "--apply"], env)
    assert unlocked.returncode == 0, unlocked.stdout + unlocked.stderr
    assert sorted(_mutations(_calls(tmp_path))) == [
        "chage -E -1 alice", "chage -E -1 dave", "passwd -u alice", "passwd -u dave"]

    # The account running the script is never locked, even if lastlog says
    # it never logged in.
    env["SUDO_USER"] = "alice"
    as_alice = _run(ACCOUNTS, ["lock-stale"], env)
    assert as_alice.returncode == 0, as_alice.stdout + as_alice.stderr
    assert "alice" not in as_alice.stdout and "dave" in as_alice.stdout


# --- repo hygiene -------------------------------------------------------------

@pytest.mark.parametrize("script", sorted(SCRIPTS.glob("*.sh")), ids=lambda p: p.name)
def test_every_shell_script_parses(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_docs_name_only_scripts_that_exist():
    docs = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), *sorted(ROOT.glob("*.md")),
            SCRIPTS / "site.env.example"]
    missing = sorted({
        f"{doc.name}: {ref}" for doc in docs
        for ref in re.findall(r"scripts/[A-Za-z0-9_.-]+\.(?:sh|py)", doc.read_text())
        if not (ROOT / ref).exists()
    })
    assert missing == []
    assert not (ROOT / "JUMPBOX_SETUP.md").exists() and not (ROOT / "JUMPBOX_SIMPLE.md").exists()
