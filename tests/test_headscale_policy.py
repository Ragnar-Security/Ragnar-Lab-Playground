"""The per-student Headscale policy renderer (pure) and its CLI."""

import copy
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

# The script loads network_slots by path (it runs without Flask), so build
# bands with its own parser: dataclass equality is per class object.
from scripts.headscale_policy import (
    PolicyBands,
    normalize_login,
    parse_vlan_bands,
    policy_diff,
    policy_inputs,
    render,
)

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "headscale_policy.py"
DEFAULT_BANDS = "entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4"
BASE = json.loads((ROOT / "config" / "headscale-acl.example.json").read_text())
CLOUDS = {"aws": "172.20.0.0/16", "gcp": "172.21.0.0/16"}
SLOTS = {
    "config": {
        "enabled_backends": ["proxmox", "aws", "gcp"],
        "aws_lab_cidr": "172.20.0.0/16",
        "gcp_lab_cidr": "172.21.0.0/16",
        "jumpbox_vlan_bands": DEFAULT_BANDS,
    },
    "users": [
        {"login": "alice", "entry_vlan": 100, "internal_vlan": 200},
        {"login": "bob", "entry_vlan": 101, "internal_vlan": 201},
    ],
    "groups": [{"slug": "red-team", "members": ["alice", "bob"],
                "entry_vlan": 300, "internal_vlan": 400}],
    "koth": [],
}
DEFAULT_POLICY_BANDS = PolicyBands(tuple(parse_vlan_bands(DEFAULT_BANDS)), proxmox_enabled=True)
# Own entry 100 plus the group's 300/400 -- never the user-internal slot 200.
EXPECTED_ALICE_CIDRS = [
    "10.100.0.0/24", "10.100.2.0/24", "10.100.4.0/24",
    "172.20.25.0/26", "172.20.75.0/26", "172.20.100.0/26",
    "172.21.25.0/26", "172.21.75.0/26", "172.21.100.0/26",
]


def _forward(policy, login):
    return next(r for r in policy["acls"] if r["src"] == [login])


# Catches a student reaching another student's networks, their own internal
# VLAN, or losing the operator's base sections.
def test_alice_reaches_exactly_her_entry_and_group_blocks_on_every_backend():
    base = copy.deepcopy(BASE)
    policy = render(base, SLOTS, DEFAULT_POLICY_BANDS, CLOUDS)
    assert base == BASE
    assert policy["groups"] == base["groups"]
    assert policy["tagOwners"] == base["tagOwners"]
    assert policy["hosts"] == base["hosts"]
    assert policy["acls"][0] == {"action": "accept", "src": ["group:staff"], "dst": ["*:*"]}
    alice_forward = _forward(policy, "alice@")
    assert alice_forward == {"action": "accept", "src": ["alice@"],
                             "dst": [f"{cidr}:*" for cidr in EXPECTED_ALICE_CIDRS]}
    alice_reverse = next(r for r in policy["acls"] if r["dst"] == ["alice@:*"])
    assert alice_reverse == {"action": "accept", "src": EXPECTED_ALICE_CIDRS,
                             "dst": ["alice@:*"]}
    assert "autogroup:member" not in json.dumps(policy)
    assert all("200" not in dst for dst in alice_forward["dst"])
    assert "10.200.0.0/24" not in json.dumps(policy)


# Catches an uppercase/whitespace/trailing-@ roster login yielding an identity Headscale never matches.
def test_login_is_normalized_exactly_once():
    assert normalize_login(" Alice@ ") == "alice@"
    assert normalize_login("j.doe-1_x") == "j.doe-1_x@"
    slots = {**SLOTS, "users": [{"login": " Alice@ ", "entry_vlan": 100, "internal_vlan": 200}],
             "groups": []}
    policy = render(BASE, slots, DEFAULT_POLICY_BANDS, {})
    assert policy["acls"][1]["src"] == ["alice@"]  # after the one preserved staff rule
    assert "Alice" not in json.dumps(policy)


