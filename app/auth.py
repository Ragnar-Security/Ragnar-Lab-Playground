"""
Authentication routes module
Handles user registration, login, and session management

This module provides secure authentication for students using their Canvas login IDs.
Students must be whitelisted (imported from Canvas CSV) before they can register.

Routes:
    POST /auth/register - Register a new user account
    POST /auth/login - Login with existing credentials
    POST /auth/logout - Logout current user
    GET /auth/me - Get current logged-in user info
    GET /auth/check - Check authentication status

Security:
    - Passwords are hashed using werkzeug.security (PBKDF2)
    - Session cookies are signed with SECRET_KEY
    - Login IDs must match Canvas whitelist
    - Session lifetime is PERMANENT_SESSION_LIFETIME (default 2 hours)

Example Usage:
    Register:
        POST /auth/register
        {
            "login_id": "jdoe1",
            "password": "mypassword123",
            "email": "jdoe1@example.edu"
        }
    
    Login:
        POST /auth/login
        {
            "login_id": "jdoe1",
            "password": "mypassword123"
        }
"""

from flask import Blueprint, current_app, request, jsonify, session, Response
from collections import OrderedDict, deque
from datetime import datetime, timezone
from functools import wraps
from typing import Callable, Any, Union, Tuple
import hashlib
import hmac
import logging
import os
import threading
import time

from sqlalchemy import delete, func, insert, select

logger = logging.getLogger(__name__)

from .models import db, User, StudentWhitelist, PasswordResetFailure
from . import __version__, coordination, password_reset_links

auth = Blueprint('auth', __name__)


class _SlidingWindow:
    """Failures per key within the trailing `window` seconds.

    Keys are kept in order of their latest failure, so expired keys sit at
    the front and are dropped there without scanning the table: every
    operation is O(1) amortized. Each key keeps at most `limit` stamps,
    which is all `blocked()` needs.
    """

    # Hard bound on tracked keys, so a spray of distinct logins or rotating
    # addresses cannot grow process memory. When the table is full of live
    # keys, the key whose latest failure is oldest is forgotten. Pushing a
    # live key out takes MAX_KEYS newer failures within one window, which
    # the per-address limit spreads over at least MAX_KEYS / PER_ADDRESS
    # client addresses.
    MAX_KEYS = 10_000

    def __init__(self, limit, window, clock, max_keys=None):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.max_keys = self.MAX_KEYS if max_keys is None else max_keys
        self._failures = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self._failures)

    def _expire(self, now):
        cutoff = now - self.window
        while self._failures:
            key, stamps = next(iter(self._failures.items()))
            if stamps[-1] > cutoff:
                return
            del self._failures[key]

    def _live(self, key, now):
        stamps = self._failures.get(key)
        if stamps is None:
            return None
        while stamps and stamps[0] <= now - self.window:
            stamps.popleft()
        if not stamps:
            del self._failures[key]
            return None
        return stamps

    def blocked(self, key):
        with self._lock:
            stamps = self._live(key, self.clock())
            return stamps is not None and len(stamps) >= self.limit

    def record(self, key):
        with self._lock:
            now = self.clock()
            self._expire(now)
            stamps = self._live(key, now)
            if stamps is None:
                while len(self._failures) >= self.max_keys:
                    self._failures.popitem(last=False)
                stamps = self._failures[key] = deque(maxlen=self.limit)
            else:
                self._failures.move_to_end(key)
            stamps.append(now)


