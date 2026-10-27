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

DEPLOYMENT_SCOPES = ('', 'individual', 'group')


def _select_deployment(user, action):
    """Find the live records of the one deployment a My Labs card names.

    Returns (vm_records, deployment_group_name, error_response); when
    error_response is set, nothing was selected and the route returns it.

    One name can cover several deployments: a student's own lab and their
    group's lab may share it, a student who changed groups still matches
    the old group's lab through created_by_user_id, and for an admin a group
    slug may equal a student's name. deployment_scope and group_id pick one
    of them. A request that still names more than one is refused rather
    than applied to all of them.
    """
    deployment_group_name = request.form.get('deployment_group', '').strip()
    student_name = request.form.get('student_name', '').strip()
    deployment_scope = request.form.get('deployment_scope', '').strip().lower()
    raw_group_id = request.form.get('group_id', '').strip()

    def refuse(message, status=200):
        return [], deployment_group_name, (
            jsonify({'success': False, 'error': message}), status)

    if not deployment_group_name:
        return refuse('Deployment group name required')
    if deployment_scope not in DEPLOYMENT_SCOPES:
        return refuse('deployment_scope must be "individual" or "group"')
    group_id = None
    if raw_group_id:
        try:
            group_id = int(raw_group_id)
        except ValueError:
            return refuse('group_id must be a number')
        if deployment_scope == 'individual':
            return refuse('group_id names a group deployment, not an '
                          'individual one')
    if user.is_admin and not student_name:
        return refuse(f'Admin must specify student_name to {action} '
                      'deployment group')

    query = VMRecord.query.filter(
        VMRecord.template_name.like(f"{deployment_group_name}/%"),
        VMRecord.destroyed == False,
    )
    if user.is_admin:
        # Without the student filter, an admin acting on one dashboard row
        # acted on every student's copy of the same lab.
        query = query.filter(VMRecord.student_name == student_name)
    else:
        member_group_ids = [row.group_id for row in
                            StudentGroupMember.query.filter_by(user_id=user.id).all()]
        if member_group_ids:
            query = query.filter(or_(
                VMRecord.created_by_user_id == user.id,
                VMRecord.owner_group_id.in_(member_group_ids),
            ))
        else:
            query = query.filter(VMRecord.created_by_user_id == user.id)
    if deployment_scope == 'individual':
        query = query.filter(VMRecord.owner_group_id.is_(None))
    elif deployment_scope == 'group':
        query = query.filter(VMRecord.owner_group_id.isnot(None))
    if group_id is not None:
        query = query.filter(VMRecord.owner_group_id == group_id)
    vm_records = query.all()

    # Pages loaded before a field existed omit it; such a request is
    # refused when the name is ambiguous.
    if len({vm.owner_group_id for vm in vm_records}) > 1:
        return refuse(f'"{deployment_group_name}" names more than one of '
                      'your deployments. Reload the page and use the '
                      f'{action.capitalize()} button on the one you want.')

    if not vm_records:
        if user.is_admin:
            return refuse(f'No VMs found for student "{student_name}" in '
                          f'deployment group "{deployment_group_name}"')
        return refuse('No VMs found in deployment group '
                      f'"{deployment_group_name}"')

    if not user.is_admin:
        for vm_record in vm_records:
            if not user_can_access_vm(user, vm_record):
                return refuse(f'You can only {action} your own deployment '
                              'groups', 403)

    return vm_records, deployment_group_name, None



@vm_lifecycle_bp.route('/extend', methods=['POST'])
@login_required
def extend_vm():
    """
    Extend VM lifetime for one student's active deployment.

    Non-admin users can only extend VMs they may access. Admin users must
    identify the student whose deployment is being extended.

    Expected form data:
        - deployment_group: str - Deployment group name to extend
        - student_name: str - Student name (required for admins)
        - deployment_scope: 'individual' | 'group' - which of the same-named
          deployments to extend
        - group_id: int - the owning group, for a group deployment
        See _select_deployment for how these pick one deployment.

    Returns:
        JSON: { success: bool, new_expiration: str, extended_count: int }
    """
    user = get_authenticated_user()

    try:
        # Scope the deployment before checking its extension window.
        vm_records, deployment_group_name, error = _select_deployment(
            user, 'extend')
        if error:
            return error

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

        # Extend each VM under its lifecycle lock, re-reading the row first.
        # The expiry worker decides and destroys under the same lock, so an
        # extension either commits before its expiry re-check (and the VM
        # survives) or waits until the destroy finished (and finds no row),
        # never in between.
        proxmox = get_proxmox_connection()
        extended_count = 0
        new_expiration = None

        # Capture identities first: each rollback below expires the loaded
        # objects, and touching one whose row was retired would raise.
        targets = [(vm.id, vm.vmid) for vm in vm_records]
        for record_id, vmid in targets:
            with vm_lifecycle_lock(vmid):
                # End MySQL's REPEATABLE READ snapshot under the lock.
                db.session.rollback()
                vm_record = db.session.get(VMRecord, record_id)
                if (vm_record is None or vm_record.destroyed
                        or vm_record.vmid != vmid):
                    continue

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

        if new_expiration is None:
            return jsonify({
                'success': False,
                'error': f'No live VMs left to extend in deployment group "{deployment_group_name}"',
            })

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
    Destroy one deployment.

    Non-admin users can only destroy VMs they may access. Admin users must
    identify the student whose deployment is being destroyed.

    Expected form data:
        - deployment_group: str - Deployment group name to destroy (required)
        - student_name: str - Student name (required for admins)
        - deployment_scope: 'individual' | 'group' - which of the same-named
          deployments to destroy
        - group_id: int - the owning group, for a group deployment
        See _select_deployment for how these pick one deployment.

    Returns:
        JSON: { success: bool, output: str, destroyed_count: int }
    """
    user = get_authenticated_user()

    try:
        vm_records, deployment_group_name, error = _select_deployment(
            user, 'destroy')
        if error:
            return error

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
