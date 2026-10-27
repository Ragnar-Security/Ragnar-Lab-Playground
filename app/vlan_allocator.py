"""
Centralized VLAN allocation for users and student groups.

Scopes:
    'user'  — entry 100-200, internal 200-300 (existing user pool)
    'group' — entry GROUP_ENTRY_VLAN_MIN-MAX, internal GROUP_INTERNAL_VLAN_MIN-MAX
             (from app.config, default 300-400 / 400-500)

Both pools are checked for collisions across User, StudentWhitelist, and
StudentGroup — a tag assigned to any of them is considered used.
"""

from typing import Literal, Optional, Tuple

from .models import db, User, StudentWhitelist, StudentGroup, KothGame
from .config import (
    GROUP_ENTRY_VLAN_MIN, GROUP_ENTRY_VLAN_MAX,
    GROUP_INTERNAL_VLAN_MIN, GROUP_INTERNAL_VLAN_MAX,
)

USER_ENTRY_MIN, USER_ENTRY_MAX = 100, 200
USER_INTERNAL_MIN, USER_INTERNAL_MAX = 200, 300


class VlanPoolExhausted(Exception):
    """Raised when no free VLAN tag remains in the requested pool."""


class VlanInUse(Exception):
    """Raised when an explicit VLAN tag collides with an existing assignment."""


def _pool_for(scope: Literal['user', 'group']) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    if scope == 'user':
        return ((USER_ENTRY_MIN, USER_ENTRY_MAX),
                (USER_INTERNAL_MIN, USER_INTERNAL_MAX))
    if scope == 'group':
        return ((GROUP_ENTRY_VLAN_MIN, GROUP_ENTRY_VLAN_MAX),
                (GROUP_INTERNAL_VLAN_MIN, GROUP_INTERNAL_VLAN_MAX))
    raise ValueError(f"Unknown scope: {scope!r}")


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
