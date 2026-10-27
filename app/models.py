"""
Database models for VM tracking
Defines the VMRecord model for storing VM metadata and lifecycle info
"""

import secrets
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy import Index, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import validates
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def _iso_local(value):
    """ISO-8601 with an explicit UTC offset, or None.

    Every datetime column here is written with `datetime.now()` (naive,
    host-local — see CLAUDE.md). `.astimezone()` on a naive value assumes
    the host timezone and attaches its offset, which is what the browser
    needs in order to parse the instant correctly. Serializing a bare
    naive string makes JS reinterpret it in the *viewer's* timezone.
    """
    return value.astimezone().isoformat() if value else None

class VMRecord(db.Model):
    """
    Database model for tracking created VMs
    
    Attributes:
        id (int): Primary key
        vmid (int): Proxmox VM ID (unique)
        vm_name (str): VM hostname/name (not unique; same student re-launching a lab reuses the name)
        vm_type (str): 'lxc' for containers or 'qemu' for VMs
        node (str): Proxmox node where VM is located
        template_name (str): Name of the template used to create this VM
        student_name (str): Student who owns this VM
        memory (int): RAM allocated in MB
        cores (int): CPU cores allocated
        storage (int): Storage allocated in GB
        ip_address (str): Primary IP address of the VM
        created_at (datetime): When the VM was created
        expires_at (datetime): When the VM should be auto-destroyed
        destroyed (bool): Whether the VM has been destroyed
        destroyed_at (datetime): When the VM was destroyed
    """
    
    __tablename__ = 'vm_records'
    
    # Primary key
    id = db.Column(db.Integer, primary_key=True)
    
    # VM identification
    vmid = db.Column(db.Integer, unique=True, nullable=False, index=True)
    vm_name = db.Column(db.String(255), nullable=False, index=True)
    vm_type = db.Column(db.String(50), nullable=False)  # 'lxc' or 'qemu'
    node = db.Column(db.String(255), nullable=False, index=True)
    
    # Template and ownership
    template_name = db.Column(db.String(255), nullable=False)
    student_name = db.Column(db.String(255), nullable=False, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)  # Link to user who created it
    
    # Resource specifications
    memory = db.Column(db.Integer)  # in MB
    cores = db.Column(db.Integer)
    storage = db.Column(db.Integer)  # in GB
    
    # Network information
    ip_address = db.Column(db.String(50))
    is_entry_point = db.Column(db.Boolean, default=False)  # Whether this VM is the entry point in its deployment group

    # Group ownership (populated for group-mode deployments; NULL for individual-mode)
    owner_group_id = db.Column(db.Integer, db.ForeignKey('student_groups.id'),
                               nullable=True, index=True)

    # KOTH game ownership (populated for VMs that belong to a KOTH game deployment)
    koth_game_id = db.Column(db.Integer, db.ForeignKey('koth_games.id'),
                             nullable=True, index=True)

    # Lifecycle tracking
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    destroyed = db.Column(db.Boolean, default=False, index=True)
    destroyed_at = db.Column(db.DateTime)
    deletion_retry_count = db.Column(db.Integer, default=0)  # Track failed deletion attempts
    deletion_last_attempt_at = db.Column(db.DateTime, nullable=True)
    deletion_alert_sent = db.Column(db.Boolean, default=False, nullable=False)

    owner_group = db.relationship('StudentGroup', foreign_keys=[owner_group_id])

    def to_dict(self):
        """Convert model to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'vmid': self.vmid,
            'vm_name': self.vm_name,
            'vm_type': self.vm_type,
            'node': self.node,
            'template_name': self.template_name,
            'student_name': self.student_name,
            'memory': self.memory,
            'cores': self.cores,
            'storage': self.storage,
            'ip_address': self.ip_address,
            'created_at': self.created_at.isoformat(),
            'expires_at': self.expires_at.isoformat(),
            'is_entry_point': self.is_entry_point,
            'owner_group_id': self.owner_group_id,
            'destroyed': self.destroyed,
            'deletion_retry_count': self.deletion_retry_count,
            'deletion_last_attempt_at': (
                self.deletion_last_attempt_at.isoformat()
                if self.deletion_last_attempt_at else None
            ),
            'deletion_alert_sent': self.deletion_alert_sent,
            'destroyed_at': self.destroyed_at.isoformat() if self.destroyed_at else None
        }
    
    def __repr__(self):
        """String representation of the VM record"""
        return f"<VMRecord {self.vmid}: {self.vm_name} ({self.vm_type})>"


class User(db.Model):
    """
    Database model for user authentication
    
    Attributes:
        id (int): Primary key
        login_id (str): Student login ID (from CSV, e.g., jdoe1)
        password_hash (str): Hashed password
        email (str): Optional email address
        full_name (str): Full name of the student
        is_admin (bool): Whether the user has admin privileges
        entry_vlan_tag (int): Entry point VLAN tag (100-200), IP: 10.0.<vlan>.x
        internal_vlan_tag (int): Internal VLAN tag (200-300) for all VMs
        created_at (datetime): When the account was created
        last_login (datetime): Last login timestamp
    """
    
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    login_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    full_name = db.Column(db.String(255))
    is_admin = db.Column(db.Boolean, default=False, nullable=False, index=True)
    entry_vlan_tag = db.Column(db.Integer, unique=True, nullable=True, index=True)  # Entry point VLAN (100-200), IP: 10.0.<vlan>.x
    internal_vlan_tag = db.Column(db.Integer, unique=True, nullable=True, index=True)  # Internal VLAN (200-300) for all VMs
    headscale_key = db.Column(db.String(255), nullable=True)  # Headscale/Tailscale auth key
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    last_login = db.Column(db.DateTime)
    disabled = db.Column(db.Boolean, default=False, nullable=False, index=True)
    session_version = db.Column(
        db.Integer, default=0, server_default='0', nullable=False
    )

    def set_password(self, password):
        """Hash and set the user's password"""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Verify a password against the stored hash"""
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        """Convert model to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'login_id': self.login_id,
            'email': self.email,
            'full_name': self.full_name,
            'is_admin': self.is_admin,
            'entry_vlan_tag': self.entry_vlan_tag,
            'internal_vlan_tag': self.internal_vlan_tag,
            'headscale_key': self.headscale_key,
            'created_at': self.created_at.isoformat(),
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'disabled': self.disabled
        }

    def __repr__(self):
        """String representation of the user"""
        return f"<User {self.login_id}>"


class StudentWhitelist(db.Model):
    """
    Database model for whitelisted students (from Canvas CSV)
    
    Attributes:
        id (int): Primary key
        login_id (str): Student login ID / SIS Login ID (e.g., jdoe1)
        sis_user_id (str): SIS User ID from Canvas (e.g., 100000001)
        full_name (str): Full name of the student
        student_id (str): Canvas student ID
        section (str): Course section
        added_at (datetime): When added to whitelist
    """

    __tablename__ = 'student_whitelist'

    id = db.Column(db.Integer, primary_key=True)
    login_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    sis_user_id = db.Column(db.String(100), nullable=True, index=True)
    full_name = db.Column(db.String(255))
    student_id = db.Column(db.String(100))
    section = db.Column(db.String(100))
    entry_vlan_tag = db.Column(db.Integer, unique=True, nullable=True)  # Pre-assigned entry VLAN (100-200)
    internal_vlan_tag = db.Column(db.Integer, unique=True, nullable=True)  # Pre-assigned internal VLAN (200-300)
    headscale_key = db.Column(db.String(255), nullable=True)  # Headscale/Tailscale auth key
    added_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    def to_dict(self):
        """Convert model to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'login_id': self.login_id,
            'sis_user_id': self.sis_user_id,
            'full_name': self.full_name,
            'student_id': self.student_id,
            'section': self.section,
            'entry_vlan_tag': self.entry_vlan_tag,
            'internal_vlan_tag': self.internal_vlan_tag,
            'headscale_key': self.headscale_key,
            'added_at': self.added_at.isoformat()
        }
    
    def __repr__(self):
        """String representation of the whitelisted student"""
        return f"<StudentWhitelist {self.login_id}: {self.full_name}>"


