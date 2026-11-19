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
    # The portal reads app.config.VLAN_BANDS. The environment is cleared so
    # no standalone script can pass by reading it instead of its own input.
    monkeypatch.setattr(_config(), "VLAN_BANDS", CUSTOM)
    monkeypatch.delenv("VLAN_BANDS", raising=False)


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
            # The default, and the pre-upgrade default an upgrade seeds from.
            if path.name in ("jumpbox_netgen.py", "migrate_add_vlan_layout_record.py"):
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
                        ROOT / "scripts" / "preflight_env.py",
                        # reads the pre-upgrade name to seed the old layout
                        ROOT / "scripts" / "migrate_add_vlan_layout_record.py"):
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


def test_a_custom_layout_caps_the_changeover_roster(tmp_path, monkeypatch):
    from scripts.build_students_merged import read_canvas, roster_max
    monkeypatch.delenv("VLAN_BANDS", raising=False)
    assert roster_max(CUSTOM) == 50 and roster_max("") == 100
    assert len(read_canvas(_roster(tmp_path / "fifty.csv", 50), CUSTOM)) == 50
    with pytest.raises(SystemExit, match="51 students.*at most 50"):
        read_canvas(_roster(tmp_path / "fifty-one.csv", 51), CUSTOM)


def _keys(path, count):
    path.write_text("".join(f"s{n:03d},s{n:03d}@example.test,key-{n}\n" for n in range(count)))
    return str(path)


# Catches the roster cap ignoring a layout set only in scripts/site.env: the
# operator's shell does not export it, so only the file names it.
def test_the_merge_tool_reads_the_cap_from_the_site_file_it_is_given(tmp_path):
    import os
    import subprocess
    import sys
    site = tmp_path / "site.env"
    site.write_text(f'PORTAL_HOST=portal-host\nVLAN_BANDS="{CUSTOM}"\n')
    env = {k: v for k, v in os.environ.items() if k != "VLAN_BANDS"}
    tool = [sys.executable, str(ROOT / "scripts" / "build_students_merged.py")]

    def merge(count, *layout):
        return subprocess.run(
            [*tool, *layout, _roster(tmp_path / f"c{count}.csv", count),
             _keys(tmp_path / f"k{count}.csv", count), str(tmp_path / "out.csv")],
            env=env, capture_output=True, text=True, timeout=30)

    over = merge(51, "--site-env", str(site))
    assert over.returncode != 0 and "51 students" in over.stderr and "at most 50" in over.stderr
    assert not (tmp_path / "out.csv").exists()
    assert merge(50, "--site-env", str(site)).returncode == 0
    assert merge(51, "--bands", "").returncode == 0            # empty: the default 100
    unnamed = merge(1)
    assert unnamed.returncode != 0 and "--site-env" in unnamed.stderr


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
    _seed(app, CUSTOM)
    with app.app_context():
        db.session.add(_student(model, "alice", 100, 200))
        db.session.commit()
    with pytest.raises(ValueError, match=r"alice.*entry VLAN 100.*VLAN_BANDS"):
        _start(app, monkeypatch)


def test_startup_accepts_student_pairs_inside_the_bands(app, monkeypatch, custom):
    _seed(app, CUSTOM)
    with app.app_context():
        db.session.add_all([_student(User, "alice", 1000, 1050),
                            _student(StudentWhitelist, "alice", 1000, 1050)])
        db.session.commit()
    _start(app, monkeypatch)


def _seed(app, bands):
    """Seed the layout record, as the migration does on an upgrade."""
    _migration().run_migration(app.config["SQLALCHEMY_DATABASE_URI"], bands=bands)


