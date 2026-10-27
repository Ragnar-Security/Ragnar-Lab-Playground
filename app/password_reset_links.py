"""Admin-issued, single-use, expiring password reset links.

The link is the whole credential, so it carries 32 random bytes and only
its SHA-256 is stored. An administrator hands it to the student out of band;
the site needs no mail server for it.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import delete, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import PasswordResetToken, User, db


def _now():
    # Naive local time, like every other column (see CLAUDE.md conventions).
    return datetime.now()


def _digest(raw_token):
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def _replace_link(dialect, values):
    """One statement that inserts the user's row or overwrites it in place.

    The unique user_id makes the database serialize issuers: the second
    writer's statement waits on the first's row and then overwrites it. A
    delete followed by an insert would leave a window between the two in
    which another issuer could add its own row.
    """
    replacement = {key: value for key, value in values.items() if key != 'user_id'}
    if dialect.name in ('mysql', 'mariadb'):
        return mysql_insert(PasswordResetToken).values(**values).on_duplicate_key_update(
            **replacement)
    if dialect.name == 'sqlite':
        return sqlite_insert(PasswordResetToken).values(**values).on_conflict_do_update(
            index_elements=['user_id'], set_=replacement)
    raise NotImplementedError(f'password reset links need MySQL or SQLite, not {dialect.name}')


def issue(user, created_by):
    """Create a link for `user` and return ``(raw_token, expires_at)``.

    Any earlier link for the same user stops working: an admin who issues a
    second link usually suspects the first went to the wrong place. When two
    admins issue at once, both are shown a link and only the later write's
    link works.
    """
    now = _now()
    raw_token = secrets.token_urlsafe(32)
    digest = _digest(raw_token)
    expires_at = now + timedelta(minutes=current_app.config['PASSWORD_RESET_TOKEN_TTL_MINUTES'])
    db.session.execute(_replace_link(db.session.get_bind().dialect, {
        'user_id': user.id,
        'created_by_user_id': created_by.id if created_by is not None else None,
        'token_hash': digest,
        'created_at': now,
        'expires_at': expires_at,
        'used_at': None,
    }))
    # A no-op once migrate_add_password_reset_tokens.py has made user_id
    # unique. On a MySQL table that predates it, ON DUPLICATE KEY UPDATE finds
    # no duplicate and inserts beside the old row; this still retires it.
    db.session.execute(
        delete(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.token_hash != digest)
        .execution_options(synchronize_session=False)
    )
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
