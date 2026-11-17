"""Per-student VLAN numbering in the seed, the importer and the migration."""

import pytest

from app.models import db, KothGame, StudentGroup, StudentWhitelist, User
from app.vlan_allocator import VlanPoolExhausted
from scripts import import_students, migrate_add_vlan
from scripts.init_db import import_student_whitelist


def _roster(path, count, start=0):
    rows = ''.join(f'{9000 + n},s{n:03d},Student {n}\n' for n in range(start, start + count))
    path.write_text('SIS User ID,SIS Login ID,Student\n' + rows)
    return str(path)


def test_whitelist_seed_numbers_students_in_file_order(app, tmp_path):
    with app.app_context():
        import_student_whitelist(_roster(tmp_path / 'r.csv', 3))
        rows = StudentWhitelist.query.order_by(StudentWhitelist.entry_vlan_tag).all()
        assert [(r.login_id, r.entry_vlan_tag, r.internal_vlan_tag) for r in rows] == [
            ('s000', 100, 200), ('s001', 101, 201), ('s002', 102, 202)]


def test_whitelist_seed_refuses_a_roster_larger_than_the_pool(app, tmp_path):
    with app.app_context():
        with pytest.raises(ValueError, match='101'):
            import_student_whitelist(_roster(tmp_path / 'r.csv', 101))
        db.session.rollback()
        assert StudentWhitelist.query.count() == 0


def _seed_whitelist(app, count):
    with app.app_context():
        for i in range(count):
            db.session.add(StudentWhitelist(login_id=f'old{i}', entry_vlan_tag=100 + i,
                                            internal_vlan_tag=200 + i))
        db.session.commit()


def test_import_students_persists_the_last_pair(app, tmp_path, monkeypatch):
    monkeypatch.setattr(import_students, 'create_app', lambda: app)
    _seed_whitelist(app, 99)  # 100/200 .. 198/298
    import_students.import_students_from_csv(_roster(tmp_path / 'one.csv', 1, start=500))
    with app.app_context():
        last = StudentWhitelist.query.filter_by(login_id='s500').one()
        assert (last.entry_vlan_tag, last.internal_vlan_tag) == (199, 299)
        assert StudentWhitelist.query.count() == 100


def test_import_students_refuses_an_overflow_atomically(app, tmp_path, monkeypatch):
    monkeypatch.setattr(import_students, 'create_app', lambda: app)
    _seed_whitelist(app, 99)
    with pytest.raises(SystemExit):
        import_students.import_students_from_csv(_roster(tmp_path / 'two.csv', 2, start=600))
    with app.app_context():
        assert StudentWhitelist.query.count() == 99
        assert StudentWhitelist.query.filter_by(login_id='s600').first() is None


def _row(model, login, entry, internal):
    row = model(login_id=login, entry_vlan_tag=entry, internal_vlan_tag=internal)
    if model is User:
        row.set_password('x')
    return row


ASSIGNERS = [(StudentWhitelist, 'assign_vlans_to_whitelist'), (User, 'assign_vlans_to_users')]


@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_numbers_the_last_free_pair(app, model, assign):
    with app.app_context():
        rows = [_row(model, f'n{i}', 100 + i, 200 + i) for i in range(99)]
        late = _row(model, 'late', None, None)
        db.session.add_all(rows + [late])
        db.session.commit()
        getattr(migrate_add_vlan, assign)([late])
        db.session.expire_all()
        late = model.query.filter_by(login_id='late').one()
        assert (late.entry_vlan_tag, late.internal_vlan_tag) == (199, 299)


# Catches the migration committing a partial numbering and leaving the rest unnumbered.
@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_refuses_more_records_than_free_pairs_atomically(app, model, assign):
    with app.app_context():
        rows = [_row(model, f'n{i}', None, None) for i in range(101)]
        db.session.add_all(rows)
        db.session.commit()
        with pytest.raises(VlanPoolExhausted):
            getattr(migrate_add_vlan, assign)(rows)
        db.session.rollback()
        assert model.query.filter(model.entry_vlan_tag.isnot(None)).count() == 0


# Catches one login ending up with two different pairs across the two tables.
@pytest.mark.parametrize('model, other, assign', [
    (User, StudentWhitelist, 'assign_vlans_to_users'),
    (StudentWhitelist, User, 'assign_vlans_to_whitelist')])
def test_migrate_add_vlan_reuses_the_matching_logins_pair(app, model, other, assign):
    with app.app_context():
        db.session.add(_row(other, 'alice', 105, 205))
        target = _row(model, 'alice', None, None)
        db.session.add(target)
        db.session.commit()
        getattr(migrate_add_vlan, assign)([target])
        db.session.expire_all()
        row = model.query.filter_by(login_id='alice').one()
        assert (row.entry_vlan_tag, row.internal_vlan_tag) == (105, 205)


# Catches a pair that is free in its own table but held by another student,
# a group or a live KOTH game.
@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_skips_tags_held_in_other_tables(app, model, assign):
    other = User if model is StudentWhitelist else StudentWhitelist
    with app.app_context():
        db.session.add_all([
            _row(other, 'carol', 100, 200),
            StudentGroup(name='g', display_name='G', entry_vlan_tag=101, internal_vlan_tag=401),
            KothGame(name='k', deployment_group_name='arena', entry_vlan_tag=352,
                     internal_vlan_tag=202, status='draft'),
        ])
        target = _row(model, 'dave', None, None)
        db.session.add(target)
        db.session.commit()
        getattr(migrate_add_vlan, assign)([target])
        db.session.expire_all()
        row = model.query.filter_by(login_id='dave').one()
        assert (row.entry_vlan_tag, row.internal_vlan_tag) == (103, 203)


# Catches the migration taking a pair another table holds once the pool is full.
@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_refuses_when_no_globally_safe_pair_exists(app, model, assign):
    other = User if model is StudentWhitelist else StudentWhitelist
    with app.app_context():
        db.session.add_all([_row(other, f'w{i}', 100 + i, 200 + i) for i in range(99)])
        db.session.add(StudentGroup(name='g', display_name='G',
                                    entry_vlan_tag=199, internal_vlan_tag=401))
        target = _row(model, 'late', None, None)
        db.session.add(target)
        db.session.commit()
        with pytest.raises(VlanPoolExhausted):
            getattr(migrate_add_vlan, assign)([target])
        db.session.rollback()
        assert model.query.filter_by(login_id='late').one().entry_vlan_tag is None
