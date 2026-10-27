"""Admin-issued, single-use, expiring password reset links.

The link is the whole credential, so it carries 32 random bytes and only
its SHA-256 is stored. An administrator hands it to the student out of band;
the site needs no mail server for it.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import update

from .models import PasswordResetToken, User, db


def _now():
    # Naive local time, like every other column (see CLAUDE.md conventions).
    return datetime.now()


def _digest(raw_token):
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def issue(user, created_by):
    """Create a link for `user` and return ``(raw_token, expires_at)``.

    Any earlier unused link for the same user stops working: an admin who
    issues a second link usually suspects the first went to the wrong place.
    """
    now = _now()
    raw_token = secrets.token_urlsafe(32)
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).delete(
        synchronize_session=False)
    expires_at = now + timedelta(minutes=current_app.config['PASSWORD_RESET_TOKEN_TTL_MINUTES'])
    db.session.add(PasswordResetToken(
        user_id=user.id,
        created_by_user_id=created_by.id if created_by is not None else None,
        token_hash=_digest(raw_token),
        created_at=now,
        expires_at=expires_at,
    ))
    db.session.commit()
    return raw_token, expires_at


def redeem(raw_token, new_password):
    """Set `new_password` for the link's user and return the user, or return
    ``None`` when the link is unknown, expired or already used.

    The link is claimed with one conditional UPDATE before anything else
    happens. Two submissions racing on the same link (two tabs, or an
    attacker who saw it) both reach that statement; the database applies it
    to the row once, so exactly one sees a row count of 1. Reading the row
    first and then marking it used would let both readers win.
    """
    now = _now()
    digest = _digest(raw_token)
    claimed = db.session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == digest,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.session.rollback()
        return None

    token = PasswordResetToken.query.filter_by(token_hash=digest).one()
    user = db.session.get(User, token.user_id)
    user.set_password(new_password)
    # A reset is how a student takes an account back; it must also retire
    # every cookie copied while someone else held it.
    user.session_version = User.session_version + 1
    db.session.commit()
    return user
