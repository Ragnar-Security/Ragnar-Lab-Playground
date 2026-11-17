#!/usr/bin/env python3
"""Check that every student's VLAN pair fits the per-student bands.

Reads the output of scripts/export_network_slots.py (a file, or "-" for
stdin) and reports every user whose pair is not entry 100-199 with
internal = entry + 100, and every VLAN tag held by more than one owner
across users, groups and KOTH games. Prints each problem and exits 1, or
prints a one-line summary and exits 0. Standard library only: it runs on the
operator's workstation.

    docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py \\
        | python3 scripts/audit_vlan_pairs.py -
"""

import json
import sys
from collections import defaultdict
from typing import Any, List, Mapping

# Literals, so the script needs no portal import. tests/test_audit_vlan_pairs.py
# pins them to USER_ENTRY_MIN/MAX and USER_INTERNAL_MIN in app/vlan_allocator.py.
USER_ENTRY_FIRST, USER_ENTRY_LAST, USER_INTERNAL_OFFSET = 100, 199, 100


def _fits(entry: Any, internal: Any) -> bool:
    return (isinstance(entry, int) and isinstance(internal, int)
            and USER_ENTRY_FIRST <= entry <= USER_ENTRY_LAST
            and internal == entry + USER_INTERNAL_OFFSET)


def audit(slots: Mapping[str, Any]) -> List[str]:
    """Return the sorted problem lines for one export_network_slots.py export."""
    problems: List[str] = []
    holders = defaultdict(list)

    def hold(owner: str, *tags: Any) -> None:
        for tag in tags:
            if tag is not None:
                holders[tag].append(owner)

    for user in slots.get("users", []):
        login, entry, internal = user["login"], user["entry_vlan"], user["internal_vlan"]
        if not _fits(entry, internal):
            problems.append(
                f"{login}: pair {entry}/{internal} is outside entry "
                f"{USER_ENTRY_FIRST}-{USER_ENTRY_LAST} with internal = entry + {USER_INTERNAL_OFFSET}")
        hold(login, entry, internal)
    for group in slots.get("groups", []):
        hold(f"group {group['slug']}", group["entry_vlan"], group["internal_vlan"])
    for game in slots.get("koth", []):
        hold(f"KOTH game {game['game_id']}", game["entry_vlan"], game["internal_vlan"])

    for tag, owners in holders.items():
        if len(owners) > 1:
            names = sorted(owners)
            problems.append(f"VLAN {tag} is held by {', '.join(names[:-1])} and {names[-1]}")
    return sorted(problems)


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("usage: audit_vlan_pairs.py SLOTS.json|-", file=sys.stderr)
        return 2
    if argv[1] == "-":
        slots = json.load(sys.stdin)
    else:
        with open(argv[1], "r", encoding="utf-8") as handle:
            slots = json.load(handle)
    problems = audit(slots)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(f"ok: {len(slots.get('users', []))} users, {len(slots.get('groups', []))} groups, "
          f"{len(slots.get('koth', []))} KOTH games")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