# Catches an empty login becoming the identity "@" that matches nobody or everybody.
@pytest.mark.parametrize("raw", ["", "   ", "@", " @ ", "a@b", "al ice", "alice@@", "*", "a:b"])
def test_empty_or_malformed_login_is_rejected(raw):
    with pytest.raises(ValueError):
        normalize_login(raw)
    with pytest.raises(ValueError):
        render(BASE, {**SLOTS, "users": [{"login": raw, "entry_vlan": 100,
                                          "internal_vlan": 200}], "groups": []},
               DEFAULT_POLICY_BANDS, {})


# Catches KOTH players losing reachability to the arena once the blanket rule is gone.
def test_every_roster_login_gets_non_ended_koth_blocks_on_proxmox_only():
    slots = {**SLOTS, "koth": [{"game_id": 1, "entry_vlan": 350, "internal_vlan": 450}]}
    policy = render(BASE, slots, DEFAULT_POLICY_BANDS, {"aws": "172.20.0.0/16"})
    for login in ("alice@", "bob@"):
        forward = _forward(policy, login)
        assert {"10.150.2.0/24:*", "10.150.4.0/24:*"} <= set(forward["dst"])
        assert not {"172.20.87.128/26:*", "172.20.112.128/26:*"} & set(forward["dst"])
        reverse = next(r for r in policy["acls"] if r["dst"] == [f"{login}:*"])
        assert {"10.150.2.0/24", "10.150.4.0/24"} <= set(reverse["src"])
    cloud_only = render(BASE, slots, replace(DEFAULT_POLICY_BANDS, proxmox_enabled=False),
                        {"aws": "172.20.0.0/16"})
    assert "10.150." not in json.dumps(cloud_only)


# Catches the operator's infrastructure rules (e.g. the portal host probing KOTH) being dropped.
def test_base_acls_are_preserved_except_the_blanket_member_rule():
    infra = {"action": "accept", "src": ["node1-v4"], "dst": ["10.150.4.0/24:*"]}
    members_and_more = {"action": "accept", "src": ["autogroup:member", "group:staff"],
                        "dst": ["tag:jumpbox:22"]}
    base = {**BASE, "acls": [BASE["acls"][0], BASE["acls"][1], infra, members_and_more]}
    policy = render(base, SLOTS, DEFAULT_POLICY_BANDS, {})
    assert policy["acls"][:3] == [BASE["acls"][0], infra, members_and_more]
    assert not any(r["src"] == ["autogroup:member"] for r in policy["acls"])


# Catches operator sections outside groups/tagOwners/hosts (autoApprovers) being lost.
def test_other_base_sections_are_kept_verbatim():
    approvers = {"routes": {"10.0.0.0/8": ["tag:jumpbox"]}}
    policy = render({**BASE, "autoApprovers": approvers}, SLOTS, DEFAULT_POLICY_BANDS, {})
    assert policy["autoApprovers"] == approvers


# Catches the CLI granting blocks from workstation settings instead of the portal's.
def test_policy_inputs_come_from_the_portal_export():
    bands, cidrs = policy_inputs({**SLOTS, "config": {
        "enabled_backends": ["aws"], "aws_lab_cidr": "172.20.0.0/16",
        "gcp_lab_cidr": "172.21.0.0/16", "jumpbox_vlan_bands": DEFAULT_BANDS}})
    assert bands.proxmox_enabled is False
    assert bands.values == tuple(parse_vlan_bands(DEFAULT_BANDS))
    assert cidrs == {"aws": "172.20.0.0/16"}

    bands, cidrs = policy_inputs(SLOTS)
    assert bands == DEFAULT_POLICY_BANDS
    assert cidrs == CLOUDS


