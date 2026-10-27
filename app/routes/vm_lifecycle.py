"""
VM Lifecycle routes module
Handles VM extension and destruction operations
"""

from flask import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
import time
import logging

from .. import tasks
from ..models import db, VMRecord, User, StudentGroupMember
from ..proxmox_service import (
    confirm_proxmox_absence, get_proxmox_connection, inspect_vm_guest,
    reset_cluster_vm_index, vm_lifecycle_lock, wait_for_proxmox_task,
    VM_GUEST_ABSENT, VM_GUEST_MATCH,
)
from ..auth import login_required, get_authenticated_user
from .utils import (
    _verify_vm_exists, user_can_access_vm, invalidate_vm_ip_cache,
    VM_CONFIG_ERROR,
)
from sqlalchemy import or_

logger = logging.getLogger(__name__)

# Create blueprint
vm_lifecycle_bp = Blueprint('vm_lifecycle', __name__)


@vm_lifecycle_bp.route('/extend', methods=['POST'])
@login_required
def extend_vm():
    """
    Extend VM lifetime for one student's active deployment group.

    Non-admin users can only extend VMs they may access. Admin users must
    identify the student whose deployment is being extended.

    Expected form data:
        - deployment_group: str - Deployment group name to extend
        - student_name: str - Student name (required for admins)

    Returns:
        JSON: { success: bool, new_expiration: str, extended_count: int }
    """
    user = get_authenticated_user()
    user_id = user.id

    deployment_group_name = request.form.get('deployment_group', '').strip()
    student_name = request.form.get('student_name', '').strip()
    if not deployment_group_name:
        return jsonify({'success': False, 'error': 'Deployment group name required'})

    if user.is_admin and not student_name:
        return jsonify({
            'success': False,
            'error': 'Admin must specify student_name to extend deployment group',
        })

    try:
        # Scope the deployment before checking its extension window. Without
        # the student filter, an admin extending one dashboard row extended
        # every student's copy of the same lab.
        if user.is_admin:
            vm_records = VMRecord.query.filter(
                VMRecord.template_name.like(f"{deployment_group_name}/%"),
                VMRecord.student_name == student_name,
                VMRecord.destroyed == False,
            ).all()
        else:
            member_group_ids = [row.group_id for row in
                                StudentGroupMember.query.filter_by(user_id=user_id).all()]
            if member_group_ids:
                vm_records = VMRecord.query.filter(
                    VMRecord.template_name.like(f"{deployment_group_name}/%"),
                    or_(
                        VMRecord.created_by_user_id == user_id,
                        VMRecord.owner_group_id.in_(member_group_ids),
                    ),
                    VMRecord.destroyed == False,
                ).all()
            else:
                vm_records = VMRecord.query.filter(
                    VMRecord.template_name.like(f"{deployment_group_name}/%"),
                    VMRecord.created_by_user_id == user_id,
                    VMRecord.destroyed == False,
                ).all()

        if not vm_records:
            if user.is_admin:
                return jsonify({
                    'success': False,
                    'error': f'No live VMs found for student "{student_name}" in deployment group "{deployment_group_name}"',
                })
            return jsonify({'success': False, 'error': f'No VMs found in deployment group "{deployment_group_name}"'})

        # Check authorization for non-admins
        if not user.is_admin:
            for vm_record in vm_records:
                if not user_can_access_vm(user, vm_record):
                    return jsonify({'success': False, 'error': 'You can only extend your own deployment groups'}), 403

        # Check if any VM has less than 60 minutes remaining
        can_extend = False
        for vm_record in vm_records:
            time_remaining = vm_record.expires_at - datetime.now()
            minutes_remaining = int(time_remaining.total_seconds() / 60)
            if minutes_remaining < 60:
                can_extend = True
                break

        if not can_extend:
            return jsonify({
                'success': False,
                'error': f'Deployment group cannot be extended. All VMs must have less than 60 minutes remaining.'
            })

        # Get extend_time_minutes from the deployment group configuration
        extend_minutes = 1440  # Default fallback

        from ..template_manager import load_template_config, get_deployment_group_by_name
        config = load_template_config()
        deployment_group = get_deployment_group_by_name(config, deployment_group_name)

        if deployment_group:
            extend_minutes = deployment_group.get('extend_time_minutes', 1440)

        # Extend all VMs in the deployment group
        proxmox = get_proxmox_connection()
        extended_count = 0
        new_expiration = None

        for vm_record in vm_records:
            # Update expiration time
            new_expiration = vm_record.expires_at + timedelta(minutes=extend_minutes)
            vm_record.expires_at = new_expiration

            # Update VM description in Proxmox
            try:
                if vm_record.vm_type == 'lxc':
                    proxmox.nodes(vm_record.node).lxc(vm_record.vmid).config.put(
                        description=f"Expires: {new_expiration.isoformat()}"
                    )
                else:
                    proxmox.nodes(vm_record.node).qemu(vm_record.vmid).config.put(
                        description=f"Expires: {new_expiration.isoformat()}"
                    )
                extended_count += 1
            except Exception as e:
                logger.warning(f"Warning: Could not update Proxmox description for VM {vm_record.vmid}: {str(e)}")

        db.session.commit()

        return jsonify({
            'success': True,
            'output': f"Deployment group \"{deployment_group_name}\" extended by {extend_minutes} minutes. New expiration: {new_expiration.isoformat()}",
            'extended_count': extended_count,
            'new_expiration': new_expiration.isoformat()
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})


