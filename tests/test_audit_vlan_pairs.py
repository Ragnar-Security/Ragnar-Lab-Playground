"""The VLAN pair audit refuses what the user bands cannot serve."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.audit_vlan_pairs import audit

ROOT = Path(__file__).resolve().parent.parent


def _slots(users=(), groups=(), koth=()):
    bands = "entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4"
    return {"config": {"vlan_bands": bands}, "users": list(users), "groups": list(groups),
            "koth": list(koth)}


def _user(login, entry, internal):
    return {"login": login, "entry_vlan": entry, "internal_vlan": internal}


def test_a_clean_export_has_no_problems():
    assert audit(_slots([_user("alice", 100, 200), _user("bob", 199, 299)],
                        [{"slug": "red", "members": ["alice"], "entry_vlan": 300, "internal_vlan": 400}],
                        [{"game_id": 1, "entry_vlan": 350, "internal_vlan": 450}])) == []


# Catches a pair the old importers numbered past the 100th student.
def test_a_legacy_200_300_pair_is_reported():
    problems = audit(_slots([_user("alice", 100, 200), _user("zed", 200, 300)]))
    assert any("zed" in p and "200/300" in p for p in problems)
    assert any("VLAN 200" in p and "alice" in p and "zed" in p for p in problems)


@pytest.mark.parametrize("entry, internal", [(99, 199), (150, 251), (150, 150)])
def test_a_pair_outside_the_rule_is_reported(entry, internal):
    assert audit(_slots([_user("eve", entry, internal)]))


# Catches one tag held by owners of different kinds.
@pytest.mark.parametrize("groups, koth", [
    ([{"slug": "red", "members": [], "entry_vlan": 200, "internal_vlan": 400}], []),
    ([], [{"game_id": 7, "entry_vlan": 100, "internal_vlan": 450}]),
])
def test_cross_kind_collisions_are_reported(groups, koth):
    assert any(p.startswith("VLAN ") for p in audit(_slots([_user("alice", 100, 200)], groups, koth)))


def test_cli_exits_nonzero_and_names_each_problem(tmp_path):
    path = tmp_path / "slots.json"
    path.write_text(json.dumps(_slots([_user("zed", 200, 300)])))
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_vlan_pairs.py"), str(path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "zed" in result.stdout


# Catches the audit judging pairs by a different layout than the allocator's.
def test_audit_accepts_every_pair_the_allocator_numbers():
    from app.config import VLAN_BANDS
    from app.vlan_allocator import user_pair_for_index, vlan_layout
    users = [_user(f"s{i}", *user_pair_for_index(i)) for i in range(vlan_layout().roster_max)]
    assert audit({"config": {"vlan_bands": VLAN_BANDS}, "users": users}) == []