class PasswordResetToken(db.Model):
    """A single-use password reset link an administrator issued for a user.

    Only the SHA-256 of the token is stored: the link is the credential, so
    a database dump or backup must not hand out working links. A fast hash
    is enough because the token carries 32 random bytes; there is nothing to
    brute-force the way there is with a password.

    One row per user, enforced by the database: issuing a link replaces the
    user's row, so an earlier link stops working however two issuers
    interleave. A used row stays until the next link replaces it.
    """

    __tablename__ = 'password_reset_tokens'
    __table_args__ = (
        # Also the index the user_id foreign key needs on MySQL.
        db.UniqueConstraint('user_id', name='uq_password_reset_tokens_user_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    # scripts/purge_roster.sql deletes student users at term changeover; a
    # plain foreign key would stop that DELETE at the first student who ever
    # got a link. Links die with their user; the issuing admin may go too.
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                   nullable=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<PasswordResetToken {self.id} user={self.user_id}>'


class StudentLabAssignment(db.Model):
    """
    Controls per-student visibility of deployment groups (lab restriction system).

    The restriction model is opt-in, not whitelist-based:
      - If a deployment group has ZERO rows in this table, it is unrestricted
        and visible to every student.
      - If a deployment group has ANY rows, it becomes restricted: only the
        students listed here can see (and deploy from) that group.
      - Admins always bypass this filter entirely.

    This means adding the first assignment for a group is a breaking change for
    all other students -- they will lose visibility of that group. Use
    bulk_assign to assign all intended students at once, or clear_assignments
    to remove restrictions.

    The unique constraint (user_id, deployment_group_name) prevents duplicate
    assignments. deployment_group_name matches the display_name field in
    templates.yaml.
    """

    __tablename__ = 'student_lab_assignments'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    deployment_group_name = db.Column(db.String(255), nullable=False)  # matches display_name from templates.yaml
    assigned_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    assigned_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Relationships
    user = db.relationship('User', foreign_keys=[user_id], backref='lab_assignments')
    assigner = db.relationship('User', foreign_keys=[assigned_by])

    __table_args__ = (
        db.UniqueConstraint('user_id', 'deployment_group_name', name='uq_user_deployment'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'deployment_group_name': self.deployment_group_name,
            'assigned_at': self.assigned_at.isoformat(),
            'assigned_by': self.assigned_by,
            'user_login_id': self.user.login_id if self.user else None,
            'user_full_name': self.user.full_name if self.user else None,
        }

    def __repr__(self):
        return f'<StudentLabAssignment user={self.user_id} group={self.deployment_group_name}>'


class AdminAlert(db.Model):
    """
    Database model for tracking admin alerts (e.g., failed VM deletions)
    
    Attributes:
        id (int): Primary key
        alert_type (str): Type of alert (e.g., 'vm_deletion_failed')
        severity (str): Alert severity ('warning', 'error', 'critical')
        message (str): Alert message
        vmid (int): Related VM ID (if applicable)
        vm_name (str): Related VM name (if applicable)
        details (str): Additional details (JSON or text)
        created_at (datetime): When alert was created
        acknowledged (bool): Whether alert has been acknowledged
        acknowledged_at (datetime): When alert was acknowledged
    """
    
    __tablename__ = 'admin_alerts'
    
    id = db.Column(db.Integer, primary_key=True)
    alert_type = db.Column(db.String(100), nullable=False, index=True)
    severity = db.Column(db.String(50), nullable=False, default='warning')  # warning, error, critical
    message = db.Column(db.Text, nullable=False)
    vmid = db.Column(db.Integer, index=True)
    vm_name = db.Column(db.String(255))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    acknowledged = db.Column(db.Boolean, default=False, index=True)
    acknowledged_at = db.Column(db.DateTime)
    
    def to_dict(self):
        """Convert model to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'alert_type': self.alert_type,
            'severity': self.severity,
            'message': self.message,
            'vmid': self.vmid,
            'vm_name': self.vm_name,
            'details': self.details,
            'created_at': self.created_at.isoformat(),
            'acknowledged': self.acknowledged,
            'acknowledged_at': self.acknowledged_at.isoformat() if self.acknowledged_at else None
        }
    
    def __repr__(self):
        """String representation of the admin alert"""
        return f'<AdminAlert {self.id} [{self.severity}] {self.alert_type}>'


class StudentGroup(db.Model):
    """
    A cohort of students who share group-mode lab deployments.

    A group owns its own VLAN pair (entry + internal), distinct from any user
    VLAN. Members collaborate on a single instance of any group-mode
    deployment_group; see StudentGroupMember.

    The `name` field is the canonical slug used in Proxmox VM names
    (`group-{name}-{template}`) and MUST NOT be changed after creation —
    changing it would orphan existing VMs. Callers should treat it as
    immutable at the route layer. `display_name` is the human label and may be
    edited freely.
    """

    __tablename__ = 'student_groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    entry_vlan_tag = db.Column(db.Integer, unique=True, nullable=True, index=True)
    internal_vlan_tag = db.Column(db.Integer, unique=True, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'entry_vlan_tag': self.entry_vlan_tag,
            'internal_vlan_tag': self.internal_vlan_tag,
            'created_at': self.created_at.isoformat(),
            'created_by': self.created_by,
        }

    def __repr__(self):
        return f'<StudentGroup {self.name}>'


class StudentGroupMember(db.Model):
    """
    Links a user to at most one student group.

    The unique constraint on user_id is the load-bearing rule: a student can
    only belong to one group at a time. Changing group membership requires
    removing the row and creating a new one.
    """

    __tablename__ = 'student_group_members'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('student_groups.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        unique=True, nullable=False, index=True)
    added_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    added_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    group = db.relationship('StudentGroup', backref=db.backref('members', cascade='all, delete-orphan'))
    user = db.relationship('User', foreign_keys=[user_id], backref='group_membership')
    adder = db.relationship('User', foreign_keys=[added_by])

    def to_dict(self):
        """Convert model to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'group_id': self.group_id,
            'user_id': self.user_id,
            'added_at': self.added_at.isoformat(),
            'added_by': self.added_by,
            'user_login_id': self.user.login_id if self.user else None,
            'user_full_name': self.user.full_name if self.user else None,
        }

    def __repr__(self):
        """String representation of the group member"""
        return f'<StudentGroupMember group={self.group_id} user={self.user_id}>'


class KothGame(db.Model):
    """A King of the Hill game. At most one row may have is_active=True."""
    __tablename__ = 'koth_games'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    deployment_group_name = db.Column(db.String(255), nullable=False)
    entry_vlan_tag = db.Column(db.Integer, nullable=False, index=True)
    internal_vlan_tag = db.Column(db.Integer, nullable=False, index=True)
    # draft -> deploying -> active -> ended (deploying is transient while
    # Proxmox clones VMs; reverts to draft on failure or stuck-on-boot recovery)
    status = db.Column(db.String(20), nullable=False, default='draft')
    is_active = db.Column(db.Boolean, nullable=False, default=False, index=True)
    # Sentinel column kept in sync with is_active by @validates below.
    # `1` when is_active is True, NULL otherwise. A UNIQUE index on this column
    # enforces "at most one active game" on both MySQL and SQLite, since both
    # engines allow multiple NULL values in a UNIQUE index but only one `1`.
    # We can't use a partial index (`WHERE is_active = 1`) because MySQL
    # doesn't support them.
    active_singleton = db.Column(db.Boolean, nullable=True)
    pause_other_deployments = db.Column(db.Boolean, nullable=False, default=False)
    show_scoreboard = db.Column(db.Boolean, nullable=False, default=True)
    show_network_map = db.Column(db.Boolean, nullable=False, default=True)
    # Scoring tunables (per-game overrides)
    scan_interval_sec = db.Column(db.Integer, nullable=False, default=12)
    claim_cooldown_sec = db.Column(db.Integer, nullable=False, default=30)
    # How the scorer learns who owns a box.
    #   'file'   — read `KothBox.claim_file_path` through the Proxmox guest
    #              agent. One HTTPS API call per box per tick.
    #   'beacon' — read the owner token off a TCP listener the team runs on
    #              `claim_port`. One extra local connect per box per tick,
    #              issued in the same bounded fan-out as the service
    #              probes, and zero Proxmox API calls.
    #   'both'   — try the beacon first, fall back to the file. Lets Linux
    #              boxes use the cheap path while Windows boxes keep the
    #              file, and gives students a way back in if their listener
    #              dies. Beacon wins on conflict because it is live proof
    #              the team still controls the box.
    # Server default is 'file' so games created before this column existed
    # keep behaving exactly as they did; new games default to
    # KOTH_DEFAULT_CLAIM_MODE (see app/config.py).
    claim_mode = db.Column(db.String(10), nullable=False, default='file')
    claim_port = db.Column(db.Integer, nullable=False, default=31337)
    box_ownership_score = db.Column(db.Float, nullable=False, default=1.0)
    port_open_score = db.Column(db.Float, nullable=False, default=3.0)
    exp_scoring_enabled = db.Column(db.Boolean, nullable=False, default=True)
    exp_val = db.Column(db.Integer, nullable=False, default=60)
    scoring_iteration = db.Column(db.Integer, nullable=False, default=0)
    started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    # Optional time-boxing. NULL duration_minutes = unlimited; otherwise the
    # scheduler tick auto-ends the game when datetime.now() >= ends_at.
    # ends_at is computed when the game becomes active (= started_at + duration)
    # and recomputed on PATCH if duration changes mid-game.
    duration_minutes = db.Column(db.Integer, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    creator = db.relationship('User', foreign_keys=[created_by])

    __table_args__ = (
        Index('uq_koth_active_singleton', 'active_singleton', unique=True),
        CheckConstraint("status IN ('draft','deploying','active','ended')",
                        name='ck_koth_games_status'),
    )

    @validates('is_active')
    def _sync_active_singleton(self, key, value):
        # Keep active_singleton in lockstep with is_active so the UNIQUE
        # backstop on koth_games.active_singleton enforces the "one active
        # game" invariant without depending on partial-index support.
        self.active_singleton = True if value else None
        return value

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'deployment_group_name': self.deployment_group_name,
            'entry_vlan_tag': self.entry_vlan_tag,
            'internal_vlan_tag': self.internal_vlan_tag,
            'status': self.status, 'is_active': self.is_active,
            'pause_other_deployments': self.pause_other_deployments,
            'show_scoreboard': self.show_scoreboard,
            'show_network_map': self.show_network_map,
            'scan_interval_sec': self.scan_interval_sec,
            'claim_cooldown_sec': self.claim_cooldown_sec,
            'claim_mode': self.claim_mode,
            'claim_port': self.claim_port,
            'box_ownership_score': self.box_ownership_score,
            'port_open_score': self.port_open_score,
            'exp_scoring_enabled': self.exp_scoring_enabled,
            'exp_val': self.exp_val,
            'scoring_iteration': self.scoring_iteration,
            # started_at / ended_at / ends_at are naive LOCAL datetimes
            # (datetime.now(), the repo-wide convention). `.astimezone()`
            # on a naive value attaches the host's UTC offset, so the
            # browser's `new Date(...)` lands on the right instant. The
            # previous code appended a bare 'Z' to a value that the deploy
            # path wrote with utcnow() while the tick compared it against
            # now() — the countdown looked right but the server-side
            # auto-end fired a whole tz offset late, leaving the VMs up.
            'started_at': _iso_local(self.started_at),
            'ended_at': _iso_local(self.ended_at),
            'duration_minutes': self.duration_minutes,
            'ends_at': _iso_local(self.ends_at),
            'created_at': _iso_local(self.created_at),
        }

    def __repr__(self):
        return f'<KothGame {self.id} {self.name!r} [{self.status}]>'


class KothTeam(db.Model):
    """
    A competing team within a KothGame, one row per StudentGroup binding.

    Each team links a StudentGroup to a KothGame with a unique
    `submission_token`. Teams prove ownership of a box with that token,
    either by writing it into the box's claim file or by serving it from a
    TCP beacon — see `KothGame.claim_mode`. The `color` is drawn from the
    team palette and is unique per game. The token is opaque and rotatable
    by an admin if it leaks.

    `score` is the authoritative running total from box ownership and open
    services. It lives here so reading the scoreboard is one row per team
    instead of a "latest snapshot" lookup per team against a table that
    grows by `teams × ticks` all game. Accepted-flag bonuses are NOT
    folded in here — they are summed from KothFlagSubmission at read time,
    which is what keeps the tick and a concurrent flag submission from
    clobbering each other's writes.
    """
    __tablename__ = 'koth_teams'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('koth_games.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    student_group_id = db.Column(db.Integer, db.ForeignKey('student_groups.id'),
                                 nullable=False, index=True)
    color = db.Column(db.String(20), nullable=False)
    submission_token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    display_name = db.Column(db.String(255), nullable=False)
    score = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    game = db.relationship('KothGame', backref=db.backref('teams', cascade='all, delete-orphan'))
    student_group = db.relationship('StudentGroup')

    __table_args__ = (
        UniqueConstraint('game_id', 'student_group_id', name='uq_koth_team_group_per_game'),
        UniqueConstraint('game_id', 'color', name='uq_koth_team_color_per_game'),
    )

    @staticmethod
    def new_token():
        return secrets.token_urlsafe(32)

    def to_dict(self, include_token=False):
        d = {
            'id': self.id, 'game_id': self.game_id,
            'student_group_id': self.student_group_id,
            'color': self.color, 'display_name': self.display_name,
        }
        if include_token:
            d['submission_token'] = self.submission_token
        return d

    def __repr__(self):
        return f'<KothTeam game={self.game_id} color={self.color} {self.display_name!r}>'


class KothBox(db.Model):
    """
    Mirrors one VM in the KOTH lab as a claimable box.

    `vm_record_id` links to the cloned `VMRecord` for this box. `services` is
    a JSON list of `{name, port, status}` dicts updated by the port scanner
    each scoring tick. `owner_team_id` flips when a team claims the box (and
    `claimed_at` is updated). `position` (x,y) is auto-laid-out at deploy
    time and persisted so the network map is stable across page reloads.
    """
    __tablename__ = 'koth_boxes'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('koth_games.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    vm_record_id = db.Column(db.Integer, db.ForeignKey('vm_records.id'),
                             nullable=True, index=True)
    name = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=True)
    ips = db.Column(db.JSON, nullable=False, default=list)
    services = db.Column(db.JSON, nullable=False, default=list)
    owner_team_id = db.Column(db.Integer, db.ForeignKey('koth_teams.id', ondelete='SET NULL'),
                              nullable=True, index=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    position = db.Column(db.JSON, nullable=True)
    # Path the scoring tick reads via the QEMU guest agent to determine
    # current ownership. Default `/koth/claim` for Linux templates;
    # Windows templates override to `C:\koth\claim` via templates.yaml.
    claim_file_path = db.Column(db.String(255), nullable=True)

    game = db.relationship('KothGame', backref=db.backref('boxes', cascade='all, delete-orphan'))
    owner = db.relationship('KothTeam', foreign_keys=[owner_team_id])
    vm_record = db.relationship('VMRecord')

    def to_dict(self):
        return {
            'id': self.id, 'game_id': self.game_id,
            'vm_record_id': self.vm_record_id,
            'name': self.name, 'display_name': self.display_name or self.name,
            'ips': self.ips or [], 'services': self.services or [],
            'owner_team_id': self.owner_team_id,
            'claimed_at': self.claimed_at.isoformat() if self.claimed_at else None,
            'position': self.position,
            'claim_file_path': self.claim_file_path,
        }

    def __repr__(self):
        return f'<KothBox game={self.game_id} {self.name!r} owner={self.owner_team_id}>'


class KothFlag(db.Model):
    """
    Admin-defined bonus flag awarded when a player submits the matching
    `value` (typically a UUID) through the player page form
    (`POST /koth/api/flags/submit`, session-authenticated).

    `box_id` may be null, meaning "any box" -- the flag is valid regardless
    of which box a team is currently on. When set, the flag is logically
    tied to a specific box. Plaintext storage of `value` is intentional so
    that admins can edit flags after creation; the value is never exposed
    to students. `points` is the score delta applied on accepted submission.
    """
    __tablename__ = 'koth_flags'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('koth_games.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    box_id = db.Column(db.Integer, db.ForeignKey('koth_boxes.id', ondelete='SET NULL'),
                       nullable=True)
    name = db.Column(db.String(255), nullable=False)
    value = db.Column(db.String(512), nullable=False)
    points = db.Column(db.Float, nullable=False, default=0)
    submission_count_limit = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    game = db.relationship('KothGame', backref=db.backref('flags', cascade='all, delete-orphan'))
    box = db.relationship('KothBox')

    def __repr__(self):
        return f'<KothFlag game={self.game_id} {self.name!r}>'


class KothFlagSubmission(db.Model):
    """
    Append-only record of every flag submission, accepted or not.

    The partial-unique index on `(flag_id, team_id) WHERE accepted` enforces
    at most one accepted submission per (flag, team) pair. Rejected
    submissions are also recorded for audit/anti-abuse purposes (and are not
    constrained by the partial unique). New rows are inserted by the
    session-authed `submit_flag` route in `app/routes/koth_player.py`;
    rows are never updated in place.
    """
    __tablename__ = 'koth_flag_submissions'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('koth_games.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    flag_id = db.Column(db.Integer, db.ForeignKey('koth_flags.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey('koth_teams.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    submitted_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    accepted = db.Column(db.Boolean, nullable=False, default=False)
    submitted_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    flag = db.relationship('KothFlag')
    team = db.relationship('KothTeam')
    submitted_by = db.relationship('User', foreign_keys=[submitted_by_user_id])

    __table_args__ = (
        # one accepted submission per (flag, team)
        Index('uq_koth_flag_sub_accepted', 'flag_id', 'team_id',
              unique=True, sqlite_where=db.text('accepted = 1')),
    )

    def __repr__(self):
        return f'<KothFlagSubmission flag={self.flag_id} team={self.team_id} accepted={self.accepted}>'


class KothClaimEvent(db.Model):
    """
    Append-only audit log of scoring/claim activity for a KothGame.

    `event_type` is one of `claim`, `reclaim`, `flag`, `score_recompute`,
    enforced via a CheckConstraint. `meta` (JSON) carries event-specific
    extras such as manual flag, source IP, flag_id, etc. The column is named
    `meta` instead of `metadata` because `metadata` is reserved on
    `db.Model`. Rows persist even if the related box or team is deleted
    (FKs SET NULL) so the audit history is preserved.
    """
    __tablename__ = 'koth_claim_events'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('koth_games.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    box_id = db.Column(db.Integer, db.ForeignKey('koth_boxes.id', ondelete='SET NULL'),
                       nullable=True)
    team_id = db.Column(db.Integer, db.ForeignKey('koth_teams.id', ondelete='SET NULL'),
                        nullable=True)
    event_type = db.Column(db.String(20), nullable=False)  # claim|reclaim|flag|score_recompute
    points_delta = db.Column(db.Float, nullable=False, default=0)
    meta = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)

    team = db.relationship('KothTeam')
    box = db.relationship('KothBox')

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('claim','reclaim','flag','score_recompute')",
            name='ck_koth_claim_event_type',
        ),
    )

    def __repr__(self):
        return f'<KothClaimEvent {self.event_type} team={self.team_id} box={self.box_id}>'


class KothScoreSnapshot(db.Model):
    """
    Per-team score history, written at a fixed wall-clock cadence.

    This is the timeline record only. The live scoreboard reads
    `KothTeam.score`, so nothing on a request path touches this table —
    which is why rows are written every `KOTH_SNAPSHOT_INTERVAL_SEC`
    rather than on every tick. At a 12 s tick that is a 5x reduction in
    rows (22 teams x 3 h went from ~19,800 rows to ~3,960) with no loss
    of anything a chart would plot.

    Rows are append-only and are hard-deleted with the game.
    """
    __tablename__ = 'koth_score_snapshots'
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('koth_games.id', ondelete='CASCADE'),
                        nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('koth_teams.id', ondelete='CASCADE'),
                        nullable=False)
    score = db.Column(db.Float, nullable=False, default=0)
    iteration = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    team = db.relationship('KothTeam')

    __table_args__ = (
        Index('ix_koth_snap_game_iter', 'game_id', 'iteration'),
        # Per-team timeline reads filter on (game_id, team_id); without
        # team_id in the index MySQL filesorts the whole game's history.
        Index('ix_koth_snap_game_team_iter', 'game_id', 'team_id', 'iteration'),
    )

    def __repr__(self):
        return f'<KothScoreSnapshot game={self.game_id} team={self.team_id} iter={self.iteration} score={self.score}>'


