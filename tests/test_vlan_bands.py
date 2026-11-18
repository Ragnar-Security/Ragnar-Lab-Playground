"""VLAN_BANDS is the one source of every VLAN pool, pair and address.

The default layout must reproduce the reference deployment exactly; a custom
layout must move the allocator, the roster cap, the seed, the importer, the
pair audit, the export and the jumpbox's address plan together."""

import csv
import importlib
import importlib.util
import logging
import re
import sys
from pathlib import Path

import pytest

from app.backends.network_slots import parse_vlan_layout, proxmox_slot_cidr
from app.models import KothGame, StudentGroup, StudentWhitelist, User, db
from app.vlan_allocator import VlanPoolExhausted, allocate_pair, user_pair_for_index
from tests.test_config import _restore_reloaded_config, reload_config  # noqa: F401
from tests.test_jumpbox_scripts import CONFIGURE, NETPLAN, _configure_env, _run

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ("entry:100-199:0:0 internal:200-299:100:1 "
           "group-entry:300-399:200:2 group-internal:400-500:300:4")
# 50 students; every band maps onto 10.100-149.<third>.0/24.
CUSTOM = ("entry:1000-1049:900:0 internal:1050-1099:950:1 "
          "group-entry:1100-1149:1000:2 group-internal:1150-1199:1050:4")
PROXMOX = {"PROXMOX_HOST": "example.test", "PROXMOX_PASSWORD": "placeholder"}