class _SharedSlidingWindow:
    """`_SlidingWindow`'s contract, kept in the application database.

    Every gunicorn worker counts the same failures, so adding a worker no
    longer multiplies the allowance. Writes take the limiter's coordination
    row, so the key cap holds exactly however workers interleave; reads
    need no lock. Keys are stored hashed (see PasswordResetFailure).
    """

    MAX_KEYS = _SlidingWindow.MAX_KEYS

    def __init__(self, scope, limit, window, clock, max_keys=None):
        self.scope = scope
        self.limit = limit
        self.window = window
        self.clock = clock
        self.max_keys = self.MAX_KEYS if max_keys is None else max_keys
        self._table = PasswordResetFailure.__table__

    @staticmethod
    def _hash(key):
        return hashlib.sha256(str(key).encode('utf-8')).hexdigest()

    def _live(self, cutoff):
        table = self._table
        return (table.c.scope == self.scope) & (table.c.failed_at > cutoff)

    def __len__(self):
        table = self._table
        with db.engine.connect() as conn:
            return conn.execute(
                select(func.count(func.distinct(table.c.key_hash)))
                .where(self._live(self.clock() - self.window))).scalar()

    def blocked(self, key):
        table = self._table
        with db.engine.connect() as conn:
            failures = conn.execute(
                select(func.count()).select_from(table)
                .where(self._live(self.clock() - self.window),
                       table.c.key_hash == self._hash(key))).scalar()
        return failures >= self.limit

    def record(self, key):
        key_hash = self._hash(key)
        coordination.run_locked(
            db.engine, coordination.PASSWORD_RESET_LIMITER,
            lambda conn: self._record(conn, key_hash))

    def _record(self, conn, key_hash):
        table = self._table
        now = self.clock()
        in_scope = table.c.scope == self.scope
        conn.execute(delete(table).where(
            in_scope, table.c.failed_at <= now - self.window))

        mine = in_scope & (table.c.key_hash == key_hash)
        if conn.execute(select(table.c.id).where(mine).limit(1)).first() is None:
            tracked = conn.execute(
                select(func.count(func.distinct(table.c.key_hash)))
                .where(in_scope)).scalar()
            if tracked >= self.max_keys:
                # Forget the keys whose latest failure is oldest, as the
                # in-process window does.
                latest = func.max(table.c.failed_at)
                oldest = conn.execute(
                    select(table.c.key_hash)
                    .where(in_scope)
                    .group_by(table.c.key_hash)
                    .order_by(latest, table.c.key_hash)
                    .limit(tracked - self.max_keys + 1)).scalars().all()
                conn.execute(delete(table).where(
                    in_scope, table.c.key_hash.in_(oldest)))
        else:
            # Keep the newest limit - 1 stamps; the insert below makes limit.
            stale = conn.execute(
                select(table.c.id)
                .where(mine)
                .order_by(table.c.failed_at.desc(), table.c.id.desc())
                .offset(self.limit - 1)).scalars().all()
            if stale:
                conn.execute(delete(table).where(table.c.id.in_(stale)))
        conn.execute(insert(table).values(
            scope=self.scope, key_hash=key_hash, failed_at=now))


class PasswordResetLimiter:
    """Caps failed reset attempts per login ID and per client address.

    The app's limiter is `shared`: its counts live in the database, so
    every gunicorn worker enforces one allowance. `shared=False` keeps the
    counts in this process only.

    The per-login limit stops guessing one student's SIS ID from many
    addresses. The per-address limit stops sweeping many login IDs from one
    address; it is looser because a campus NAT puts a classroom behind one
    address.

    Reset-link guesses count against the per-address limit only. They name
    no login ID, and a per-link counter would let anyone who knows nothing
    burn a student's valid link.
    """

    WINDOW_SECONDS = 15 * 60
    PER_LOGIN = 5
    PER_ADDRESS = 20

    def __init__(self, clock=time.monotonic, shared=False):
        if shared:
            self._by_login = _SharedSlidingWindow(
                'login', self.PER_LOGIN, self.WINDOW_SECONDS, clock)
            self._by_address = _SharedSlidingWindow(
                'address', self.PER_ADDRESS, self.WINDOW_SECONDS, clock)
        else:
            self._by_login = _SlidingWindow(self.PER_LOGIN, self.WINDOW_SECONDS, clock)
            self._by_address = _SlidingWindow(self.PER_ADDRESS, self.WINDOW_SECONDS, clock)

    def blocked(self, address, login_id):
        return self._by_address.blocked(address) or self._by_login.blocked(login_id)

    def record_failure(self, address, login_id):
        self._by_address.record(address)
        self._by_login.record(login_id)

    def address_blocked(self, address):
        return self._by_address.blocked(address)

    def record_address_failure(self, address):
        self._by_address.record(address)


