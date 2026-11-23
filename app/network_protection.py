"""Group and KOTH internal networks on Proxmox are reached only by pivoting.

The Headscale policy withholds them from the VPN and the jumpbox drops tailnet
traffic to and from them. Both are installed by the operator's apply script
(scripts/apply_headscale_policy.sh), which verifies them and then records the
protected VLAN tags here. Until a group's or KOTH game's internal VLAN is
recorded, its Proxmox deployment does not start; and a tag the jumpbox still
drops is never handed out as a network players must reach directly (an entry
VLAN, or a student's own internal VLAN).
"""

from datetime import datetime
from typing import Iterable, Optional, Set

from .models import ProtectedVlan, db

APPLY_HINT = ("the operator must run scripts/apply_headscale_policy.sh --apply, which installs "
              "and verifies the jumpbox drop and the Headscale policy for it")


def protected_tags() -> Set[int]:
    return {tag for (tag,) in db.session.query(ProtectedVlan.tag).all()}


def record(tags: Iterable[int]) -> None:
    """Replace the recorded set in one transaction."""
    tags = list(tags)
    for tag in tags:
        if isinstance(tag, bool) or not isinstance(tag, int) or not 2 <= tag <= 4094:
            raise ValueError(f"VLAN tag {tag!r} must be an integer 2-4094")
    now = datetime.now()
    try:
        ProtectedVlan.query.delete()
        db.session.add_all(ProtectedVlan(tag=tag, verified_at=now) for tag in sorted(set(tags)))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def deployment_refusal(entry: Optional[int], internal: Optional[int]) -> Optional[str]:
    """Why a Proxmox group or KOTH deployment on this pair may not start yet, or None."""
    tags = protected_tags()
    if internal not in tags:
        return (f"Internal VLAN {internal} is not protected yet: players would reach it "
                f"straight from the VPN. {APPLY_HINT[0].upper()}{APPLY_HINT[1:]}.")
    if entry in tags:
        return (f"Entry VLAN {entry} is still protected (the jumpbox drops it): "
                f"{APPLY_HINT}.")
    return None


def reuse_refusal(tag: int, role: str) -> Optional[str]:
    """Why `tag` may not become a directly reachable `role` network yet, or None."""
    if tag in protected_tags():
        return (f"VLAN {tag} is still protected (the jumpbox drops it to and from the VPN), "
                f"so it cannot be {role} yet: {APPLY_HINT}.")
    return None
