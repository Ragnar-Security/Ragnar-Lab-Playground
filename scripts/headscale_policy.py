#!/usr/bin/env python3
"""Render the per-student Headscale policy from the portal's network export.

    headscale_policy.py render BASE_POLICY.json SLOTS.json   # desired policy
    headscale_policy.py diff CURRENT.json DESIRED.json       # unified diff
    headscale_policy.py missing-peers NODES.json SLOTS.json STATUS.json
    headscale_policy.py protection BASE_POLICY.json SLOTS.json NODES.json [--staff-ips LIST]
    headscale_policy.py jumpbox-check FACTS.json NODES.json SLOTS.json PROTECTION.json
    headscale_policy.py verify-egress LIVE.json PROTECTION.json

BASE_POLICY is the operator's policy file (config/headscale-acl.json): every
section and every ACL rule is kept, except the blanket rule whose src is
exactly ["autogroup:member"], which is cut to its autogroup:self
destinations (each member keeps reaching their own devices). Any other rule
whose src includes autogroup:member or "*" must grant only autogroup:self,
or the render fails: such a rule would keep the blanket access the
per-student rules replace. SLOTS is scripts/export_network_slots.py
output from the running portal; the backends, lab CIDRs and VLAN bands come
only from it, so the policy grants exactly the networks that portal
allocates. Each roster login gets one rule to its own entry and internal
networks, its groups' networks, and every non-ended KOTH game's entry network
(Proxmox only), plus the reverse rule back to its devices from its own and its
groups' networks. On Proxmox a group's or KOTH game's internal network is never
granted, in either direction: players reach it only by pivoting through a host
they hold on the entry network. The cloud backends do not enforce that yet: a
group's cloud slots, internal included, are granted as before. KOTH networks
get no reverse rule: a box any player has rooted must not reach every player's
devices.

missing-peers reads `headscale nodes list -o json`, the slots export and
`tailscale status --json` from the jumpbox, and prints each roster device the
jumpbox does not list as a peer, one per line (nothing when all are there).

protection prints what the jumpbox must enforce, as JSON: `vlans`, every
group's and non-ended KOTH game's internal tag, and `staff_ips`, the
JUMPBOX_TAILNET_STAFF_IPS addresses, each verified as the exact address of a
live node of a non-roster user (a staff device or the portal host). It fails
when a base rule would reopen a protected network, counting the roster
devices' own addresses from the node list. jumpbox-check fails, while any
VLAN is protected, unless the jumpbox has the staff-only SSH filter (admitting
no roster device) and no listener a student could use as a proxy.
verify-egress fails unless the lab egress table the jumpbox loaded
(`nft -j list table inet jumpbox_lab_egress`) holds exactly the protection.

Standard library only: this runs on the operator's workstation.
"""

import argparse
import copy
import difflib
import importlib.util
import ipaddress
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set, Tuple


def _load_network_slots():
    # By path, not `import app...`: the app package needs Flask and its
    # configuration, which the workstation does not have.
    name = "_lab_network_slots"
    path = Path(__file__).resolve().parent.parent / "app" / "backends" / "network_slots.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


_slots = _load_network_slots()
VlanBand = _slots.VlanBand
cloud_slot_cidr = _slots.cloud_slot_cidr
parse_vlan_bands = _slots.parse_vlan_bands
proxmox_slot_cidr = _slots.proxmox_slot_cidr
routed_slots = _slots.routed_slots
validate_lab_cidr = _slots.validate_lab_cidr

CLOUD_BACKENDS = ("aws", "gcp")
KNOWN_BACKENDS = ("proxmox",) + CLOUD_BACKENDS
BLANKET_SRC = ["autogroup:member"]
# Sources that cover every student.
BROAD_SOURCES = frozenset({"autogroup:member", "*"})
SELF_DST_PREFIX = "autogroup:self:"
_LOGIN = re.compile(r"[a-z0-9][a-z0-9._-]*")