# Catches a malformed export being read as "no backends" instead of refused.
@pytest.mark.parametrize("config", [
    None,
    {"enabled_backends": ["proxmox", "azure"], "aws_lab_cidr": "172.20.0.0/16",
     "gcp_lab_cidr": "172.21.0.0/16", "jumpbox_vlan_bands": DEFAULT_BANDS},
    {"enabled_backends": "proxmox", "aws_lab_cidr": "172.20.0.0/16",
     "gcp_lab_cidr": "172.21.0.0/16", "jumpbox_vlan_bands": DEFAULT_BANDS},
    {"enabled_backends": ["aws"], "gcp_lab_cidr": "172.21.0.0/16",
     "jumpbox_vlan_bands": DEFAULT_BANDS},
    {"enabled_backends": ["proxmox"], "aws_lab_cidr": "172.20.0.0/16",
     "gcp_lab_cidr": "172.21.0.0/16", "jumpbox_vlan_bands": "entry:1-2"},
])
def test_policy_inputs_refuse_a_malformed_config(config):
    with pytest.raises(ValueError):
        policy_inputs({**SLOTS, "config": config})


# Catches the policy churning (and Headscale restarting) because input order changed.
def test_output_order_is_deterministic():
    shuffled = {**SLOTS, "users": list(reversed(SLOTS["users"])),
                "groups": [{**SLOTS["groups"][0], "members": ["bob", "alice"]}]}
    first = render(BASE, SLOTS, DEFAULT_POLICY_BANDS, CLOUDS)
    second = render(BASE, shuffled, DEFAULT_POLICY_BANDS, {"gcp": CLOUDS["gcp"], "aws": CLOUDS["aws"]})
    assert first == second
    sources = [r["src"] for r in first["acls"][1:]]
    assert sources[0] == ["alice@"] and sources[2] == ["bob@"]


# Catches a disabled cloud's ranges being granted.
def test_disabled_cloud_is_absent():
    policy = render(BASE, SLOTS, DEFAULT_POLICY_BANDS, {"aws": "172.20.0.0/16"})
    text = json.dumps(policy)
    assert "172.20.25.0/26" in text
    assert "172.21." not in text


# Catches a group member reaching a different group's networks.
def test_group_membership_grants_only_that_groups_blocks():
    slots = {**SLOTS, "groups": [
        {"slug": "red-team", "members": ["alice"], "entry_vlan": 300, "internal_vlan": 400},
        {"slug": "blue-team", "members": ["bob"], "entry_vlan": 301, "internal_vlan": 401},
    ]}
    policy = render(BASE, slots, DEFAULT_POLICY_BANDS, {})
    assert _forward(policy, "alice@")["dst"] == [
        "10.100.0.0/24:*", "10.100.2.0/24:*", "10.100.4.0/24:*"]
    assert _forward(policy, "bob@")["dst"] == [
        "10.101.0.0/24:*", "10.101.2.0/24:*", "10.101.4.0/24:*"]


# Catches an empty dst list (invalid policy) for a student with nothing routed.
def test_student_with_no_routed_block_gets_no_rule():
    slots = {**SLOTS, "users": [{"login": "carol", "entry_vlan": 250, "internal_vlan": 260}],
             "groups": []}
    policy = render(BASE, slots, DEFAULT_POLICY_BANDS, CLOUDS)
    assert policy["acls"] == [BASE["acls"][0]]


def _with(**changes):
    return {**SLOTS, **changes}


