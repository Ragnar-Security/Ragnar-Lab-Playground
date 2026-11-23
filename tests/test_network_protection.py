"""The portal's side of group and KOTH internal-network protection.

On Proxmox a group's or KOTH game's internal network is reached only by
pivoting through a host on its entry network. The operator's apply script
installs the jumpbox's drop and the Headscale policy, verifies both, and then
records the protected VLANs here. A deployment that relies on the protection
starts only once its internal VLAN is recorded, and an entry VLAN is never
handed out while the jumpbox still drops it.
"""

import io
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect

from app import network_protection
from app.models import KothGame, ProtectedVlan, StudentGroup, User, db
from app.vlan_allocator import VlanInUse, allocate_pair
from scripts import migrate_add_protected_vlans, record_protected_vlans


def _tags():
    return sorted(row.tag for row in ProtectedVlan.query.all())


# Catches a stale tag surviving a newer verification (a deleted group's
# internal VLAN would stay "protected" and block its reuse forever), or a
# partial write leaving a mixed set.
def test_recording_replaces_the_whole_set(app):
    with app.app_context():
        network_protection.record([450, 400])
        assert _tags() == [400, 450]
        network_protection.record([401])
        assert _tags() == [401]
        assert network_protection.protected_tags() == {401}
        network_protection.record([])
        assert _tags() == []


@pytest.mark.parametrize("tags", [[1], [4095], ["400"], [True], [400, 400.5]])
def test_recording_refuses_an_invalid_tag_and_keeps_the_old_set(app, tags):
    with app.app_context():
        network_protection.record([400])
        with pytest.raises(ValueError):
            network_protection.record(tags)
        assert _tags() == [400]


# Catches a group or KOTH deployment starting before the jumpbox drop and the
# narrowed policy for its internal network are verified (the players would
# reach it straight from the VPN), or while its entry network is still dropped.
def test_a_protected_deployment_needs_its_internal_vlan_recorded_and_its_entry_free(app):
    with app.app_context():
        refusal = network_protection.deployment_refusal(300, 400)
        assert refusal and "400" in refusal and "apply_headscale_policy.sh" in refusal
        network_protection.record([400])
        assert network_protection.deployment_refusal(300, 400) is None
        network_protection.record([300, 400])
        refusal = network_protection.deployment_refusal(300, 400)
        assert refusal and "300" in refusal


# Catches a VLAN the jumpbox still drops being handed out as a network players
# must reach directly: an entry network, or a student's own internal network.
def test_allocation_never_hands_out_a_protected_tag_as_a_reachable_network(app):
    with app.app_context():
        network_protection.record([300, 400, 100, 200])
        with pytest.raises(VlanInUse, match="protected"):
            allocate_pair('group', explicit_entry=300, explicit_internal=401)
        with pytest.raises(VlanInUse, match="protected"):
            allocate_pair('user', explicit_entry=101, explicit_internal=200)
        # A protected tag may become another group's internal network: it stays protected.
        assert allocate_pair('group', explicit_entry=301, explicit_internal=400) == (301, 400)
        # Automatic allocation skips a protected tag for an entry VLAN, and may
        # reuse one as an internal VLAN, which stays protected.
        assert allocate_pair('group') == (301, 400)
        assert allocate_pair('user') == (101, 201)


def test_record_script_reads_the_protection_json_and_reports_it(app, monkeypatch, capsys):
    monkeypatch.setattr(record_protected_vlans, "create_app", lambda **_: app)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"vlans": [450, 400], "staff_ips": []})))
    assert record_protected_vlans.main() == 0
    assert "recorded 2 protected VLAN(s): 400, 450" in capsys.readouterr().out
    with app.app_context():
        assert _tags() == [400, 450]
    monkeypatch.setattr("sys.stdin", io.StringIO('{"vlans": "400"}'))
    assert record_protected_vlans.main() == 1
    with app.app_context():
        assert _tags() == [400, 450]


def test_migration_creates_the_table_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    migrate_add_protected_vlans.run_migration(url)
    migrate_add_protected_vlans.run_migration(url)
    columns = {c["name"] for c in inspect(create_engine(url)).get_columns("protected_vlans")}
    assert columns == {"tag", "verified_at"}
    table = migrate_add_protected_vlans._table()
    model = ProtectedVlan.__table__
    assert [(c.name, type(c.type).__name__, c.nullable, c.primary_key) for c in table.columns] == \
        [(c.name, type(c.type).__name__, c.nullable, c.primary_key) for c in model.columns]
