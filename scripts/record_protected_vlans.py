#!/usr/bin/env python3
"""Record the protected VLANs the jumpbox and the Headscale policy enforce.

Runs inside the portal application container; scripts/apply_headscale_policy.sh
runs it last, after it has installed and verified the jumpbox drop and the
Headscale policy. Reads `headscale_policy.py protection` output on stdin and
replaces the portal's recorded set (app.network_protection), which lets the
Proxmox group and KOTH deployments on those internal VLANs start.
"""

import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, network_protection  # noqa: E402


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
        vlans = data.get("vlans") if isinstance(data, dict) else None
        if not isinstance(vlans, list):
            raise ValueError("stdin must be the protection JSON, with a vlans list")
        with contextlib.redirect_stdout(sys.stderr):
            app = create_app(start_background_tasks=False)
            with app.app_context():
                network_protection.record(vlans)
    except Exception as exc:  # nothing is recorded unless the whole set is
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    tags = sorted(set(vlans))
    print(f"recorded {len(tags)} protected VLAN(s): {', '.join(map(str, tags)) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
