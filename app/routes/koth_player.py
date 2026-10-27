"""Player + spectator KOTH routes (read-only state, my-team, SSE stream)."""

import hmac
import logging
from flask import current_app, jsonify, render_template, request, Response
from sqlalchemy.exc import IntegrityError

from ..auth import login_required, get_authenticated_user
from ..models import (
    db, User, KothGame, KothTeam, KothBox, KothFlag, KothFlagSubmission,
    KothClaimEvent, StudentGroupMember,
)
from .koth import koth_bp
from .. import koth_scorer

logger = logging.getLogger(__name__)


def _active_game():
    return KothGame.query.filter_by(is_active=True).first()


def _is_admin(user):
    return bool(user and user.is_admin)


def _user_team(game, user):
    if user is None:
        return None
    membership = StudentGroupMember.query.filter_by(user_id=user.id).first()
    if not membership:
        return None
    return KothTeam.query.filter_by(
        game_id=game.id, student_group_id=membership.group_id).first()


@koth_bp.route('/koth', methods=['GET'])
@login_required
def player_page():
    user = get_authenticated_user()
    # The page needs to know this before the first SSE frame: admins see
    # the unredacted state, which is deliberately never put on the shared
    # event bus, so their client re-fetches on each scan. Students render
    # straight from the pushed frame.
    return render_template('koth.html', current_user_is_admin=_is_admin(user))


@koth_bp.route('/koth/api/state', methods=['GET'])
@login_required
def state():
    user = get_authenticated_user()
    game = _active_game()
    if game is None:
        return ('', 204)
    teams = KothTeam.query.filter_by(game_id=game.id).all()
    boxes = KothBox.query.filter_by(game_id=game.id).all()
    # Same builder the scoring tick publishes on the `scan` event, so the
    # player page can render a pushed frame without re-fetching this.
    return jsonify(koth_scorer.build_state_payload(
        game, teams, boxes, reveal_hidden=_is_admin(user)))


@koth_bp.route('/koth/api/my-team', methods=['GET'])
@login_required
def my_team():
    user = get_authenticated_user()
    game = _active_game()
    if game is None:
        return jsonify({'error': 'no active game'}), 404
    team = _user_team(game, user)
    if team is None:
        return jsonify({'error': 'not on a team'}), 404
    return jsonify({'team': team.to_dict(include_token=True)})


