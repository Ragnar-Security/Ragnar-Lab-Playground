"""
Admin-only routes for Lab Playground.

Provides student management, lab assignment restrictions, bulk VM operations,
and alert testing. All endpoints require admin role unless noted otherwise.
"""

from flask import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
import time
import os
import logging
import requests

logger = logging.getLogger(__name__)

from ..auth import login_required, admin_required, get_authenticated_user
from ..models import db, VMRecord, User, StudentLabAssignment
from .. import proxmox_service, tasks
from .utils import _config_issue_alerts_sent

# Create admin blueprint
admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin/test-alerts', methods=['POST'])
@login_required
@admin_required
def test_alerts():
    """
    Admin-only endpoint to test alert notifications (email and webhook).
    Sends test alerts using mock VM data to verify configuration.

    Expected form data:
        - test_email: bool - Whether to test email alerts (optional, default: true)
        - test_webhook: bool - Whether to test webhook alerts (optional, default: true)

    Returns:
        JSON: {
            success: bool,
            results: {
                email: { sent: bool, error: str },
                webhook: { sent: bool, error: str },
                database: { created: bool, error: str },
                file: { logged: bool, error: str }
            }
        }
    """
    try:
        # Get test options from request
        test_email = request.form.get('test_email', 'true').lower() == 'true'
        test_webhook = request.form.get('test_webhook', 'true').lower() == 'true'

        # Create mock VM record for testing
        mock_vm_name = f"test-alert-vm-{int(time.time())}"
        mock_vmid = 999999

        # Create a mock VMRecord object (not persisted to database)
        mock_vm_record = VMRecord(
            vmid=mock_vmid,
            vm_name=mock_vm_name,
            vm_type='lxc',
            node='test-node',
            template_name='test-template',
            student_name='test-admin',
            memory=2048,
            cores=2,
            storage=20,
            created_at=datetime.now(),
            expires_at=datetime.now(),
            deletion_retry_count=5
        )

        # Collect results
        results = {}

        # Test file logging (always enabled)
        try:
            tasks._log_alert_to_file(mock_vm_record, "TEST ALERT - Manual test from admin interface")
            results['file'] = {'logged': True}
        except Exception as e:
            results['file'] = {'logged': False, 'error': str(e)}

        # Test database alert (always enabled)
        try:
            with current_app.app_context():
                tasks._create_database_alert(mock_vm_record, "TEST ALERT - Manual test from admin interface")
                db.session.commit()
            results['database'] = {'created': True}
        except Exception as e:
            results['database'] = {'created': False, 'error': str(e)}

        # Test email alerts (if requested and configured)
        if test_email:
            try:
                tasks._send_email_alert(mock_vm_record, "TEST ALERT - Manual test from admin interface")
                results['email'] = {'sent': True}
            except Exception as e:
                results['email'] = {'sent': False, 'error': str(e)}
        else:
            results['email'] = {'sent': False, 'error': 'Email test not requested'}

        # Test webhook alerts (if requested and configured)
        if test_webhook:
            try:
                tasks._send_webhook_alert(mock_vm_record, "TEST ALERT - Manual test from admin interface")
                results['webhook'] = {'sent': True}
            except Exception as e:
                results['webhook'] = {'sent': False, 'error': str(e)}
        else:
            results['webhook'] = {'sent': False, 'error': 'Webhook test not requested'}

        # Determine overall success
        any_success = (results.get('file', {}).get('logged', False) or
                      results.get('database', {}).get('created', False) or
                      results.get('email', {}).get('sent', False) or
                      results.get('webhook', {}).get('sent', False))

        return jsonify({
            'success': any_success,
            'results': results,
            'message': 'Test alerts completed. Check results for details.'
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Error testing alerts: {str(e)}'
        }), 500


@admin_bp.route('/admin/students', methods=['GET'])
@login_required
@admin_required
def list_students():
    """List all registered students (for assignment UI dropdowns)"""
    users = User.query.order_by(User.login_id).all()
    return jsonify({
        'success': True,
        'students': [
            {
                'id': u.id,
                'login_id': u.login_id,
                'full_name': u.full_name,
                'is_admin': u.is_admin
            }
            for u in users
        ]
    })


@admin_bp.route('/admin/students/full', methods=['GET'])
@login_required
@admin_required
def list_students_full():
    """List all students with full profile details, VLAN tags, and a count of their active (non-destroyed) VMs."""
    users = User.query.order_by(User.login_id).all()
    vm_counts = {}
    active_vms = VMRecord.query.filter_by(destroyed=False).all()
    for vm in active_vms:
        uid = vm.created_by_user_id
        if uid:
            vm_counts[uid] = vm_counts.get(uid, 0) + 1
    return jsonify({
        'success': True,
        'students': [
            {
                'id': u.id, 'login_id': u.login_id, 'full_name': u.full_name,
                'email': u.email, 'is_admin': u.is_admin, 'disabled': u.disabled,
                'entry_vlan_tag': u.entry_vlan_tag, 'internal_vlan_tag': u.internal_vlan_tag,
                'last_login': u.last_login.isoformat() if u.last_login else None,
                'created_at': u.created_at.isoformat(),
                'active_vm_count': vm_counts.get(u.id, 0)
            }
            for u in users
        ]
    })