def _migration():
    spec = importlib.util.spec_from_file_location(
        "migrate_add_vlan_layout_record", ROOT / "scripts" / "migrate_add_vlan_layout_record.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


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


# ---- the recorded layout: a change under stored pairs needs acceptance -----

SHIFTED = ("entry:100-199:50:0 internal:200-299:100:1 "
           "group-entry:300-399:200:2 group-internal:400-500:300:4")


def _recorded(app):
    from app.models import VlanLayoutRecord
    with app.app_context():
        row = db.session.get(VlanLayoutRecord, 1)
        return row.bands if row is not None else None


def test_the_first_start_records_the_layout(app, monkeypatch):
    assert _recorded(app) is None
    _start(app, monkeypatch)
    assert _recorded(app) == parse_vlan_layout(DEFAULT).canonical == DEFAULT


def test_an_offset_change_under_stored_pairs_refuses(app, monkeypatch):
    _start(app, monkeypatch)
    with app.app_context():
        db.session.add(_student(StudentWhitelist, "alice", 100, 200))
        db.session.commit()
    monkeypatch.setattr(_config(), "VLAN_BANDS", SHIFTED)  # every tag still in its band
    with pytest.raises(ValueError, match=(r"(?s)recorded: " + re.escape(DEFAULT)
                                          + r".*VLAN_BANDS: " + re.escape(SHIFTED)
                                          + r".*accept_vlan_layout.py")):
        _start(app, monkeypatch)
    assert _recorded(app) == DEFAULT


def test_a_layout_change_with_no_stored_pair_is_recorded(app, monkeypatch):
    _start(app, monkeypatch)
    monkeypatch.setattr(_config(), "VLAN_BANDS", SHIFTED)
    _start(app, monkeypatch)
    assert _recorded(app) == SHIFTED


def _accept(app, monkeypatch, *args):
    from scripts import accept_vlan_layout
    monkeypatch.setattr(accept_vlan_layout, "create_app", lambda **_: app)
    return accept_vlan_layout.main(list(args))


def test_accepting_a_layout_between_terms_records_it(app, monkeypatch, capsys):
    _start(app, monkeypatch)
    with app.app_context():
        db.session.add(_student(StudentWhitelist, "alice", 100, 200))
        db.session.commit()
    monkeypatch.setattr(_config(), "VLAN_BANDS", SHIFTED)
    assert _accept(app, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "alice: entry VLAN 100: 10.100.0.0/24 -> 10.50.0.0/24" in out
    assert "internal VLAN 200" not in out  # its subnet does not move
    assert _recorded(app) == DEFAULT       # the dry run records nothing
    assert _accept(app, monkeypatch, "--apply") == 0
    assert _recorded(app) == SHIFTED
    _start(app, monkeypatch)


def test_accepting_a_layout_refuses_while_a_vm_is_live(app, monkeypatch, capsys):
    from datetime import datetime
    from app.models import VMRecord
    _start(app, monkeypatch)
    with app.app_context():
        db.session.add_all([
            _student(StudentWhitelist, "alice", 100, 200),
            VMRecord(vmid=7000, vm_name="student-alice-srv01", vm_type="qemu", node="node-a",
                     template_name="t", student_name="alice", expires_at=datetime.now())])
        db.session.commit()
    monkeypatch.setattr(_config(), "VLAN_BANDS", SHIFTED)
    assert _accept(app, monkeypatch, "--apply") == 1
    assert "1 VM record(s) are live" in capsys.readouterr().err
    assert _recorded(app) == DEFAULT


def test_the_layout_migration_creates_the_model_table_and_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect
    from app.models import VlanLayoutRecord
    spec = importlib.util.spec_from_file_location(
        "migrate_add_vlan_layout_record", ROOT / "scripts" / "migrate_add_vlan_layout_record.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    url = f"sqlite:///{tmp_path / 'm.db'}"
    migration.run_migration(url)
    migration.run_migration(url)
    columns = {c["name"]: (str(c["type"]), c["nullable"])
               for c in inspect(create_engine(url)).get_columns("vlan_layout_record")}
    model = {c.name: (str(c.type), c.nullable) for c in VlanLayoutRecord.__table__.columns}
    assert columns == model


# ---- introducing the record on a site that already has students ------------

def _with_pairs(app):
    with app.app_context():
        db.session.add_all([_student(StudentWhitelist, "alice", 100, 200),
                            _student(User, "alice", 100, 200)])
        db.session.commit()


# Catches the introducing deploy blessing whatever VLAN_BANDS it starts with.
def test_stored_pairs_with_no_recorded_layout_refuse_to_start(app, monkeypatch):
    _with_pairs(app)
    with pytest.raises(ValueError, match=r"No VLAN layout is recorded.*migrate_add_vlan_layout_record"):
        _start(app, monkeypatch)
    assert _recorded(app) is None


def test_a_changed_offset_on_the_introducing_deploy_refuses(app, monkeypatch):
    _with_pairs(app)
    with pytest.raises(SystemExit, match="2 student VLAN pair"):
        _seed(app, None)  # a layout is required once pairs exist
    _seed(app, DEFAULT)   # the correct pre-upgrade layout
    assert _recorded(app) == DEFAULT
    monkeypatch.setattr(_config(), "VLAN_BANDS", SHIFTED)
    with pytest.raises(ValueError, match="differs from the layout"):
        _start(app, monkeypatch)
    monkeypatch.setattr(_config(), "VLAN_BANDS", DEFAULT)
    _start(app, monkeypatch)


def test_the_seed_is_validated_against_every_stored_pair(app):
    _with_pairs(app)
    with pytest.raises(SystemExit, match="user alice: entry VLAN 100 is in no band"):
        _seed(app, CUSTOM)
    assert _recorded(app) is None


# The release before VLAN_BANDS routed no internal band: seeding that layout
# makes the upgrade show, and require accepting, every internal network move.
def test_upgrading_from_the_pre_internal_layout_lists_the_moves(app, monkeypatch, tmp_path, capsys):
    _with_pairs(app)
    site = tmp_path / "site.env"
    site.write_text("PORTAL_HOST=portal-host\n")  # JUMPBOX_VLAN_BANDS was unset
    migration = _migration()
    _seed(app, migration.bands_from_site_env(str(site)))
    assert _recorded(app) == migration.PRE_VLAN_BANDS_DEFAULT
    with pytest.raises(ValueError, match="differs from the layout"):
        _start(app, monkeypatch)
    assert _accept(app, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "alice: internal VLAN 200: 10.200.0.0/24 -> 10.100.1.0/24" in out
    assert "entry VLAN 100" not in out
    assert _accept(app, monkeypatch, "--apply") == 0
    _start(app, monkeypatch)
    site.write_text(f'JUMPBOX_VLAN_BANDS="{CUSTOM}"\n')
    assert migration.bands_from_site_env(str(site)) == CUSTOM


# ---- accept and admission exclude each other ------------------------------

def test_an_admitted_deployment_blocks_the_accept(app, monkeypatch, capsys):
    from app import coordination
    from app.models import NodeMemoryReservation
    _start(app, monkeypatch)
    with app.app_context():
        db.session.add(NodeMemoryReservation(reservation_id="r1", node="node-a", memory_bytes=1,
                                             expires_at=coordination.lease_clock() + 600))
        db.session.commit()
    monkeypatch.setattr(_config(), "VLAN_BANDS", SHIFTED)
    assert _accept(app, monkeypatch, "--apply") == 1
    assert "1 deployment admission(s) are in progress" in capsys.readouterr().err
    assert _recorded(app) == DEFAULT


# Catches a portal still running the old layout placing labs after an accept.
def test_an_old_layout_process_refuses_to_deploy_after_an_accept(app, monkeypatch):
    from app.backends.base import AdmissionRefused
    from app.backends.cloud_admission import reserve_cloud_capacity
    from app.models import NodeMemoryReservation
    from app.vlan_allocator import accept_vlan_layout_locked
    _start(app, monkeypatch)
    with app.app_context():
        accept_vlan_layout_locked(SHIFTED)  # another process accepted SHIFTED
        with pytest.raises(AdmissionRefused, match="differs from the recorded layout — restart required"):
            reserve_cloud_capacity("aws", "zone-a", 5, "r-old")
        assert NodeMemoryReservation.query.count() == 0
    monkeypatch.setattr(_config(), "VLAN_BANDS", SHIFTED)  # the restarted process
    with app.app_context():
        assert reserve_cloud_capacity("aws", "zone-a", 5, "r-new") == "zone-a"


# Catches the accept skipping the admission lock: while an admission holds
# it, the accept must wait and record nothing.
def test_the_accept_waits_for_the_admission_lock(app, monkeypatch):
    import threading
    from app import coordination
    from app.vlan_allocator import accept_vlan_layout_locked
    _start(app, monkeypatch)

    def accept():
        with app.app_context():
            accept_vlan_layout_locked(SHIFTED)

    lock = coordination._process_locks[coordination.NODE_ADMISSION]
    with lock:
        worker = threading.Thread(target=accept)
        worker.start()
        worker.join(timeout=1)
        assert worker.is_alive()
        assert _recorded(app) == DEFAULT
    worker.join(timeout=10)
    assert not worker.is_alive() and _recorded(app) == SHIFTED


# ---- group VLAN changes are serialized with admission and the accept -------

def _admin_client(app):
    with app.app_context():
        admin = User(login_id="admin", is_admin=True)
        admin.set_password("placeholder")
        db.session.add(admin)
        db.session.commit()
    client = app.test_client()
    assert client.post("/auth/login", data={"login_id": "admin", "password": "placeholder"}
                       ).status_code in (200, 302)
    return client


def _groups(app):
    with app.app_context():
        return sorted((g.name, g.display_name, g.entry_vlan_tag, g.internal_vlan_tag)
                      for g in StudentGroup.query.all())


def _yaml_apply(client, blob):
    import hashlib
    import io
    return client.post("/admin/groups/yaml/apply",
                       data={"file": (io.BytesIO(blob.encode()), "groups.yaml"),
                             "plan_token": hashlib.sha256(blob.encode()).hexdigest()},
                       content_type="multipart/form-data")


GROUP_YAML = "groups:\n  - name: blue\n    display_name: Blue\n"


# Catches a portal left on the old layout committing group pairs from its old
# group bands after an accept.
def test_group_changes_refuse_under_a_stale_layout_and_write_nothing(app):
    client = _admin_client(app)
    with app.app_context():
        db.session.add(StudentGroup(name="red", display_name="Red",
                                    entry_vlan_tag=300, internal_vlan_tag=400))
        db.session.commit()
        red_id = StudentGroup.query.filter_by(name="red").one().id
    _seed(app, SHIFTED)  # another process accepted SHIFTED; this one runs DEFAULT
    before = _groups(app)
    for response in (
            client.post("/admin/groups", json={"name": "green", "display_name": "Green"}),
            client.put(f"/admin/groups/{red_id}", json={"display_name": "Renamed",
                                                       "entry_vlan_tag": 301,
                                                       "internal_vlan_tag": 401}),
            _yaml_apply(client, GROUP_YAML)):
        assert response.status_code == 409, response.data
        assert "restart required" in response.get_json()["error"]
    assert _groups(app) == before


def test_group_changes_under_the_current_layout_are_unchanged(app):
    client = _admin_client(app)
    _seed(app, DEFAULT)
    created = client.post("/admin/groups", json={"name": "green", "display_name": "Green"})
    assert created.status_code == 201, created.data
    green_id = created.get_json()["group"]["id"]
    assert client.put(f"/admin/groups/{green_id}", json={
        "entry_vlan_tag": 310, "internal_vlan_tag": 410}).status_code == 200
    assert _yaml_apply(client, GROUP_YAML).status_code == 200
    assert _groups(app) == [("blue", "Blue", 300, 400), ("green", "Green", 310, 410)]


# Catches a group change skipping the admission lock.
def test_group_creation_waits_for_the_admission_lock(app):
    import threading
    from app import coordination
    client = _admin_client(app)
    result = {}

    def create():
        result["status"] = client.post("/admin/groups", json={
            "name": "green", "display_name": "Green"}).status_code

    with coordination._process_locks[coordination.NODE_ADMISSION]:
        worker = threading.Thread(target=create)
        worker.start()
        worker.join(timeout=1)
        assert worker.is_alive() and _groups(app) == []
    worker.join(timeout=10)
    assert result["status"] == 201 and [g[0] for g in _groups(app)] == ["green"]
