"""KOTH scoring loop.

`run_tick(game_id)` runs one claim-reconcile + service-probe + score pass
and publishes the result. `start_scheduler(app)` drives it from an
APScheduler BackgroundScheduler bound to the active game's tick interval.

Shape of a tick, and why:

    auto-end check  ->  claim capture  ->  score  ->  one publish

Everything that touches the network is batched through
`app.concurrency.map_concurrent`, because the previous serial version
scaled as `boxes x (1 Proxmox API call + services x 2 s probe timeout)`.
At 22 boxes with a 12 s interval that routinely outran its own tick and
APScheduler dropped runs with "maximum number of running instances
reached". Network work now happens in two bounded fan-outs with no DB
session involvement, and every DB write happens on this greenlet.

Realtime fan-out lives in `app.koth_events`; the SSE endpoint subscribes
to `event_bus` there.
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config import KOTH_SNAPSHOT_INTERVAL_SEC
from .koth_events import event_bus
from .koth_scanner import (
    DEFAULT_CONCURRENCY, probe_beacon, probe_beacons, probe_many, probe_port,
)
from .koth_scoring import compute_team_deltas
from .models import (
    db, KothGame, KothBox, KothTeam, KothScoreSnapshot, KothClaimEvent, VMRecord,
)

logger = logging.getLogger(__name__)

# Guest-agent reads fail for boxes that are powered off, have no agent
# installed, or whose agent has wedged. Retrying those every tick costs a
# full Proxmox round trip (and its timeout) for a result we already know.
# box_id -> (consecutive_failures, monotonic deadline before next attempt).
_claim_read_backoff: Dict[int, Tuple[int, float]] = {}
_BACKOFF_BASE_SEC = 15.0
_BACKOFF_MAX_SEC = 300.0

# Populating box IPs needs one guest-agent call per box. A box that never
# reports an IP would otherwise trigger that sweep on every single tick.
_last_ip_refresh: Dict[int, float] = {}
_IP_REFRESH_MIN_INTERVAL_SEC = 60.0


# --- helpers --------------------------------------------------------------
def _primary_ip(box: KothBox) -> Optional[str]:
    for ip in (box.ips or []):
        if ip:
            return ip
    return None


def _parse_claim_token(raw: Optional[str]) -> Optional[str]:
    """First non-empty trimmed line of a claim payload, or None.

    Shared by the file reader and the beacon reader. Unknown tokens are
    handled by `_apply_claim`, not here.
    """
    if not raw:
        return None
    for line in raw.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


def _should_attempt_claim_read(box_id: int, now: float) -> bool:
    state = _claim_read_backoff.get(box_id)
    return state is None or now >= state[1]


def _note_claim_read(box_id: int, ok: bool, now: float) -> None:
    if ok:
        _claim_read_backoff.pop(box_id, None)
        return
    failures = _claim_read_backoff.get(box_id, (0, 0.0))[0] + 1
    delay = min(_BACKOFF_BASE_SEC * (2 ** (failures - 1)), _BACKOFF_MAX_SEC)
    _claim_read_backoff[box_id] = (failures, now + delay)


# --- claim capture --------------------------------------------------------
def _read_claim_files(boxes: List[KothBox]) -> Dict[int, Optional[str]]:
    """Read every eligible box's claim file through the Proxmox guest agent.

    Returns {box_id: raw file content or None}. Boxes in backoff, with no
    configured path, or with no live VMRecord are omitted entirely.

    The Proxmox calls run on a worker pool, so the (node, vmid, path)
    triples are resolved from the DB here, on the caller's greenlet —
    worker greenlets must never touch the session.
    """
    from .proxmox_service import get_proxmox_connection
    from .routes.utils import _read_guest_file

    now = time.monotonic()
    targets = []
    for box in boxes:
        if not box.claim_file_path or box.vm_record_id is None:
            continue
        if not _should_attempt_claim_read(box.id, now):
            continue
        vm = db.session.get(VMRecord, box.vm_record_id)
        if vm is None or vm.destroyed:
            continue
        targets.append((box.id, vm.node, vm.vmid, box.claim_file_path))
    if not targets:
        return {}

    try:
        proxmox = get_proxmox_connection()
    except Exception:
        logger.exception("tick: cannot get Proxmox connection for claim poll")
        return {}

    raws = map_concurrent_guest_reads(proxmox, targets, _read_guest_file)
    out: Dict[int, Optional[str]] = {}
    for (box_id, _node, _vmid, _path), raw in zip(targets, raws):
        _note_claim_read(box_id, raw is not None, now)
        out[box_id] = raw
    return out


def map_concurrent_guest_reads(proxmox, targets, reader):
    """Fan `reader(proxmox, node, vmid, path)` out over `targets`.

    Split out so tests can patch the reader without reaching into the
    concurrency helper.
    """
    from .concurrency import map_concurrent
    return map_concurrent(
        lambda t: reader(proxmox, t[1], t[2], t[3]),
        targets, concurrency=DEFAULT_CONCURRENCY, label='guest claim read')


def _read_claim_beacons(boxes: List[KothBox], port: int
                        ) -> Tuple[Dict[int, Optional[str]], Dict[int, str]]:
    """Connect to every box's claim beacon once.

    Returns ({box_id: token or None}, {box_id: 'open'|'closed'}). The
    status half is kept so the admin diagnostics can tell "no listener"
    apart from "listener up but silent".
    """
    targets = [(_primary_ip(box), port) for box in boxes]
    results = probe_beacons(targets, prober=probe_beacon)
    tokens: Dict[int, Optional[str]] = {}
    statuses: Dict[int, str] = {}
    for box, (status, raw) in zip(boxes, results):
        statuses[box.id] = status
        tokens[box.id] = _parse_claim_token(raw)
    return tokens, statuses


def _apply_claim(box: KothBox, token: Optional[str], source: str,
                 game: KothGame,
                 teams_by_token: Optional[Dict[str, KothTeam]] = None,
                 last_claim_at: Optional[Dict[int, datetime]] = None
                 ) -> Optional[dict]:
    """Reconcile one box's ownership against a captured token.

    Returns a `claim` event payload if ownership changed (the caller
    publishes it only AFTER commit, so SSE clients can never see a flip
    that later rolls back), else None. No-ops: empty token, token that
    matches no team in this game, token that matches the current owner,
    or an in-force per-game claim cooldown.

    `teams_by_token` and `last_claim_at` are the tick's batched lookups.
    Omit them and this loads its own — correct, but one query each, which
    is exactly what the per-box version used to cost.

    Side effects on a flip: sets `owner_team_id` / `claimed_at` and adds a
    KothClaimEvent to the session. Commit is the caller's job.
    """
    if token is None:
        return None
    if teams_by_token is None:
        teams_by_token = {t.submission_token: t
                          for t in KothTeam.query.filter_by(game_id=game.id).all()}
    if last_claim_at is None:
        last_claim_at = _last_claim_times(game.id)
    team = teams_by_token.get(token)
    if team is None:
        logger.debug("claim payload on box %d (%r) has unknown token %r",
                     box.id, box.name,
                     token[:8] + '...' if len(token) > 8 else token)
        return None
    if box.owner_team_id == team.id:
        return None
    # Stops a script that rewrites the claim source every tick from
    # flapping a box back and forth. claim_cooldown_sec=0 disables.
    if game.claim_cooldown_sec > 0:
        last = last_claim_at.get(box.id)
        if last is not None:
            since = datetime.now() - last
            if since < timedelta(seconds=game.claim_cooldown_sec):
                logger.debug("claim cooldown active on box %d: %.1fs since last",
                             box.id, since.total_seconds())
                return None
    prev_owner = box.owner_team_id
    box.owner_team_id = team.id
    box.claimed_at = datetime.now()
    last_claim_at[box.id] = box.claimed_at
    db.session.add(KothClaimEvent(
        game_id=game.id, box_id=box.id, team_id=team.id,
        event_type='claim',
        meta={'source': source, 'previous_owner_team_id': prev_owner},
    ))
    logger.info("KOTH claim: box %d (%r) -> team %d (%r) via %s",
                box.id, box.name, team.id, team.color, source)
    return {
        'game_id': game.id, 'box_id': box.id, 'team_id': team.id,
        'team_color': team.color, 'box_name': box.name,
    }


def _last_claim_times(game_id: int) -> Dict[int, datetime]:
    """Most recent claim timestamp per box, in ONE query.

    The old cooldown check ran a separate ordered query per box per tick.
    """
    rows = (db.session.query(KothClaimEvent.box_id,
                             db.func.max(KothClaimEvent.created_at))
            .filter(KothClaimEvent.game_id == game_id,
                    KothClaimEvent.event_type == 'claim')
            .group_by(KothClaimEvent.box_id).all())
    return {box_id: ts for box_id, ts in rows if box_id is not None and ts}


def _poll_claims(game: KothGame, boxes: List[KothBox]) -> int:
    """Capture ownership for every box and commit the flips.

    Which transport is used is `game.claim_mode`:
      'file'   — Proxmox guest-agent read of `KothBox.claim_file_path`.
      'beacon' — TCP read of the token the team's listener serves on
                 `game.claim_port`. Costs no Proxmox API calls.
      'both'   — beacon first, claim file as fallback. The beacon wins a
                 conflict: a live listener is stronger evidence the team
                 still holds the box than a file somebody wrote once.

    Returns the number of ownership changes applied. Per-box failures are
    absorbed; one unreachable box must not cost the tick.
    """
    mode = (game.claim_mode or 'file').lower()
    beacon_tokens: Dict[int, Optional[str]] = {}
    file_tokens: Dict[int, Optional[str]] = {}
    if mode in ('beacon', 'both'):
        beacon_tokens, _ = _read_claim_beacons(boxes, game.claim_port)
    if mode in ('file', 'both'):
        file_tokens = {
            box_id: _parse_claim_token(raw)
            for box_id, raw in _read_claim_files(boxes).items()
        }

    teams_by_token = {t.submission_token: t
                      for t in KothTeam.query.filter_by(game_id=game.id).all()}
    last_claim_at = _last_claim_times(game.id)

    pending = []
    for box in boxes:
        token = beacon_tokens.get(box.id)
        source = 'beacon'
        if token is None:
            token = file_tokens.get(box.id)
            source = 'file_poll'
        if token is None:
            continue
        payload = _apply_claim(box, token, source, game,
                               teams_by_token, last_claim_at)
        if payload is not None:
            pending.append(payload)

    if not pending:
        return 0
    try:
        db.session.commit()
    except Exception:
        logger.exception("tick: claim commit failed; suppressing %d events",
                         len(pending))
        db.session.rollback()
        return 0
    for payload in pending:
        event_bus.publish('claim', payload)
    return len(pending)


# --- service probing ------------------------------------------------------
def _probe_services(boxes: List[KothBox]) -> None:
    """Refresh every box's service statuses in one bounded fan-out.

    The prober is passed explicitly so every target — one or a hundred —
    goes through this module's `probe_port` binding. Letting the fan-out
    bind the scanner module's copy directly would make a one-service game
    and a twenty-service game take different code paths.
    """
    targets: List[Tuple[Optional[str], int]] = []
    index: List[Tuple[int, int]] = []  # (box position, service position)
    for bi, box in enumerate(boxes):
        ip = _primary_ip(box)
        for si, svc in enumerate(box.services or []):
            targets.append((ip, svc['port']))
            index.append((bi, si))
    if not targets:
        return
    statuses = probe_many(targets, prober=probe_port)

    updated = [list(box.services or []) for box in boxes]
    for (bi, si), status in zip(index, statuses):
        updated[bi][si] = {**updated[bi][si], 'status': status}
    for box, services in zip(boxes, updated):
        # Reassign so SQLAlchemy sees the JSON column as dirty.
        box.services = services


# --- IP population --------------------------------------------------------
def _maybe_refresh_ips(game: KothGame, boxes: List[KothBox]) -> List[KothBox]:
    """Top up `KothBox.ips` for boxes that still have none.

    Boxes are materialized with `ips=[]` because `VMRecord.ip_address` is
    None at clone time. Without IPs there is nothing to probe or beacon
    against. Rate-limited because the sweep is one guest-agent call per
    box and a box that is simply powered off never stops qualifying.
    """
    if all(b.ips for b in boxes):
        return boxes
    now = time.monotonic()
    last = _last_ip_refresh.get(game.id, 0.0)
    if now - last < _IP_REFRESH_MIN_INTERVAL_SEC:
        return boxes
    _last_ip_refresh[game.id] = now
    try:
        from .routes.koth_admin import _refresh_box_ips
        _refresh_box_ips(game.id)
    except Exception:
        logger.exception("tick: opportunistic IP refresh failed")
        return boxes
    return KothBox.query.filter_by(game_id=game.id).all()


# --- the tick -------------------------------------------------------------
def _snapshot_every_n_ticks(game: KothGame) -> int:
    """How many ticks between history rows, >= 1.

    Snapshots are a timeline record nothing on a request path reads, so
    they are written on wall-clock cadence rather than per tick.
    """
    interval = max(1, int(game.scan_interval_sec or 1))
    return max(1, round(KOTH_SNAPSHOT_INTERVAL_SEC / interval))


def run_tick(game_id: int) -> None:
    game = db.session.get(KothGame, game_id)
    if game is None or game.status != 'active':
        return
    # Auto-end before scoring so the final iteration's deltas aren't
    # credited past the end time.
    if game.ends_at is not None and datetime.now() >= game.ends_at:
        logger.info("KOTH game %d (%r) auto-ending: ends_at=%s reached",
                    game.id, game.name, game.ends_at)
        _auto_end(game)
        return

    started = time.monotonic()
    boxes = KothBox.query.filter_by(game_id=game.id).all()
    boxes = _maybe_refresh_ips(game, boxes)

    try:
        if _poll_claims(game, boxes) > 0:
            # Ownership changed, so the scoring inputs did too.
            boxes = KothBox.query.filter_by(game_id=game.id).all()
    except Exception:
        logger.exception("tick: claim poll failed")

    _probe_services(boxes)

    game.scoring_iteration += 1
    deltas = compute_team_deltas(
        [{'owner_team_id': b.owner_team_id, 'services': b.services} for b in boxes],
        iteration=game.scoring_iteration,
        box_ownership_score=game.box_ownership_score,
        port_open_score=game.port_open_score,
        exp_scoring=game.exp_scoring_enabled,
        exp_val=game.exp_val,
    )

    teams = KothTeam.query.filter_by(game_id=game.id).all()
    write_snapshot = game.scoring_iteration % _snapshot_every_n_ticks(game) == 0 \
        or game.scoring_iteration == 1
    for team in teams:
        team.score = round((team.score or 0.0) + float(deltas.get(team.id, 0.0)), 2)
        if write_snapshot:
            db.session.add(KothScoreSnapshot(
                game_id=game.id, team_id=team.id,
                score=team.score, iteration=game.scoring_iteration,
            ))
    db.session.commit()

    elapsed = time.monotonic() - started
    _record_tick_duration(elapsed, game)
    event_bus.publish('scan', build_state_payload(game, teams, boxes),
                      coalesce_key='scan')


# --- tick timing (admin diagnostics) --------------------------------------
_tick_stats = {'last_sec': 0.0, 'max_sec': 0.0, 'count': 0, 'overruns': 0}


def _record_tick_duration(elapsed: float, game: KothGame) -> None:
    _tick_stats['last_sec'] = round(elapsed, 3)
    _tick_stats['max_sec'] = round(max(_tick_stats['max_sec'], elapsed), 3)
    _tick_stats['count'] += 1
    if elapsed > (game.scan_interval_sec or 1):
        _tick_stats['overruns'] += 1
        logger.warning(
            "KOTH tick for game %d took %.1fs, longer than its %ds interval; "
            "APScheduler will skip runs", game.id, elapsed, game.scan_interval_sec)


def tick_stats() -> dict:
    return dict(_tick_stats)


# --- shared state payload -------------------------------------------------
def build_state_payload(game: KothGame, teams, boxes, *,
                        reveal_hidden: bool = False) -> dict:
    """The one state shape both `GET /koth/api/state` and the SSE `scan`
    event return.

    Keeping them identical is what lets the player page render straight
    from the pushed event. It used to answer every `scan` by re-fetching
    `/koth/api/state`, so one tick cost one full-state request per open
    tab — the fan-out that exhausted the DB pool and the worker.

    `reveal_hidden=True` ignores the instructor's hide switches and is for
    admins only. The bus always carries the redacted student view.
    """
    show_scores = game.show_scoreboard or reveal_hidden
    show_map = game.show_network_map or reveal_hidden
    return {
        'game': game.to_dict(),
        'teams': [t.to_dict() for t in teams],
        'scores': latest_scores(game.id, teams=teams) if show_scores else {},
        'boxes': [b.to_dict() for b in boxes] if show_map else [],
        'hidden': {
            'scoreboard': not show_scores,
            'network_map': not show_map,
        },
    }


def latest_scores(game_id: int, *, teams=None) -> dict:
    """{team_id: total score} = live tick score + accepted-flag bonuses.

    Two queries regardless of team count. The previous version ran one
    ordered KothScoreSnapshot lookup per team, so a 22-team game cost 23
    queries per scoreboard read — multiplied by every open tab, every
    tick.

    Flag bonuses stay separate from `KothTeam.score` on purpose: the tick
    and a concurrent flag submission would otherwise race on the same
    value, and the tick's read-modify-write would silently eat the bonus.
    """
    from .models import KothFlag, KothFlagSubmission
    bonus_rows = (db.session.query(
                    KothFlagSubmission.team_id,
                    db.func.coalesce(db.func.sum(KothFlag.points), 0.0))
                  .join(KothFlag, KothFlag.id == KothFlagSubmission.flag_id)
                  .filter(KothFlagSubmission.game_id == game_id,
                          KothFlagSubmission.accepted.is_(True))
                  .group_by(KothFlagSubmission.team_id)
                  .all())
    bonus_by_team = {tid: float(pts or 0.0) for tid, pts in bonus_rows}
    if teams is None:
        teams = KothTeam.query.filter_by(game_id=game_id).all()
    return {t.id: (t.score or 0.0) + bonus_by_team.get(t.id, 0.0) for t in teams}


# --- game end -------------------------------------------------------------
def _auto_end(game: KothGame) -> None:
    """Tick-driven auto-end: destroy VMs, mark ended, stop the scheduler.

    `koth_admin` is imported lazily to avoid a routes <-> scorer cycle.
    """
    try:
        from .routes.koth_admin import _destroy_game_vms
        destroyed = _destroy_game_vms(game.id)
    except Exception:
        logger.exception("auto-end: VM destroy failed for game %d", game.id)
        destroyed = 0
    game.status = 'ended'
    game.is_active = False
    if game.ended_at is None:
        game.ended_at = datetime.now()
    db.session.commit()
    forget_game(game.id)
    event_bus.publish('game_ended', {
        'game_id': game.id, 'reason': 'duration_elapsed',
        'destroyed_vms': destroyed,
    })
    stop_scheduler()


def forget_game(game_id: int) -> None:
    """Drop the per-game and per-box bookkeeping this module caches.

    Called when a game ends so the backoff and IP-refresh dicts can't
    accumulate entries across a term's worth of games.
    """
    _last_ip_refresh.pop(game_id, None)
    box_ids = [b.id for b in KothBox.query.filter_by(game_id=game_id).all()]
    for box_id in box_ids:
        _claim_read_backoff.pop(box_id, None)


# --- scheduler ------------------------------------------------------------
_scheduler = None
_scheduler_lock = threading.Lock()


def _resolve_initial_interval(app, fallback=12):
    """The active game's `scan_interval_sec`, else `fallback`.

    Without this, restarting the container reverted the effective tick
    rate to a hard-coded 12 s until the admin re-PATCHed the setting.
    """
    try:
        with app.app_context():
            game = KothGame.query.filter_by(is_active=True).first()
            if game and game.scan_interval_sec:
                return int(game.scan_interval_sec)
    except Exception:
        logger.exception("could not read scan_interval_sec at boot; using fallback")
    return fallback


def start_scheduler(app, interval_sec=None):
    """Start the scoring scheduler if it isn't running. Idempotent.

    `interval_sec` overrides the per-game value (the deploy path passes
    the freshly-saved setting); omitted, the active game's value is read
    from the DB.
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            logger.warning("APScheduler not installed; KOTH scheduler disabled")
            return
        _scheduler = BackgroundScheduler(daemon=True)

        def tick():
            with app.app_context():
                game = KothGame.query.filter_by(is_active=True).first()
                if not game:
                    return
                try:
                    run_tick(game.id)
                except Exception:
                    logger.exception("KOTH tick failed")
                finally:
                    # The tick owns its session; leaving it checked out
                    # parks a pooled DB connection between runs.
                    db.session.remove()

        seconds = interval_sec if interval_sec is not None else _resolve_initial_interval(app)
        _scheduler.add_job(tick, 'interval', seconds=seconds, id='koth_tick')
        _scheduler.start()
        logger.info("KOTH scheduler started (tick interval %ds)", seconds)


def stop_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


def reschedule(interval_sec: int):
    """Update the tick interval in place (admin changed the setting)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            return
        job = _scheduler.get_job('koth_tick')
        if job:
            job.reschedule('interval', seconds=interval_sec)
