#!/usr/bin/env python3
"""Print the networks this portal allocates, for scripts/headscale_policy.py.

Runs inside the portal application container, so the roster comes from its
database and the backends, lab CIDRs and VLAN bands from its own
configuration: the policy then grants exactly what this portal hands out.
Compact JSON goes to stdout; anything else goes to stderr.
"""

import contextlib
import importlib
import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.models import KothGame, StudentGroup, StudentWhitelist, User  # noqa: E402


def _pair(what: str, entry: Optional[int], internal: Optional[int]) -> Tuple[int, int]:
    if entry is None or internal is None:
        raise ValueError(f"{what} has an incomplete VLAN pair "
                         f"(entry={entry}, internal={internal}); assign both first")
    return entry, internal


def collect_network_slots() -> Dict[str, Any]:
    """The roster, groups and non-ended KOTH games with their VLAN pairs.

    The whitelist is the complete term roster, including students who have
    not registered yet. A registered student's User row wins: /create uses
    its tags, and an admin's reassignment after registration lives there.
    """
    config = importlib.import_module('app.config')
    registered = {u.login_id: u for u in User.query.all()}

    users = []
    for row in StudentWhitelist.query.order_by(StudentWhitelist.login_id).all():
        source = registered.get(row.login_id, row)
        entry, internal = _pair(f"student {row.login_id}",
                                source.entry_vlan_tag, source.internal_vlan_tag)
        users.append({"login": row.login_id, "entry_vlan": entry, "internal_vlan": internal})
    roster = {user["login"] for user in users}

    groups = []
    for group in StudentGroup.query.order_by(StudentGroup.name).all():
        entry, internal = _pair(f"group {group.name}",
                                group.entry_vlan_tag, group.internal_vlan_tag)
        members = sorted(member.user.login_id for member in group.members)
        outsiders = [login for login in members if login not in roster]
        if outsiders:
            raise ValueError(f"group {group.name} has members not on the roster: "
                             f"{', '.join(outsiders)}")
        groups.append({"slug": group.name, "members": members,
                       "entry_vlan": entry, "internal_vlan": internal})

    koth = [{"game_id": game.id, "entry_vlan": game.entry_vlan_tag,
             "internal_vlan": game.internal_vlan_tag}
            for game in KothGame.query.filter(KothGame.status != 'ended')
            .order_by(KothGame.id).all()]

    return {
        "config": {
            "enabled_backends": list(config.ENABLED_BACKENDS),
            "aws_lab_cidr": config.AWS_LAB_CIDR,
            "gcp_lab_cidr": config.GCP_LAB_CIDR,
            "jumpbox_vlan_bands": config.JUMPBOX_VLAN_BANDS,
        },
        "users": users,
        "groups": groups,
        "koth": koth,
    }


def main() -> int:
    try:
        # Keep stdout for the JSON alone, whatever app start-up prints.
        with contextlib.redirect_stdout(sys.stderr):
            app = create_app()
            with app.app_context():
                slots = collect_network_slots()
    except Exception as exc:  # any failure must leave stdout empty
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(slots, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