@koth_bp.route('/koth/api/flags/submit', methods=['POST'])
@login_required
def submit_flag():
    """Player submits a flag UUID via the website (session-authed).

    Team identity comes from session -> StudentGroupMember -> KothTeam.
    Returns:
      200 {accepted: true,  points: N, flag_name: ...}  on first match
      200 {accepted: false, reason: 'duplicate'}        already accepted by this team
      200 {accepted: false}                             value did not match any flag
      403  user is not on a participating team
      404  no active game
    """
    user = get_authenticated_user()
    game = _active_game()
    if game is None:
        return jsonify({'error': 'no active game'}), 404
    team = _user_team(game, user)
    if team is None:
        return jsonify({'error': 'you are not on a participating team'}), 403

    data = request.get_json(silent=True) or {}
    value = (data.get('value') or '').strip()
    if not value:
        return jsonify({'error': 'value is required'}), 400

    # Constant-time compare against every flag in this game so we don't
    # leak which value was closest. Fine at the small scale we care about
    # (handful of flags per game).
    candidates = KothFlag.query.filter_by(game_id=game.id).all()
    matched = None
    for f in candidates:
        if hmac.compare_digest(f.value, value):
            matched = f
            break

    if matched is None:
        if candidates:
            db.session.add(KothFlagSubmission(
                game_id=game.id, flag_id=candidates[0].id,
                team_id=team.id, accepted=False,
                submitted_by_user_id=user.id))
            db.session.commit()
        return jsonify({'accepted': False}), 200

    # One-time-per-team: each flag awards bonus points to a team exactly
    # once. Wrong-value attempts above are recorded as accepted=False for
    # audit and DON'T burn the team's chance — they can keep guessing.
    # Backed by the partial unique index uq_koth_flag_sub_accepted which
    # would also reject the duplicate at the DB level.
    existing = KothFlagSubmission.query.filter_by(
        flag_id=matched.id, team_id=team.id, accepted=True).first()
    if existing:
        return jsonify({'accepted': False, 'reason': 'duplicate'}), 200

    db.session.add(KothFlagSubmission(
        game_id=game.id, flag_id=matched.id, team_id=team.id,
        accepted=True, submitted_by_user_id=user.id))
    db.session.add(KothClaimEvent(
        game_id=game.id, box_id=matched.box_id, team_id=team.id,
        event_type='flag', points_delta=matched.points,
        meta={'flag_id': matched.id, 'flag_name': matched.name,
              'submitted_by_user_id': user.id}))
    try:
        db.session.commit()
    except IntegrityError:
        # The check-then-insert above has a race window: two concurrent
        # POSTs from the same team can both pass the check and try to
        # insert. The partial unique index uq_koth_flag_sub_accepted then
        # rejects the second one. Convert it to the same friendly response
        # the synchronous duplicate-check returns instead of bubbling a 500.
        db.session.rollback()
        return jsonify({'accepted': False, 'reason': 'duplicate'}), 200

    # No KothScoreSnapshot bump here. koth_scorer.latest_scores combines
    # the team's live tick score with the sum of accepted flag bonuses at
    # read time, so the bonus shows up on the next state fetch without
    # competing with the tick over the same value. See the docstring on
    # latest_scores for the race that prompted this split.
    logger.info("KOTH flag submitted: game %d team %d (%r) flag %r (+%s)",
                game.id, team.id, team.color, matched.name, matched.points)
    koth_scorer.event_bus.publish('flag', {
        'game_id': game.id, 'team_id': team.id, 'team_color': team.color,
        'flag_name': matched.name, 'points': matched.points,
    })
    return jsonify({
        'accepted': True,
        'points': matched.points,
        'flag_name': matched.name,
    }), 200


@koth_bp.route('/koth/api/stream', methods=['GET'])
@login_required
def stream():
    # SSE streams stay open for the entire client session (minutes to
    # hours), so capture the authenticated identity for periodic revocation
    # checks after the request context is gone. Each check uses a short-lived
    # application context and returns its connection immediately.
    user = get_authenticated_user()
    user_id = user.id
    session_version = user.session_version
    app = current_app._get_current_object()
    db.session.remove()

    def session_is_current():
        with app.app_context():
            try:
                current_version = (
                    db.session.query(User.session_version)
                    .filter(
                        User.id == user_id,
                        User.disabled.is_(False),
                    )
                    .scalar()
                )
                return current_version == session_version
            finally:
                db.session.remove()

    def gen():
        # Subscribe INSIDE the generator. A WSGI server that closes a
        # response iterable it never iterated calls generator.close()
        # without running the body, so a subscription taken in the view
        # would never hit the `finally` and would sit in the bus buffering
        # frames for the life of the process — and with max_requests=0
        # this worker never recycles to clear it.
        sub = koth_scorer.event_bus.subscribe()
        # Frames arrive already serialized: the bus builds each one once
        # per publish instead of once per subscriber. With 60 open tabs
        # that is 1 json.dumps per tick instead of 60.
        try:
            # Tell the browser not to hammer us if the stream drops.
            yield "retry: 5000\n\n"
            while True:
                frames = sub.drain(timeout=15)
                if not session_is_current():
                    return
                if not frames:
                    yield ": ping\n\n"
                    continue
                for frame in frames:
                    yield frame
        finally:
            koth_scorer.event_bus.unsubscribe(sub)

    # No stream_with_context: this generator never touches `request`, and
    # wrapping it pins a full Flask request context (and its environ) for
    # the lifetime of every open stream.
    return Response(gen(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        # Defeat proxy buffering, which otherwise holds frames until a
        # buffer fills and makes the scoreboard look frozen.
        'X-Accel-Buffering': 'no',
    })