# Catches inconsistent exports being rendered into a policy that grants a
# block to the wrong student or crashes Headscale.
@pytest.mark.parametrize("slots, clouds", [
    (_with(users=[{"login": "alice", "entry_vlan": 100, "internal_vlan": 200},
                  {"login": "Alice", "entry_vlan": 101, "internal_vlan": 201}], groups=[]), {}),
    (_with(users=[{"login": "alice", "entry_vlan": 100, "internal_vlan": 200},
                  {"login": "bob", "entry_vlan": 100, "internal_vlan": 201}], groups=[]), {}),
    (_with(groups=[{"slug": "red-team", "members": ["alice"], "entry_vlan": 101,
                    "internal_vlan": 400}]), {}),
    (_with(koth=[{"game_id": 1, "entry_vlan": 300, "internal_vlan": 450}]), {}),
    (_with(groups=[{"slug": "red-team", "members": ["mallory"], "entry_vlan": 300,
                    "internal_vlan": 400}]), {}),
    (_with(users=[{"login": "alice", "entry_vlan": "100", "internal_vlan": 200}], groups=[]), {}),
    (_with(users=[{"login": "alice", "entry_vlan": True, "internal_vlan": 200}], groups=[]), {}),
    (_with(users=[{"login": "alice", "entry_vlan": 100}], groups=[]), {}),
    (_with(users="alice"), {}),
    (SLOTS, {"aws": "172.20.0.0/16", "gcp": "172.20.0.0/17"}),
    (SLOTS, {"aws": "172.20.0.0/24"}),
    (SLOTS, {"aws": "10.20.0.0/16"}),
    (SLOTS, {"azure": "172.22.0.0/16"}),
])
def test_invalid_or_overlapping_inputs_are_refused(slots, clouds):
    with pytest.raises(ValueError):
        render(BASE, slots, DEFAULT_POLICY_BANDS, clouds)


@pytest.mark.parametrize("base", [{}, {"acls": {}}, {"acls": ["rule"]}, []])
def test_base_without_an_acl_list_is_refused(base):
    with pytest.raises(ValueError):
        render(base, SLOTS, DEFAULT_POLICY_BANDS, {})


# Catches a diff that hides the removal of the blanket rule from the operator.
def test_diff_shows_the_blanket_rule_removed_and_login_rules_added():
    desired = render(BASE, SLOTS, DEFAULT_POLICY_BANDS, {})
    diff = policy_diff(BASE, desired)
    lines = diff.splitlines()
    assert lines[:2] == ["--- current", "+++ desired"]
    assert any(line.startswith("-") and "autogroup:member" in line for line in lines)
    assert any(line.startswith("+") and '"alice@"' in line for line in lines)
    assert any(line.startswith("+") and '"bob@:*"' in line for line in lines)
    assert not any(line.startswith("-") and "group:staff" in line for line in lines[2:])
    assert policy_diff(desired, json.loads(json.dumps(desired))) == ""


def _cli(*args):
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, timeout=30)


# Catches the CLI rendering from anything but the base file and portal export.
def test_cli_render_emits_canonical_json_from_the_export(tmp_path):
    base, slots = tmp_path / "base.json", tmp_path / "slots.json"
    base.write_text(json.dumps(BASE))
    slots.write_text(json.dumps({**SLOTS, "config": {**SLOTS["config"],
                                                     "enabled_backends": ["aws"]}}))
    result = _cli("render", str(base), str(slots))
    assert result.returncode == 0, result.stderr
    expected = render(BASE, {**SLOTS, "config": {**SLOTS["config"], "enabled_backends": ["aws"]}},
                      replace(DEFAULT_POLICY_BANDS, proxmox_enabled=False), {"aws": CLOUDS["aws"]})
    assert result.stdout == json.dumps(expected, indent=2, sort_keys=True) + "\n"
    assert "10.100." not in result.stdout

    assert _cli("render", str(base), str(slots), "--bands", DEFAULT_BANDS).returncode != 0


def test_cli_diff_is_empty_when_equal_and_errors_go_to_stderr(tmp_path):
    current, desired = tmp_path / "current.json", tmp_path / "desired.json"
    current.write_text(json.dumps(BASE))
    desired.write_text(json.dumps(BASE, indent=4))
    result = _cli("diff", str(current), str(desired))
    assert (result.returncode, result.stdout) == (0, "")

    desired.write_text(json.dumps(render(BASE, SLOTS, DEFAULT_POLICY_BANDS, {})))
    result = _cli("diff", str(current), str(desired))
    assert result.returncode == 0
    assert "-        \"autogroup:member\"" in result.stdout

    desired.write_text("{not json")
    result = _cli("diff", str(current), str(desired))
    assert result.returncode != 0
    assert result.stdout == ""
    assert "desired.json" in result.stderr
