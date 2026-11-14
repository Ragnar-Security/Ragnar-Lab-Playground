#!/usr/bin/env python3
"""Render the per-student Headscale policy from the portal's network export.

    headscale_policy.py render BASE_POLICY.json SLOTS.json   # desired policy
    headscale_policy.py diff CURRENT.json DESIRED.json       # unified diff

BASE_POLICY is the operator's policy file (config/headscale-acl.json): every
section and every ACL rule is kept, except the blanket rule whose src is
exactly ["autogroup:member"], which is cut to its autogroup:self
destinations (each member keeps reaching their own devices). Any other rule
whose src includes autogroup:member or "*" must grant only autogroup:self,
or the render fails: such a rule would keep the blanket access the
per-student rules replace. SLOTS is scripts/export_network_slots.py
output from the running portal; the backends, lab CIDRs and VLAN bands come
only from it, so the policy grants exactly the networks that portal
allocates. Each roster login gets one rule to its own entry network, its
group's entry and internal networks, and every non-ended KOTH game's
networks (Proxmox only), plus the reverse rule back to its devices from its
own and its groups' networks. KOTH networks get no reverse rule: a box any
player has rooted must not reach every player's devices.

Standard library only: this runs on the operator's workstation.
"""

import argparse
import copy
import difflib
import importlib.util
import json
import re
import sys
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
    unknown = sorted(set(enabled) - set(KNOWN_BACKENDS))
    if unknown:
        raise ValueError(f"config.enabled_backends names unknown backend(s) {', '.join(unknown)}")
    spec = config.get("jumpbox_vlan_bands")
    if not isinstance(spec, str):
        raise ValueError("config.jumpbox_vlan_bands must be a band string")
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

    owners: Dict[int, str] = {}

    def claim(tag: int, owner: str) -> int:
        if tag in owners:
            raise ValueError(f"VLAN {tag} is assigned to both {owners[tag]} and {owner}")
        owners[tag] = owner
        return tag

    entries: Dict[str, int] = {}
    for user in _list(slots, "users"):
        identity = normalize_login(user.get("login"))
        if identity in entries:
            raise ValueError(f"login {identity} appears twice in the roster")
        entries[identity] = claim(_tag(user, "entry_vlan", identity), identity)
        claim(_tag(user, "internal_vlan", identity), identity)

    granted: Dict[str, Set[int]] = {identity: {entry} for identity, entry in entries.items()}
    slugs = set()
    for group in _list(slots, "groups"):
        slug = group.get("slug")
        if not isinstance(slug, str) or not slug or slug in slugs:
            raise ValueError(f"group slug {slug!r} is empty or repeated")
        slugs.add(slug)
        what = f"group {slug}"
        pair = {claim(_tag(group, "entry_vlan", what), what),
                claim(_tag(group, "internal_vlan", what), what)}
        members = group.get("members")
        if not isinstance(members, list):
            raise ValueError(f"{what} members must be a list")
        for member in members:
            identity = normalize_login(member)
            if identity not in granted:
                raise ValueError(f"{what} member {identity} is not on the roster")
            granted[identity] |= pair

    koth: Set[int] = set()
    for game in _list(slots, "koth"):
        what = f"KOTH game {game.get('game_id')!r}"
        koth |= {claim(_tag(game, "entry_vlan", what), what),
                 claim(_tag(game, "internal_vlan", what), what)}
    # KOTH is refused on the cloud backends, so its slots exist on Proxmox only.
    koth_routed = routed_slots(koth, bands.values) if bands.proxmox_enabled else []

    acls = _base_rules(base_policy["acls"])
    for identity in sorted(granted):
        own = routed_slots(granted[identity], bands.values)
        forward, reverse = [], []
        if bands.proxmox_enabled:
            for slot in sorted(set(own) | set(koth_routed)):
                prefix = proxmox_slot_cidr(slot, bands.values)
                forward.append(prefix)
                if slot in own:
                    reverse.append(prefix)
        for backend in sorted(cloud_cidrs):
            cloud = [cloud_slot_cidr(cloud_cidrs[backend], slot) for slot in own]
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render", help="print the desired policy")
    render_cmd.add_argument("base_policy")
    render_cmd.add_argument("slots")
    diff_cmd = commands.add_parser("diff", help="print a diff; empty when equal")
    diff_cmd.add_argument("current")
    diff_cmd.add_argument("desired")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            slots = _load(args.slots)
            output = canonical(render(_load(args.base_policy), slots, *policy_inputs(slots)))
        else:
            output = policy_diff(_load(args.current), _load(args.desired))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