@dataclass(frozen=True)
class PolicyBands:
    """`values` decide which slots are routed on every backend;
    `proxmox_enabled` keeps a cloud-only portal off stale Proxmox ranges."""
    values: Tuple[VlanBand, ...]
    proxmox_enabled: bool


def normalize_login(raw: str) -> str:
    """The Headscale v0.27 identity `<login>@` for one roster login."""
    if not isinstance(raw, str):
        raise ValueError(f"login {raw!r} is not a string")
    login = raw.strip().lower()
    if login.endswith("@"):
        login = login[:-1]
    if not _LOGIN.fullmatch(login):
        raise ValueError(f"login {raw!r} is empty or not a plain login name")
    return f"{login}@"


def policy_inputs(slots: Mapping[str, Any]) -> Tuple[PolicyBands, Dict[str, str]]:
    """Bands and enabled cloud CIDRs, from the portal's exported config only."""
    config = slots.get("config") if isinstance(slots, Mapping) else None
    if not isinstance(config, Mapping):
        raise ValueError("the slots export has no config block")
    enabled = config.get("enabled_backends")
    if not isinstance(enabled, list) or not all(isinstance(b, str) for b in enabled):
        raise ValueError("config.enabled_backends must be a list of backend names")
    if not enabled:
        raise ValueError("config.enabled_backends is empty: the portal exports at least one backend")
    unknown = sorted(set(enabled) - set(KNOWN_BACKENDS))
    if unknown:
        raise ValueError(f"config.enabled_backends names unknown backend(s) {', '.join(unknown)}")
    spec = config.get("vlan_bands")
    if not isinstance(spec, str):
        raise ValueError("config.vlan_bands must be a band string (the portal's VLAN_BANDS)")
    cidrs = {}
    for backend in CLOUD_BACKENDS:
        if backend in enabled:
            cidr = config.get(f"{backend}_lab_cidr")
            if not isinstance(cidr, str) or not cidr:
                raise ValueError(f"config.{backend}_lab_cidr is missing for enabled {backend}")
            cidrs[backend] = cidr
    bands = PolicyBands(tuple(parse_vlan_bands(spec)), proxmox_enabled="proxmox" in enabled)
    return bands, cidrs


def _list(slots: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = slots.get(key)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"slots.{key} must be a list of objects")
    return value


