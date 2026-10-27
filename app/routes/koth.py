"""KOTH blueprint root.

This module defines the shared `koth_bp` Blueprint and imports the per-area
modules (admin, player) so their handlers are registered. Other modules
MUST register routes via `@koth_bp.route(...)` after importing `koth_bp`
from here.

The legacy `koth_callbacks` module (curl-based POST /koth/claim and
POST /koth/flag with token auth) was removed when the claim mechanism
switched to file-poll via guest agent (claims) and session-authed web
form (flags). See app/koth_scorer.py:_poll_claims and
app/routes/koth_player.py:submit_flag.
"""

from flask import Blueprint

koth_bp = Blueprint('koth', __name__)

# Side-effect imports that register routes on koth_bp.
from . import koth_admin  # noqa: F401, E402
from . import koth_player  # noqa: F401, E402