def _password_reset_limiter():
    # The counts live in the app's database, so two apps share them exactly
    # when they share a database, as two gunicorn workers do.
    limiter = current_app.extensions.get('password_reset_limiters')
    if limiter is None:
        limiter = current_app.extensions['password_reset_limiters'] = PasswordResetLimiter(
            clock=coordination.lease_clock, shared=True)
    return limiter


# One message for an unknown login, a missing account, a missing SIS ID on
# file and a wrong SIS ID: distinct messages would let anyone probe which
# login IDs are on the roster and which accounts have no SIS ID to check.
_RESET_IDENTITY_FAILED = ('We could not verify that login ID and SIS User ID. '
                          'Check both, or ask an administrator to reset your password.')

# Likewise one message for unknown, expired and used links, so a probe
# learns nothing about which links exist or were ever issued.
_RESET_LINK_FAILED = ('This password reset link is invalid, expired, or already used. '
                      'Ask an administrator for a new link.')


def _password_problem(password):
    """The registration password policy, shared by every path that sets one."""
    if len(password) < 8:
        return 'Password must be at least 8 characters'
    return None


def _sis_matches(stored, submitted):
    """Constant-time SIS comparison that fails closed without a stored ID.

    A roster row without a SIS ID has nothing to prove identity against;
    accepting any submission there handed the account to whoever asked."""
    stored = (stored or '').strip()
    if not stored:
        return False
    return hmac.compare_digest(stored.encode('utf-8'), submitted.encode('utf-8'))


def get_authenticated_user():
    """Return the user authenticated by this signed session, or ``None``.

    A per-user version makes copied cookies revocable.  Missing versions are
    deliberately invalid so every cookie issued before this mechanism was
    deployed is retired on first use.
    """
    user_id = session.get('user_id')
    cookie_version = session.get('session_version')
    if user_id is None:
        return None
    if cookie_version is None:
        session.clear()
        return None

    user = db.session.get(User, user_id)
    if (
        user is None
        or user.disabled
        or cookie_version != user.session_version
    ):
        session.clear()
        return None

    return user


