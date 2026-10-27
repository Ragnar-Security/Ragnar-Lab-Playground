"""Admin-only KOTH routes (game CRUD, settings, flags, tokens)."""

from __future__ import annotations  # lazy annotations — threading.Lock is a
                                    # factory function in CPython <3.13, so
                                    # `threading.Lock | None` blows up at
                                    # module load without this.

import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional
from flask import request, jsonify, render_template

from ..auth import login_required, admin_required, get_authenticated_user
from ..models import (
    db, StudentGroup, KothGame, KothTeam, KothBox, KothFlag,
    KothFlagSubmission, KothClaimEvent, VMRecord,
)
from ..config import KOTH_CLAIM_MODES, KOTH_TEAM_COLORS, get_koth_defaults
from ..vlan_allocator import validate_vlan_free, VlanInUse
from ..template_manager import (
    load_template_config, get_koth_groups, get_koth_services,
    get_koth_claim_file,
)
from ..koth_layout import compute_layout
from .koth import koth_bp
from .vm_creation import deploy_bundle, DeployFailed, cleanup_vms

logger = logging.getLogger(__name__)


# Per-game in-process locks to reject concurrent deploy/redeploy/end/cleanup
# attempts within a single Flask worker. Multi-worker setups need a DB-level
# lock (status='deploying' is the canonical marker for cross-worker sync).
_game_locks: Dict[int, "threading.Lock"] = {}
_game_locks_mutex = threading.Lock()


def _acquire_game_lock(game_id: int) -> Optional["threading.Lock"]:
    """Acquire the per-game lock without blocking. Returns the lock if
    acquired (caller must release), or None if another request is holding it."""
    with _game_locks_mutex:
        lock = _game_locks.setdefault(game_id, threading.Lock())
    return lock if lock.acquire(blocking=False) else None


def _destroy_game_vms(game_id: int) -> int:
    """Destroy all KOTH VMs for this game — both the ones we have a
    VMRecord for AND any stranded VMs in Proxmox whose record never landed
    (e.g. the deploy worker was SIGTERM'd mid-clone).

    Stranded VMs are found by name prefix `koth-<game_id>-` (set by
    `vm_creation.build_koth_vm_name`). This is the only safe recovery for
    SIGTERM'd deploys, since SIGTERM doesn't trigger our Python except
    handlers — Proxmox is the source of truth here, not our DB.

    Idempotent — VMs already destroyed (or already absent in Proxmox) are
    skipped. Returns the total count of VMs destroyed.
    """
    from ..proxmox_service import find_vms_by_name_prefix

    # 1. Destroy any VMs we have a record for.
    record_vms = VMRecord.query.filter_by(koth_game_id=game_id, destroyed=False).all()
    record_keys = {(v.vmid, v.node) for v in record_vms}
    record_infos = [
        {
            'vmid': v.vmid,
            'node': v.node,
            'vm_type': v.vm_type,
            'vm_name': v.vm_name,
        }
        for v in record_vms
    ]

    # 2. Sweep Proxmox for stranded VMs matching the name prefix that we
    # have NO record for. These are the SIGTERM-orphaned ones.
    stranded_infos = []
    try:
        for vm in find_vms_by_name_prefix(f"koth-{game_id}-"):
            if (vm['vmid'], vm['node']) not in record_keys:
                stranded_infos.append({
                    'vmid': vm['vmid'],
                    'node': vm['node'],
                    'vm_type': vm['vm_type'],
                    'vm_name': vm.get('name'),
                })
    except Exception:
        logger.exception(
            "_destroy_game_vms: Proxmox stranded-VM sweep failed for game %d "
            "(continuing with VMRecord-known VMs only)", game_id)

    all_infos = record_infos + stranded_infos
    if not all_infos:
        return 0
    # cleanup_vms now returns the set of vmids it actually destroyed (or
    # were already 404'd). Mark VMRecord.destroyed only for those — Proxmox
    # can refuse a destroy (lock, snapshot, storage) and the previous code
    # would mark the row anyway, hiding orphans.
    destroyed = cleanup_vms(all_infos)
    now = datetime.now()
    for v in record_vms:
        if v.vmid in destroyed:
            v.destroyed = True
            v.destroyed_at = now
    db.session.commit()
    missing = [info['vmid'] for info in all_infos if info['vmid'] not in destroyed]
    if missing:
        logger.error(
            "KOTH game %d: %d VM(s) could not be destroyed: %s — VMRecord(s) "
            "left active for retry", game_id, len(missing), missing)
    logger.info(
        "KOTH game %d: destroyed %d/%d VMs (%d from VMRecord, %d stranded)",
        game_id, len(destroyed), len(all_infos), len(record_vms), len(stranded_infos))
    return len(destroyed)


def _refresh_box_ips(game_id: int) -> int:
    """Re-query Proxmox QEMU guest agent for each KothBox's current IPs.

    Boxes get materialized with empty `ips=[]` because VMRecord.ip_address
    is None at clone time (the VM hasn't booted, no DHCP lease yet).
    Without this refresh, KothBox.ips stays empty forever and the
    source-IP claim verification can never succeed.

    Returns the number of boxes whose IPs were updated.
    """
    from .utils import _get_vm_ip_addresses
    from ..proxmox_service import get_proxmox_connection
    boxes = KothBox.query.filter_by(game_id=game_id).all()
    if not boxes:
        return 0
    proxmox = get_proxmox_connection()
    updated = 0
    for box in boxes:
        rec = db.session.get(VMRecord, box.vm_record_id) if box.vm_record_id else None
        if rec is None or rec.destroyed:
            continue
        try:
            ips = _get_vm_ip_addresses(proxmox, rec.node, rec.vmid, rec.vm_type)
        except Exception:
            logger.exception("refresh_box_ips: failed for box %d (vmid=%s)",
                              box.id, rec.vmid)
            continue
        # _get_vm_ip_addresses returns ['N/A'] when nothing is available.
        ips = [ip for ip in ips if ip and ip != 'N/A']
        if ips and ips != (box.ips or []):
            box.ips = ips
            updated += 1
    if updated:
        db.session.commit()
        logger.info("KOTH game %d: refreshed IPs on %d box(es)", game_id, updated)
    return updated


