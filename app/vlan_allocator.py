"""
Centralized VLAN allocation for users and student groups.

Every pool comes from VLAN_BANDS (app.config.vlan_layout()), read at call
time:
    'user'  — the entry and internal bands. Student index i gets
             entry.first+i / internal.first+i, so the pool holds as many
             students as the entry band has tags.
    'group' — the first free tag of group-entry and of group-internal.

Both pools are checked for collisions across User, StudentWhitelist,
StudentGroup and unfinished KOTH games — a tag assigned to any of them is
considered used.
"""

import importlib
from contextlib import contextmanager
from datetime import datetime
import logging
from typing import List, Literal, Optional, Tuple

from sqlalchemy import func, insert, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError

from .models import db, User, StudentWhitelist, StudentGroup, KothGame, VlanLayoutRecord

logger = logging.getLogger(__name__)


class VlanPoolExhausted(Exception):
    """Raised when no free VLAN tag remains in the requested pool."""


class VlanInUse(Exception):
    """Raised when an explicit VLAN tag collides with an existing assignment."""


def vlan_layout():
    # Not `from .config import ...`: the package re-exports a config class
    # under that name, and tests patch the module's VLAN_BANDS.
    return importlib.import_module('app.config').vlan_layout()


def _pool_for(scope: Literal['user', 'group']) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    layout = vlan_layout()
    if scope == 'user':
        entry, internal = layout.entry, layout.internal
    elif scope == 'group':
        entry, internal = layout.group_entry, layout.group_internal
    else:
        raise ValueError(f"Unknown scope: {scope!r}")
    return (entry.first, entry.last), (internal.first, internal.last)


def _used_tags() -> set:
    """Return every VLAN tag (entry or internal) currently assigned anywhere."""
    used = set()
    for row in db.session.query(User.entry_vlan_tag, User.internal_vlan_tag).all():
        used.update(t for t in row if t is not None)
    for row in db.session.query(StudentWhitelist.entry_vlan_tag,
                                StudentWhitelist.internal_vlan_tag).all():
        used.update(t for t in row if t is not None)
    for row in db.session.query(StudentGroup.entry_vlan_tag,
                                StudentGroup.internal_vlan_tag).all():
        used.update(t for t in row if t is not None)
    for row in db.session.query(KothGame.entry_vlan_tag,
                                KothGame.internal_vlan_tag).filter(
                                    KothGame.status != 'ended').all():
        used.update(t for t in row if t is not None)
    return used


def _first_free(lo: int, hi: int, used: set) -> int:
    """Return the lowest integer in [lo, hi] not in `used`, or raise."""
    for tag in range(lo, hi + 1):
        if tag not in used:
            return tag
    raise VlanPoolExhausted(f"No free VLAN in range {lo}-{hi}")


def user_pair_for_index(index: int) -> Tuple[int, int]:
    """Return the (entry, internal) pair of the student at `index` in the pool."""
    if index < 0:
        raise ValueError(f"Student index must not be negative: {index}")
    layout = vlan_layout()
    if index >= layout.roster_max:
        raise VlanPoolExhausted(
            f"Student index {index} is past the per-student pool of {layout.roster_max}")
    return layout.entry.first + index, layout.internal.first + index