def login_required(f: Callable[..., Any]) -> Callable[..., Union[Response, Tuple[Response, int]]]:
    """
    Decorator to require login for routes

    Usage:
        @auth.route('/protected')
        @login_required
        def protected_route():
            return jsonify({'message': 'This is protected'})

    Args:
        f: The function to wrap

    Returns:
        Wrapped function that checks for authentication
        Returns 401 if user is not logged in
    """
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Union[Response, Tuple[Response, int], Any]:
        if get_authenticated_user() is None:
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f: Callable[..., Any]) -> Callable[..., Union[Response, Tuple[Response, int]]]:
    """
    Decorator to require admin privileges for routes

    Usage:
        @auth.route('/admin/users')
        @admin_required
        def admin_route():
            return jsonify({'message': 'Admin only'})

    Args:
        f: The function to wrap

    Returns:
        Wrapped function that checks for admin authentication
        Returns 401 if not logged in, 403 if not admin
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_authenticated_user()
        if user is None:
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        if not user.is_admin:
            return jsonify({'success': False, 'error': 'Admin privileges required'}), 403
        return f(*args, **kwargs)
    return decorated_function


@auth.route('/register', methods=['POST'])
def register():
    """
    Register a new user account
    
    Validates that the login ID exists in the student whitelist before
    allowing registration. Auto-logs in the user after successful registration.
    
    Request Body (JSON or Form):
        login_id (str): Canvas login ID / SIS Login ID (e.g., 'jdoe1')
        sis_user_id (str): SIS User ID from Canvas (e.g., '100000001')
        password (str): Password (minimum 8 characters)
        email (str, optional): Email address
    
    Returns:
        JSON response:
            success (bool): True if registration successful
            message (str): Success message
            user (dict): User information
            error (str): Error message if failed
    
    Status Codes:
        200: Registration successful
        400: Invalid input or login ID not whitelisted
    
    Example:
        POST /auth/register
        {
            "login_id": "jdoe1",
            "password": "securepass123",
            "email": "jdoe1@example.edu"
        }
        
        Response:
        {
            "success": true,
            "message": "Account created successfully",
            "user": {
                "id": 1,
                "login_id": "jdoe1",
                "full_name": "Jane Doe",
                "email": "jdoe1@example.edu"
            }
        }
    """
    data = request.get_json() if request.is_json else request.form

    login_id = data.get('login_id', '').strip().lower()
    sis_user_id = data.get('sis_user_id', '').strip()
    password = data.get('password', '').strip()
    email = data.get('email', '').strip()

    try:
        # Validate input
        if not login_id or not sis_user_id or not password:
            return jsonify({'success': False, 'error': 'Login ID, SIS User ID, and password are required'})

        problem = _password_problem(password)
        if problem:
            return jsonify({'success': False, 'error': problem})

        # Check if login ID is in the whitelist
        whitelisted = StudentWhitelist.query.filter_by(login_id=login_id).first()

        if not whitelisted:
            return jsonify({
                'success': False,
                'error': 'Login ID not found. Please use your Canvas login ID.'
            })

        # Verify SIS User ID matches the whitelist entry. A row without one
        # fails closed, as in the reset path: there is nothing to prove.
        if not _sis_matches(whitelisted.sis_user_id, sis_user_id):
            return jsonify({
                'success': False,
                'error': 'SIS User ID does not match our records for this Login ID.'
            })
        
        # Check if user already exists
        existing_user = User.query.filter_by(login_id=login_id).first()
        
        if existing_user:
            return jsonify({
                'success': False,
                'error': 'An account with this login ID already exists. Please login instead.'
            })
        
        # Create new user with pre-assigned VLANs and headscale key from whitelist
        user = User(
            login_id=login_id,
            email=email,
            full_name=whitelisted.full_name,
            entry_vlan_tag=whitelisted.entry_vlan_tag,
            internal_vlan_tag=whitelisted.internal_vlan_tag,
            headscale_key=whitelisted.headscale_key
        )
        user.set_password(password)
        
        # Check if user should be admin (from ADMIN_USERS environment variable)
        import os
        admin_users = os.getenv('ADMIN_USERS', '').strip()
        if admin_users:
            admin_list = [u.strip().lower() for u in admin_users.split(',') if u.strip()]
            if login_id in admin_list:
                user.is_admin = True
                logger.info(f"Granting admin privileges to {login_id}")
        
        db.session.add(user)
        db.session.commit()
        
        # Auto-login after registration
        session.permanent = True  # Enable session expiration (2 hours)
        session['user_id'] = user.id
        session['session_version'] = user.session_version
        session['login_id'] = user.login_id
        session['full_name'] = user.full_name
        session['is_admin'] = user.is_admin
        
        return jsonify({
            'success': True,
            'message': 'Account created successfully',
            'user': user.to_dict()
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Registration error: {e}")
        return jsonify({'success': False, 'error': 'An unexpected error occurred'})


@auth.route('/login', methods=['POST'])
def login():
    """
    Login with existing credentials
    
    Authenticates user and creates a session. Updates the last_login timestamp.
    
    Request Body (JSON or Form):
        login_id (str): Canvas login ID
        password (str): User's password
    
    Returns:
        JSON response:
            success (bool): True if login successful
            message (str): Success message
            user (dict): User information
            error (str): Error message if failed
    
    Status Codes:
        200: Login successful
        400: Invalid credentials
    
    Example:
        POST /auth/login
        {
            "login_id": "jdoe1",
            "password": "securepass123"
        }
        
        Response:
        {
            "success": true,
            "message": "Login successful",
            "user": {
                "id": 1,
                "login_id": "jdoe1",
                "full_name": "Jane Doe",
                "last_login": "2026-01-27T21:30:00"
            }
        }
    """
    data = request.get_json() if request.is_json else request.form
    
    login_id = data.get('login_id', '').strip().lower()
    password = data.get('password', '').strip()
    
    try:
        if not login_id or not password:
            return jsonify({'success': False, 'error': 'Login ID and password are required'})
        
        # Find user
        user = User.query.filter_by(login_id=login_id).first()
        
        if not user or not user.check_password(password):
            return jsonify({
                'success': False,
                'error': 'Invalid login ID or password'
            })

        if user.disabled:
            return jsonify({'success': False, 'error': 'Account has been disabled'}), 403

        # Update last login
        user.last_login = datetime.now()
        db.session.commit()
        
        # Set session
        session.permanent = True  # Enable session expiration (2 hours)
        session['user_id'] = user.id
        session['session_version'] = user.session_version
        session['login_id'] = user.login_id
        session['full_name'] = user.full_name
        session['is_admin'] = user.is_admin
        
        return jsonify({
            'success': True,
            'message': 'Login successful',
            'user': user.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'success': False, 'error': 'An unexpected error occurred'})


@auth.route('/reset-password', methods=['POST'])
def reset_password():
    """
    Reset a user's password by verifying their identity with login_id and SIS User ID.

    Request Body (JSON or Form):
        login_id (str): Canvas login ID
        sis_user_id (str): SIS User ID from Canvas (9-digit)
        password (str): New password (minimum 8 characters)

    Returns:
        JSON response with success/error status. Every identity failure
        carries the same message.

    Status Codes:
        200: Reset succeeded, or failed with ``success: false``
        404: Self-service reset is disabled (SELF_SERVICE_PASSWORD_RESET=false)
        429: Too many failed attempts for this login ID or client address
    """
    if not current_app.config.get('SELF_SERVICE_PASSWORD_RESET', False):
        return jsonify({
            'success': False,
            'error': 'Self-service password reset is disabled. Ask an administrator to reset your password.',
        }), 404

    data = request.get_json() if request.is_json else request.form

    login_id = data.get('login_id', '').strip().lower()
    sis_user_id = data.get('sis_user_id', '').strip()
    password = data.get('password', '').strip()

    try:
        if not login_id or not sis_user_id or not password:
            return jsonify({'success': False, 'error': 'Login ID, SIS User ID, and new password are required'})

        problem = _password_problem(password)
        if problem:
            return jsonify({'success': False, 'error': problem})

        # Checked before verification so a correct guess after the limit is
        # refused too; otherwise the limit would only slow wrong guesses.
        limiter = _password_reset_limiter()
        client_address = request.remote_addr or 'unknown'
        if limiter.blocked(client_address, login_id):
            return jsonify({
                'success': False,
                'error': 'Too many password reset attempts. Try again later or ask an administrator.',
            }), 429

        whitelisted = StudentWhitelist.query.filter_by(login_id=login_id).first()
        user = User.query.filter_by(login_id=login_id).first()
        stored_sis = whitelisted.sis_user_id if whitelisted else None
        if not _sis_matches(stored_sis, sis_user_id) or user is None:
            limiter.record_failure(client_address, login_id)
            return jsonify({'success': False, 'error': _RESET_IDENTITY_FAILED})

        # Password recovery also retires any copied authenticated cookie.
        user.set_password(password)
        user.session_version = User.session_version + 1
        db.session.commit()

        logger.info(f"Password reset successful for user: {login_id}")

        return jsonify({
            'success': True,
            'message': 'Password reset successfully. You can now login with your new password.'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Password reset error for {login_id}: {str(e)}")
        return jsonify({'success': False, 'error': 'An error occurred. Please try again.'})


@auth.route('/reset-password/link', methods=['POST'])
def reset_password_with_link():
    """Set a new password with an administrator-issued reset link.

    Request Body (JSON):
        token (str): the value after ``#token=`` in the link
        password (str): new password (registration policy)

    Works whatever SELF_SERVICE_PASSWORD_RESET says: an administrator
    issued the link, so it proves more than a roster SIS ID does.

    Status Codes:
        200: Reset succeeded, or failed with ``success: false``
        429: Too many failed link attempts from this client address

    Never log the token: it is a working credential until it is used.
    """
    data = request.get_json(silent=True) or {}
    token = data.get('token')
    password = data.get('password')
    token = token if isinstance(token, str) else ''
    password = password.strip() if isinstance(password, str) else ''

    problem = _password_problem(password)
    if problem:
        return jsonify({'success': False, 'error': problem})

    limiter = _password_reset_limiter()
    client_address = request.remote_addr or 'unknown'
    if limiter.address_blocked(client_address):
        return jsonify({
            'success': False,
            'error': 'Too many password reset attempts. Try again later or ask an administrator.',
        }), 429

    try:
        user = password_reset_links.redeem(token, password)
    except Exception as e:
        db.session.rollback()
        # The class only: a driver message can echo statement parameters.
        logger.error("Password reset link error: %s", type(e).__name__)
        return jsonify({'success': False, 'error': 'An error occurred. Please try again.'})

    if user is None:
        limiter.record_address_failure(client_address)
        return jsonify({'success': False, 'error': _RESET_LINK_FAILED})

    logger.info("Password reset with an admin-issued link for user: %s", user.login_id)
    return jsonify({
        'success': True,
        'message': 'Password reset successfully. You can now login with your new password.'
    })


@auth.route('/logout', methods=['POST'])
def logout():
    """
    Logout current user
    
    Clears the session, effectively logging out the user.
    
    Returns:
        JSON response:
            success (bool): Always true
            message (str): Success message
    
    Example:
        POST /auth/logout
        
        Response:
        {
            "success": true,
            "message": "Logged out successfully"
        }
    """
    user = get_authenticated_user()
    if user is not None:
        user.session_version = User.session_version + 1
        db.session.commit()
    session.clear()
    return jsonify({'success': True, 'message': 'Logged out successfully'})


@auth.route('/me', methods=['GET'])
@login_required
def get_current_user():
    """
    Get current logged in user info
    
    Requires authentication (login_required decorator).
    
    Returns:
        JSON response:
            success (bool): True if user found
            user (dict): Current user information
    
    Status Codes:
        200: User info retrieved successfully
        401: Not authenticated
        404: User not found (session invalid)
    
    Example:
        GET /auth/me
        
        Response:
        {
            "success": true,
            "user": {
                "id": 1,
                "login_id": "jdoe1",
                "full_name": "Jane Doe",
                "email": "jdoe1@example.edu",
                "created_at": "2026-01-27T10:00:00",
                "last_login": "2026-01-27T21:30:00"
            }
        }
    """
    user = get_authenticated_user()
    if user is None:
        return jsonify({'success': False, 'error': 'Authentication required'}), 401
    
    return jsonify({
        'success': True,
        'user': user.to_dict()
    })


@auth.route('/check', methods=['GET'])
def check_auth():
    """
    Check if user is authenticated
    
    Useful for frontend to determine if user is logged in without
    requiring authentication. Does not return 401 if not authenticated.
    
    Returns:
        JSON response:
            authenticated (bool): True if user has valid session
            user (dict, optional): User info if authenticated
    
    Example:
        GET /auth/check
        
        Response (authenticated):
        {
            "authenticated": true,
            "user": {
                "id": 1,
                "login_id": "jdoe1",
                "full_name": "Jane Doe"
            }
        }
        
        Response (not authenticated):
        {
            "authenticated": false
        }
    """
    user = get_authenticated_user()
    if user is not None:
        return jsonify({
            'authenticated': True,
            'user': user.to_dict()
        })

    return jsonify({'authenticated': False})


@auth.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint for monitoring and load balancers
    
    Returns:
        JSON response with status, the release version, and the git commit
        the image was built from. Release images bake that commit into
        APP_REVISION; source builds pass none, so the image sets it empty
        and revision is null.

    Example:
        GET /auth/health

        Response:
        {
            "status": "ok",
            "timestamp": "2026-01-28T12:00:00.123456",
            "version": "1.0.0",
            "revision": "0123456789abcdef0123456789abcdef01234567"
        }
    """
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'version': __version__,
        'revision': os.environ.get('APP_REVISION') or None,
    })