def _wipe_ended_games() -> int:
    """Hard-delete every `status='ended'` KothGame and ALL of its child rows.

    Per project policy, KOTH game history is not preserved across games —
    the admin only ever sees the current game's scoreboard. So everything
    goes: KothTeam, KothBox, KothFlag, KothFlagSubmission, KothClaimEvent,
    KothScoreSnapshot, AND the VMRecord rows that pointed at the game.

    Deletions are explicit (not relying on relationship cascade config) so
    we don't depend on FK ON DELETE settings being present in MySQL.

    Order matters because of two FK chains MySQL won't auto-resolve:
      koth_boxes.vm_record_id  -> vm_records.id   (no ON DELETE)
      vm_records.koth_game_id  -> koth_games.id   (no ON DELETE)

    So we delete bottom-up: leaf rows first, then KothBox, then VMRecord,
    then the rest, finally KothGame.

    Returns the number of games deleted.
    """
    from ..models import KothScoreSnapshot
    ended_ids = [g.id for g in KothGame.query.filter_by(status='ended').all()]
    if not ended_ids:
        return 0
    # 1. Leaf rows (depend on flags/teams/boxes via various ON DELETE actions
    # — safe to delete first regardless of those actions).
    KothScoreSnapshot.query.filter(KothScoreSnapshot.game_id.in_(ended_ids)).delete(synchronize_session=False)
    KothClaimEvent.query.filter(KothClaimEvent.game_id.in_(ended_ids)).delete(synchronize_session=False)
    KothFlagSubmission.query.filter(KothFlagSubmission.game_id.in_(ended_ids)).delete(synchronize_session=False)
    KothFlag.query.filter(KothFlag.game_id.in_(ended_ids)).delete(synchronize_session=False)
    # 2. KothBox before VMRecord — koth_boxes.vm_record_id is the FK that
    # blocks the VMRecord delete with errno 1451 if we go the other way.
    KothBox.query.filter(KothBox.game_id.in_(ended_ids)).delete(synchronize_session=False)
    # 3. VMRecord now that no KothBox references it.
    VMRecord.query.filter(VMRecord.koth_game_id.in_(ended_ids)).delete(synchronize_session=False)
    # 4. Teams (KothBox.owner_team_id is SET NULL; nothing else still
    # references teams at this point).
    KothTeam.query.filter(KothTeam.game_id.in_(ended_ids)).delete(synchronize_session=False)
    # 5. Finally the parent game rows.
    KothGame.query.filter(KothGame.id.in_(ended_ids)).delete(synchronize_session=False)
    db.session.commit()
    return len(ended_ids)


def _compute_ends_at(game) -> Optional[datetime]:
    """Return the datetime the game should auto-end, or None if unlimited."""
    if game.duration_minutes is None or game.started_at is None:
        return None
    return game.started_at + timedelta(minutes=game.duration_minutes)


@koth_bp.route('/admin/koth', methods=['GET'])
@login_required
@admin_required
def admin_page():
    return render_template('koth_admin.html')


@koth_bp.route('/koth/api/games', methods=['POST'])
@login_required
@admin_required
def create_game():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    deployment_group_name = (data.get('deployment_group_name') or '').strip()
    try:
        entry = int(data.get('entry_vlan_tag'))
        internal = int(data.get('internal_vlan_tag'))
    except (TypeError, ValueError):
        return jsonify({'error': 'entry_vlan_tag and internal_vlan_tag are required ints'}), 400
    if not name or not deployment_group_name:
        return jsonify({'error': 'name and deployment_group_name are required'}), 400
    if entry == internal:
        return jsonify({
            'error': 'entry_vlan_tag and internal_vlan_tag must be different'
        }), 400
    if KothGame.query.filter_by(is_active=True).first():
        return jsonify({'error': 'A KOTH game is already active. End it first.'}), 409
    # Reject if there's already a draft or in-flight deploy holding either
    # VLAN — the admin should delete or deploy the existing one first.
    existing = (KothGame.query
                .filter(KothGame.status.in_(['draft', 'deploying']))
                .filter((KothGame.entry_vlan_tag.in_([entry, internal])) |
                        (KothGame.internal_vlan_tag.in_([entry, internal])))
                .first())
    if existing:
        return jsonify({
            'error': (f"{existing.status} game {existing.name!r} (id {existing.id}) "
                      "already reserves one of these VLANs; deploy or delete it first")
        }), 409

    # Cap participating teams at the size of the color palette. Each team
    # gets a unique color (UNIQUE(game_id, color) at the DB level), so we
    # can't have more teams than colors. Catch this BEFORE the wipe / VM
    # work below so a too-big request doesn't have side effects.
    team_group_ids = data.get('team_group_ids') or []
    if len(team_group_ids) > len(KOTH_TEAM_COLORS):
        return jsonify({
            'error': (
                f'too many teams: {len(team_group_ids)} requested but the '
                f'color palette only has {len(KOTH_TEAM_COLORS)} distinct '
                f'colors. Pick fewer teams, or expand KOTH_TEAM_COLORS in '
                f'app/config.py.'),
            'max_teams': len(KOTH_TEAM_COLORS),
        }), 400

    # Optional time-boxing: NULL = unlimited.
    duration_minutes = data.get('duration_minutes')
    if duration_minutes in ('', None):
        duration_minutes = None
    else:
        try:
            duration_minutes = int(duration_minutes)
        except (TypeError, ValueError):
            return jsonify({'error': 'duration_minutes must be an int or null'}), 400
        if duration_minutes <= 0:
            return jsonify({'error': 'duration_minutes must be > 0 (omit for unlimited)'}), 400

    # Wipe any prior `ended` games (and their cascade — boxes, snapshots,
    # claim events, flag submissions, teams, flags) before creating the new
    # one. The user only wants to see the *current* scoreboard; old games
    # accumulate as dead rows nothing renders. Also clears stranded
    # destroyed VMRecords' game_id FK (set null via cascade isn't configured
    # everywhere, so we drop them outright).
    wiped_ended = _wipe_ended_games()
    if wiped_ended:
        logger.info("Cleared %d ended KOTH game(s) before creating %r",
                    wiped_ended, name)

    try:
        validate_vlan_free(entry)
        validate_vlan_free(internal)
    except VlanInUse as e:
        return jsonify({'error': str(e)}), 409

    defaults = get_koth_defaults()
    game = KothGame(
        name=name,
        deployment_group_name=deployment_group_name,
        entry_vlan_tag=entry,
        internal_vlan_tag=internal,
        status='draft',
        is_active=False,
        duration_minutes=duration_minutes,
        scan_interval_sec=defaults['scan_interval_sec'],
        claim_cooldown_sec=defaults['claim_cooldown_sec'],
        claim_mode=defaults['claim_mode'],
        claim_port=defaults['claim_port'],
        box_ownership_score=defaults['box_ownership_score'],
        port_open_score=defaults['port_open_score'],
        exp_scoring_enabled=defaults['exp_scoring'],
        exp_val=defaults['exp_val'],
        created_by=get_authenticated_user().id,
    )
    db.session.add(game)
    db.session.flush()

    teams = []
    for i, gid in enumerate(team_group_ids):
        sg = db.session.get(StudentGroup, gid)
        if sg is None:
            db.session.rollback()
            return jsonify({'error': f'unknown student_group {gid}'}), 400
        # Index directly — the team-count cap above guarantees i < len.
        # The previous modulo wrap silently masked the cap and let the
        # 9th team's INSERT fail with a UNIQUE violation on `color`.
        color = KOTH_TEAM_COLORS[i]
        team = KothTeam(
            game_id=game.id, student_group_id=sg.id, color=color,
            submission_token=KothTeam.new_token(),
            display_name=sg.display_name,
        )
        db.session.add(team)
        teams.append(team)
    db.session.commit()
    return jsonify({
        'game': game.to_dict(),
        'teams': [t.to_dict(include_token=True) for t in teams],
    }), 201