# ---- Cross-worker coordination (see app/coordination.py) -------------------
#
# Leases and failure stamps are epoch seconds from time.time(), not naive
# local DateTimes: one worker writes them and another compares them, and a
# lease must not stretch or shrink by an hour across a DST change.

class CoordinationState(db.Model):
    """Named rows that serialize a check-and-reserve across workers.

    Each operation locks its row first (SELECT ... FOR UPDATE on MySQL).
    `vmid_allocator` also stores the VMID high-water mark, so a released
    block is never handed out again while its clone may still exist.
    """

    __tablename__ = 'coordination_state'

    name = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.BigInteger, nullable=False, default=0)


class NodeMemoryReservation(db.Model):
    """Memory promised to a create between node selection and its VMRecord."""

    __tablename__ = 'node_memory_reservations'

    reservation_id = db.Column(db.String(64), primary_key=True)
    node = db.Column(db.String(255), nullable=False)
    memory_bytes = db.Column(db.BigInteger, nullable=False)
    expires_at = db.Column(db.Double, nullable=False)

    __table_args__ = (
        Index('ix_node_memory_reservations_expires_at', 'expires_at'),
    )


class VMIDReservation(db.Model):
    """One VMID held for a create until its VMRecord commits.

    The primary key is the VMID itself, so the database refuses a duplicate
    allocation even if the allocator's lock were bypassed.
    """

    __tablename__ = 'vmid_reservations'

    vmid = db.Column(db.Integer, primary_key=True, autoincrement=False)
    reservation_id = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.Double, nullable=False)

    __table_args__ = (
        Index('ix_vmid_reservations_reservation_id', 'reservation_id'),
        Index('ix_vmid_reservations_expires_at', 'expires_at'),
    )


class PasswordResetFailure(db.Model):
    """One failed self-service reset attempt, for the sliding-window limiter.

    Keys are stored as SHA-256: they are attacker-supplied login IDs and
    client addresses, of any length, and the table needs only equality.
    """

    __tablename__ = 'password_reset_failures'

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(16), nullable=False)
    key_hash = db.Column(db.String(64), nullable=False)
    failed_at = db.Column(db.Double, nullable=False)

    __table_args__ = (
        Index('ix_password_reset_failures_key', 'scope', 'key_hash', 'failed_at'),
        Index('ix_password_reset_failures_failed_at', 'failed_at'),
    )
