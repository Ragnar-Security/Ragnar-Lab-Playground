"""Coordination state that two gunicorn workers must share.

Each worker is a separate process with its own module globals, so the
tests that matter run the allocator in separate spawned processes against
one SQLite file, the way two workers share one MySQL database.
"""

import importlib.util
import multiprocessing
import queue
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from flask import Flask
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import OperationalError

from app import proxmox_service
from app.auth import PasswordResetLimiter, auth
from app.models import StudentWhitelist, User, db

GIB = 1024 ** 3
LB_ON = {'enabled': True, 'cpu_threshold': 80.0, 'memory_threshold': 85.0}
REPO = Path(__file__).resolve().parent.parent
SPAWN = multiprocessing.get_context('spawn')
WORKER_TIMEOUT = 120


def _worker_app(db_path):
    application = Flask(__name__)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f'sqlite:///{db_path}',
        SECRET_KEY='coordination-test',
        SELF_SERVICE_PASSWORD_RESET=True,
    )
    db.init_app(application)
    application.register_blueprint(auth, url_prefix='/auth')
    return application


def _proxmox(*, available_gib, guest_vmids=()):
    proxmox = MagicMock()
    proxmox.nodes.get.return_value = [{'node': 'pve'}]
    proxmox.nodes.return_value.status.get.return_value = {
        'cpu': 0.05,
        'uptime': 1000,
        'cpuinfo': {'cpus': 32},
        'memory': {
            'total': 16 * GIB,
            'used': (16 - available_gib) * GIB,
            'free': 1 * GIB,
            'available': available_gib * GIB,
        },
    }
    proxmox.cluster.resources.get.return_value = [
        {'vmid': vmid, 'node': 'pve', 'mem': 0} for vmid in guest_vmids]
    return proxmox


# ---- worker processes (module level so spawn can import them) ----

def _admit_worker(db_path, worker, attempts, barrier, results):
    application = _worker_app(db_path)
    proxmox = _proxmox(available_gib=10)
    barrier.wait()
    for attempt in range(attempts):
        with application.app_context():
            node = proxmox_service.select_best_node(
                proxmox,
                required_memory_mb=3072,
                required_cores=1,
                load_balancing_config=LB_ON,
                available_nodes=['pve'],
                reservation_id=f'worker-{worker}-{attempt}',
            )
        results.put((worker, node))


def _allocate_worker(db_path, worker, blocks, barrier, release, results):
    application = _worker_app(db_path)
    proxmox = _proxmox(available_gib=10, guest_vmids=[9706])
    if barrier is not None:
        barrier.wait()
    for index, count in enumerate(blocks):
        reservation_id = f'worker-{worker}-{index}'
        with application.app_context():
            base = proxmox_service.get_next_available_vmid(
                proxmox, count=count, reservation_id=reservation_id)
        if release:
            proxmox_service.release_vmid_reservation(reservation_id)
        results.put(list(range(base, base + count)))