@koth_bp.route('/koth/api/games/<int:game_id>', methods=['PATCH'])
@login_required
@admin_required
def update_settings(game_id):
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(silent=True) or {}
    EDITABLE_BOOLS = ['pause_other_deployments', 'show_scoreboard', 'show_network_map',
                      'exp_scoring_enabled']
    # Per-field server-side bounds. The JS UI mirrors these (e.g. min=1
    # max=3600 on the POLL INTERVAL input) but a curl PATCH bypasses the JS,
    # so the cap also lives here. scan_interval_sec=0 would thrash the
    # scheduler; >3600s is silly for any practical KOTH game. Money math
    # (scores) is left wider but still finite — see _validate_finite below.
    EDITABLE_NUM_BOUNDS = {
        'scan_interval_sec':   {'type': int,   'min': 1,    'max': 3600},
        'claim_cooldown_sec':  {'type': int,   'min': 0,    'max': 3600},
        # Privileged ports would need the beacon listener to run as root
        # inside the box, which is the opposite of what we want students
        # doing; keep the claim beacon in the unprivileged range.
        'claim_port':          {'type': int,   'min': 1024, 'max': 65535},
        'box_ownership_score': {'type': float, 'min': 0.0,  'max': 1e6},
        'port_open_score':     {'type': float, 'min': 0.0,  'max': 1e6},
        'exp_val':             {'type': int,   'min': 1,    'max': 1_000_000},
    }
    if 'claim_mode' in data:
        mode = str(data['claim_mode'] or '').strip().lower()
        if mode not in KOTH_CLAIM_MODES:
            return jsonify({
                'error': f"claim_mode must be one of {list(KOTH_CLAIM_MODES)}"
            }), 400
        game.claim_mode = mode
    for k in EDITABLE_BOOLS:
        if k in data:
            setattr(game, k, bool(data[k]))
    for k, spec in EDITABLE_NUM_BOUNDS.items():
        if k not in data:
            continue
        try:
            v = spec['type'](data[k])
        except (TypeError, ValueError):
            return jsonify({'error': f'{k} must be a number'}), 400
        if spec['type'] is float:
            import math
            if not math.isfinite(v):
                return jsonify({'error': f'{k} must be a finite number'}), 400
        if v < spec['min'] or v > spec['max']:
            return jsonify({
                'error': f"{k} must be between {spec['min']} and {spec['max']}"
            }), 400
        setattr(game, k, v)
    if 'name' in data:
        game.name = (data['name'] or '').strip() or game.name
    # Duration is editable any time. NULL or omitted = unlimited.
    # extend_minutes is a convenience: add N minutes to current duration
    # (treats unlimited as 0 + N for the first time-box).
    if 'duration_minutes' in data:
        v = data['duration_minutes']
        if v in ('', None):
            game.duration_minutes = None
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                return jsonify({'error': 'duration_minutes must be an int or null'}), 400
            if v <= 0:
                return jsonify({'error': 'duration_minutes must be > 0'}), 400
            game.duration_minutes = v
    if 'extend_minutes' in data:
        try:
            extend = int(data['extend_minutes'])
        except (TypeError, ValueError):
            return jsonify({'error': 'extend_minutes must be an int'}), 400
        if extend <= 0:
            return jsonify({'error': 'extend_minutes must be > 0'}), 400
        game.duration_minutes = (game.duration_minutes or 0) + extend
    # Recompute ends_at whenever duration touched on an active game.
    if 'duration_minutes' in data or 'extend_minutes' in data:
        game.ends_at = _compute_ends_at(game)
    db.session.commit()
    if 'scan_interval_sec' in data:
        from .. import koth_scorer
        koth_scorer.reschedule(game.scan_interval_sec)
    return jsonify({'game': game.to_dict()})