def _netgen():
    path = ROOT / "scripts" / "jumpbox_netgen.py"
    spec = importlib.util.spec_from_file_location("jumpbox_netgen_bands", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config():
    return importlib.import_module("app.config")


@pytest.fixture
def custom(monkeypatch):
    monkeypatch.setattr(_config(), "VLAN_BANDS", CUSTOM)
    monkeypatch.setenv("VLAN_BANDS", CUSTOM)


def _roster(path, count):
    rows = "".join(f"{9000 + n},s{n:03d},Student {n}\n" for n in range(count))
    path.write_text("SIS User ID,SIS Login ID,Student\n" + rows)
    return str(path)


# ---- the default reproduces the reference layout ---------------------------

def test_the_default_is_the_jumpbox_default_string():
    assert _netgen().DEFAULT_BANDS == DEFAULT
    assert _config().DEFAULT_VLAN_BANDS == DEFAULT


def test_the_default_layout_reproduces_every_pool():
    layout = parse_vlan_layout(DEFAULT)
    assert (layout.entry.first, layout.entry.last) == (100, 199)
    assert (layout.internal.first, layout.internal.last) == (200, 299)
    assert (layout.group_entry.first, layout.group_entry.last) == (300, 399)
    assert (layout.group_internal.first, layout.group_internal.last) == (400, 500)
    assert (layout.roster_max, layout.pair_offset, layout.highest_tag) == (100, 100, 500)


def test_the_default_layout_reproduces_every_pair_and_address():
    layout = parse_vlan_layout(DEFAULT)
    for index in range(100):
        entry, internal = user_pair_for_index(index)
        assert (entry, internal) == (100 + index, 200 + index)
        assert proxmox_slot_cidr(entry, layout.bands) == f"10.{100 + index}.0.0/24"
        assert proxmox_slot_cidr(internal, layout.bands) == f"10.{100 + index}.1.0/24"
    assert proxmox_slot_cidr(300, layout.bands) == "10.100.2.0/24"
    assert proxmox_slot_cidr(500, layout.bands) == "10.200.4.0/24"


# Catches a pool number creeping back into the code beside the one default.
def test_no_pool_literal_outside_the_default_string():
    pool_range = re.compile(r"\b(100|200|300|400)\s*-\s*(199|299|399|400|500)\b")
    hits = []
    for base in ("app", "scripts"):
        for path in sorted((ROOT / base).rglob("*")):
            if path.suffix not in (".py", ".sh") or "__pycache__" in path.parts:
                continue
            if path.name == "jumpbox_netgen.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pool_range.search(line) or "ROSTER_MAX" in line:
                    hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert hits == []


# Catches a removed setting still being read somewhere.
def test_removed_settings_are_read_nowhere():
    removed = re.compile(r"GROUP_(ENTRY|INTERNAL)_VLAN_(MIN|MAX)|JUMPBOX_VLAN_BANDS")
    hits = []
    for base in ("app", "scripts"):
        for path in sorted((ROOT / base).rglob("*")):
            if path.suffix not in (".py", ".sh", ".example") or "__pycache__" in path.parts:
                continue
            if path in (ROOT / "app" / "config.py", ROOT / "scripts" / "jumpbox_configure.sh",
                        ROOT / "scripts" / "preflight_env.py"):
                continue  # each names a removed setting once, to report a stale value
            text = path.read_text(encoding="utf-8")
            if removed.search(text):
                hits.append(str(path.relative_to(ROOT)))
    assert hits == []
    config = _config()
    for name in ("GROUP_ENTRY_VLAN_MIN", "GROUP_ENTRY_VLAN_MAX", "GROUP_INTERNAL_VLAN_MIN",
                 "GROUP_INTERNAL_VLAN_MAX", "JUMPBOX_VLAN_BANDS", "get_group_vlan_ranges"):
        assert not hasattr(config, name), name


# ---- a custom layout moves every consumer together -------------------------

def test_a_custom_layout_drives_the_allocator(app, custom):
    with app.app_context():
        assert allocate_pair(scope="user") == (1000, 1050)
        assert allocate_pair(scope="group") == (1100, 1150)
        assert user_pair_for_index(49) == (1049, 1099)
        with pytest.raises(VlanPoolExhausted, match="50"):
            user_pair_for_index(50)


def test_a_custom_layout_drives_the_group_yaml_plan(app, custom):
    from app.routes.student_groups import _build_plan
    with app.app_context():
        plan, errors, _warnings = _build_plan([{"name": "red", "display_name": "Red"}])
        assert errors == []
        assert plan["vlan_allocations"]["red"] == (1100, 1150)


def test_a_custom_layout_caps_the_changeover_roster(tmp_path, custom):
    from scripts.build_students_merged import read_canvas, roster_max
    assert roster_max() == 50
    assert len(read_canvas(_roster(tmp_path / "fifty.csv", 50))) == 50
    with pytest.raises(SystemExit, match="51 students.*at most 50"):
        read_canvas(_roster(tmp_path / "fifty-one.csv", 51))


def test_a_custom_layout_drives_the_seed(app, tmp_path, custom):
    from scripts.init_db import import_student_whitelist
    with app.app_context():
        with pytest.raises(ValueError, match="51 students.*holds 50"):
            import_student_whitelist(_roster(tmp_path / "big.csv", 51))
        db.session.rollback()
        import_student_whitelist(_roster(tmp_path / "ok.csv", 2))
        rows = StudentWhitelist.query.order_by(StudentWhitelist.entry_vlan_tag).all()
        assert [(r.entry_vlan_tag, r.internal_vlan_tag) for r in rows] == [(1000, 1050), (1001, 1051)]


def test_a_custom_layout_drives_the_importer(app, tmp_path, monkeypatch, custom):
    from scripts import import_students
    monkeypatch.setattr(import_students, "create_app", lambda: app)
    with app.app_context():
        db.session.add(StudentWhitelist(login_id="old", entry_vlan_tag=1000, internal_vlan_tag=1050))
        db.session.commit()
    import_students.import_students_from_csv(_roster(tmp_path / "one.csv", 1))
    with app.app_context():
        row = StudentWhitelist.query.filter_by(login_id="s000").one()
        assert (row.entry_vlan_tag, row.internal_vlan_tag) == (1001, 1051)


def test_a_custom_layout_drives_the_migration(app, custom):
    from scripts import migrate_add_vlan
    with app.app_context():
        row = StudentWhitelist(login_id="late")
        db.session.add(row)
        db.session.commit()
        migrate_add_vlan.assign_vlans_to_whitelist([row])
        db.session.expire_all()
        row = StudentWhitelist.query.filter_by(login_id="late").one()
        assert (row.entry_vlan_tag, row.internal_vlan_tag) == (1000, 1050)


def test_a_custom_layout_drives_the_export_and_the_audit(app, custom):
    from scripts.audit_vlan_pairs import audit
    from scripts.export_network_slots import collect_network_slots
    with app.app_context():
        db.session.add_all([
            StudentWhitelist(login_id="alice", entry_vlan_tag=1000, internal_vlan_tag=1050),
            StudentWhitelist(login_id="zed", entry_vlan_tag=100, internal_vlan_tag=200)])
        db.session.commit()
        slots = collect_network_slots()
    assert slots["config"]["vlan_bands"] == CUSTOM
    assert "jumpbox_vlan_bands" not in slots["config"]
    problems = audit(slots)
    assert problems == ["zed: pair 100/200 is outside entry 1000-1049 with internal = entry + 50"]


def test_the_audit_refuses_an_export_without_bands():
    from scripts.audit_vlan_pairs import audit
    with pytest.raises(ValueError, match="vlan_bands"):
        audit({"config": {}, "users": [], "groups": [], "koth": []})


def test_a_custom_layout_gives_the_jumpbox_the_portals_addresses():
    netgen, layout = _netgen(), parse_vlan_layout(CUSTOM)
    jumpbox = netgen.parse_bands(CUSTOM)
    assert netgen.vlan_tags(jumpbox) == list(range(1000, 1200))
    for tag in netgen.vlan_tags(jumpbox):
        gateway = netgen.gateway(tag, jumpbox)
        assert proxmox_slot_cidr(tag, layout.bands) == gateway.replace(".1/24", ".0/24")


def test_jumpbox_configure_reads_vlan_bands_from_site_env(tmp_path):
    env, sysroot = _configure_env(tmp_path, f'VLAN_BANDS="{CUSTOM}"\n')
    result = _run(CONFIGURE, ["--apply", "--no-try"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    import yaml
    vlans = yaml.safe_load((sysroot / NETPLAN).read_text())["network"]["vlans"]
    assert sorted(int(name[4:]) for name in vlans) == list(range(1000, 1200))
    assert vlans["vlan1000"]["addresses"] == ["10.100.0.1/24"]
    assert vlans["vlan1199"]["addresses"] == ["10.149.4.1/24"]


# Catches a layout left under the old name: the jumpbox would fall back to
# the default bands and renumber every lab network.
def test_jumpbox_configure_refuses_the_old_setting_name(tmp_path):
    env, sysroot = _configure_env(tmp_path, f'JUMPBOX_VLAN_BANDS="{CUSTOM}"\n')
    before = {p: p.read_bytes() for p in sysroot.rglob("*") if p.is_file()}
    result = _run(CONFIGURE, ["--apply", "--no-try"], env)
    assert result.returncode != 0
    assert "JUMPBOX_VLAN_BANDS was renamed VLAN_BANDS" in result.stderr
    assert {p: p.read_bytes() for p in sysroot.rglob("*") if p.is_file()} == before


# Catches a policy that grants networks the jumpbox does not route: the
# portal's exported bands and the jumpbox's site.env bands differ.
def test_the_policy_refuses_when_site_env_bands_differ_from_the_portal(tmp_path):
    from tests.test_ops_scripts import _apply_policy, _log, _policy_rig
    env, policy, original, _desired = _policy_rig(tmp_path, VLAN_BANDS=CUSTOM)
    result = _apply_policy(env, "--apply")
    assert result.returncode != 0
    assert "Set the same VLAN_BANDS" in result.stderr
    assert CUSTOM in result.stderr
    assert "headscale-host" not in _log(env, "SSH_LOG")
    assert policy.read_text() == original


def test_the_default_layout_starts_without_warnings(monkeypatch, caplog):
    cfg = reload_config(monkeypatch, SECRET_KEY="test-secret-key", **PROXMOX)
    with caplog.at_level(logging.WARNING, logger="app.config"):
        cfg.validate_config()
    assert caplog.records == []


# Catches the host preflight passing a .env the portal then refuses to start on.
@pytest.mark.parametrize("extra, problem", [
    ({"VLAN_BANDS": f'"{CUSTOM}"'}, None),
    ({"VLAN_BANDS": '"entry:100-199:0:0"'}, "VLAN_BANDS: band internal, group-entry"),
    ({"VLAN_BANDS": f'"{CUSTOM}"', "ENABLED_BACKENDS": "proxmox,aws"}, "VLAN_BANDS: reaches VLAN 1199"),
    ({"JUMPBOX_VLAN_BANDS": f'"{DEFAULT}"'}, "JUMPBOX_VLAN_BANDS: is no longer read"),
    ({"GROUP_ENTRY_VLAN_MIN": "300"}, "GROUP_ENTRY_VLAN_MIN: is no longer read"),
])
def test_the_host_preflight_applies_the_startup_rules(tmp_path, extra, problem):
    from tests.test_preflight_env import AWS_ENV, GOOD, _run, _write_env
    values = {**GOOD, **AWS_ENV, **extra} if "aws" in extra.get("ENABLED_BACKENDS", "") \
        else {**GOOD, **extra}
    result = _run(str(_write_env(tmp_path / ".env", values)))
    if problem is None:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode == 1 and problem in result.stdout + result.stderr


# ---- startup refuses a layout the portal cannot serve ----------------------

@pytest.mark.parametrize("spec, message", [
    ("entry:100-199", "bad VLAN band"),
    ("entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2", "group-internal"),
    ("entry:100-199:0:0 internal:150-249:50:1 group-entry:300-399:200:2 "
     "group-internal:400-500:300:4", "is in bands entry and internal"),
    ("entry:100-199:0:0 internal:200-249:100:1 group-entry:300-399:200:2 "
     "group-internal:400-500:300:4", "internal has 50 tags but entry has 100"),
    ("entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 "
     "group-internal:400-449:300:4", "group-internal has 50 tags, fewer than group-entry's 100"),
    ("entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 "
     "group-internal:4000-4100:3900:4", "2 <= first <= last <= 4094"),
    ("entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 "
     "group-internal:400-500:100:4", "is not an address"),
    ("entry:100-149:0:0 entry:150-199:0:3 internal:200-299:100:1 group-entry:300-399:200:2 "
     "group-internal:400-500:300:4", "entry appears more than once"),
])
def test_startup_refuses_a_layout_it_cannot_serve(monkeypatch, spec, message):
    cfg = reload_config(monkeypatch, VLAN_BANDS=spec, **PROXMOX)
    with pytest.raises(ValueError, match=f"VLAN_BANDS.*{re.escape(message)}"):
        cfg.validate_config()


def test_startup_accepts_the_default_and_a_custom_layout(monkeypatch):
    reload_config(monkeypatch, **PROXMOX).validate_config()
    reload_config(monkeypatch, VLAN_BANDS=CUSTOM, **PROXMOX).validate_config()


# Catches a layout past the highest cloud network slot with a cloud enabled:
# its tags would have no subnet.
def test_startup_refuses_tags_past_the_cloud_slots(monkeypatch):
    from tests.test_config import AWS_VALUES
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS="aws", VLAN_BANDS=CUSTOM, **AWS_VALUES)
    with pytest.raises(ValueError, match="VLAN_BANDS.*1199.*slot 500"):
        cfg.validate_config()


@pytest.mark.parametrize("name", ["GROUP_ENTRY_VLAN_MIN", "GROUP_ENTRY_VLAN_MAX",
                                  "GROUP_INTERNAL_VLAN_MIN", "GROUP_INTERNAL_VLAN_MAX",
                                  "JUMPBOX_VLAN_BANDS"])
def test_a_removed_setting_in_the_environment_is_reported(monkeypatch, caplog, name):
    cfg = reload_config(monkeypatch, **{name: "300"}, **PROXMOX)
    with caplog.at_level(logging.WARNING, logger="app.config"):
        cfg.validate_config()
    assert any(name in r.getMessage() and "VLAN_BANDS" in r.getMessage()
               for r in caplog.records), caplog.text


# ---- startup refuses stored tags the layout no longer serves ---------------

def _start(app, monkeypatch):
    """create_app() along the non-test path, against the fixture's database."""
    import app as app_package
    monkeypatch.setattr(app_package.config, "SQLALCHEMY_DATABASE_URI",
                        app.config["SQLALCHEMY_DATABASE_URI"])
    monkeypatch.setattr(app_package.config, "SQLALCHEMY_ENGINE_OPTIONS", {}, raising=False)
    monkeypatch.setattr(app_package.config, "TESTING", False)
    monkeypatch.setattr(app_package, "validate_config", lambda: None)
    return app_package.create_app(start_background_tasks=False)


def _student(model, login, entry, internal):
    row = model(login_id=login, entry_vlan_tag=entry, internal_vlan_tag=internal)
    if model is User:
        row.set_password("placeholder")
    return row


@pytest.mark.parametrize("model", [User, StudentWhitelist])
def test_startup_refuses_a_student_pair_outside_the_bands(app, monkeypatch, model, custom):
    with app.app_context():
        db.session.add(_student(model, "alice", 100, 200))
        db.session.commit()
    with pytest.raises(ValueError, match=r"alice.*entry VLAN 100.*VLAN_BANDS"):
        _start(app, monkeypatch)


def test_startup_accepts_student_pairs_inside_the_bands(app, monkeypatch, custom):
    with app.app_context():
        db.session.add_all([_student(User, "alice", 1000, 1050),
                            _student(StudentWhitelist, "alice", 1000, 1050)])
        db.session.commit()
    _start(app, monkeypatch)


# Explicit group and KOTH VLANs may sit outside the pools by design; they are
# reported, never a reason to refuse.
def test_startup_warns_about_group_and_koth_tags_outside_every_band(app, monkeypatch, caplog):
    with app.app_context():
        db.session.add_all([
            StudentGroup(name="red", display_name="Red", entry_vlan_tag=999, internal_vlan_tag=1000),
            KothGame(name="k", deployment_group_name="arena", entry_vlan_tag=550,
                     internal_vlan_tag=551, status="draft"),
            KothGame(name="old", deployment_group_name="arena", entry_vlan_tag=560,
                     internal_vlan_tag=561, status="ended"),
        ])
        db.session.commit()
    with caplog.at_level(logging.WARNING):
        _start(app, monkeypatch)
    text = caplog.text
    assert "group red" in text and "999" in text and "1000" in text
    assert "KOTH game" in text and "550" in text and "551" in text
    assert "560" not in text


def test_startup_skips_the_check_before_the_tables_exist(app, monkeypatch, custom):
    with app.app_context():
        db.drop_all()
    _start(app, monkeypatch)
    with app.app_context():
        db.create_all()
