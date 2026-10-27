"""
VM Listing routes module.
Handles displaying and querying VM information.

Orphaned-record cleanup: when /list is called, each DB record is verified
against Proxmox. If the VM no longer exists (manually deleted, failed cleanup,
etc.), the DB record is deleted on the spot so the dashboard stays accurate.
This is the primary self-healing mechanism for DB/Proxmox drift.
"""

from flask import Blueprint, request, jsonify, current_app
from datetime import datetime
import logging

from ..models import db, VMRecord, StudentGroupMember
from ..proxmox_service import get_proxmox_connection, get_cluster_vm_index
from ..auth import login_required, get_authenticated_user
from ..template_manager import load_template_config, get_template_for_vm
from .utils import (
    _verify_vm_exists, _get_vm_ip_addresses, _format_time_remaining,
    _ip_cache_get, invalidate_vm_ip_cache, mask_ip_address, note_vm_state,
    forget_vm_state, user_can_access_vm,
    ORPHAN_CONFIRM_SECONDS, VM_CONFIG_ERROR, VM_MISSING, VM_UNKNOWN,
)
from .admin import _send_config_issue_webhook
from sqlalchemy import or_

logger = logging.getLogger(__name__)

# Create blueprint
vm_listing_bp = Blueprint('vm_listing', __name__)


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
        orphaned_records = []  # Track VMs that don't exist in Proxmox

        # Get live IP addresses from Proxmox
        proxmox = get_proxmox_connection()

        # Load template config once for per-VM mask_ip lookup. Admins always
        # see real IPs (so they can debug); non-admins get masked IPs for any
        # VM whose template sets mask_ip: true in templates.yaml.
        template_config = load_template_config()

        # One cluster-wide inventory for the whole render. Without it each
        # VM cost a `config.get()` plus a guest-agent call: 14 Proxmox
        # round trips for a 7-VM student, ~350 for an admin, ~1,400 when a
        # class refreshes at once. Best-effort — `_verify_vm_exists` falls
        # back to its per-VM path when the snapshot is absent or unsure.
        try:
            vm_index = get_cluster_vm_index(proxmox)
        except Exception as e:
            current_app.logger.warning(
                "cluster inventory unavailable (%s); falling back to per-VM "
                "config reads for this request", e)
            vm_index = None

        for record in vm_records:
            vm_state, error_msg = _verify_vm_exists(
                proxmox, record.node, record.vmid, record.vm_type,
                index=vm_index)

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
                        f"Proxmox cluster; holding the record until a second "
                        f"sighting confirms it")
                    continue
                current_app.logger.warning(
                    f"VM {record.vmid} ({record.vm_name}) has been absent from "
                    f"the Proxmox cluster for over {ORPHAN_CONFIRM_SECONDS:.0f}s "
                    f"- marking for cleanup")
                orphaned_records.append(record)
                continue  # Skip this VM from the list

            if vm_state == VM_UNKNOWN:
                # We could not reach Proxmox, or could not classify the
                # error. Keep showing the VM and NEVER delete its row: a
                # deleted row means the expiry daemon stops tracking the
                # VM and it runs in Proxmox forever.
                current_app.logger.warning(
                    f"VM {record.vmid} ({record.vm_name}): state unknown "
                    f"({error_msg}); listing it unverified and keeping the record")

            has_config_issues = vm_state == VM_CONFIG_ERROR
            if has_config_issues:
                # The cluster still lists this VM but its config file is
                # unreadable. It is real, it is consuming resources, and it
                # needs a manual purge.
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
            # lab can share a name; the card's scope is what its Extend
            # button sends, so one card must never mix the two.
            deployment_scope = 'individual' if record.owner_group_id is None else 'group'
            card_key = (group_name, deployment_scope)
            if card_key not in students[student_key]['deployment_groups']:
                remaining_secs = int((record.expires_at - datetime.now()).total_seconds()) if record.expires_at else 0
                students[student_key]['deployment_groups'][card_key] = {
                    'group_name': group_name,
                    'deployment_scope': deployment_scope,
                    'vm_count': 0,
                    'created_at': record.created_at.isoformat() if record.created_at else None,
                    'expires_at': record.expires_at.isoformat() if record.expires_at else None,
                    'time_remaining_seconds': remaining_secs,
                    'vms': []
                }

            # Fetch live IP addresses from Proxmox (skip when we already
            # know Proxmox won't answer for this VM).
            if has_config_issues:
                ip_display = 'CONFIG ERROR'
                vm_status = 'error'
            elif vm_state == VM_UNKNOWN:
                # Proxmox did not answer for this VM, so a guest-agent call
                # would only be another timeout. The cache may still hold a
                # seconds-old address though, and reading it costs nothing -
                # a student mid-SSH should not lose the address they were
                # about to use because of a one-render blip.
                cached_ips = _ip_cache_get((record.node, record.vmid)) or []
                usable = [ip for ip in cached_ips if ip.startswith('10.')]
                ip_display = ', '.join(usable) if usable else 'UNKNOWN'
                vm_status = 'unknown'
            elif record.is_entry_point:
                # Entry point VM: show only 10.0.x.x IP (entry VLAN network)
                ip_addresses = _get_vm_ip_addresses(proxmox, record.node, record.vmid, record.vm_type)
                entry_ips = [ip for ip in ip_addresses if ip.startswith('10.')]
                ip_display = ', '.join(entry_ips) if entry_ips else 'N/A'
                vm_status = 'running'
            else:
                # Non-entry-point VMs: show internal 10.x.1.x IP
                ip_addresses = _get_vm_ip_addresses(proxmox, record.node, record.vmid, record.vm_type)
                internal_ips = [ip for ip in ip_addresses if ip.startswith('10.')]
                ip_display = ', '.join(internal_ips) if internal_ips else '-'
                vm_status = 'running'

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

        # Orphaned record cleanup: rows the Proxmox CLUSTER INVENTORY has
        # not listed for ORPHAN_CONFIRM_SECONDS - deleted through the
        # Proxmox UI, or a destroy that removed the VM but crashed before
        # updating the database. Only a CONFIRMED VM_MISSING reaches this
        # list. A VM we merely failed to reach, or saw missing once, must
        # never land here: the expiry daemon tracks VMs by database row, so
        # deleting the row of a live VM leaks it permanently.
        if orphaned_records:
            logger.info(f"Cleaning up {len(orphaned_records)} orphaned database records...")
            for orphaned_record in orphaned_records:
                try:
                    db.session.delete(orphaned_record)
                    # vmids are reissued, so leave nothing keyed to this one.
                    forget_vm_state(orphaned_record.node, orphaned_record.vmid)
                    invalidate_vm_ip_cache(orphaned_record.node, orphaned_record.vmid)
                    logger.info(f"Deleted orphaned record for VM {orphaned_record.vmid} ({orphaned_record.vm_name})")
                except Exception as cleanup_error:
                    logger.error(f"Error cleaning up orphaned record {orphaned_record.vmid}: {str(cleanup_error)}")

            try:
                db.session.commit()
                logger.info(f"Successfully cleaned up {len(orphaned_records)} orphaned records")
            except Exception as commit_error:
                logger.error(f"Error committing orphaned record cleanup: {str(commit_error)}")
                db.session.rollback()

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
    Get details of a specific VM by ID from Proxmox and the database.

    Non-admin users can view VMs assigned to them or one of their groups.
    Admin users can view any VM.

    Returns:
        JSON: { vm: {...} }
    """
    try:
        user = get_authenticated_user()

        # Authorize against the database record before making any Proxmox
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

        proxmox = get_proxmox_connection()

        # Search for the VM in Proxmox across all nodes
        vm_info = None
        for node_data in proxmox.nodes.get():
            node = node_data['node']
            try:
                # Check QEMU VMs
                vms = proxmox.nodes(node).qemu.get()
                for vm in vms:
                    if vm['vmid'] == vmid:
                        # Get full config for QEMU VM
                        config = proxmox.nodes(node).qemu(vmid).config.get()
                        status = proxmox.nodes(node).qemu(vmid).status.current.get()

                        vm_info = {
                            'vmid': vm['vmid'],
                            'name': vm['name'],
                            'type': 'qemu',
                            'node': node,
                            'status': status.get('status', 'unknown'),
                            'memory': config.get('memory'),
                            'cores': config.get('cores'),
                            'uptime': status.get('uptime')
                        }
                        # Get IP addresses for QEMU VM
                        vm_info['ip_addresses'] = _get_vm_ip_addresses(proxmox, node, vmid, 'qemu')
                        break
            except Exception:
                pass

            try:
                # Check LXC containers
                containers = proxmox.nodes(node).lxc.get()
                for container in containers:
                    if container['vmid'] == vmid:
                        # Get full config for LXC container
                        config = proxmox.nodes(node).lxc(vmid).config.get()
                        status = proxmox.nodes(node).lxc(vmid).status.current.get()

                        vm_info = {
                            'vmid': container['vmid'],
                            'name': container['name'],
                            'type': 'lxc',
                            'node': node,
                            'status': status.get('status', 'unknown'),
                            'memory': config.get('memory'),
                            'cores': config.get('cores'),
                            'uptime': status.get('uptime')
                        }
                        # Get IP addresses for LXC container
                        vm_info['ip_addresses'] = _get_vm_ip_addresses(proxmox, node, vmid, 'lxc')
                        break
            except Exception:
                pass

            if vm_info:
                break

        if not vm_info:
            return jsonify({'success': False, 'error': f'VM {vmid} not found in Proxmox'}), 404

        # Combine with database info if available
        if db_vm:
            vm_info['database_info'] = db_vm.to_dict()
            vm_info['expires_at'] = db_vm.expires_at.isoformat() if db_vm.expires_at else None
            vm_info['created_at'] = db_vm.created_at.isoformat() if db_vm.created_at else None
            # Calculate time remaining
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