@koth_bp.route('/koth/api/games/<int:game_id>/end', methods=['POST'])
@login_required
@admin_required
def end_game(game_id):
    """End game: stop scheduler, destroy all KOTH VMs in Proxmox, mark ended.

    Idempotent: calling end on an already-ended game still cleans up any
    VMRecords that survived a previous attempt (recovery path for stranded
    VMs from earlier bugs).
    """
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    lock = _acquire_game_lock(game_id)
    if lock is None:
        return jsonify({'error': 'another operation in progress for this game'}), 409
    try:
        from .. import koth_scorer
        koth_scorer.stop_scheduler()
        destroyed = _destroy_game_vms(game.id)
        game.status = 'ended'
        game.is_active = False
        if game.ended_at is None:
            game.ended_at = datetime.now()
        db.session.commit()
        # Drop this game's backoff / IP-refresh bookkeeping. The tick-driven
        # auto-end already does; this is the path an instructor actually
        # uses, and with max_requests=0 the worker never recycles to clear
        # it. Must run after the commit so the box rows are still readable.
        koth_scorer.forget_game(game.id)
        return jsonify({'game': game.to_dict(), 'destroyed_vms': destroyed})
    finally:
        lock.release()


@koth_bp.route('/koth/api/games/<int:game_id>/cleanup-vms', methods=['POST'])
@login_required
@admin_required
def cleanup_game_vms(game_id):
    """Destroy any non-destroyed VMRecords for this KOTH game.

    Used to recover from partial deploys / stranded VMs without ending the
    game. If the game is currently 'active' the next scoring tick will see
    boxes whose VMs no longer exist (services will all probe 'closed') —
    typically only useful when paired with redeploy or end_game.
    """
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    lock = _acquire_game_lock(game_id)
    if lock is None:
        return jsonify({'error': 'another operation in progress for this game'}), 409
    try:
        destroyed = _destroy_game_vms(game.id)
        return jsonify({'destroyed_vms': destroyed})
    finally:
        lock.release()


