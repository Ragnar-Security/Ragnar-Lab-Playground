"""The portal-side export the Headscale policy is rendered from."""

import importlib
import json

import pytest

from app.models import KothGame, StudentGroup, StudentGroupMember, StudentWhitelist, User, db
from scripts import export_network_slots
from scripts.export_network_slots import collect_network_slots

DEFAULT_BANDS = "entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4"


@pytest.fixture
def portal_config(monkeypatch):
    config = importlib.import_module("app.config")
    monkeypatch.setattr(config, "ENABLED_BACKENDS", ("proxmox", "aws"))
    monkeypatch.setattr(config, "AWS_LAB_CIDR", "172.20.0.0/16")
    monkeypatch.setattr(config, "GCP_LAB_CIDR", "172.21.0.0/16")
    monkeypatch.setattr(config, "JUMPBOX_VLAN_BANDS", DEFAULT_BANDS)
    return config


def _user(login, entry, internal):
    user = User(login_id=login, full_name=login.title(),
                entry_vlan_tag=entry, internal_vlan_tag=internal)
    user.set_password("x")
    return user


def _game(name, entry, internal, status):
    return KothGame(name=name, deployment_group_name="arena",
                    entry_vlan_tag=entry, internal_vlan_tag=internal,
                    status=status, is_active=status == "active")


def _seed_roster():
    # Reverse order: the export must sort, not echo insertion order.
    db.session.add(StudentWhitelist(login_id="bob", entry_vlan_tag=101, internal_vlan_tag=201))
    db.session.add(StudentWhitelist(login_id="alice", entry_vlan_tag=100, internal_vlan_tag=200))
    # Alice was reassigned after registration; the User row is what /create uses.
    bob, alice = _user("bob", 101, 201), _user("alice", 105, 205)
    group = StudentGroup(name="red-team", display_name="Red",
                         entry_vlan_tag=300, internal_vlan_tag=400)
    db.session.add_all([bob, alice, group])
    db.session.flush()
    db.session.add_all([StudentGroupMember(group_id=group.id, user_id=bob.id),
                        StudentGroupMember(group_id=group.id, user_id=alice.id)])
    active = _game("live", 350, 450, "active")
    db.session.add_all([active, _game("old", 351, 451, "ended")])
    db.session.commit()
    return active.id


# Catches the policy granting a stale or partial roster or another portal's networks.
def test_export_is_the_sorted_roster_groups_koth_and_portal_config(app, portal_config):
    with app.app_context():
        koth_game_id = _seed_roster()
        assert collect_network_slots() == {
            "config": {
                "enabled_backends": ["proxmox", "aws"],
                "aws_lab_cidr": "172.20.0.0/16",
                "gcp_lab_cidr": "172.21.0.0/16",
                "jumpbox_vlan_bands": DEFAULT_BANDS,
            },
            "users": [
                {"login": "alice", "entry_vlan": 105, "internal_vlan": 205},
                {"login": "bob", "entry_vlan": 101, "internal_vlan": 201},
            ],
            "groups": [{
                "slug": "red-team", "members": ["alice", "bob"],
                "entry_vlan": 300, "internal_vlan": 400,
            }],
            "koth": [{"game_id": koth_game_id, "entry_vlan": 350, "internal_vlan": 450}],
        }


# Catches a student who has not registered yet losing access until first login.
def test_unregistered_student_is_exported_from_the_whitelist(app, portal_config):
    with app.app_context():
        db.session.add(StudentWhitelist(login_id="carol", entry_vlan_tag=102, internal_vlan_tag=202))
        db.session.commit()
        assert collect_network_slots()["users"] == [
            {"login": "carol", "entry_vlan": 102, "internal_vlan": 202}]


# Catches a registered non-roster account (an admin) being granted student blocks.
def test_users_absent_from_the_whitelist_are_not_exported(app, portal_config):
    with app.app_context():
        db.session.add(_user("admin", 150, 250))
        db.session.commit()
        assert collect_network_slots()["users"] == []


# Catches a half-assigned student being silently granted one block, or none.
@pytest.mark.parametrize("entry, internal", [(None, 200), (100, None), (None, None)])
def test_whitelist_row_missing_a_vlan_is_refused(app, portal_config, entry, internal):
    with app.app_context():
        db.session.add(StudentWhitelist(login_id="alice", entry_vlan_tag=entry,
                                        internal_vlan_tag=internal))
        db.session.commit()
        with pytest.raises(ValueError, match="alice"):
            collect_network_slots()


# Catches the whitelist's old pair masking a registered user whose own pair is broken.
def test_registered_user_missing_a_vlan_is_refused(app, portal_config):
    with app.app_context():
        db.session.add(StudentWhitelist(login_id="alice", entry_vlan_tag=100, internal_vlan_tag=200))
        db.session.add(_user("alice", 105, None))
        db.session.commit()
        with pytest.raises(ValueError, match="alice"):
            collect_network_slots()


# Catches a group whose blocks are half-granted.
@pytest.mark.parametrize("entry, internal", [(None, 400), (300, None)])
def test_group_missing_a_vlan_is_refused(app, portal_config, entry, internal):
    with app.app_context():
        db.session.add(StudentGroup(name="red-team", display_name="Red",
                                    entry_vlan_tag=entry, internal_vlan_tag=internal))
        db.session.commit()
        with pytest.raises(ValueError, match="red-team"):
            collect_network_slots()


# Catches a non-roster member being granted group blocks the roster never grants.
def test_group_member_absent_from_the_whitelist_is_refused(app, portal_config):
    with app.app_context():
        db.session.add(StudentWhitelist(login_id="alice", entry_vlan_tag=100, internal_vlan_tag=200))
        mallory = _user("mallory", 150, 250)
        group = StudentGroup(name="red-team", display_name="Red",
                             entry_vlan_tag=300, internal_vlan_tag=400)
        db.session.add_all([mallory, group])
        db.session.flush()
        db.session.add(StudentGroupMember(group_id=group.id, user_id=mallory.id))
        db.session.commit()
        with pytest.raises(ValueError, match="mallory"):
            collect_network_slots()


# Catches a failed export reaching stdout, where the policy renderer would read it.
def test_main_prints_compact_json_and_reports_errors_on_stderr(app, portal_config,
                                                               monkeypatch, capsys):
    monkeypatch.setattr(export_network_slots, "create_app", lambda: app)
    with app.app_context():
        db.session.add(StudentWhitelist(login_id="alice", entry_vlan_tag=100, internal_vlan_tag=200))
        db.session.commit()
    assert export_network_slots.main() == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["users"] == [{"login": "alice", "entry_vlan": 100, "internal_vlan": 200}]
    assert out == json.dumps(json.loads(out), separators=(",", ":")) + "\n"

    with app.app_context():
        db.session.add(StudentWhitelist(login_id="bob", entry_vlan_tag=101))
        db.session.commit()
    assert export_network_slots.main() != 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "bob" in err
