"""Coordination state that every gunicorn worker shares.

Node-memory admission, VMID allocation, VM lifecycle locks and the
password-reset limiter used to keep their state in process memory, which
was correct only with exactly one worker. Their state now lives in the
application database. Each check-and-reserve runs in one transaction that
first locks a named `coordination_state` row: SELECT ... FOR UPDATE on
MySQL, or a write on SQLite, whose RESERVED lock excludes every other
writer of the file. A process lock in front of each row makes greenlets of
one worker queue in memory instead of each holding a pooled connection
while it waits for the row.
"""

import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Iterable, Optional, Tuple, TypeVar

from sqlalchemy import create_engine, delete, inspect, insert, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool

from .models import (
    CoordinationState, NodeMemoryReservation, VMIDReservation, db)

logger = logging.getLogger(__name__)

NODE_ADMISSION = 'node_admission'
VMID_ALLOCATOR = 'vmid_allocator'
PASSWORD_RESET_LIMITER = 'password_reset_limiter'
LOCK_ROWS = (NODE_ADMISSION, VMID_ALLOCATOR, PASSWORD_RESET_LIMITER)

_STATE = CoordinationState.__table__
_LEASE_TABLES = (NodeMemoryReservation.__table__, VMIDReservation.__table__)

_process_locks = {name: threading.Lock() for name in LOCK_ROWS}

T = TypeVar('T')


def lease_clock() -> float:
    """Wall-clock seconds for leases and failure stamps.

    Not time.monotonic(): another worker compares the value, and a lease
    written before a host reboot must not outlive it by the old uptime.
    """
    return time.time()


# ---- which database --------------------------------------------------------

_process_engine: Optional[Engine] = None
_process_engine_guard = threading.Lock()


def _process_local_engine() -> Engine:
    """A private in-memory store for callers with no application database.

    Scripts and direct unit tests call the allocator outside any Flask app
    context. They still get atomic, TTL-bounded reservations, shared by the
    threads of this process only.
    """
    global _process_engine
    with _process_engine_guard:
        if _process_engine is None:
            engine = create_engine(
                'sqlite://',
                poolclass=StaticPool,
                connect_args={'check_same_thread': False},
                # One connection serves every thread. The default rollback
                # on checkin would let one thread's read end another
                # thread's open reservation transaction.
                pool_reset_on_return=None,
            )
            db.metadata.create_all(
                engine, tables=[_STATE, *_LEASE_TABLES])
            _process_engine = engine
        return _process_engine


def current_engine() -> Engine:
    """The application database, or the process-local store without one."""
    from flask import has_app_context
    if has_app_context():
        return db.engine
    return _process_local_engine()


def is_application_database(engine: Engine) -> bool:
    """Whether `engine` holds the application tables (vm_records etc.)."""
    return engine is not _process_engine


# Renewals and releases run in create-path helper threads that have no app
# context. This map routes them to the database their reservation was
# written to; the rows themselves are the shared state. Every renewal and
# release comes from the worker that made the reservation.
_lease_engines: Dict[Tuple[str, str], Engine] = {}
_lease_engines_guard = threading.Lock()


def remember_lease_engine(table, reservation_id: str, engine: Engine) -> None:
    with _lease_engines_guard:
        _lease_engines[(table.name, reservation_id)] = engine


def _lease_engine(table, reservation_id: str) -> Engine:
    with _lease_engines_guard:
        engine = _lease_engines.get((table.name, reservation_id))
    return engine if engine is not None else current_engine()


def _forget_lease_engine(table, reservation_id: str) -> None:
    with _lease_engines_guard:
        _lease_engines.pop((table.name, reservation_id), None)


# ---- serialized transactions -----------------------------------------------

def _lock_row(conn: Connection, name: str) -> bool:
    """Lock coordination row `name` for this transaction; False if missing."""
    if conn.dialect.name == 'sqlite':
        # SQLite has no row locks. A write takes the file's RESERVED lock,
        # which excludes every other writer until this transaction ends, and
        # it is the first statement, so no earlier read can make the upgrade
        # fail with SQLITE_BUSY instead of waiting.
        result = conn.execute(
            update(_STATE)
            .where(_STATE.c.name == name)
            .values(value=_STATE.c.value))
        return result.rowcount == 1
    # Locking reads do not open InnoDB's REPEATABLE READ snapshot, so every
    # plain read after this sees what earlier lock holders committed.
    row = conn.execute(
        select(_STATE.c.name)
        .where(_STATE.c.name == name)
        .with_for_update()).first()
    return row is not None


def _create_row(engine: Engine, name: str) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(insert(_STATE).values(name=name, value=0))
    except IntegrityError:
        pass  # Another worker created it first.


def run_locked(engine: Engine, name: str,
               operation: Callable[[Connection], T]) -> T:
    """Run `operation(conn)` in one transaction holding row `name`.

    The migration seeds the rows. A database built by db.create_all() gets
    each row on first use, created in its own transaction so two workers
    racing to create it never deadlock on InnoDB gap locks.
    """
    with _process_locks[name]:
        for _attempt in range(2):
            with engine.begin() as conn:
                if _lock_row(conn, name):
                    return operation(conn)
            _create_row(engine, name)
    raise RuntimeError(f'Coordination row {name!r} could not be created')


