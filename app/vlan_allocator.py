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
import logging
from typing import List, Literal, Optional, Tuple

from sqlalchemy import inspect

from .models import db, User, StudentWhitelist, StudentGroup, KothGame

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


def check_stored_vlan_tags() -> None:
    """Refuse to start when a stored student pair lies outside VLAN_BANDS,
    so a changed layout cannot silently cut students off their labs; log
    each group or KOTH VLAN outside every band."""
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
