"""Capped admission for the cloud backends: one machine per reservation.

A cloud has no node memory to weigh, only a per-backend machine cap. The
reservation rides the same node_memory_reservations ledger and
NODE_ADMISSION lock as Proxmox admission, keyed by the backend name with
memory_bytes=1, so renewal, release and TTL expiry are the existing ones and
every worker counts against one ledger.
"""

from sqlalchemy import delete, func, insert, select

from app import coordination
from app.backends.base import AdmissionRefused
from app.models import NodeMemoryReservation, VMRecord

_RESERVATIONS = NodeMemoryReservation.__table__


def _live_record_count(conn, backend: str) -> int:
    # The process-local store (no app context) has no VMRecords.
    if not coordination.is_application_database(conn.engine):
        return 0
    records = VMRecord.__table__
    return int(conn.execute(
        select(func.count())
        .select_from(records)
        .where(records.c.backend == backend,
               records.c.destroyed == False)  # noqa: E712
    ).scalar_one())


def reserve_cloud_capacity(backend: str, location: str, max_vms: int,
                           reservation_id: str) -> str:
    """Reserve one of `backend`'s `max_vms` machines; return `location`.

    Live VMRecords and unexpired reservations both count, read inside the
    admission transaction, so concurrent creates in any worker can never
    exceed the cap. Raises AdmissionRefused at the cap.
    """
    # Lazy: app.proxmox_service imports app.config at module load.
    from app.proxmox_service import RESERVATION_TTL_SECONDS

    # A missing cap must never read as "unlimited".
    if not isinstance(max_vms, int) or max_vms < 1:
        raise AdmissionRefused(f'{backend} has no positive machine cap configured')
    engine = coordination.current_engine()

    def admit(conn):
        now = coordination.lease_clock()
        conn.execute(delete(_RESERVATIONS).where(_RESERVATIONS.c.expires_at <= now))
        if conn.execute(
                select(_RESERVATIONS.c.reservation_id)
                .where(_RESERVATIONS.c.reservation_id == reservation_id)
        ).first():
            raise ValueError(
                f'Duplicate node reservation identifier: {reservation_id}')
        reserved = int(conn.execute(
            select(func.count())
            .select_from(_RESERVATIONS)
            .where(_RESERVATIONS.c.node == backend)).scalar_one())
        in_use = _live_record_count(conn, backend) + reserved
        if in_use >= max_vms:
            raise AdmissionRefused(
                f'{backend} is at its cap of {max_vms} machines ({in_use} in use)')
        conn.execute(insert(_RESERVATIONS).values(
            reservation_id=reservation_id,
            node=backend,
            memory_bytes=1,
            expires_at=now + RESERVATION_TTL_SECONDS,
        ))

    coordination.run_locked(engine, coordination.NODE_ADMISSION, admit)
    coordination.remember_lease_engine(_RESERVATIONS, reservation_id, engine)
    return location