def _tag(item: Mapping[str, Any], key: str, what: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4094:
        raise ValueError(f"{what} has invalid {key} {value!r}")
    return value


def render(base_policy: Mapping[str, Any], slots: Mapping[str, Any],
           bands: PolicyBands, cloud_cidrs: Mapping[str, str]) -> Dict[str, Any]:
    """The desired policy. Pure: reads nothing but its arguments."""
    if not isinstance(base_policy, Mapping) or not isinstance(base_policy.get("acls"), list) \
            or not all(isinstance(rule, Mapping) for rule in base_policy["acls"]):
        raise ValueError("the base policy must be an object with an acls list of rules")
    if not isinstance(slots, Mapping):
        raise ValueError("the slots export must be an object")
    for backend in sorted(cloud_cidrs):
        if backend not in CLOUD_BACKENDS:
            raise ValueError(f"unknown cloud backend {backend!r}")
        others = {other: cidr for other, cidr in cloud_cidrs.items() if other != backend}
        validate_lab_cidr(backend, cloud_cidrs[backend], others=others)

    assigned = _assignments(slots)
    # KOTH is refused on the cloud backends, so its slots exist on Proxmox only.
    koth_routed = routed_slots(assigned.koth_entry, bands.values) if bands.proxmox_enabled else []

    acls = _base_rules(base_policy["acls"])
    _refuse_protected_base_rules(base_policy, assigned, bands, roster_ips=())
    for identity in sorted(assigned.proxmox):
        own = routed_slots(assigned.proxmox[identity], bands.values)
        cloud_own = routed_slots(assigned.cloud[identity], bands.values)
        forward, reverse = [], []
        if bands.proxmox_enabled:
            for slot in sorted(set(own) | set(koth_routed)):
                prefix = proxmox_slot_cidr(slot, bands.values)
                forward.append(prefix)
                if slot in own:
                    reverse.append(prefix)
        for backend in sorted(cloud_cidrs):
            cloud = [cloud_slot_cidr(cloud_cidrs[backend], slot) for slot in cloud_own]
            forward += cloud
            reverse += cloud
        if not forward:
            continue
        acls.append({"action": "accept", "src": [identity],
                     "dst": [f"{prefix}:*" for prefix in forward]})
        if reverse:
            acls.append({"action": "accept", "src": reverse, "dst": [f"{identity}:*"]})

    policy = copy.deepcopy(dict(base_policy))
    policy["acls"] = acls
    return policy


@dataclass(frozen=True)
class Assignments:
    """Who is granted which VLAN tags, from the portal's export.

    `proxmox`: each login's own entry and internal tags plus its groups' entry
    tags. `cloud`: the same plus its groups' internal tags; group isolation is
    not enforced on the cloud backends yet, so they keep the earlier grants.
    `koth_entry`: every non-ended KOTH game's entry tag (every login gets it).
    `protected`: every group's and non-ended KOTH game's internal tag, which no
    login is granted on Proxmox and the jumpbox drops to and from the tailnet.
    """
    proxmox: Dict[str, Set[int]]
    cloud: Dict[str, Set[int]]
    koth_entry: Set[int]
    protected: Set[int]


def _assignments(slots: Mapping[str, Any]) -> Assignments:
    owners: Dict[int, str] = {}

    def claim(tag: int, owner: str) -> int:
        if tag in owners:
            raise ValueError(f"VLAN {tag} is assigned to both {owners[tag]} and {owner}")
        owners[tag] = owner
        return tag

    proxmox: Dict[str, Set[int]] = {}
    cloud: Dict[str, Set[int]] = {}
    for user in _list(slots, "users"):
        identity = normalize_login(user.get("login"))
        if identity in proxmox:
            raise ValueError(f"login {identity} appears twice in the roster")
        proxmox[identity] = {claim(_tag(user, "entry_vlan", identity), identity),
                             claim(_tag(user, "internal_vlan", identity), identity)}
        cloud[identity] = set(proxmox[identity])
    protected: Set[int] = set()
    slugs = set()
    for group in _list(slots, "groups"):
        slug = group.get("slug")
        if not isinstance(slug, str) or not slug or slug in slugs:
            raise ValueError(f"group slug {slug!r} is empty or repeated")
        slugs.add(slug)
        what = f"group {slug}"
        entry = claim(_tag(group, "entry_vlan", what), what)
        internal = claim(_tag(group, "internal_vlan", what), what)
        protected.add(internal)
        members = group.get("members")
        if not isinstance(members, list):
            raise ValueError(f"{what} members must be a list")
        for member in members:
            identity = normalize_login(member)
            if identity not in proxmox:
                raise ValueError(f"{what} member {identity} is not on the roster")
            proxmox[identity].add(entry)
            cloud[identity] |= {entry, internal}

    koth_entry: Set[int] = set()
    for game in _list(slots, "koth"):
        what = f"KOTH game {game.get('game_id')!r}"
        koth_entry.add(claim(_tag(game, "entry_vlan", what), what))
        protected.add(claim(_tag(game, "internal_vlan", what), what))
    return Assignments(proxmox, cloud, koth_entry, protected)


def _protected_networks(assigned: Assignments, bands: PolicyBands) -> List[Any]:
    """The Proxmox networks of the protected tags; none when Proxmox is off."""
    if not bands.proxmox_enabled:
        return []
    return [ipaddress.ip_network(proxmox_slot_cidr(tag, bands.values))
            for tag in routed_slots(assigned.protected, bands.values)]


def _refuse_protected_base_rules(base_policy: Mapping[str, Any], assigned: Assignments,
                                 bands: PolicyBands, roster_ips: Set[str]) -> None:
    """Fail when a base rule reopens a protected network.

    Headscale unions every rule, so a base rule granting a roster login (by
    name, through a group or `*`, or by a device address) a protected network
    or a covering prefix, or letting such a network (by prefix, host alias or
    `*`) reach a roster device, would undo the withheld grants. Staff groups
    without roster logins, host aliases and tags are not roster devices, so
    staff and the portal keep their access. `roster_ips` are the roster
    devices' tailnet addresses, which only Headscale's node list knows.
    """
    protected = _protected_networks(assigned, bands)
    if not protected:
        return
    roster = set(assigned.proxmox)
    groups = base_policy.get("groups") or {}
    hosts = base_policy.get("hosts") or {}
    roster_addrs = {ipaddress.ip_address(ip) for ip in roster_ips}

    def network(name: str):
        try:
            return ipaddress.ip_network(hosts.get(name, name), strict=False)
        except (TypeError, ValueError):
            return None

    def names_roster(name: str) -> bool:
        if name in ("*", "autogroup:member"):
            return True
        if name.startswith("group:"):
            return any(_identity_or_none(m) in roster for m in groups.get(name) or [])
        if _identity_or_none(name) in roster:
            return True
        net = network(name)
        return net is not None and any(addr in net for addr in roster_addrs)

    def reaches_protected(name: str) -> bool:
        if name == "*":
            return True
        net = network(name)
        return net is not None and any(net.version == p.version and net.overlaps(p)
                                       for p in protected)

    problems = []
    for index, rule in enumerate(base_policy["acls"]):
        src = [s for s in rule.get("src") or [] if isinstance(s, str)]
        targets = [d.rsplit(":", 1)[0] for d in rule.get("dst") or [] if isinstance(d, str)]
        # The blanket rule is cut to autogroup:self (_base_rules), which no
        # protected network can be.
        if src == BLANKET_SRC or all(t.startswith("autogroup:self") for t in targets):
            continue
        if (any(map(names_roster, src)) and any(map(reaches_protected, targets))) or \
                (any(map(reaches_protected, src)) and any(map(names_roster, targets))):
            problems.append(f"acls[{index}] {rule.get('src')} -> {rule.get('dst')}")
    if problems:
        raise ValueError(
            "base policy rule(s) connect roster devices with a protected group or KOTH "
            "internal network: " + "; ".join(problems) + ". Those networks are reached only "
            "by pivoting through an entry host; narrow the rule to staff or infrastructure.")


def _identity_or_none(name: Any):
    try:
        return normalize_login(name)
    except ValueError:
        return None


def _live_nodes(nodes: Any) -> List[Mapping[str, Any]]:
    if not isinstance(nodes, list) or not all(isinstance(n, Mapping) for n in nodes):
        raise ValueError("the Headscale node list must be a JSON list of nodes")
    now = time.time()
    return [n for n in nodes if not 0 < ((n.get("expiry") or {}).get("seconds") or 0) <= now]


def protection(base_policy: Mapping[str, Any], slots: Mapping[str, Any], nodes: Any,
               staff_ips: List[str]) -> Dict[str, List[Any]]:
    """What the jumpbox must enforce, verified against Headscale's node list.

    `vlans`: every group's and non-ended KOTH game's internal tag. `staff_ips`:
    the addresses still allowed to and from them, each the exact address of a
    live node whose user is not on the roster (a staff device, the portal
    host). Fails when a base rule reopens a protected network, counting the
    roster devices' own addresses.
    """
    bands, _cidrs = policy_inputs(slots)
    assigned = _assignments(slots)
    live = _live_nodes(nodes)
    roster_ips, other_ips = set(), set()
    for node in live:
        user = _identity_or_none((node.get("user") or {}).get("name") or "")
        (roster_ips if user in assigned.proxmox else other_ips).update(
            str(ipaddress.ip_address(ip)) for ip in node.get("ip_addresses") or [])
    _refuse_protected_base_rules(base_policy, assigned, bands, roster_ips)
    staff = set()
    for raw in staff_ips:
        try:
            address = str(ipaddress.ip_address(raw.strip()))
        except ValueError:
            raise ValueError(f"JUMPBOX_TAILNET_STAFF_IPS: {raw!r} is not a single IPv4 or "
                             "IPv6 address") from None
        if address in roster_ips or address not in other_ips:
            raise ValueError(f"JUMPBOX_TAILNET_STAFF_IPS: {address} is not the address of a "
                             "live non-roster node (a staff device or the portal host)")
        staff.add(address)
    return {"vlans": sorted(assigned.protected),
            "staff_ips": sorted(staff, key=lambda a: (ipaddress.ip_address(a).version,
                                                      ipaddress.ip_address(a)))}


# Processes that may listen on a jumpbox address other than loopback: sshd
# (behind the staff-only filter), dnsmasq (DHCP and DNS for the lab VLANs),
# tailscaled, and the resolver and DHCP client of the host itself.
JUMPBOX_LISTENERS = frozenset({"sshd", "dnsmasq", "tailscaled", "systemd-resolve",
                               "systemd-network"})


def _listener_problems(listing: str) -> List[str]:
    problems = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        local = fields[4]
        host = local.rsplit(":", 1)[0].strip("[]").split("%")[0]
        try:
            if host not in ("*", "0.0.0.0", "::") and ipaddress.ip_address(host).is_loopback:
                continue
        except ValueError:
            pass
        names = re.findall(r'\("([^"]+)"', line)
        if not names:
            problems.append(f"an unidentified process listens on {local}")
        problems += [f"{name} listens on {local}" for name in names if name not in JUMPBOX_LISTENERS]
    return problems


def jumpbox_problems(facts: Mapping[str, Any], nodes: Any, slots: Mapping[str, Any],
                     protection_data: Mapping[str, Any]) -> List[str]:
    """Why the jumpbox cannot carry protected networks yet; empty when it can.

    A student who can log in to the jumpbox, or use a service on it as a
    proxy, originates traffic from the jumpbox itself: outside its forward
    hook and without any entry host. So while any VLAN is protected the
    jumpbox needs the staff-only SSH filter, admitting no roster device, and
    no listener beyond sshd, dnsmasq, tailscaled and the host's own resolver.
    """
    if not protection_data.get("vlans"):
        return []
    assigned = _assignments(slots)
    roster_ips = set()
    for node in _live_nodes(nodes):
        if _identity_or_none((node.get("user") or {}).get("name") or "") in assigned.proxmox:
            roster_ips.update(str(ipaddress.ip_address(ip)) for ip in node.get("ip_addresses") or [])
    problems = []
    items = ((facts.get("ssh_table") or {}).get("nftables") or []) if isinstance(facts, Mapping) else []
    rules = [i["rule"] for i in items if isinstance(i, Mapping) and "rule" in i]
    if not any(any("drop" in e for e in r.get("expr") or [] if isinstance(e, Mapping)) for r in rules):
        problems.append("the staff-only SSH filter (table inet jumpbox_ssh) is not installed: "
                        "run jumpbox_configure.sh --apply --ssh-staff-only")
    for item in items:
        elems = (item.get("set") or {}).get("elem") if isinstance(item, Mapping) else None
        for elem in elems or []:
            if isinstance(elem, str) and elem in roster_ips:
                problems.append(f"the staff-only SSH filter admits roster device {elem}")
    listing = facts.get("listeners") if isinstance(facts, Mapping) else None
    if not isinstance(listing, str):
        problems.append("could not list the jumpbox's listening sockets")
    else:
        problems += [f"{p}: a student could use it as a proxy" for p in _listener_problems(listing)]
    return problems


def egress_mismatch(live: Any, protection_data: Mapping[str, Any]) -> List[str]:
    """Differences between the jumpbox's loaded lab egress table and the protection."""
    items = (live or {}).get("nftables") or [] if isinstance(live, Mapping) else []
    sets = {i["set"]["name"]: sorted(e for e in i["set"].get("elem") or [] if isinstance(e, str))
            for i in items if isinstance(i, Mapping) and isinstance(i.get("set"), Mapping)}
    vlans = protection_data.get("vlans") or []
    staff = protection_data.get("staff_ips") or []
    want = {"tailnet_internal": sorted(f"vlan{v}" for v in vlans),
            "staff": sorted(staff)} if vlans else {"tailnet_internal": [], "staff": []}
    have = {"tailnet_internal": sets.get("tailnet_internal", []),
            "staff": sorted(sets.get("tailnet_staff_v4", []) + sets.get("tailnet_staff_v6", []))}
    drops = sum(1 for i in items if isinstance(i, Mapping) and "@tailnet_internal" in json.dumps(i.get("rule"))
                and '"drop"' in json.dumps(i.get("rule")))
    problems = [f"{key}: loaded {have[key]}, expected {want[key]}" for key in want if have[key] != want[key]]
    if vlans and drops != 2:
        problems.append(f"{drops} tailnet_internal drop rule(s) loaded, expected 2")
    return problems


def _base_rules(base_acls: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The base rules the policy keeps.

    A rule whose src includes a broad source (autogroup:member, "*") may
    grant only autogroup:self, which reaches each member's own devices. The
    exact blanket rule is cut down to that part; any other broad rule that
    grants more fails the render, since keeping it would keep the access
    the per-student rules exist to replace.
    """
    kept = []
    for index, base_rule in enumerate(base_acls):
        rule = copy.deepcopy(dict(base_rule))
        src, dst = rule.get("src"), rule.get("dst")
        if not isinstance(src, list) or not any(s in BROAD_SOURCES for s in src
                                                if isinstance(s, str)):
            kept.append(rule)
            continue
        if not isinstance(dst, list) or not all(isinstance(d, str) for d in dst):
            raise ValueError(f"base policy acls[{index}] has a malformed dst {dst!r}")
        self_only = [d for d in dst if d.startswith(SELF_DST_PREFIX)]
        if self_only == dst:
            kept.append(rule)
        elif src == BLANKET_SRC:
            if self_only:
                rule["dst"] = self_only
                kept.append(rule)
        else:
            broad = [d for d in dst if not d.startswith(SELF_DST_PREFIX)]
            raise ValueError(
                f"base policy acls[{index}] gives {src} access to {broad}: a rule "
                "whose src includes autogroup:member or \"*\" may grant only "
                "autogroup:self, and only the rule whose src is exactly "
                "[\"autogroup:member\"] is replaced by the per-student rules. "
                "Narrow its src or its dst, or remove it")
    return kept


def canonical(policy: Any) -> str:
    return json.dumps(policy, indent=2, sort_keys=True) + "\n"


def policy_diff(current: Mapping[str, Any], desired: Mapping[str, Any]) -> str:
    """Unified diff of the canonical forms; empty when they are equal."""
    lines = difflib.unified_diff(canonical(current).splitlines(),
                                 canonical(desired).splitlines(),
                                 "current", "desired", lineterm="")
    return "".join(f"{line}\n" for line in lines)


def _load(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from None


def missing_peers(nodes: Any, slots: Mapping[str, Any], status: Any) -> List[str]:
    """Roster devices Headscale knows that the jumpbox does not list as peers.

    The policy grants each roster login routes the jumpbox serves, so every
    device of a roster login must be a peer of the jumpbox; a device that is
    not cannot reach any lab. Headscale v0.27.1 left the jumpbox with no peers
    once after a policy reload and once after the jumpbox reconnected (staging,
    2026-11-18). Offline devices stay in a netmap and count; expired ones are no
    one's peer and are skipped, as are staff and infrastructure devices, which
    the roster does not name. A jumpbox whose backend is not Running keeps its
    last netmap's peers in the status, so then every device counts as missing.
    """
    if not isinstance(nodes, list) or not all(isinstance(n, Mapping) for n in nodes):
        raise ValueError("the Headscale node list must be a JSON list of nodes")
    if not isinstance(status, Mapping):
        raise ValueError("the jumpbox status must be a JSON object")
    roster = {normalize_login(user.get("login")) for user in _list(slots, "users")}
    peer_ips: Set[str] = set()
    if status.get("BackendState") == "Running":
        for peer in (status.get("Peer") or {}).values():
            peer_ips.update(peer.get("TailscaleIPs") or [])
    now = time.time()
    missing = []
    for node in nodes:
        user = (node.get("user") or {}).get("name") or ""
        try:
            if normalize_login(user) not in roster:
                continue
        except ValueError:
            continue
        expiry = (node.get("expiry") or {}).get("seconds") or 0
        if 0 < expiry <= now:
            continue
        if not set(node.get("ip_addresses") or []) & peer_ips:
            missing.append(f"{node.get('given_name') or node.get('name')} ({user})")
    return sorted(missing)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render", help="print the desired policy")
    render_cmd.add_argument("base_policy")
    render_cmd.add_argument("slots")
    diff_cmd = commands.add_parser("diff", help="print a diff; empty when equal")
    diff_cmd.add_argument("current")
    diff_cmd.add_argument("desired")
    peers_cmd = commands.add_parser("missing-peers",
                                    help="print roster devices the jumpbox lacks as peers")
    peers_cmd.add_argument("nodes")
    peers_cmd.add_argument("slots")
    peers_cmd.add_argument("status")
    protection_cmd = commands.add_parser(
        "protection", help="print the jumpbox's protected VLANs and staff exemptions as JSON")
    protection_cmd.add_argument("base_policy")
    protection_cmd.add_argument("slots")
    protection_cmd.add_argument("nodes")
    protection_cmd.add_argument("--staff-ips", default="",
                                help="JUMPBOX_TAILNET_STAFF_IPS: comma/space separated")
    check_cmd = commands.add_parser(
        "jumpbox-check", help="fail unless the jumpbox may carry the protected networks")
    for name in ("facts", "nodes", "slots", "protection"):
        check_cmd.add_argument(name)
    verify_cmd = commands.add_parser(
        "verify-egress", help="fail unless the loaded lab egress table matches the protection")
    verify_cmd.add_argument("live")
    verify_cmd.add_argument("protection")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            slots = _load(args.slots)
            output = canonical(render(_load(args.base_policy), slots, *policy_inputs(slots)))
        elif args.command == "missing-peers":
            output = "".join(f"{device}\n" for device in missing_peers(
                _load(args.nodes), _load(args.slots), _load(args.status)))
        elif args.command in ("jumpbox-check", "verify-egress"):
            data = _load(args.protection)
            problems = (jumpbox_problems(_load(args.facts), _load(args.nodes), _load(args.slots), data)
                        if args.command == "jumpbox-check" else egress_mismatch(_load(args.live), data))
            if problems:
                raise ValueError("; ".join(problems))
            output = ""
        elif args.command == "protection":
            staff = [s for s in re.split(r"[\s,]+", args.staff_ips) if s]
            output = json.dumps(protection(_load(args.base_policy), _load(args.slots),
                                           _load(args.nodes), staff), sort_keys=True) + "\n"
        else:
            output = policy_diff(_load(args.current), _load(args.desired))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