def _run_workers(target, argument_sets):
    results = SPAWN.Queue()
    processes = [
        SPAWN.Process(target=target, args=(*arguments, results))
        for arguments in argument_sets
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(WORKER_TIMEOUT)
        assert process.exitcode == 0, f'worker failed with {process.exitcode}'
    collected = []
    while True:
        try:
            collected.append(results.get(timeout=1))
        except queue.Empty:
            return collected


@pytest.fixture
def shared_db(tmp_path):
    db_path = tmp_path / 'shared.sqlite'
    application = _worker_app(db_path)
    with application.app_context():
        db.create_all()
    proxmox_service._clear_node_reservations()
    proxmox_service._clear_vmid_reservations()
    yield db_path
    proxmox_service._clear_node_reservations()
    proxmox_service._clear_vmid_reservations()


# ---- admission and VMID allocation across workers ----

def test_two_workers_never_admit_more_memory_than_the_node_has(shared_db):
    """10 GiB available fits three 3 GiB guests, whichever worker asks."""
    barrier = SPAWN.Barrier(2)
    outcomes = _run_workers(
        _admit_worker, [(str(shared_db), worker, 4, barrier) for worker in (1, 2)])

    admitted = [node for _worker, node in outcomes if node]
    assert len(outcomes) == 8
    assert len(admitted) == 3


def test_two_workers_never_hand_out_the_same_vmid(shared_db):
    barrier = SPAWN.Barrier(2)
    blocks = [1, 3, 2, 1, 2, 3, 1, 1]
    outcomes = _run_workers(
        _allocate_worker,
        [(str(shared_db), worker, blocks, barrier, False) for worker in (1, 2)])

    vmids = [vmid for block in outcomes for vmid in block]
    assert len(vmids) == 2 * sum(blocks)
    assert len(set(vmids)) == len(vmids), sorted(vmids)
    assert min(vmids) == 9707


def test_released_vmids_are_not_reused_by_another_worker(shared_db):
    """A clone whose block was released without a VMRecord (cleanup path)
    may still be stopping in Proxmox; another worker must not reuse it."""
    [first] = _run_workers(
        _allocate_worker, [(str(shared_db), 1, [3], None, True)])
    [second] = _run_workers(
        _allocate_worker, [(str(shared_db), 2, [1], None, True)])

    assert first == [9707, 9708, 9709]
    assert second == [9710]


def test_vmid_reserved_by_one_worker_is_protected_from_another_reaper(shared_db):
    """The orphan reaper may run in a different worker than the create."""
    [block] = _run_workers(
        _allocate_worker, [(str(shared_db), 1, [2], None, False)])

    application = _worker_app(shared_db)
    with application.app_context():
        assert all(proxmox_service.is_vmid_reserved(vmid) for vmid in block)
        assert not proxmox_service.is_vmid_reserved(block[-1] + 1)


def test_node_reservation_release_frees_capacity_for_another_worker(shared_db):
    proxmox = _proxmox(available_gib=4)
    first_worker = _worker_app(shared_db)
    with first_worker.app_context():
        assert proxmox_service.select_best_node(
            proxmox, required_memory_mb=3072, required_cores=1,
            load_balancing_config=LB_ON, available_nodes=['pve'],
            reservation_id='held') == 'pve'

    # Released from a worker thread with no app context, as the create path
    # does once its VMRecord commits.
    assert proxmox_service.release_node_reservation('held')

    second_worker = _worker_app(shared_db)
    with second_worker.app_context():
        assert proxmox_service.select_best_node(
            proxmox, required_memory_mb=3072, required_cores=1,
            load_balancing_config=LB_ON, available_nodes=['pve'],
            reservation_id='next') == 'pve'


# ---- password reset limiter across workers ----

def _seed_student(application, login_id='victim', sis='100000001'):
    with application.app_context():
        db.session.add(StudentWhitelist(
            login_id=login_id, sis_user_id=sis, full_name='Example Student'))
        user = User(login_id=login_id, full_name='Example Student')
        user.set_password('original-pass-1')
        db.session.add(user)
        db.session.commit()


def _reset(client, login_id, sis):
    return client.post('/auth/reset-password', json={
        'login_id': login_id, 'sis_user_id': sis,
        'password': 'replacement-pass-2',
    })


def test_reset_failures_on_one_worker_block_the_login_on_another(shared_db):
    first_worker = _worker_app(shared_db)
    second_worker = _worker_app(shared_db)
    _seed_student(first_worker)

    guesser = first_worker.test_client()
    for _ in range(PasswordResetLimiter.PER_LOGIN):
        assert _reset(guesser, 'victim', '999999999').status_code == 200

    response = _reset(second_worker.test_client(), 'victim', '100000001')
    assert response.status_code == 429
    with second_worker.app_context():
        user = User.query.filter_by(login_id='victim').one()
        assert user.check_password('original-pass-1')


def test_link_guesses_on_one_worker_block_the_address_on_another(shared_db):
    first_worker = _worker_app(shared_db)
    second_worker = _worker_app(shared_db)

    guesser = first_worker.test_client()
    for _ in range(PasswordResetLimiter.PER_ADDRESS):
        guesser.post('/auth/reset-password/link', json={
            'token': 'not-a-real-token', 'password': 'replacement-pass-2'})

    response = second_worker.test_client().post(
        '/auth/reset-password/link',
        json={'token': 'not-a-real-token', 'password': 'replacement-pass-2'})
    assert response.status_code == 429


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_shared_limiter_table_stays_bounded_and_newest_key_is_limited(shared_db):
    from app.auth import _SharedSlidingWindow
    application = _worker_app(shared_db)
    clock = _Clock()
    with application.app_context():
        window = _SharedSlidingWindow('login', limit=2, window=900, clock=clock,
                                      max_keys=3)
        for i in range(10):
            window.record(f'spray-{i}')
            assert len(window) <= 3
        window.record('newest')
        window.record('newest')

        assert len(window) <= 3
        assert window.blocked('newest')


def test_shared_limiter_drops_expired_keys_before_evicting_live_ones(shared_db):
    from app.auth import _SharedSlidingWindow
    application = _worker_app(shared_db)
    clock = _Clock()
    with application.app_context():
        window = _SharedSlidingWindow('login', limit=2, window=900, clock=clock,
                                      max_keys=2)
        window.record('stale')
        clock.now += 901

        window.record('live')
        window.record('live')
        window.record('other')

        assert window.blocked('live')
        assert not window.blocked('stale')


def test_shared_limiter_scopes_do_not_mix(shared_db):
    """A login ID and an address with the same text are different keys."""
    from app.auth import _SharedSlidingWindow
    application = _worker_app(shared_db)
    clock = _Clock()
    with application.app_context():
        by_login = _SharedSlidingWindow('login', 2, 900, clock)
        by_address = _SharedSlidingWindow('address', 2, 900, clock)
        by_login.record('same-text')
        by_login.record('same-text')

        assert by_login.blocked('same-text')
        assert not by_address.blocked('same-text')


# ---- vm_lifecycle_lock on MySQL ----

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeMySQLConnection:
    """Records the named-lock statements one pooled connection receives."""

    def __init__(self, get_lock_result=1, release_error=None):
        self.statements = []
        self.get_lock_result = get_lock_result
        self.release_error = release_error
        self.closed = False
        self.invalidated = False

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append((sql, dict(parameters or {})))
        if 'GET_LOCK' in sql:
            return _FakeResult(self.get_lock_result)
        if 'RELEASE_LOCK' in sql:
            if self.release_error is not None:
                raise self.release_error
            return _FakeResult(1)
        raise AssertionError(f'unexpected statement {sql}')

    def invalidate(self):
        self.invalidated = True

    def close(self):
        self.closed = True


class _FakeMySQLEngine:
    def __init__(self, connection=None, connect_error=None):
        self.dialect = MagicMock()
        self.dialect.name = 'mysql'
        self.connection = connection
        self.connect_error = connect_error

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection


@contextmanager
def _mysql_engine(engine):
    application = Flask(__name__)
    application.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    db.init_app(application)
    with application.app_context(), patch.object(
            type(db), 'engine', new_callable=PropertyMock, return_value=engine):
        yield


def _named_lock_calls(connection, function):
    return [params for sql, params in connection.statements if function in sql]


def test_mysql_lifecycle_lock_holds_a_server_lock_named_for_the_vmid():
    connection = _FakeMySQLConnection()
    seen_inside = []
    with _mysql_engine(_FakeMySQLEngine(connection)):
        with proxmox_service.vm_lifecycle_lock(700):
            seen_inside = list(connection.statements)

    [acquire] = _named_lock_calls(connection, 'GET_LOCK')
    assert acquire['name'] == 'vm-lifecycle-700'
    assert acquire['timeout'] > 0
    assert len(seen_inside) == 1, 'body must run while the lock is held'
    [release] = _named_lock_calls(connection, 'RELEASE_LOCK')
    assert release['name'] == 'vm-lifecycle-700'
    assert connection.closed


def test_mysql_lifecycle_lock_is_released_when_the_body_raises():
    connection = _FakeMySQLConnection()
    with _mysql_engine(_FakeMySQLEngine(connection)):
        with pytest.raises(RuntimeError, match='destroy failed'):
            with proxmox_service.vm_lifecycle_lock(701):
                raise RuntimeError('destroy failed')

    assert _named_lock_calls(connection, 'RELEASE_LOCK')
    assert connection.closed


def test_mysql_lifecycle_lock_timeout_never_runs_the_body():
    connection = _FakeMySQLConnection(get_lock_result=0)
    ran = []
    with _mysql_engine(_FakeMySQLEngine(connection)):
        with pytest.raises(TimeoutError):
            with proxmox_service.vm_lifecycle_lock(702):
                ran.append(True)

    assert ran == []
    assert not _named_lock_calls(connection, 'RELEASE_LOCK')
    assert connection.closed


def test_failed_release_discards_the_connection_instead_of_pooling_the_lock():
    """GET_LOCK belongs to the session; a pooled connection that still held
    it would block that VMID for every later lifecycle operation."""
    connection = _FakeMySQLConnection(
        release_error=OperationalError('RELEASE_LOCK', {}, Exception('gone')))
    with _mysql_engine(_FakeMySQLEngine(connection)):
        with proxmox_service.vm_lifecycle_lock(703):
            pass

    assert connection.invalidated


def test_unreachable_database_still_lets_cleanup_run_under_process_lock():
    """Cleanup after a failed VMRecord commit must still remove the guest
    during a database outage (see cleanup_vms)."""
    engine = _FakeMySQLEngine(
        connect_error=OperationalError('connect', {}, Exception('down')))
    ran = []
    with _mysql_engine(engine):
        with proxmox_service.vm_lifecycle_lock(704):
            ran.append(True)

    assert ran == [True]


# ---- the migration ----

def _load_migration():
    path = REPO / 'scripts' / 'migrate_add_coordination_tables.py'
    assert path.exists(), 'coordination migration script is missing'
    spec = importlib.util.spec_from_file_location(
        'migrate_add_coordination_tables', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COORDINATION_TABLES = (
    'coordination_state',
    'node_memory_reservations',
    'vmid_reservations',
    'password_reset_failures',
)


def test_migration_creates_tables_matching_the_models_and_is_idempotent(tmp_path):
    migration = _load_migration()
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"

    migration.run_migration(database_url)
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(db.metadata.tables['vmid_reservations'].insert().values(
            vmid=9707, reservation_id='live-create', expires_at=1.0))
    migration.run_migration(database_url)

    inspector = inspect(engine)
    for name in COORDINATION_TABLES:
        model = db.metadata.tables[name]
        migrated = {c['name']: c for c in inspector.get_columns(name)}
        assert set(migrated) == {c.name for c in model.columns}, name
        for column in model.columns:
            assert migrated[column.name]['nullable'] == column.nullable, (
                name, column.name)
        model_indexes = {tuple(i.columns.keys()) for i in model.indexes}
        migrated_indexes = {
            tuple(i['column_names']) for i in inspector.get_indexes(name)}
        assert model_indexes <= migrated_indexes, name
    with engine.connect() as conn:
        state = db.metadata.tables['coordination_state']
        names = set(conn.execute(select(state.c.name)).scalars())
        assert names == {
            'node_admission', 'vmid_allocator', 'password_reset_limiter'}
        reservations = db.metadata.tables['vmid_reservations']
        assert conn.execute(
            select(reservations.c.reservation_id)).scalars().all() == [
                'live-create']
