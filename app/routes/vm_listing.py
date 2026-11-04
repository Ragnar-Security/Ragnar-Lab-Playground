"""
VM Listing routes module.
Handles displaying and querying VM information.

Orphaned-record cleanup: when /list is called, each DB record is verified
against its own backend. A record whose VM the backend's inventory has not
listed for ORPHAN_CONFIRM_SECONDS (manually deleted, failed cleanup, etc.)
is retired through the reconcile pass's helper, under the VMID lifecycle
lock and with an admin alert, so the dashboard stays accurate.
"""

from contextlib import ExitStack
from flask import Blueprint, request, jsonify, current_app
from datetime import datetime
import logging

from .. import tasks, vm_provisioning
from ..backends import backend_for
from ..backends.base import BACKEND_PROXMOX, VMRef
from ..models import db, VMRecord, StudentGroupMember
from ..auth import login_required, get_authenticated_user
from ..template_manager import load_template_config, get_template_for_vm
from .utils import (
    _format_time_remaining, mask_ip_address, note_vm_state,
    user_can_access_vm, ORPHAN_CONFIRM_SECONDS, VM_CONFIG_ERROR, VM_MISSING,
    VM_PRESENT, VM_UNKNOWN,
)
from .admin import _send_config_issue_webhook
from sqlalchemy import or_

logger = logging.getLogger(__name__)

# Create blueprint
vm_listing_bp = Blueprint('vm_listing', __name__)


class _RenderBackends:
    """Each record's backend for one render, on one connection per backend.

    A Proxmox render must stay one login and one cluster read however many
    VMs it shows (ruling C5). A backend that cannot be resolved or reached —
    one later removed from ENABLED_BACKENDS included (ruling I18) — answers
    None, and its VMs are listed unverified with their records kept.
    """

    def __init__(self, sessions):
        self._sessions = sessions
        self._backends = {}

    def __call__(self, record):
        name = record.backend or BACKEND_PROXMOX
        if name not in self._backends:
            try:
                backend = backend_for(record)
                self._sessions.enter_context(
                    vm_provisioning.provider_session(backend))
            except Exception as error:
                current_app.logger.warning(
                    "backend %s unavailable for this render: %s", name, error)
                backend = None
            self._backends[name] = backend
        return self._backends[name]


def _display_state(status):
    """Map a backend status onto the confirmation states /list acts on."""
    if status == VM_MISSING:
        return VM_MISSING
    if status == VM_CONFIG_ERROR:
        return VM_CONFIG_ERROR
    if status == 'unknown':
        return VM_UNKNOWN
    return VM_PRESENT


def _observe(backend, record):
    """(backend, state, status, ips, diagnostic) of one record for this render.

    A disabled or unreachable backend, or a failed read, is UNKNOWN: the
    VM is listed unverified and its record is never put up for retirement.
    """
    if backend is None:
        return None, VM_UNKNOWN, 'unknown', [], None
    ref = VMRef.from_record(record)
    # Proxmox answers status, its error text (the config-error webhook
    # carries it) and addresses from one probe: an unknown guest's addresses
    # come from its cache, never from a second probe that would time out too.
    display_read = getattr(backend, 'display_read', None)
    if display_read is not None:
        try:
            status, diagnostic, ips = display_read(ref)
        except Exception as error:
            current_app.logger.warning("status of VM %s unavailable: %s",
                                       record.vmid, error)
            return backend, VM_UNKNOWN, 'unknown', [], str(error)
        return backend, _display_state(status), status, ips, diagnostic
    try:
        status, diagnostic = backend.status(ref), None
    except Exception as error:
        current_app.logger.warning("status of VM %s unavailable: %s",
                                   record.vmid, error)
        status, diagnostic = 'unknown', str(error)
    state = _display_state(status)
    ips = []
    if state in (VM_PRESENT, VM_UNKNOWN):
        try:
            ips = backend.ip_addresses(ref)
        except Exception as error:
            current_app.logger.warning("addresses of VM %s unavailable: %s",
                                       record.vmid, error)
    return backend, state, status, ips, diagnostic


def _uptime(backend, record):
    # Only Proxmox reports one; the page shows N/A for None.
    read = getattr(backend, 'uptime', None)
    return read(VMRef.from_record(record)) if read is not None else None