@koth_bp.route('/koth/api/games/<int:game_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_game(game_id):
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    if game.status in ('active', 'deploying'):
        return jsonify({'error': 'end game before deleting'}), 400
    # status in ('draft', 'ended') — both deletable. Drafts have no VMs by
    # construction; ended games may still have destroyed=False VMRecords if
    # end_game's cleanup partially failed. Refuse rather than orphan.
    live = (VMRecord.query.filter_by(koth_game_id=game.id, destroyed=False).count())
    if live:
        return jsonify({
            'error': (f'{live} KOTH VMs still alive; POST /koth/api/games/'
                      f'{game.id}/cleanup-vms first')
        }), 400
    db.session.delete(game)
    db.session.commit()
    return jsonify({'ok': True})


@koth_bp.route('/koth/api/games/current', methods=['GET'])
@login_required
@admin_required
def current_game():
    """Return the latest non-ended game (draft or active), with teams + tokens.

    The /koth/api/state endpoint only returns active games (player view).
    Admins also need to see drafts so they can deploy or delete them, so
    this admin-only endpoint surfaces both.
    """
    game = (KothGame.query
            .filter(KothGame.status != 'ended')
            .order_by(KothGame.created_at.desc())
            .first())
    if game is None:
        return jsonify({'game': None}), 200
    teams = KothTeam.query.filter_by(game_id=game.id).all()
    flags = (KothFlag.query
             .filter_by(game_id=game.id)
             .order_by(KothFlag.id.asc()).all())
    boxes = KothBox.query.filter_by(game_id=game.id).order_by(KothBox.id.asc()).all()
    box_name_by_id = {b.id: b.name for b in boxes}
    return jsonify({
        'game': game.to_dict(),
        'teams': [t.to_dict(include_token=True) for t in teams],
        # Flags surfaced here so the admin UI can list + edit them on the
        # same payload it loads for the current-game card. box_name pulled
        # in alongside box_id so the table can show a human-readable scope
        # ("global" vs "final-dc") without a second fetch.
        'flags': [{
            'id': f.id, 'name': f.name, 'value': f.value,
            'points': f.points, 'box_id': f.box_id,
            'box_name': box_name_by_id.get(f.box_id),
            'submission_count_limit': f.submission_count_limit,
        } for f in flags],
        # Minimal box list for the edit-flag dropdown's box-scope choices.
        # Empty on draft (boxes only materialize at deploy time).
        'boxes': [{'id': b.id, 'name': b.name} for b in boxes],
    })


@koth_bp.route('/koth/api/games/<int:game_id>/flags', methods=['POST'])
@login_required
@admin_required
def add_flag(game_id):
    """Add a single flag. Allowed in `draft`, `deploying`, and `active`
    states so the admin can prep the flag set before deploy. Rejected
    on `ended` games (history is wiped on next create-game anyway)."""
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    if game.status == 'ended':
        return jsonify({'error': 'cannot add flags to an ended game'}), 409
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    value = (data.get('value') or '').strip()
    if not name or not value:
        return jsonify({'error': 'name and value are required'}), 400
    try:
        points = float(data.get('points', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'points must be a number'}), 400
    import math as _math
    if not _math.isfinite(points) or points < 0:
        return jsonify({'error': 'points must be a finite number >= 0'}), 400
    flag = KothFlag(
        game_id=game.id, name=name, value=value,
        points=points,
        box_id=data.get('box_id'),
        submission_count_limit=int(data.get('submission_count_limit', 1)),
    )
    db.session.add(flag); db.session.commit()
    return jsonify({'flag': {'id': flag.id, 'name': flag.name,
                             'points': flag.points, 'box_id': flag.box_id}}), 201


@koth_bp.route('/koth/api/games/<int:game_id>/flags/upload', methods=['POST'])
@login_required
@admin_required
def upload_flags(game_id):
    """Bulk-add flags from a YAML body.

    Accepted bodies:
      - Content-Type: application/x-yaml or text/yaml -> request.data is the YAML
      - Content-Type: application/json -> body is {"yaml": "<yaml text>"}
        (browser convenience — XMLHttpRequest with a string field)

    Schema: top-level is a flat list of flag objects.
      - name: "Root flag — DC"   # required
        value: "<UUID>"          # required, the string players submit
        points: 10               # optional, default 0
        box_name: "final-dc"     # optional; resolved to box_id by name lookup. Omit for global.
        submission_count_limit: 1   # optional, default 1

    Atomic: any per-row validation failure aborts the whole upload (zero rows
    inserted). Rejected on ended games — same rule as single-flag add.
    """
    import yaml as _yaml
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    if game.status == 'ended':
        return jsonify({'error': 'cannot add flags to an ended game'}), 409

    # Pull YAML text from whichever content type the browser used.
    # Even though this endpoint is admin-only, we still cap the input —
    # an alias-heavy YAML can balloon memory through safe_load even with
    # safe_load's no-tag-construction guarantee.
    MAX_YAML_BYTES = 64 * 1024   # 64 KB — way more than any practical flag set
    MAX_FLAG_ROWS = 1000         # also generous; protects the per-row loop
    ctype = (request.content_type or '').lower()
    if 'yaml' in ctype:
        yaml_text = (request.get_data(as_text=True) or '').strip()
    else:
        body = request.get_json(silent=True) or {}
        yaml_text = (body.get('yaml') or '').strip() if isinstance(body, dict) else ''
    if not yaml_text:
        return jsonify({'error': 'empty YAML body'}), 400
    if len(yaml_text) > MAX_YAML_BYTES:
        return jsonify({
            'error': f'YAML body too large (>{MAX_YAML_BYTES} bytes)'
        }), 413

    try:
        rows = _yaml.safe_load(yaml_text)
    except _yaml.YAMLError as e:
        return jsonify({'error': f'YAML parse error: {e}'}), 400
    if not isinstance(rows, list):
        return jsonify({
            'error': 'YAML must be a flat list of flag objects (e.g. `- name: ...`)'
        }), 400
    if not rows:
        return jsonify({'error': 'no flag rows in YAML'}), 400
    if len(rows) > MAX_FLAG_ROWS:
        return jsonify({
            'error': f'too many flag rows ({len(rows)}); cap is {MAX_FLAG_ROWS}'
        }), 400

    # Pre-resolve every box_name in one query so we only walk the box list once.
    box_id_by_name = {b.name: b.id
                      for b in KothBox.query.filter_by(game_id=game.id).all()}

    # Validate every row before inserting any (atomic).
    parsed = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return jsonify({'error': f'row {i}: not a mapping'}), 400
        name = (row.get('name') or '').strip() if isinstance(row.get('name'), str) else ''
        value = (row.get('value') or '').strip() if isinstance(row.get('value'), str) else ''
        if not name or not value:
            return jsonify({'error': f'row {i}: name and value are required'}), 400
        try:
            points = float(row.get('points', 0))
        except (TypeError, ValueError):
            return jsonify({'error': f'row {i}: points must be a number'}), 400
        # Reject NaN / +Inf / -Inf — they slip through float() and would
        # quietly corrupt downstream score math.
        import math as _math
        if not _math.isfinite(points):
            return jsonify({'error': f'row {i}: points must be a finite number'}), 400
        if points < 0:
            return jsonify({'error': f'row {i}: points must be >= 0'}), 400
        try:
            limit = int(row.get('submission_count_limit', 1))
        except (TypeError, ValueError):
            return jsonify({'error': f'row {i}: submission_count_limit must be an int'}), 400
        if limit <= 0:
            return jsonify({'error': f'row {i}: submission_count_limit must be > 0'}), 400
        box_name = row.get('box_name')
        box_id = None
        if box_name:
            if box_name not in box_id_by_name:
                return jsonify({
                    'error': (f"row {i}: box_name {box_name!r} doesn't match any "
                              f"KothBox in this game (known: "
                              f"{sorted(box_id_by_name)})")
                }), 400
            box_id = box_id_by_name[box_name]
        parsed.append({
            'name': name, 'value': value, 'points': points,
            'box_id': box_id, 'submission_count_limit': limit,
        })

    # All validated — insert in one transaction.
    inserted = []
    for p in parsed:
        flag = KothFlag(game_id=game.id, **p)
        db.session.add(flag)
        inserted.append(flag)
    db.session.commit()
    return jsonify({
        'added': len(inserted),
        'flags': [{'id': f.id, 'name': f.name, 'points': f.points,
                   'box_id': f.box_id} for f in inserted],
    }), 201


@koth_bp.route('/koth/api/games/<int:game_id>/flags/<int:flag_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_flag(game_id, flag_id):
    flag = db.session.get(KothFlag, flag_id)
    if flag is None or flag.game_id != game_id:
        return jsonify({'error': 'not found'}), 404
    accepted = KothFlagSubmission.query.filter_by(flag_id=flag.id, accepted=True).count()
    force = request.args.get('force') == '1'
    if accepted and not force:
        # Return a structured `force_required` flag alongside the message
        # so the admin UI can detect this without string-matching the
        # human-readable error text.
        return jsonify({
            'error': f'{accepted} accepted submissions; pass force=1 to delete',
            'force_required': True,
            'accepted_count': accepted,
        }), 409
    db.session.delete(flag); db.session.commit()
    return jsonify({'ok': True})


@koth_bp.route('/koth/api/games/<int:game_id>/flags/<int:flag_id>', methods=['PATCH'])
@login_required
@admin_required
def update_flag(game_id, flag_id):
    """Edit an existing flag in place.

    Editable: name, value, points, box_id (or box_name as a convenience),
    submission_count_limit. Validations mirror add_flag / upload_flags so a
    PATCH can't sneak past the bounds those endpoints enforce. Rejected on
    `ended` games.
    """
    import math as _math
    flag = db.session.get(KothFlag, flag_id)
    if flag is None or flag.game_id != game_id:
        return jsonify({'error': 'not found'}), 404
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    if game.status == 'ended':
        return jsonify({'error': 'cannot edit flags on an ended game'}), 409
    data = request.get_json(silent=True) or {}

    if 'name' in data:
        v = (data.get('name') or '').strip() if isinstance(data.get('name'), str) else ''
        if not v:
            return jsonify({'error': 'name cannot be empty'}), 400
        flag.name = v
    if 'value' in data:
        v = (data.get('value') or '').strip() if isinstance(data.get('value'), str) else ''
        if not v:
            return jsonify({'error': 'value cannot be empty'}), 400
        flag.value = v
    if 'points' in data:
        try:
            pts = float(data['points'])
        except (TypeError, ValueError):
            return jsonify({'error': 'points must be a number'}), 400
        if not _math.isfinite(pts) or pts < 0:
            return jsonify({'error': 'points must be a finite number >= 0'}), 400
        flag.points = pts
    # Box scope: accept either box_id (int|None) or box_name (str|None) for
    # browser convenience. box_name resolves through the current game's
    # KothBox table; explicit None / '' clears the scope (= global flag).
    # Reject both at once — silently picking one is a footgun if the two
    # disagree.
    if 'box_id' in data and 'box_name' in data:
        return jsonify({
            'error': 'pass either box_id or box_name, not both'
        }), 400
    if 'box_id' in data:
        bid = data['box_id']
        if bid in (None, '', 0):
            flag.box_id = None
        else:
            try:
                bid = int(bid)
            except (TypeError, ValueError):
                return jsonify({'error': 'box_id must be an int or null'}), 400
            box = db.session.get(KothBox, bid)
            if box is None or box.game_id != game_id:
                return jsonify({'error': f'box_id {bid} not in this game'}), 400
            flag.box_id = bid
    elif 'box_name' in data:
        bn = data.get('box_name')
        if bn in (None, ''):
            flag.box_id = None
        else:
            box = KothBox.query.filter_by(game_id=game_id, name=bn).first()
            if box is None:
                return jsonify({'error': f'box_name {bn!r} not in this game'}), 400
            flag.box_id = box.id
    if 'submission_count_limit' in data:
        try:
            lim = int(data['submission_count_limit'])
        except (TypeError, ValueError):
            return jsonify({'error': 'submission_count_limit must be an int'}), 400
        if lim <= 0:
            return jsonify({'error': 'submission_count_limit must be > 0'}), 400
        flag.submission_count_limit = lim

    db.session.commit()
    return jsonify({'flag': {
        'id': flag.id, 'name': flag.name, 'value': flag.value,
        'points': flag.points, 'box_id': flag.box_id,
        'submission_count_limit': flag.submission_count_limit,
    }})


@koth_bp.route('/koth/api/games/<int:game_id>/teams/<int:team_id>/rotate-token',
               methods=['POST'])
@login_required
@admin_required
def rotate_token(game_id, team_id):
    team = db.session.get(KothTeam, team_id)
    if team is None or team.game_id != game_id:
        return jsonify({'error': 'not found'}), 404
    team.submission_token = KothTeam.new_token()
    db.session.commit()
    return jsonify({'team': team.to_dict(include_token=True)})


@koth_bp.route('/koth/api/games/<int:game_id>/boxes/<int:box_id>/manual-claim',
               methods=['POST'])
@login_required
@admin_required
def manual_claim(game_id, box_id):
    box = db.session.get(KothBox, box_id)
    if box is None or box.game_id != game_id:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(silent=True) or {}
    team_id = data.get('team_id')
    if team_id is not None:
        team = db.session.get(KothTeam, team_id)
        if team is None or team.game_id != game_id:
            return jsonify({'error': 'unknown team'}), 400
    box.owner_team_id = team_id
    box.claimed_at = datetime.now() if team_id else None
    db.session.add(KothClaimEvent(
        game_id=game_id, box_id=box.id, team_id=team_id,
        event_type='claim', meta={'manual': True}))
    db.session.commit()
    return jsonify({'box': box.to_dict()})


# ---------------------------------------------------------------------------
# Deploy / redeploy
#
# Both go through `deploy_bundle()` from app/routes/vm_creation.py — the same
# parallel-clone orchestration the regular `/create` endpoint uses. KOTH adds
# only:
#   - status transitions (draft -> deploying -> active; revert on failure)
#   - the per-template KothBox row insertion + initial network-map layout
#   - scheduler start
# ---------------------------------------------------------------------------


def _resolve_koth_group(game):
    """Look up the koth_mode deployment_group dict for a game, or return
    (None, error_response). Centralises the templates.yaml lookup so both
    deploy and redeploy share the same error wording.
    """
    cfg = load_template_config()
    matches = [g for g in get_koth_groups(cfg)
               if g['display_name'] == game.deployment_group_name]
    if not matches:
        return None, (jsonify({
            'error': f'deployment group {game.deployment_group_name!r} not koth_mode or not found'
        }), 400)
    group = matches[0]
    if not group.get('templates'):
        return None, (jsonify({'error': 'deployment group has no templates'}), 400)
    return group, None


def _materialize_koth_boxes(game, group, vm_records, *, reuse_existing=False):
    """Turn deploy_bundle's VMRecord results into KothBox rows.

    `reuse_existing=True` (used by redeploy) updates existing KothBox rows
    in place when their name matches, preserving the network-map position
    and KothBox.id (so KothClaimEvent FKs survive). Otherwise creates fresh
    rows. Boxes whose template is no longer in the group are deleted.
    """
    # Map vm_records by template name (vm.template_name is "<group>/<template>")
    rec_by_template = {}
    for rec in vm_records:
        tmpl_name = rec.template_name.split('/', 1)[-1]
        rec_by_template[tmpl_name] = rec

    existing_by_name = (
        {b.name: b for b in KothBox.query.filter_by(game_id=game.id).all()}
        if reuse_existing else {}
    )

    # Drop boxes whose template is no longer in the group (redeploy only).
    new_template_names = {t['name'] for t in group['templates']}
    if reuse_existing:
        for name, box in list(existing_by_name.items()):
            if name not in new_template_names:
                db.session.delete(box)
                existing_by_name.pop(name, None)

    boxes_in_order = []
    for t in group['templates']:
        rec = rec_by_template.get(t['name'])
        if rec is None:
            # deploy_bundle should produce one record per template; defensive.
            continue
        services = [{**s, 'status': 'closed'}
                    for s in get_koth_services(group, t['name'])]
        box_ips = [rec.ip_address] if rec.ip_address else []
        claim_file = get_koth_claim_file(group, t['name'])
        if t['name'] in existing_by_name:
            box = existing_by_name[t['name']]
            box.vm_record_id = rec.id
            box.ips = box_ips
            box.services = services
            box.owner_team_id = None
            box.claimed_at = None
            # Refresh in case templates.yaml was edited between deploy + redeploy.
            box.claim_file_path = claim_file
        else:
            box = KothBox(
                game_id=game.id, vm_record_id=rec.id,
                name=t['name'], display_name=t['name'],
                ips=box_ips, services=services,
                claim_file_path=claim_file,
            )
            db.session.add(box)
        boxes_in_order.append(box)

    db.session.flush()  # ensure box.id is populated for downstream consumers

    # Layout: only assign to boxes that don't already have a position
    # (preserves admin-visible map across redeploys).
    layout = compute_layout([{'name': b.name} for b in boxes_in_order])
    for b in boxes_in_order:
        if b.position is None:
            b.position = layout.get(b.name)
    return boxes_in_order


def _start_scheduler(game):
    """Start the scoring scheduler at the game's configured tick interval.

    `start_scheduler` is idempotent — if it's already running (e.g. from
    boot recovery), the explicit `interval_sec` arg is ignored and we
    follow up with `reschedule` to bring the running scheduler in line
    with the game we just deployed.
    """
    from flask import current_app
    from .. import koth_scorer
    koth_scorer.start_scheduler(
        current_app._get_current_object(),
        interval_sec=game.scan_interval_sec,
    )
    koth_scorer.reschedule(game.scan_interval_sec)


@koth_bp.route('/koth/api/games/<int:game_id>/deploy', methods=['POST'])
@login_required
@admin_required
def deploy(game_id):
    """Deploy a draft game via the shared deploy_bundle orchestration.

    Lifecycle: draft -> deploying (committed before the slow Proxmox clones)
    -> active (on success) or back to draft (on failure, with any partial
    VMs already cleaned up by deploy_bundle).
    """
    actor_id = get_authenticated_user().id
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    if game.status != 'draft':
        return jsonify({
            'error': f"can only deploy a draft game (current status: {game.status})"
        }), 409
    lock = _acquire_game_lock(game_id)
    if lock is None:
        return jsonify({'error': 'a deploy is already in progress for this game'}), 409
    try:
        group, err = _resolve_koth_group(game)
        if err is not None:
            return err

        game.status = 'deploying'
        db.session.commit()

        # Bulletproof revert: ANY exception below (not just DeployFailed —
        # network errors, proxmoxer connection drops, etc.) must revert the
        # row to 'draft' so the admin isn't stuck staring at a perpetual
        # "deploying" panel. Without this, a tailscale/network blip kills
        # the request mid-flight and only `lock.release()` runs.
        try:
            vm_records = deploy_bundle(
                deployment_group_name=game.deployment_group_name,
                templates=group['templates'],
                owning_identifier=str(game.id),
                entry_vlan=game.entry_vlan_tag,
                internal_vlan=game.internal_vlan_tag,
                uptime_minutes=group.get('uptime_minutes', 240),
                user_id=actor_id,
                load_balancing_config=group.get('load_balancing'),
                koth_game_id=game.id,
            )
            boxes = _materialize_koth_boxes(game, group, vm_records, reuse_existing=False)
            game.status = 'active'
            game.is_active = True
            game.started_at = datetime.now()
            game.ends_at = _compute_ends_at(game)
            db.session.commit()
        except DeployFailed as e:
            logger.exception('KOTH deploy failed')
            db.session.rollback()
            game = db.session.get(KothGame, game_id)
            if game and game.status == 'deploying':
                game.status = 'draft'
                db.session.commit()
            return jsonify({'error': f'deploy failed: {e}'}), 500
        except Exception as e:
            logger.exception('KOTH deploy crashed (non-DeployFailed exception)')
            db.session.rollback()
            game = db.session.get(KothGame, game_id)
            if game and game.status == 'deploying':
                game.status = 'draft'
                db.session.commit()
            # Best-effort cleanup of any VMs that did get created before the crash.
            try:
                _destroy_game_vms(game_id)
            except Exception:
                logger.exception('KOTH post-crash VM cleanup also failed')
            return jsonify({'error': f'deploy crashed: {e}'}), 500

        _start_scheduler(game)
        return jsonify({
            'game': game.to_dict(),
            'boxes': [b.to_dict() for b in boxes],
        })
    finally:
        lock.release()


@koth_bp.route('/koth/api/games/<int:game_id>/redeploy', methods=['POST'])
@login_required
@admin_required
def redeploy(game_id):
    """Destroy current KOTH VMs, re-clone templates via deploy_bundle, refresh
    KothBox rows in place.

    Preserves: KothGame, KothTeam (incl. tokens), KothScoreSnapshot, KothClaimEvent.
    Resets: KothBox.owner_team_id, claimed_at. New ips/vm_record_id.
    """
    actor_id = get_authenticated_user().id
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    if game.status != 'active':
        return jsonify({'error': 'game must be active to redeploy'}), 409
    lock = _acquire_game_lock(game_id)
    if lock is None:
        return jsonify({'error': 'another operation in progress for this game'}), 409
    try:
        group, err = _resolve_koth_group(game)
        if err is not None:
            return err

        game.status = 'deploying'
        db.session.commit()

        # Tear down old VMs in Proxmox + mark destroyed in DB before re-cloning,
        # so the load balancer sees the freed capacity when picking nodes.
        _destroy_game_vms(game.id)

        try:
            vm_records = deploy_bundle(
                deployment_group_name=game.deployment_group_name,
                templates=group['templates'],
                owning_identifier=str(game.id),
                entry_vlan=game.entry_vlan_tag,
                internal_vlan=game.internal_vlan_tag,
                uptime_minutes=group.get('uptime_minutes', 240),
                user_id=actor_id,
                load_balancing_config=group.get('load_balancing'),
                koth_game_id=game.id,
            )
            _materialize_koth_boxes(game, group, vm_records, reuse_existing=True)
            game.status = 'active'
            db.session.commit()
        except DeployFailed as e:
            logger.exception('KOTH redeploy failed')
            db.session.rollback()
            game = db.session.get(KothGame, game_id)
            if game and game.status == 'deploying':
                game.status = 'active'
                db.session.commit()
            return jsonify({'error': f'redeploy failed: {e}'}), 500
        except Exception as e:
            logger.exception('KOTH redeploy crashed (non-DeployFailed exception)')
            db.session.rollback()
            game = db.session.get(KothGame, game_id)
            if game and game.status == 'deploying':
                game.status = 'active'
                db.session.commit()
            try:
                _destroy_game_vms(game_id)
            except Exception:
                logger.exception('KOTH post-crash VM cleanup also failed')
            return jsonify({'error': f'redeploy crashed: {e}'}), 500

        return jsonify({
            'game': game.to_dict(),
            'boxes': [b.to_dict() for b in KothBox.query.filter_by(game_id=game.id).all()],
        })
    finally:
        lock.release()


@koth_bp.route('/koth/api/games/<int:game_id>/refresh-ips', methods=['POST'])
@login_required
@admin_required
def refresh_ips(game_id):
    """Re-query Proxmox guest agent for each box's IPs and persist them.

    Manual trigger for the admin UI when boxes are missing IPs (typically
    right after deploy, before the scoring tick has run, or when guest
    agent was slow to respond on first boot).
    """
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    updated = _refresh_box_ips(game_id)
    return jsonify({
        'updated': updated,
        'boxes': [b.to_dict() for b in KothBox.query.filter_by(game_id=game_id).all()],
    })


@koth_bp.route('/koth/api/games/<int:game_id>/force-reset', methods=['POST'])
@login_required
@admin_required
def force_reset(game_id):
    """Manual recovery for a game stuck in 'deploying'.

    A network drop / timeout / killed worker can leave a game with
    status='deploying' even after the bulletproof exception path was added,
    if the request died before the rollback ran (e.g. the user closed the
    tab and the WSGI worker SIGTERM'd). This endpoint is the in-UI escape
    hatch: revert to 'draft' and clean up any partial VMs.

    Idempotent for non-deploying games — they're returned unchanged with a
    descriptive error.
    """
    game = db.session.get(KothGame, game_id)
    if game is None:
        return jsonify({'error': 'not found'}), 404
    if game.status != 'deploying':
        return jsonify({
            'error': f"force-reset only applies to 'deploying' games (current: {game.status})"
        }), 409
    # Bypass the in-process lock: that lock was held by whichever request
    # got stuck, and that request is gone. Holding it would make this
    # endpoint useless. The only risk is racing a still-live deploy worker,
    # which would re-flip the status — but a still-live worker would not
    # leave us in 'deploying' to begin with.
    destroyed = 0
    try:
        destroyed = _destroy_game_vms(game.id)
    except Exception:
        logger.exception('force-reset: VM cleanup failed (continuing with status revert)')
    game.status = 'draft'
    db.session.commit()
    # Clear the lock dict entry so future operations get a fresh lock.
    with _game_locks_mutex:
        _game_locks.pop(game.id, None)
    logger.warning("KOTH game %d (%r) force-reset to draft; cleaned up %d VM(s)",
                   game.id, game.name, destroyed)
    return jsonify({'game': game.to_dict(), 'destroyed_vms': destroyed})


@koth_bp.route('/koth/api/_diag', methods=['GET'])
@login_required
@admin_required
def diagnostics():
    """Live resource numbers for a running game.

    Every failure that took the server down during a classroom game was
    invisible while it was happening: SSE subscribers piling up, a tick
    running longer than its own interval so APScheduler silently skipped
    runs, boxes whose guest agent had stopped answering. Each of those
    now reports a number here.

    Read this during a game, not after.
    """
    from .. import koth_scorer
    game = KothGame.query.filter_by(is_active=True).first()
    body = {
        'events': koth_scorer.event_bus.stats(),
        'tick': koth_scorer.tick_stats(),
        'claim_read_backoff_boxes': len(koth_scorer._claim_read_backoff),
        'scheduler_running': koth_scorer._scheduler is not None,
        'game': None,
    }
    engine = db.get_engine() if hasattr(db, 'get_engine') else db.engine
    pool = getattr(engine, 'pool', None)
    if pool is not None and hasattr(pool, 'checkedout'):
        body['db_pool'] = {
            'checked_out': pool.checkedout(),
            'checked_in': pool.checkedin(),
            'overflow': pool.overflow(),
        }
    if game is not None:
        boxes = KothBox.query.filter_by(game_id=game.id).all()
        body['game'] = {
            'id': game.id, 'name': game.name,
            'iteration': game.scoring_iteration,
            'scan_interval_sec': game.scan_interval_sec,
            'claim_mode': game.claim_mode,
            'claim_port': game.claim_port,
            'boxes': len(boxes),
            'boxes_without_ip': sum(1 for b in boxes if not (b.ips or [])),
            'boxes_owned': sum(1 for b in boxes if b.owner_team_id),
            'teams': KothTeam.query.filter_by(game_id=game.id).count(),
            'services_probed_per_tick': sum(len(b.services or []) for b in boxes),
        }
    return jsonify(body)
