"""
VM status and monitoring routes
Handles VM expiration time and status queries
"""

from flask import Blueprint, request, jsonify, session
from datetime import datetime
from ..auth import login_required
from ..models import db, User, VMRecord
from .utils import user_can_access_vm

# Create VM status blueprint
vm_status_bp = Blueprint('vm_status', __name__)


@vm_status_bp.route('/time-remaining', methods=['GET'])
@login_required
def time_remaining():
    """Get time remaining for a VM the logged-in user may access."""
    vm_id = request.args.get('vm_id', '').strip()
    try:
        vmid = int(vm_id)
    except ValueError:
        return jsonify({'success': False, 'error': 'Valid VM ID required'}), 400

    user = db.session.get(User, session.get('user_id'))
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401

    vm_record = VMRecord.query.filter_by(vmid=vmid, destroyed=False).first()
    if not vm_record:
        return jsonify({'success': False, 'error': f'VM {vm_id} not found'}), 404
    if not user_can_access_vm(user, vm_record):
        return jsonify({
            'success': False,
            'error': 'You do not have permission to view this VM',
        }), 403

    # The database is the lifecycle source of truth. Reading expires_at here
    # avoids exposing the cluster inventory and several Proxmox round trips
    # for information already maintained by create/extend/expiry operations.
    return _get_time_remaining(vm_record.expires_at, vm_id)


def _get_time_remaining(expiration, vm_id):
    """Calculate time remaining from the tracked database expiration."""
    remaining = expiration - datetime.now()

    if remaining.total_seconds() < 0:
        return jsonify({
            'success': True,
            'vm_id': vm_id,
            'time_remaining': 'Expired',
            'seconds_remaining': 0,
        })

    hours = remaining.total_seconds() / 3600
    return jsonify({
        'success': True,
        'vm_id': vm_id,
        'time_remaining': f'{hours:.1f} hours',
        'seconds_remaining': int(remaining.total_seconds()),
        'expires_at': expiration.isoformat(),
    })
