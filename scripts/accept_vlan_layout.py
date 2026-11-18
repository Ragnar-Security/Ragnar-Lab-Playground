#!/usr/bin/env python3
"""Accept a changed VLAN_BANDS for the student pairs already in the database.

The portal records the layout its student VLAN pairs were numbered under and
refuses to start when VLAN_BANDS differs from it while any pair is stored:
the same tags would then sit on other subnets, and the jumpbox and Headscale
policy would serve the students different networks than their labs use.

    docker exec <PORTAL_APP_CONTAINER> python scripts/accept_vlan_layout.py           # dry run
    docker exec <PORTAL_APP_CONTAINER> python scripts/accept_vlan_layout.py --apply

The dry run prints the recorded and configured layouts and every student
network that changes (old -> new subnet). --apply records the configured
layout, and refuses while any VM record is live or a deployment is being
admitted (a running lab keeps the old addresses; the check and the write
hold the admission lock together) or while a stored pair lies outside the
new entry/internal bands (those students need renumbering first). Run it
between terms with the portal stopped, then start the portal, re-run
jumpbox_configure.sh --apply and apply_headscale_policy.sh. A portal process
still running the old layout refuses every deployment until restarted.
"""

import argparse
import importlib
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.backends.network_slots import parse_vlan_bands, proxmox_slot_cidr  # noqa: E402
from app.models import StudentWhitelist, User, VMRecord, db  # noqa: E402
from app.vlan_allocator import (  # noqa: E402
    AcceptRefused, accept_vlan_layout_locked, recorded_vlan_layout, stored_vlan_problems)


def _subnet(tag: Optional[int], bands) -> str:
    if tag is None:
        return "-"
    return proxmox_slot_cidr(tag, bands) or "outside every band"


def network_changes(recorded: Optional[str], current) -> List[str]:
    """One line per student network whose subnet differs between layouts."""
    old = parse_vlan_bands(recorded) if recorded else None
    pairs = {}
    for model in (StudentWhitelist, User):
        for login, entry, internal in db.session.query(
                model.login_id, model.entry_vlan_tag, model.internal_vlan_tag):
            if entry is not None or internal is not None:
                pairs.setdefault(login, set()).add((entry, internal))
    lines = []
    for login in sorted(pairs):
        for entry, internal in sorted(pairs[login], key=str):
            for label, tag in (("entry", entry), ("internal", internal)):
                before = _subnet(tag, old) if old is not None else "unrecorded"
                after = _subnet(tag, current.bands)
                if before != after:
                    lines.append(f"{login}: {label} VLAN {tag}: {before} -> {after}")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="record the configured VLAN_BANDS (default: dry run)")
    args = parser.parse_args(argv)

    app = create_app(start_background_tasks=False, check_vlan_layout=False)
    with app.app_context():
        current = importlib.import_module("app.config").vlan_layout()
        recorded = recorded_vlan_layout()
        print(f"recorded:   {recorded or 'none'}")
        print(f"VLAN_BANDS: {current.canonical}")
        if recorded == current.canonical:
            print("nothing to do: VLAN_BANDS is the recorded layout.")
            return 0
        changes = network_changes(recorded, current)
        for line in changes:
            print(f"  {line}")
        print(f"{len(changes)} student network(s) change.")
        live = VMRecord.query.filter(VMRecord.destroyed.is_(False)).count()
        outside, _warnings = stored_vlan_problems()
        blockers = []
        if live:
            blockers.append(f"{live} VM record(s) are live; destroy every lab first, "
                            "because a running lab keeps its old addresses")
        blockers += [f"{problem}; renumber it first" for problem in outside]
        for blocker in blockers:
            print(f"BLOCKED: {blocker}", file=sys.stderr)
        if not args.apply:
            print("dry run: nothing recorded. Re-run with --apply to record VLAN_BANDS.")
            return 1 if blockers else 0
        if blockers:
            print("refused: nothing recorded.", file=sys.stderr)
            return 1
        # The live-VM check and the write are repeated in one transaction under
        # the deployment admission lock, so no deployment can be admitted in
        # between.
        db.session.remove()
        try:
            accept_vlan_layout_locked(current.canonical)
        except AcceptRefused as error:
            print(f"BLOCKED: {error}", file=sys.stderr)
            print("refused: nothing recorded.", file=sys.stderr)
            return 1
        print(f"recorded {current.canonical}. Restart the portal, then re-run "
              "jumpbox_configure.sh --apply and apply_headscale_policy.sh --apply.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