def read_state(conn: Connection, name: str) -> int:
    return int(conn.execute(
        select(_STATE.c.value).where(_STATE.c.name == name)).scalar_one())


def write_state(conn: Connection, name: str, value: int) -> None:
    conn.execute(
        update(_STATE).where(_STATE.c.name == name).values(value=value))


# ---- leases ----------------------------------------------------------------
#
# Renewals and releases take the same row as the allocation they modify, so
# every write to a lease table is serialized and InnoDB never has to order
# a release's row lock against an allocation's range scan.

def _lock_name(table) -> str:
    if table is NodeMemoryReservation.__table__:
        return NODE_ADMISSION
    return VMID_ALLOCATOR


def renew_lease(table, reservation_id: str, ttl_seconds: float) -> bool:
    """Extend an unexpired lease; False if it expired, vanished or the
    database could not be reached (callers treat all three as lost)."""
    engine = _lease_engine(table, reservation_id)

    def renew(conn):
        now = lease_clock()
        return conn.execute(
            update(table)
            .where(table.c.reservation_id == reservation_id,
                   table.c.expires_at > now)
            .values(expires_at=now + ttl_seconds)).rowcount

    try:
        renewed = run_locked(engine, _lock_name(table), renew)
    except SQLAlchemyError:
        logger.warning("Could not renew %s reservation %s",
                       table.name, reservation_id, exc_info=True)
        return False
    if not renewed:
        _forget_lease_engine(table, reservation_id)
    return renewed > 0


def release_lease(table, reservation_id: str) -> bool:
    """Delete a lease. A failed delete leaves it to expire with its TTL."""
    engine = _lease_engine(table, reservation_id)

    def release(conn):
        return conn.execute(
            delete(table)
            .where(table.c.reservation_id == reservation_id)).rowcount

    try:
        released = run_locked(engine, _lock_name(table), release)
    except SQLAlchemyError:
        logger.warning("Could not release %s reservation %s; it expires "
                       "with its TTL", table.name, reservation_id,
                       exc_info=True)
        return False
    _forget_lease_engine(table, reservation_id)
    return released > 0


def reset_for_tests(tables: Iterable, state_rows: Iterable[str] = ()) -> None:
    """Empty `tables` and zero `state_rows` in every reachable store."""
    tables = list(tables)
    state_rows = list(state_rows)
    engines = [_process_local_engine()]
    from flask import has_app_context
    if has_app_context():
        engines.append(db.engine)
    for engine in engines:
        # Inspect before the transaction: on a single-connection pool, the
        # inspector's own checkout would roll the transaction back.
        existing = set(inspect(engine).get_table_names())
        with engine.begin() as conn:
            for table in tables:
                if table.name in existing:
                    conn.execute(delete(table))
            if state_rows and _STATE.name in existing:
                conn.execute(
                    update(_STATE)
                    .where(_STATE.c.name.in_(state_rows))
                    .values(value=0))
    with _lease_engines_guard:
        names = {table.name for table in tables}
        for key in [key for key in _lease_engines if key[0] in names]:
            del _lease_engines[key]


# ---- MySQL named locks -----------------------------------------------------

# GET_LOCK rejects longer names with an error.
MYSQL_LOCK_NAME_MAX_LENGTH = 64


@contextmanager
def mysql_named_lock(engine: Engine, name: str, timeout_seconds: int):
    """Hold MySQL's GET_LOCK(name) for the duration of the block.

    GET_LOCK belongs to the server session, so the lock gets a dedicated
    connection that does nothing else and is released before that
    connection returns to the pool. If the database cannot be reached the
    block still runs, under the caller's process lock only: cleanup after a
    failed VMRecord commit must be able to remove the guest during an
    outage, as it could before this lock existed.
    """
    if len(name) > MYSQL_LOCK_NAME_MAX_LENGTH:
        # MySQL's rejection would look like an outage and fall back to the
        # process lock, losing cross-worker exclusion without a trace.
        raise ValueError(
            f'lock name {name!r} exceeds MySQL\'s '
            f'{MYSQL_LOCK_NAME_MAX_LENGTH}-character limit')
    connection = None
    acquired = None
    try:
        connection = engine.connect()
        acquired = connection.execute(
            text('SELECT GET_LOCK(:name, :timeout)'),
            {'name': name, 'timeout': timeout_seconds}).scalar()
    except SQLAlchemyError:
        logger.warning("Database lock %s unavailable; continuing under the "
                       "process lock only", name, exc_info=True)
        if connection is not None:
            connection.invalidate()
            connection.close()
        connection = None

    if connection is None:
        yield
        return
    if acquired != 1:
        connection.close()
        raise TimeoutError(
            f'Timed out after {timeout_seconds}s waiting for lock {name}')
    try:
        yield
    finally:
        released = False
        try:
            released = connection.execute(
                text('SELECT RELEASE_LOCK(:name)'),
                {'name': name}).scalar() == 1
        except SQLAlchemyError:
            logger.warning("RELEASE_LOCK(%s) failed", name, exc_info=True)
        if not released:
            # Ending the session is the only other way to drop the lock.
            # Back in the pool, this connection would hold it forever.
            connection.invalidate()
        connection.close()