@vm_lifecycle_bp.route('/destroy', methods=['POST'])
@login_required
def destroy_vm():
    """
    Destroy an entire deployment group.

    Non-admin users: Can only destroy their own deployment groups (deployment_group param only)
    Admin users: Must specify both student_name and deployment_group

    Expected form data:
        - deployment_group: str - Deployment group name to destroy (required)
        - student_name: str - Student name (required for admin, ignored for non-admin)

    Returns:
        JSON: { success: bool, output: str, destroyed_count: int }
    """
    user = get_authenticated_user()
    user_id = user.id

    deployment_group_name = request.form.get('deployment_group', '').strip()
    student_name = request.form.get('student_name', '').strip()

    try:
        if not deployment_group_name:
            return jsonify({'success': False, 'error': 'Deployment group name required'})

        # Admin must specify student_name
        if user.is_admin and not student_name:
            return jsonify({'success': False, 'error': 'Admin must specify student_name to destroy deployment group'})

        # Find all VMs in this deployment group
        if user.is_admin:
            # Admin destroys specific student's deployment group
            vm_records = VMRecord.query.filter(
                VMRecord.template_name.like(f"{deployment_group_name}/%"),
                VMRecord.student_name == student_name,
                VMRecord.destroyed == False,
            ).all()
        else:
            # Students can destroy their own groups or their group's VMs
            member_group_ids = [row.group_id for row in
                                StudentGroupMember.query.filter_by(user_id=user_id).all()]
            if member_group_ids:
                vm_records = VMRecord.query.filter(
                    VMRecord.template_name.like(f"{deployment_group_name}/%"),
                    VMRecord.destroyed == False,
                    or_(
                        VMRecord.created_by_user_id == user_id,
                        VMRecord.owner_group_id.in_(member_group_ids),
                    ),
                ).all()
            else:
                vm_records = VMRecord.query.filter(
                    VMRecord.template_name.like(f"{deployment_group_name}/%"),
                    VMRecord.destroyed == False,
                    VMRecord.created_by_user_id == user_id,
                ).all()

        if not vm_records:
            if user.is_admin:
                return jsonify({'success': False, 'error': f'No VMs found for student "{student_name}" in deployment group "{deployment_group_name}"'})
            else:
                return jsonify({'success': False, 'error': f'No VMs found in deployment group "{deployment_group_name}"'})

        # Check authorization for non-admins
        if not user.is_admin:
            for vm_record in vm_records:
                if not user_can_access_vm(user, vm_record):
                    return jsonify({'success': False, 'error': 'You can only destroy your own deployment groups'}), 403

        proxmox = get_proxmox_connection()
        destroyed_vms = []
        failed_vms = []

        # Destroy each VM in the deployment group.
        for candidate in vm_records:
            record_id = candidate.id
            vmid = candidate.vmid
            expected_identity = (
                candidate.vm_name, candidate.node, candidate.vm_type)

            with vm_lifecycle_lock(vmid):
                # End MySQL's REPEATABLE READ snapshot under the lock so this
                # lookup sees any concurrent retirement or replacement.
                db.session.rollback()
                vm_record = db.session.get(VMRecord, record_id)
                if vm_record is None or vm_record.destroyed:
                    continue
                if (
                    vm_record.vmid != vmid
                    or (vm_record.vm_name, vm_record.node, vm_record.vm_type)
                    != expected_identity
                ):
                    logger.warning(
                        "Skipping stale destroy request for VMID %s", vmid)
                    continue

                identity = inspect_vm_guest(
                    proxmox, vm_record.vmid,
                    node=vm_record.node,
                    vm_name=vm_record.vm_name,
                    vm_type=vm_record.vm_type,
                )
                if identity == VM_GUEST_ABSENT:
                    invalidate_vm_ip_cache(vm_record.node, vm_record.vmid)
                    reset_cluster_vm_index()
                    tasks._retire_vm_record(vm_record)
                    destroyed_vms.append(vm_record.vm_name)
                    db.session.commit()
                    continue
                if identity != VM_GUEST_MATCH:
                    failed_vms.append({
                        'vm_name': vm_record.vm_name,
                        'vmid': vm_record.vmid,
                        'error': 'guest identity could not be verified',
                    })
                    continue

                try:
                    vm_state, _config_error = _verify_vm_exists(
                        proxmox, vm_record.node, vm_record.vmid,
                        vm_record.vm_type)
                    has_config_issues = vm_state == VM_CONFIG_ERROR
                    endpoint = (
                        proxmox.nodes(vm_record.node).lxc(vm_record.vmid)
                        if vm_record.vm_type == 'lxc'
                        else proxmox.nodes(vm_record.node).qemu(vm_record.vmid)
                    )

                    if has_config_issues:
                        current_app.logger.warning(
                            f"VM {vm_record.vmid} has config issues - "
                            "attempting force delete")
                    else:
                        try:
                            stop_upid = endpoint.status.stop.post()
                            stop_result = wait_for_proxmox_task(
                                proxmox, vm_record.node, stop_upid,
                                timeout=60, poll_interval=1)
                            if not stop_result.get('success'):
                                logger.debug(
                                    f"Stop task for VM {vm_record.vmid} "
                                    "returned non-OK: "
                                    f"{stop_result.get('error', stop_result.get('status'))}")
                        except Exception as stop_error:
                            logger.warning(
                                f"Stop request for VM {vm_record.vmid} "
                                f"failed (will still attempt delete): {stop_error}")

                    try:
                        if has_config_issues and vm_record.vm_type == 'qemu':
                            delete_upid = endpoint.delete(purge=1, skiplock=1)
                        elif has_config_issues:
                            delete_upid = endpoint.delete(purge=1, force=1)
                        else:
                            delete_upid = endpoint.delete()
                    except Exception as delete_error:
                        if confirm_proxmox_absence(
                                proxmox, vm_record.vmid, delete_error):
                            delete_upid = None
                        else:
                            raise

                    if delete_upid is not None:
                        delete_result = wait_for_proxmox_task(
                            proxmox, vm_record.node, delete_upid,
                            timeout=120, poll_interval=1)
                        if not delete_result.get('success'):
                            raise RuntimeError(
                                f"Destroy task for VM {vm_record.vmid} failed: "
                                f"{delete_result.get('error', delete_result.get('status'))}")

                    invalidate_vm_ip_cache(vm_record.node, vm_record.vmid)
                    reset_cluster_vm_index()
                    tasks._retire_vm_record(vm_record)
                    destroyed_vms.append(vm_record.vm_name)
                    db.session.commit()

                except Exception as vm_error:
                    failed_vms.append({
                        'vm_name': vm_record.vm_name,
                        'vmid': vm_record.vmid,
                        'error': str(vm_error),
                    })
                    logger.error(
                        f"Error destroying VM {vm_record.vmid}: {vm_error}")
        # Commit database changes
        db.session.commit()

        # Build response message
        if failed_vms:
            error_details = ", ".join([f"{vm['vm_name']} ({vm['error']})" for vm in failed_vms])
            return jsonify({
                'success': False,
                'error': f'Destroyed {len(destroyed_vms)} VM(s), but {len(failed_vms)} failed: {error_details}',
                'destroyed_count': len(destroyed_vms),
                'failed_count': len(failed_vms)
            })

        return jsonify({
            'success': True,
            'output': f'Deployment group "{deployment_group_name}" destroyed successfully ({len(destroyed_vms)} VM(s))',
            'destroyed_count': len(destroyed_vms)
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})