def validate_vlan_free(tag: int, exclude_group_id: Optional[int] = None) -> None:
    """
    Raise VlanInUse if `tag` is assigned to any user, whitelist row, or other
    group. `exclude_group_id` lets a group re-save its own VLANs without
    tripping the collision check.
    """
    if db.session.query(User.id).filter(
            (User.entry_vlan_tag == tag) | (User.internal_vlan_tag == tag)
    ).first():
        raise VlanInUse(f"VLAN {tag} is already assigned to a user")
    if db.session.query(StudentWhitelist.id).filter(
            (StudentWhitelist.entry_vlan_tag == tag) | (StudentWhitelist.internal_vlan_tag == tag)
    ).first():
        raise VlanInUse(f"VLAN {tag} is reserved on the student whitelist")
    q = db.session.query(StudentGroup.id).filter(
        (StudentGroup.entry_vlan_tag == tag) | (StudentGroup.internal_vlan_tag == tag)
    )
    if exclude_group_id is not None:
        q = q.filter(StudentGroup.id != exclude_group_id)
    if q.first():
        raise VlanInUse(f"VLAN {tag} is already assigned to another student group")
    if db.session.query(KothGame.id).filter(
            ((KothGame.entry_vlan_tag == tag) | (KothGame.internal_vlan_tag == tag))
            & (KothGame.status != 'ended')
    ).first():
        raise VlanInUse(f"VLAN {tag} is in use by a draft or active KOTH game")