@vm_listing_bp.route('/list', methods=['GET'])
@login_required
def list_vms():
    """
    List VMs grouped by student and deployment group.

    Non-admin users: Only see VMs they created
    Admin users: See all VMs

    Returns:
        JSON: { success: bool, students: [...] }
    """
    try:
        # The decorator validated the revocable session user.
        user = get_authenticated_user()
        user_id = user.id

        # Get active (non-destroyed) VMs from database
        if user.is_admin:
            vm_records = VMRecord.query.filter_by(destroyed=False).all()
        else:
            member_group_ids = [row.group_id for row in
                                StudentGroupMember.query.filter_by(user_id=user_id).all()]
            if member_group_ids:
                query = VMRecord.query.filter(
                    or_(
                        VMRecord.created_by_user_id == user_id,
                        VMRecord.owner_group_id.in_(member_group_ids),
                    ),
                    VMRecord.destroyed == False,
                )
            else:
                query = VMRecord.query.filter_by(
                    created_by_user_id=user_id, destroyed=False)
            # Hide KOTH-game VMs from non-admins; they have their own dedicated UI.
            query = query.filter(VMRecord.koth_game_id.is_(None))
            vm_records = query.all()

        # Group VMs by student, then by deployment group
        students = {}
        orphaned_records = []  # Track VMs their backend no longer lists

        # Load template config once for per-VM mask_ip lookup. Admins always
        # see real IPs (so they can debug); non-admins get masked IPs for any
        # VM whose template sets mask_ip: true in templates.yaml.
        template_config = load_template_config()

        # Each record's own backend supplies status and addresses; Proxmox
        # serves both from one cached cluster read per render, and its 10/8
        # lab-address filter and cache fallback stay inside ProxmoxBackend.
        with ExitStack() as sessions:
            backend_of = _RenderBackends(sessions)
            observed = {record.id: _observe(backend_of(record), record)
                        for record in vm_records}

        for record in vm_records:
            backend, vm_state, status, ips, error_msg = observed[record.id]

            confirmed_gone = note_vm_state(record.node, record.vmid, vm_state)

            if vm_state == VM_MISSING:
                if not confirmed_gone:
                    # First sighting. The cluster inventory derives from the
                    # same .conf file the config read needs, so one "gone"
                    # answer is one witness, not two. Wait for it to still
                    # be gone ORPHAN_CONFIRM_SECONDS later before deleting
                    # the only record that keeps the expiry daemon
                    # responsible for this VM.
                    current_app.logger.warning(
                        f"VM {record.vmid} ({record.vm_name}) is not in the "
                        f"{record.backend} inventory; holding the record "
                        f"until a second sighting confirms it")
                    continue
                current_app.logger.warning(
                    f"VM {record.vmid} ({record.vm_name}) has been absent from "
                    f"the {record.backend} inventory for over "
                    f"{ORPHAN_CONFIRM_SECONDS:.0f}s - marking for cleanup")
                orphaned_records.append((record, backend))
                continue  # Skip this VM from the list

            if vm_state == VM_UNKNOWN:
                # We could not reach the backend, or it could not classify
                # the guest. Keep showing the VM and NEVER delete its row: a
                # deleted row means the expiry daemon stops tracking the
                # VM and it runs forever.
                current_app.logger.warning(
                    f"VM {record.vmid} ({record.vm_name}): state unknown "
                    f"on {record.backend} ({error_msg}); listing it "
                    f"unverified and keeping the record")

            has_config_issues = vm_state == VM_CONFIG_ERROR
            if has_config_issues:
                # The cluster still lists this VM but its config file is
                # unreadable. It is real, it is consuming resources, and it
                # needs a manual purge.
                error_msg = error_msg or 'configuration file unreadable'
                current_app.logger.error(
                    f"VM {record.vmid} ({record.vm_name}) has configuration issues: {error_msg}")
                current_app.logger.error(
                    f"  Force-delete it with: qm destroy {record.vmid} --purge")
                _send_config_issue_webhook(record, error_msg)
                # Still show it in the list but mark it as having issues

            # Extract deployment group name from template_name (format: "groupname/vmname")
            if record.template_name and '/' in record.template_name:
                group_name = record.template_name.split('/')[0]
            else:
                group_name = 'ungrouped'

            student_key = f"{record.student_name}_{record.created_by_user_id}"

            # Create student entry if doesn't exist
            if student_key not in students:
                students[student_key] = {
                    'student_name': record.student_name,
                    'created_by_user_id': record.created_by_user_id,
                    'deployment_groups': {}
                }

            # One card per deployment. A student's own lab and their group's
            # lab can share a name, as can the labs of a group the student
            # left and the one they joined; the card's scope and group id are
            # what its Extend and Destroy buttons send, so one card must
            # never mix two deployments.
            deployment_scope = 'individual' if record.owner_group_id is None else 'group'
            card_key = (group_name, deployment_scope, record.owner_group_id)
            if card_key not in students[student_key]['deployment_groups']:
                remaining_secs = int((record.expires_at - datetime.now()).total_seconds()) if record.expires_at else 0
                students[student_key]['deployment_groups'][card_key] = {
                    'group_name': group_name,
                    'deployment_scope': deployment_scope,
                    'group_id': record.owner_group_id,
                    'vm_count': 0,
                    'created_at': record.created_at.isoformat() if record.created_at else None,
                    'expires_at': record.expires_at.isoformat() if record.expires_at else None,
                    'time_remaining_seconds': remaining_secs,
                    'vms': []
                }

            # The backend already returned only usable addresses; empty
            # means none yet (or, unverified, none cached).
            if has_config_issues:
                ip_display = 'CONFIG ERROR'
                vm_status = 'error'
            elif vm_state == VM_UNKNOWN:
                ip_display = ', '.join(ips) if ips else 'UNKNOWN'
                vm_status = 'unknown'
            elif record.is_entry_point:
                ip_display = ', '.join(ips) if ips else 'N/A'
                vm_status = status
            else:
                ip_display = ', '.join(ips) if ips else '-'
                vm_status = status

            # Per-VM mask_ip flag: hide host octet from non-admins so the
            # subnet is still visible (10.101.4.xxx) but the specific host
            # isn't disclosed. Useful for recon-style lab exercises.
            tpl = get_template_for_vm(template_config, record.template_name)
            if not user.is_admin:
                if tpl and tpl.get('mask_ip', False):
                    ip_display = mask_ip_address(ip_display)

            students[student_key]['deployment_groups'][card_key]['vm_count'] += 1
            students[student_key]['deployment_groups'][card_key]['vms'].append({
                'type': record.vm_type,
                'vmid': record.vmid,
                'name': record.vm_name,
                'node': record.node,
                'status': vm_status,
                'template': record.template_name,
                'ip_address': ip_display,
                'memory': record.memory,
                'cores': record.cores,
                'has_config_issues': has_config_issues,
                'is_entry_point': record.is_entry_point,
                'connect': tpl.get('connect') if tpl else None
            })

        # Orphaned record cleanup: rows their backend's inventory has not
        # listed for ORPHAN_CONFIRM_SECONDS - deleted through the provider's
        # own UI, or a destroy that removed the VM but crashed before
        # updating the database. Only a CONFIRMED VM_MISSING reaches this
        # list. A VM we merely failed to reach, or saw missing once, must
        # never land here: the expiry daemon tracks VMs by database row, so
        # deleting the row of a live VM leaks it permanently. The reconcile
        # pass's helper makes the final decision under the VMID lifecycle
        # lock and raises the same admin alert.
        if orphaned_records:
            # Capture identities first: the helper's rollback expires the
            # loaded rows.
            targets = [(r.id, r.vmid, r.node, backend)
                       for r, backend in orphaned_records]
            for record_id, vmid, node, backend in targets:
                try:
                    tasks.reconcile_missing_vm_record(
                        backend, record_id, vmid, node)
                except Exception as cleanup_error:
                    db.session.rollback()
                    logger.error(f"Error cleaning up orphaned record {vmid}: {cleanup_error}")

        # Convert to list format
        students_list = []
        for student_data in students.values():
            students_list.append({
                'student_name': student_data['student_name'],
                'created_by_user_id': student_data['created_by_user_id'],
                'deployment_groups': list(student_data['deployment_groups'].values())
            })

        return jsonify({
            'success': True,
            'students': students_list
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@vm_listing_bp.route('/api/vms', methods=['GET'])
@login_required
def api_vms_list():
    """
    Get list of VMs from database filtered by user role.

    Non-admin users: Only see VMs they created
    Admin users: See all VMs

    Query params:
        - active: bool - Filter for active VMs (default: true)

    Returns:
        JSON: { vms: [...] }
    """
    try:
        user = get_authenticated_user()
        user_id = user.id

        active = request.args.get('active', 'true').lower() == 'true'

        # Build query based on user role
        if user.is_admin:
            # Admin sees all VMs
            if active:
                vms = VMRecord.query.filter_by(destroyed=False).order_by(VMRecord.created_at.desc()).all()
            else:
                vms = VMRecord.query.order_by(VMRecord.created_at.desc()).all()
        else:
            # Student sees their own VMs plus group VMs for any group they belong to
            member_group_ids = [row.group_id for row in
                                StudentGroupMember.query.filter_by(user_id=user_id).all()]
            if member_group_ids:
                if active:
                    query = VMRecord.query.filter(
                        or_(
                            VMRecord.created_by_user_id == user_id,
                            VMRecord.owner_group_id.in_(member_group_ids),
                        ),
                        VMRecord.destroyed == False,
                    )
                else:
                    query = VMRecord.query.filter(
                        or_(
                            VMRecord.created_by_user_id == user_id,
                            VMRecord.owner_group_id.in_(member_group_ids),
                        )
                    )
            else:
                if active:
                    query = VMRecord.query.filter_by(
                        destroyed=False,
                        created_by_user_id=user_id
                    )
                else:
                    query = VMRecord.query.filter_by(
                        created_by_user_id=user_id
                    )
            # Hide KOTH-game VMs from non-admins; they have their own dedicated UI.
            query = query.filter(VMRecord.koth_game_id.is_(None))
            vms = query.order_by(VMRecord.created_at.desc()).all()

        return jsonify({
            'success': True,
            'count': len(vms),
            'vms': [vm.to_dict() for vm in vms]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@vm_listing_bp.route('/api/vms/<int:vmid>', methods=['GET'])
@login_required
def api_vm_detail(vmid):
    """
    Get details of a specific VM from its database record and its backend.

    Non-admin users can view VMs assigned to them or one of their groups.
    Admin users can view any VM the portal tracks.

    Returns:
        JSON: { vm: {...} }
    """
    try:
        user = get_authenticated_user()

        # Authorize against the database record before making any provider
        # calls. Group ownership is membership-based, not creator-based:
        # another member may legitimately have created the shared VM.
        db_vm = VMRecord.query.filter_by(vmid=vmid).first()
        if not user.is_admin:
            if not db_vm:
                return jsonify({
                    'error': 'VM not found or you do not have permission to view it',
                }), 403
            if not user_can_access_vm(user, db_vm):
                return jsonify({
                    'error': 'You can only view VMs assigned to you or your group',
                }), 403
        if not db_vm:
            return jsonify({'success': False, 'error': f'VM {vmid} not found'}), 404

        # The record names the VM's backend and its identity, so only that
        # backend is asked, and only for what the record cannot know.
        try:
            backend = backend_for(db_vm)
        except Exception as error:
            current_app.logger.warning(
                "backend %s unavailable for VM %s: %s", db_vm.backend, vmid, error)
            backend = None
        uptime = None
        if backend is None:
            status, ip_addresses = 'unknown', []
        else:
            with vm_provisioning.provider_session(backend):
                _backend, state, status, ip_addresses, _diagnostic = _observe(
                    backend, db_vm)
                if state == VM_PRESENT:
                    uptime = _uptime(backend, db_vm)
        if status == VM_MISSING:
            return jsonify({'success': False,
                            'error': f'VM {vmid} not found on {db_vm.backend}'}), 404

        vm_info = {
            'vmid': db_vm.vmid,
            'name': db_vm.vm_name,
            'type': db_vm.vm_type,
            'node': db_vm.node,
            'backend': db_vm.backend,
            'status': status,
            'memory': db_vm.memory,
            'cores': db_vm.cores,
            'uptime': uptime,
            'ip_addresses': ip_addresses,
            'database_info': db_vm.to_dict(),
            'expires_at': db_vm.expires_at.isoformat() if db_vm.expires_at else None,
            'created_at': db_vm.created_at.isoformat() if db_vm.created_at else None,
        }
        if db_vm.expires_at:
            time_remaining = db_vm.expires_at - datetime.now()
            vm_info['time_remaining_seconds'] = int(time_remaining.total_seconds())
            vm_info['time_remaining'] = _format_time_remaining(time_remaining)

        # Per-VM mask_ip: hide host octet for non-admins when the VM's
        # template has mask_ip: true. Mask each IP in the list independently.
        if db_vm and not user.is_admin and 'ip_addresses' in vm_info:
            template_config = load_template_config()
            tpl = get_template_for_vm(template_config, db_vm.template_name)
            if tpl and tpl.get('mask_ip', False):
                vm_info['ip_addresses'] = [
                    mask_ip_address(ip) for ip in vm_info['ip_addresses']
                ]

        return jsonify({
            'success': True,
            'vm': vm_info
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