@admin_bp.route('/admin/students/<int:user_id>', methods=['PUT'])
@login_required
@admin_required
def update_student(user_id):
    """Update a student's account properties. Accepts JSON with 'is_admin' and/or 'disabled' boolean fields."""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    data = request.get_json() if request.is_json else {}
    if 'is_admin' in data:
        user.is_admin = bool(data['is_admin'])
    if 'disabled' in data:
        disabled = bool(data['disabled'])
        if disabled and not user.disabled:
            user.session_version = User.session_version + 1
        user.disabled = disabled
    db.session.commit()
    return jsonify({'success': True, 'user': user.to_dict()})


@admin_bp.route('/admin/assignments', methods=['GET'])
@login_required
@admin_required
def list_assignments():
    """List all lab assignments. Supports ?user_id= and ?group= query params for filtering."""
    user_id = request.args.get('user_id', type=int)
    group = request.args.get('group', type=str)

    query = StudentLabAssignment.query
    if user_id:
        query = query.filter_by(user_id=user_id)
    if group:
        query = query.filter_by(deployment_group_name=group)

    assignments = query.order_by(StudentLabAssignment.assigned_at.desc()).all()
    return jsonify({
        'success': True,
        'assignments': [a.to_dict() for a in assignments]
    })


@admin_bp.route('/admin/assignments', methods=['POST'])
@login_required
@admin_required
def create_assignment():
    """
    Assign a single student to a deployment group. Once any student is assigned
    to a group, that group becomes restricted -- only assigned students can see it
    in the /templates endpoint. Returns 409 if the assignment already exists.
    """
    if request.is_json:
        user_id = request.json.get('user_id')
        deployment_group_name = request.json.get('deployment_group_name', '').strip()
    else:
        user_id = request.form.get('user_id', type=int)
        deployment_group_name = request.form.get('deployment_group_name', '').strip()

    if not user_id or not deployment_group_name:
        return jsonify({'success': False, 'error': 'user_id and deployment_group_name are required'}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    existing = StudentLabAssignment.query.filter_by(
        user_id=user_id, deployment_group_name=deployment_group_name
    ).first()
    if existing:
        return jsonify({'success': False, 'error': 'Assignment already exists'}), 409

    assignment = StudentLabAssignment(
        user_id=user_id,
        deployment_group_name=deployment_group_name,
        assigned_by=get_authenticated_user().id
    )
    db.session.add(assignment)
    db.session.commit()

    return jsonify({'success': True, 'assignment': assignment.to_dict()}), 201


@admin_bp.route('/admin/assignments/<int:assignment_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_assignment(assignment_id):
    """Remove a specific lab assignment by its primary key. If this was the last assignment
    for the group, the group becomes unrestricted (visible to all students) again."""
    assignment = db.session.get(StudentLabAssignment, assignment_id)
    if not assignment:
        return jsonify({'success': False, 'error': 'Assignment not found'}), 404

    db.session.delete(assignment)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Assignment removed'})


@admin_bp.route('/admin/assignments/bulk', methods=['POST'])
@login_required
@admin_required
def bulk_assign():
    """Bulk assign multiple students to multiple deployment groups.
    Accepts JSON with 'user_ids' and 'deployment_group_names' arrays.
    Creates the cartesian product of assignments, skipping any that already exist."""
    data = request.get_json() if request.is_json else {}
    user_ids = data.get('user_ids', [])
    deployment_group_names = data.get('deployment_group_names', [])

    if not user_ids or not deployment_group_names:
        return jsonify({'success': False, 'error': 'user_ids and deployment_group_names are required'}), 400

    # Validate all user_ids exist
    existing_users = {u.id for u in User.query.filter(User.id.in_(user_ids)).all()}
    invalid_ids = [uid for uid in user_ids if uid not in existing_users]
    if invalid_ids:
        return jsonify({'success': False, 'error': f'Invalid user IDs: {invalid_ids}'}), 400

    created = 0
    skipped = 0
    for uid in user_ids:
        for group_name in deployment_group_names:
            existing = StudentLabAssignment.query.filter_by(
                user_id=uid, deployment_group_name=group_name
            ).first()
            if existing:
                skipped += 1
                continue
            assignment = StudentLabAssignment(
                user_id=uid,
                deployment_group_name=group_name,
                assigned_by=get_authenticated_user().id
            )
            db.session.add(assignment)
            created += 1

    db.session.commit()
    return jsonify({
        'success': True,
        'created': created,
        'skipped': skipped,
        'message': f'Created {created} assignments, skipped {skipped} duplicates'
    })


@admin_bp.route('/admin/assignments/clear', methods=['POST'])
@login_required
@admin_required
def clear_assignments():
    """Remove all assignments for a deployment group, making it unrestricted (visible to all students)."""
    deployment_group_name = (request.json.get('deployment_group_name') if request.is_json else request.form.get('deployment_group_name', '').strip())

    if not deployment_group_name:
        return jsonify({'success': False, 'error': 'deployment_group_name is required'}), 400

    count = StudentLabAssignment.query.filter_by(
        deployment_group_name=deployment_group_name
    ).delete()
    db.session.commit()

    return jsonify({
        'success': True,
        'deleted': count,
        'message': f'Removed {count} assignments for {deployment_group_name}'
    })


@admin_bp.route('/admin/vms', methods=['GET'])
@login_required
@admin_required
def list_all_vms():
    """List all VMs with optional query-param filters: ?student=, ?group=, ?node=, ?status=(active|destroyed)."""
    student = request.args.get('student')
    group = request.args.get('group')
    node = request.args.get('node')
    status = request.args.get('status')

    query = VMRecord.query
    if student:
        query = query.filter(VMRecord.student_name == student)
    if group:
        query = query.filter(VMRecord.template_name.like(f"{group}/%"))
    if node:
        query = query.filter(VMRecord.node == node)
    if status == 'active':
        query = query.filter(VMRecord.destroyed == False)
    elif status == 'destroyed':
        query = query.filter(VMRecord.destroyed == True)

    vms = query.order_by(VMRecord.created_at.desc()).all()
    return jsonify({'success': True, 'vms': [vm.to_dict() for vm in vms]})


@admin_bp.route('/admin/bulk-destroy', methods=['POST'])
@login_required
@admin_required
def bulk_destroy():
    """Destroy multiple VMs: stop in Proxmox, delete from Proxmox, remove DB record."""
    data = request.get_json() if request.is_json else {}
    vm_ids = data.get('vm_ids', [])
    if not vm_ids:
        return jsonify({'success': False, 'error': 'vm_ids required'}), 400

    destroyed = 0
    errors = []
    proxmox = proxmox_service.get_proxmox_connection()

    for vm_id in vm_ids:
        candidate = db.session.get(VMRecord, vm_id)
        if not candidate:
            continue
        vmid = candidate.vmid
        with proxmox_service.vm_lifecycle_lock(vmid):
            # End the request's existing REPEATABLE READ snapshot before the
            # authoritative reread protected by this VMID lock.
            db.session.rollback()
            vm = db.session.get(VMRecord, vm_id)
            if not vm or vm.destroyed:
                continue

            identity = proxmox_service.inspect_vm_guest(
                proxmox, vm.vmid, node=vm.node, vm_name=vm.vm_name,
                vm_type=vm.vm_type)
            if identity == proxmox_service.VM_GUEST_ABSENT:
                tasks._forget_vm_caches(vm.node, vm.vmid)
                tasks._retire_vm_record(vm)
                destroyed += 1
                db.session.commit()
                continue
            if identity != proxmox_service.VM_GUEST_MATCH:
                errors.append(
                    f"VM {vm.vmid}: guest identity could not be verified")
                continue

            endpoint = (
                proxmox.nodes(vm.node).qemu(vm.vmid)
                if vm.vm_type == 'qemu'
                else proxmox.nodes(vm.node).lxc(vm.vmid)
            )
            try:
                try:
                    stop_upid = endpoint.status.stop.post()
                    stop_result = proxmox_service.wait_for_proxmox_task(
                        proxmox, vm.node, stop_upid,
                        timeout=60, poll_interval=1)
                    if not stop_result.get('success'):
                        logger.debug(
                            "Stop task for VM %s returned non-OK: %s",
                            vm.vmid,
                            stop_result.get(
                                'error', stop_result.get('status')))
                except Exception as stop_error:
                    logger.warning(
                        "Stop request for VM %s failed; attempting destroy: %s",
                        vm.vmid, stop_error)

                delete_upid = endpoint.delete()
                delete_result = proxmox_service.wait_for_proxmox_task(
                    proxmox, vm.node, delete_upid,
                    timeout=120, poll_interval=1)
                if not delete_result.get('success'):
                    errors.append(
                        f"VM {vm.vmid}: "
                        f"{delete_result.get('error', delete_result.get('status'))}")
                    continue
            except Exception as error:
                if not proxmox_service.confirm_proxmox_absence(
                        proxmox, vm.vmid, error):
                    errors.append(f"VM {vm.vmid}: {str(error)}")
                    continue

            tasks._forget_vm_caches(vm.node, vm.vmid)
            tasks._retire_vm_record(vm)
            destroyed += 1
            db.session.commit()

    db.session.commit()
    return jsonify({
        'success': True, 'destroyed': destroyed, 'errors': errors,
        'message': f'Destroyed {destroyed} VMs' + (f', {len(errors)} errors' if errors else '')
    })


@admin_bp.route('/admin/bulk-extend', methods=['POST'])
@login_required
@admin_required
def bulk_extend():
    """Extend expiration for multiple VMs by a given number of minutes (default 1440 / 24 hours).
    Unlike the student extend endpoint, this has no minimum-remaining-time restriction."""
    data = request.get_json() if request.is_json else {}
    vm_ids = data.get('vm_ids', [])
    minutes = data.get('minutes', 1440)
    if not vm_ids:
        return jsonify({'success': False, 'error': 'vm_ids required'}), 400

    extended = 0
    for vm_id in vm_ids:
        vm = db.session.get(VMRecord, vm_id)
        if not vm or vm.destroyed:
            continue
        vm.expires_at = vm.expires_at + timedelta(minutes=minutes)
        extended += 1

    db.session.commit()
    return jsonify({
        'success': True, 'extended': extended,
        'message': f'Extended {extended} VMs by {minutes} minutes'
    })


def _send_config_issue_webhook(vm_record, error_msg):
    """
    Send webhook alert when VM config file corruption is detected
    Only sends once per VM per hour to prevent spam

    Args:
        vm_record: VMRecord object with VM details
        error_msg: The error message from Proxmox
    """
    try:
        # Check if we already sent an alert for this VM recently (within 1 hour)
        vmid = vm_record.vmid
        current_time = datetime.now()

        if vmid in _config_issue_alerts_sent:
            last_alert_time = _config_issue_alerts_sent[vmid]
            time_since_alert = current_time - last_alert_time
            if time_since_alert.total_seconds() < 3600:  # 1 hour
                current_app.logger.info(f"Config issue alert for VM {vmid} already sent {int(time_since_alert.total_seconds()/60)} minutes ago - skipping")
                return

        webhook_url = os.environ.get('ALERT_WEBHOOK_URL')
        webhook_type = os.environ.get('ALERT_WEBHOOK_TYPE', 'slack')

        if not webhook_url:
            current_app.logger.info("Webhook notifications not configured - skipping config issue alert")
            return

        alert_message = (f"VM {vm_record.vmid} ({vm_record.vm_name}) on node {vm_record.node} "
                        f"has configuration file corruption. Manual intervention required.")

        # Format message for webhook type
        if webhook_type == 'discord':
            payload = {
                "embeds": [{
                    "title": "⚠️ VM Config File Corruption Detected",
                    "description": alert_message,
                    "color": 16744272,  # Orange
                    "fields": [
                        {"name": "VMID", "value": str(vm_record.vmid), "inline": True},
                        {"name": "Name", "value": vm_record.vm_name, "inline": True},
                        {"name": "Node", "value": vm_record.node, "inline": True},
                        {"name": "Student", "value": vm_record.student_name, "inline": True},
                        {"name": "Error", "value": error_msg[:1000] if error_msg else "Unknown", "inline": False},
                        {"name": "Fix", "value": f"`ssh root@{vm_record.node} 'qm destroy {vm_record.vmid} --purge --skiplock'`", "inline": False}
                    ],
                    "timestamp": datetime.now().isoformat()
                }]
            }
        else:  # Slack format
            payload = {
                "text": f"⚠️ *VM Config File Corruption Detected*\n{alert_message}",
                "attachments": [{
                    "color": "warning",
                    "fields": [
                        {"title": "VMID", "value": str(vm_record.vmid), "short": True},
                        {"title": "Name", "value": vm_record.vm_name, "short": True},
                        {"title": "Node", "value": vm_record.node, "short": True},
                        {"title": "Student", "value": vm_record.student_name, "short": True},
                        {"title": "Error", "value": error_msg[:500] if error_msg else "Unknown", "short": False},
                        {"title": "Fix", "value": f"`ssh root@{vm_record.node} 'qm destroy {vm_record.vmid} --purge --skiplock'`", "short": False}
                    ]
                }]
            }

        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        current_app.logger.info(f"Config issue webhook alert sent successfully for VM {vm_record.vmid} ({webhook_type})")

        # Record that we sent the alert
        _config_issue_alerts_sent[vmid] = current_time

        # Clean up old entries (> 2 hours old)
        expired_vmids = [vid for vid, timestamp in _config_issue_alerts_sent.items()
                        if (current_time - timestamp).total_seconds() > 7200]
        for vid in expired_vmids:
            del _config_issue_alerts_sent[vid]

    except Exception as e:
        current_app.logger.error(f"Error sending config issue webhook alert: {str(e)}")