def allocate_pair(
    scope: Literal['user', 'group'],
    explicit_entry: Optional[int] = None,
    explicit_internal: Optional[int] = None,
    exclude_group_id: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Return an (entry_vlan, internal_vlan) pair.

    - If BOTH explicit values are provided, validate uniqueness (VlanInUse)
      and range (integer 2-4094) and return them. Out-of-pool values are
      accepted silently — callers who want to warn should check the pool
      themselves.
    - If NEITHER is provided, auto-allocate the next free pair in the scope's
      pool.
    - Mixing one explicit + one auto is rejected with ValueError.
    """
    if (explicit_entry is None) != (explicit_internal is None):
        raise ValueError("explicit_entry and explicit_internal must both be set or both be None")

    if explicit_entry is not None:
        for tag in (explicit_entry, explicit_internal):
            if not isinstance(tag, int) or tag < 2 or tag > 4094:
                raise ValueError(f"VLAN {tag!r} out of valid range 2-4094")
            validate_vlan_free(tag, exclude_group_id=exclude_group_id)
        if explicit_entry == explicit_internal:
            raise ValueError("entry and internal VLAN tags must differ")
        return explicit_entry, explicit_internal

    (entry_lo, entry_hi), (internal_lo, internal_hi) = _pool_for(scope)
    used = _used_tags()
    entry = _first_free(entry_lo, entry_hi, used)
    used.add(entry)
    internal = _first_free(internal_lo, internal_hi, used)
    return entry, internal


_STUDENT_TABLES = ((User, 'user'), (StudentWhitelist, 'whitelisted student'))
_REFUSALS_SHOWN = 20


def _has_columns(model, *columns: str) -> bool:
    inspector = inspect(db.engine)
    table = model.__tablename__
    if not inspector.has_table(table):
        return False
    present = {column['name'] for column in inspector.get_columns(table)}
    return set(columns) <= present


def stored_vlan_problems() -> Tuple[List[str], List[str]]:
    """(refusals, warnings) for VLAN tags already in the database.

    A student's pair must lie in the entry and internal bands: the jumpbox
    and the Headscale policy serve exactly those, so a pair outside them
    leaves the student without a network. Groups and KOTH games may hold
    explicitly chosen VLANs outside every band, which the operator routes;
    those are only reported. Tables or columns a migration has not created
    yet are skipped."""
    layout = vlan_layout()
    refusals, warnings = [], []
    for model, kind in _STUDENT_TABLES:
        if not _has_columns(model, 'login_id', 'entry_vlan_tag', 'internal_vlan_tag'):
            continue
        rows = db.session.query(model.login_id, model.entry_vlan_tag,
                                model.internal_vlan_tag).order_by(model.login_id).all()
        for login, entry, internal in rows:
            for label, tag, band in (('entry', entry, layout.entry),
                                     ('internal', internal, layout.internal)):
                if tag is not None and tag not in band:
                    refusals.append(
                        f"{kind} {login}: {label} VLAN {tag} is outside the {band.name} "
                        f"band {band.first}-{band.last} of VLAN_BANDS")
    others = []
    if _has_columns(StudentGroup, 'name', 'entry_vlan_tag', 'internal_vlan_tag'):
        others += [(f"group {name}", entry, internal) for name, entry, internal in
                   db.session.query(StudentGroup.name, StudentGroup.entry_vlan_tag,
                                    StudentGroup.internal_vlan_tag).order_by(StudentGroup.name)]
    if _has_columns(KothGame, 'status', 'entry_vlan_tag', 'internal_vlan_tag'):
        others += [(f"KOTH game {game_id}", entry, internal) for game_id, entry, internal in
                   db.session.query(KothGame.id, KothGame.entry_vlan_tag,
                                    KothGame.internal_vlan_tag)
                   .filter(KothGame.status != 'ended').order_by(KothGame.id)]
    for owner, *tags in others:
        outside = [str(tag) for tag in tags if tag is not None and not layout.routes(tag)]
        if outside:
            warnings.append(f"{owner}: VLAN {', '.join(outside)} lies outside every VLAN_BANDS "
                            "band; the jumpbox does not route it")
    return refusals, warnings


def recorded_vlan_layout() -> Optional[str]:
    """The canonical VLAN_BANDS the database records, or None when none is
    recorded (or the table does not exist yet)."""
    if not inspect(db.engine).has_table(VlanLayoutRecord.__tablename__):
        return None
    row = db.session.get(VlanLayoutRecord, 1)
    return row.bands if row is not None else None


def record_vlan_layout(bands: str) -> None:
    """Record `bands` (canonical) as the layout of the stored pairs."""
    row = db.session.get(VlanLayoutRecord, 1)
    if row is None:
        db.session.add(VlanLayoutRecord(id=1, bands=bands))
    else:
        row.bands, row.recorded_at = bands, datetime.now()
    try:
        db.session.commit()
    except IntegrityError:
        # Another worker recorded it first; startup re-reads and compares.
        db.session.rollback()


def has_student_pairs() -> bool:
    for model, _kind in _STUDENT_TABLES:
        if _has_columns(model, 'entry_vlan_tag', 'internal_vlan_tag') and db.session.query(
                model.id).filter((model.entry_vlan_tag.isnot(None))
                                 | (model.internal_vlan_tag.isnot(None))).first():
            return True
    return False


SEED_COMMAND = ("python scripts/migrate_add_vlan_layout_record.py --bands "
                "'<the VLAN layout the stored pairs were numbered under>'")


class VlanLayoutStale(Exception):
    """This process's VLAN_BANDS is not the layout the database records."""


def check_layout_current(conn) -> None:
    """Refuse, inside the admission transaction, when the recorded layout is
    not this process's VLAN_BANDS: after an accept, a portal still running
    the old layout would otherwise deploy labs onto networks the jumpbox no
    longer serves for them. Nothing recorded (or no table) passes; startup
    handles those."""
    table = VlanLayoutRecord.__table__
    if not inspect(conn).has_table(table.name):
        return
    recorded = conn.execute(select(table.c.bands).where(table.c.id == 1)).scalar()
    current = vlan_layout().canonical
    if recorded is not None and recorded != current:
        raise VlanLayoutStale(
            "portal VLAN layout differs from the recorded layout — restart required "
            f"(recorded: {recorded}; this portal process: {current})")


def check_vlan_layout_record() -> None:
    """Compare VLAN_BANDS with the layout the database records. None
    recorded: record it, unless student pairs are stored, whose layout
    only the operator knows (the migration seeds it). Equal: nothing to do.
    Different: refuse while any student pair is stored, because the same
    tags would then lie on other subnets; with no stored pair, record the
    new layout."""
    if not inspect(db.engine).has_table(VlanLayoutRecord.__tablename__):
        return  # created by db.create_all / the migration; checked next start
    current = vlan_layout().canonical
    recorded = recorded_vlan_layout()
    if recorded is None:
        if has_student_pairs():
            raise ValueError(
                "No VLAN layout is recorded, but student VLAN pairs are stored, so the "
                "layout they were numbered under is unknown and VLAN_BANDS cannot be checked "
                f"against it. Seed it first: {SEED_COMMAND} (the pre-upgrade value; "
                "docs/INTERNAL_VLAN_ROLLOUT.md, section 2).")
        record_vlan_layout(current)
        recorded = recorded_vlan_layout()
    if recorded == current:
        return
    if has_student_pairs():
        raise ValueError(
            "VLAN_BANDS differs from the layout the stored student VLAN pairs were numbered "
            f"under.\n  recorded: {recorded}\n  VLAN_BANDS: {current}\nEither restore the "
            "recorded VLAN_BANDS, or, between terms with no live VM, review and accept the "
            "new layout: python scripts/accept_vlan_layout.py (dry run, lists every student "
            "network that moves), then python scripts/accept_vlan_layout.py --apply.")
    logger.warning("VLAN_BANDS changed with no stored student pair; recording %s (was %s)",
                   current, recorded)
    record_vlan_layout(current)


def check_stored_vlan_tags() -> None:
    """Refuse to start when VLAN_BANDS changed under stored student pairs,
    or a stored pair lies outside it, so a changed layout cannot silently
    cut students off their labs; log each group or KOTH VLAN outside every
    band."""
    check_vlan_layout_record()
    refusals, warnings = stored_vlan_problems()
    for warning in warnings:
        logger.warning("%s", warning)
    if refusals:
        shown = refusals[:_REFUSALS_SHOWN]
        if len(refusals) > len(shown):
            shown.append(f"... and {len(refusals) - len(shown)} more")
        raise ValueError(
            "Stored student VLAN pairs lie outside VLAN_BANDS. Restore the VLAN_BANDS they "
            "were numbered under, or renumber those students, before starting:\n  - "
            + "\n  - ".join(shown))


class AcceptRefused(Exception):
    """A layout cannot be accepted while a lab is live or being admitted."""


def accept_vlan_layout_locked(canonical: str) -> None:
    """Record `canonical` as the layout of the stored pairs, in one
    transaction under the lock every deployment's admission takes: no VM
    record may be live and no admission reservation held. A deployment
    admitted first therefore blocks the accept, and one admitted after it
    is refused by check_layout_current until its portal restarts."""
    from . import coordination
    from .models import NodeMemoryReservation, VMRecord

    def accept(conn):
        records = VMRecord.__table__
        live = conn.execute(select(func.count()).select_from(records).where(
            or_(records.c.destroyed.is_(False), records.c.destroyed.is_(None)))).scalar_one()
        reservations = NodeMemoryReservation.__table__
        held = conn.execute(select(func.count()).select_from(reservations).where(
            reservations.c.expires_at > coordination.lease_clock())).scalar_one()
        if live or held:
            raise AcceptRefused(
                f"{live} VM record(s) are live and {held} deployment admission(s) are in "
                "progress; stop the portal and destroy every lab first, because a running "
                "lab keeps its old addresses")
        table = VlanLayoutRecord.__table__
        if conn.execute(select(table.c.id).where(table.c.id == 1)).first():
            conn.execute(update(table).where(table.c.id == 1)
                         .values(bands=canonical, recorded_at=datetime.now()))
        else:
            conn.execute(insert(table).values(id=1, bands=canonical, recorded_at=datetime.now()))

    coordination.run_locked(coordination.current_engine(), coordination.NODE_ADMISSION, accept)


@contextmanager
def group_vlans_locked():
    """Run a group VLAN change (allocate, write, commit with db.session)
    under the deployment admission lock, after checking that the recorded
    layout is this process's own. Raises VlanLayoutStale, having written
    nothing, when it is not: a portal still on the old layout must not
    commit group pairs from its old group bands after an accept."""
    from . import coordination
    with coordination.session_locked(coordination.NODE_ADMISSION) as conn:
        check_layout_current(conn)
        yield
