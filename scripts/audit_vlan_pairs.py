#!/usr/bin/env python3
"""Check that every student's VLAN pair fits the per-student bands.

Reads the output of scripts/export_network_slots.py (a file, or "-" for
stdin) and reports every user whose pair is not in the entry band of the
exported VLAN_BANDS with internal = entry + (internal.first - entry.first),
and every VLAN tag held by more than one owner across users, groups and KOTH
games. Prints each problem and exits 1, or prints a one-line summary and
exits 0. Standard library only: it runs on the operator's workstation.

    docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py \\
        | python3 scripts/audit_vlan_pairs.py -
"""

import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, List, Mapping


def _load_network_slots():
    # By path, not `import app...`: the app package needs Flask and its
    # configuration, which the workstation does not have.
    name = "_lab_network_slots"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent.parent / "app" / "backends" / "network_slots.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def audit(slots: Mapping[str, Any]) -> List[str]:
    """Return the sorted problem lines for one export_network_slots.py export."""
    spec = (slots.get("config") or {}).get("vlan_bands")
    if not isinstance(spec, str):
        raise ValueError("the export has no config.vlan_bands; export it with a portal "
                         "that sets VLAN_BANDS")
    layout = _load_network_slots().parse_vlan_layout(spec)
    entry_band, offset = layout.entry, layout.pair_offset

    problems: List[str] = []
    holders = defaultdict(list)

    def hold(owner: str, *tags: Any) -> None:
        for tag in tags:
            if tag is not None:
                holders[tag].append(owner)

    for user in slots.get("users", []):
        login, entry, internal = user["login"], user["entry_vlan"], user["internal_vlan"]
        if not (entry in entry_band and isinstance(internal, int) and internal == entry + offset):
            problems.append(
                f"{login}: pair {entry}/{internal} is outside entry "
                f"{entry_band.first}-{entry_band.last} with internal = entry + {offset}")
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
    try:
        problems = audit(slots)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(f"ok: {len(slots.get('users', []))} users, {len(slots.get('groups', []))} groups, "
          f"{len(slots.get('koth', []))} KOTH games")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
