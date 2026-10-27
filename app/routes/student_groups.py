"""
Admin routes for managing student groups.

All routes under /admin/groups require an admin session.
"""

import re
import logging
import hashlib

import yaml as pyyaml
from flask import Blueprint, request, jsonify

from ..auth import login_required, admin_required, get_authenticated_user
from ..models import db, User, StudentGroup, StudentGroupMember, StudentWhitelist
from ..proxmox_service import find_vms_by_name_prefix
from ..template_manager import load_template_config
from ..vlan_allocator import allocate_pair, VlanPoolExhausted, VlanInUse
from .vm_creation import cleanup_vms, _expected_vm_names

logger = logging.getLogger(__name__)

student_groups_bp = Blueprint('student_groups', __name__)

SLUG_RE = re.compile(r'^[a-z0-9-]+$')


def _group_response(group):
    """Expand a StudentGroup into a response payload with member count."""
    data = group.to_dict()
    data['member_count'] = len(group.members) if group.members else 0
    return data


@student_groups_bp.route('/admin/groups', methods=['GET'])
@login_required
@admin_required
def list_groups():
    groups = StudentGroup.query.order_by(StudentGroup.name).all()
    return jsonify({'groups': [_group_response(g) for g in groups]})


@student_groups_bp.route('/admin/groups', methods=['POST'])
@login_required
@admin_required
def create_group():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    display_name = (data.get('display_name') or '').strip()
    description = (data.get('description') or '').strip() or None
    explicit_entry = data.get('entry_vlan_tag')
    explicit_internal = data.get('internal_vlan_tag')

    if not name or not SLUG_RE.match(name) or len(name) > 100:
        return jsonify({'error': 'name must match [a-z0-9-]+ and be <= 100 chars'}), 400
    if not display_name:
        return jsonify({'error': 'display_name is required'}), 400

    if StudentGroup.query.filter_by(name=name).first():
        return jsonify({'error': f'group slug {name!r} already exists'}), 409

    try:
        entry, internal = allocate_pair(
            scope='group',
            explicit_entry=explicit_entry,
            explicit_internal=explicit_internal,
        )
    except (ValueError, VlanInUse) as e:
        return jsonify({'error': str(e)}), 400
    except VlanPoolExhausted as e:
        return jsonify({'error': str(e)}), 409

    creator_id = get_authenticated_user().id
    group = StudentGroup(
        name=name,
        display_name=display_name,
        description=description,
        entry_vlan_tag=entry,
        internal_vlan_tag=internal,
        created_by=creator_id,
    )
    db.session.add(group)
    db.session.commit()
    logger.info("Created StudentGroup %s (VLAN %d/%d) by user_id=%s",
                name, entry, internal, creator_id)
    return jsonify({'group': _group_response(group)}), 201


def _active_vm_count(group_id):
    from ..models import VMRecord
    return VMRecord.query.filter_by(owner_group_id=group_id, destroyed=False).count()


@student_groups_bp.route('/admin/groups/<int:group_id>', methods=['GET'])
@login_required
@admin_required
def get_group(group_id):
    group = db.session.get(StudentGroup, group_id)
    if not group:
        return jsonify({'error': 'group not found'}), 404
    data = _group_response(group)
    data['active_vm_count'] = _active_vm_count(group.id)
    return jsonify({'group': data})


@student_groups_bp.route('/admin/groups/<int:group_id>', methods=['PUT'])
@login_required
@admin_required
def update_group(group_id):
    group = db.session.get(StudentGroup, group_id)
    if not group:
        return jsonify({'error': 'group not found'}), 404
    data = request.get_json(silent=True) or {}

    # slug is immutable — silently ignore any attempt to change it
    if 'display_name' in data:
        if not data['display_name']:
            return jsonify({'error': 'display_name cannot be empty'}), 400
        group.display_name = data['display_name'].strip()
    if 'description' in data:
        group.description = (data['description'] or '').strip() or None

    if 'entry_vlan_tag' in data or 'internal_vlan_tag' in data:
        if _active_vm_count(group.id) > 0:
            return jsonify({
                'error': 'Cannot change VLAN tags while group has active VMs. '
                         'Destroy them first.'
            }), 409
        explicit_entry = data.get('entry_vlan_tag', group.entry_vlan_tag)
        explicit_internal = data.get('internal_vlan_tag', group.internal_vlan_tag)
        try:
            entry, internal = allocate_pair(
                scope='group',
                explicit_entry=explicit_entry,
                explicit_internal=explicit_internal,
                exclude_group_id=group.id,
            )
        except (ValueError, VlanInUse) as e:
            return jsonify({'error': str(e)}), 400
        group.entry_vlan_tag = entry
        group.internal_vlan_tag = internal

    db.session.commit()
    return jsonify({'group': _group_response(group)})


@student_groups_bp.route('/admin/groups/<int:group_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_group(group_id):
    from ..models import VMRecord
    from datetime import datetime

    group = db.session.get(StudentGroup, group_id)
    if not group:
        return jsonify({'error': 'group not found'}), 404

    # Collect everything we'd need to destroy: live VMRecords AND any
    # Proxmox VMs matching this group's name prefix that have no live
    # record (orphans from a prior failed-deploy cleanup race or a SIGTERM'd
    # worker — gating on active_count alone would miss those and let us
    # delete the group row while leaving artifacts in Proxmox forever).
    active = VMRecord.query.filter_by(owner_group_id=group.id, destroyed=False).all()
    known = {(vm.vmid, vm.node) for vm in active}
    expected_names = {vm.vm_name for vm in active}
    try:
        configured_templates = [
            template
            for deployment_group in load_template_config().get(
                'deployment_groups', [])
            for template in deployment_group.get('templates', [])
        ]
        expected_names.update(_expected_vm_names(
            configured_templates,
            owning_identifier=group.name,
            owner_group_id=group.id,
        ))
    except Exception:
        # A config outage must not turn a prefix into an ownership boundary.
        # Known VMRecord names remain safe to cascade; unknown orphans wait
        # for an operator rather than risking another group's VMs.
        logger.exception(
            "delete_group: could not build exact VM-name inventory for %s",
            group.name)
    stranded = []
    try:
        for vm in find_vms_by_name_prefix(f"group-{group.name}-"):
            if (vm.get('name') in expected_names
                    and (vm['vmid'], vm['node']) not in known):
                stranded.append(vm)
    except Exception:
        logger.exception(
            "delete_group: name-prefix sweep failed for group %s "
            "(continuing with VMRecord-known VMs only)", group.name)

    total_to_destroy = len(active) + len(stranded)
    confirm = request.args.get('confirm_destroy', '').lower() in ('1', 'true', 'yes')

    if total_to_destroy > 0 and not confirm:
        return jsonify({
            'error': ('Group has active VMs (or Proxmox orphans). '
                      'Pass ?confirm_destroy=1 to cascade-destroy.'),
            'active_vm_count': len(active),
            'stranded_vm_count': len(stranded),
        }), 409

    if total_to_destroy > 0:
        # `vm_type` is required by cleanup_vms — the previous payload omitted
        # it, causing a per-VM KeyError that was silently swallowed and left
        # every VM as a Proxmox orphan while the DB was wiped.
        cleanup_payload = [{
            'vmid': vm.vmid, 'node': vm.node, 'vm_type': vm.vm_type,
            'vm_name': vm.vm_name, 'template_name': vm.template_name,
            'db_record_id': vm.id,
        } for vm in active]
        for vm in stranded:
            cleanup_payload.append({
                'vmid': vm['vmid'], 'node': vm['node'],
                'vm_type': vm['vm_type'], 'vm_name': vm.get('name'),
                'template_name': None, 'db_record_id': None,
            })

        try:
            destroyed = cleanup_vms(cleanup_payload)
        except Exception as e:
            logger.exception("Failed to cascade-destroy VMs for group %s", group.name)
            return jsonify({'error': f'Destroy failed: {e}'}), 500

        # Only mark records destroyed for vmids Proxmox actually destroyed.
        # Anything Proxmox refused (e.g. lock conflict) stays as-is so a
        # retry can pick it up.
        for vm in active:
            if vm.vmid not in destroyed:
                continue
            db.session.refresh(vm)
            if not vm.destroyed:
                vm.destroyed = True
                vm.destroyed_at = datetime.now()
        db.session.commit()

        # Failure check covers BOTH active and stranded — a stranded VM that
        # Proxmox refused to destroy is just as much an orphan blocker as
        # an active one.
        attempted_vmids = [item['vmid'] for item in cleanup_payload]
        failed_vmids = [vmid for vmid in attempted_vmids if vmid not in destroyed]
        if failed_vmids:
            logger.error(
                "delete_group: %d VM(s) could not be destroyed for group %s: %s — "
                "group row left in place for retry",
                len(failed_vmids), group.name, failed_vmids)
            return jsonify({
                'error': (f"Cascade destroy partially failed: "
                          f"{len(failed_vmids)} VM(s) could not be destroyed "
                          f"in Proxmox (vmids: {failed_vmids}). The group "
                          f"row was preserved so you can retry."),
                'failed_vmids': failed_vmids,
            }), 500

    db.session.delete(group)
    db.session.commit()
    return jsonify({
        'deleted': True,
        'active_vms_destroyed': len(active),
        'stranded_vms_destroyed': len(stranded),
    })


@student_groups_bp.route('/admin/groups/<int:group_id>/members', methods=['POST'])
@login_required
@admin_required
def add_member(group_id):
    group = db.session.get(StudentGroup, group_id)
    if not group:
        return jsonify({'error': 'group not found'}), 404
    data = request.get_json(silent=True) or {}
    login_id = (data.get('login_id') or '').strip().lower()
    if not login_id:
        return jsonify({'error': 'login_id required'}), 400
    user = User.query.filter_by(login_id=login_id).first()
    if not user:
        return jsonify({'error': f'no user with login_id={login_id!r}'}), 404
    existing = StudentGroupMember.query.filter_by(user_id=user.id).first()
    if existing:
        if existing.group_id == group.id:
            return jsonify({'error': 'user is already a member of this group'}), 409
        return jsonify({
            'error': f'user is already in group id={existing.group_id}; remove first',
        }), 409
    adder_id = get_authenticated_user().id
    m = StudentGroupMember(group_id=group.id, user_id=user.id, added_by=adder_id)
    db.session.add(m)
    db.session.commit()
    return jsonify({'member': m.to_dict()}), 201


@student_groups_bp.route('/admin/groups/<int:group_id>/members/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def remove_member(group_id, user_id):
    group = db.session.get(StudentGroup, group_id)
    if not group:
        return jsonify({'error': 'group not found'}), 404
    member = StudentGroupMember.query.filter_by(
        group_id=group.id, user_id=user_id,
    ).first()
    if not member:
        return jsonify({'error': 'not a member'}), 404
    confirm = request.args.get('confirm_revoke', '').lower() in ('1', 'true', 'yes')
    active_count = _active_vm_count(group.id)
    if active_count > 0 and not confirm:
        return jsonify({
            'error': ('Group has active VMs; removing this member will revoke '
                      'their access. Pass ?confirm_revoke=1 to proceed.'),
            'active_vm_count': active_count,
        }), 409
    db.session.delete(member)
    db.session.commit()
    return jsonify({'removed': True})


@student_groups_bp.route('/admin/groups/<int:group_id>/members', methods=['GET'])
@login_required
@admin_required
def list_members(group_id):
    group = db.session.get(StudentGroup, group_id)
    if not group:
        return jsonify({'error': 'group not found'}), 404
    return jsonify({'members': [m.to_dict() for m in group.members]})


def _parse_groups_yaml(raw_bytes):
    """Parse the uploaded YAML file to a list[dict]. Raise ValueError on bad shape."""
    try:
        doc = pyyaml.safe_load(raw_bytes)
    except pyyaml.YAMLError as e:
        raise ValueError(f'YAML parse error: {e}')
    if not isinstance(doc, dict):
        raise ValueError('top-level must be a mapping with a "groups" key')
    groups = doc.get('groups')
    if not isinstance(groups, list):
        raise ValueError('"groups" must be a list')
    return groups


def _build_plan(groups_from_yaml):
    """
    Build a diff plan between desired-state (YAML) and current-state (DB).

    Returns (plan_dict, errors_list, warnings_list).
    """
    errors = []
    warnings = []
    plan = {
        'groups_to_add': [],
        'groups_to_update': [],
        'members_to_set_by_group': {},
        'vlan_allocations': {},
    }

    # Detect duplicate members across groups in the YAML
    login_to_groups = {}
    for g in groups_from_yaml:
        for login in (g.get('members') or []):
            login_to_groups.setdefault(login, []).append(g.get('name'))
    for login, gnames in login_to_groups.items():
        if len(gnames) > 1:
            errors.append(
                f'user {login!r} appears in multiple groups in the YAML: {gnames}'
            )

    used = set()
    for row in db.session.query(User.entry_vlan_tag, User.internal_vlan_tag).all():
        used.update(t for t in row if t is not None)
    for row in db.session.query(StudentWhitelist.entry_vlan_tag,
                                StudentWhitelist.internal_vlan_tag).all():
        used.update(t for t in row if t is not None)
    for row in db.session.query(StudentGroup.entry_vlan_tag,
                                StudentGroup.internal_vlan_tag).all():
        used.update(t for t in row if t is not None)

    def _alloc(lo, hi):
        for t in range(lo, hi + 1):
            if t not in used:
                used.add(t)
                return t
        return None

    from ..config import (GROUP_ENTRY_VLAN_MIN, GROUP_ENTRY_VLAN_MAX,
                          GROUP_INTERNAL_VLAN_MIN, GROUP_INTERNAL_VLAN_MAX)

    for g in groups_from_yaml:
        name = (g.get('name') or '').strip()
        display_name = (g.get('display_name') or '').strip()
        members = g.get('members') or []
        explicit_entry = g.get('entry_vlan_tag')
        explicit_internal = g.get('internal_vlan_tag')

        if not name or not SLUG_RE.match(name) or len(name) > 100:
            errors.append(f'group {name!r}: slug must match [a-z0-9-]+ and <= 100 chars')
            continue
        if not display_name:
            errors.append(f'group {name!r}: display_name is required')
            continue

        for login in members:
            user = User.query.filter_by(login_id=login.lower()).first()
            wl = StudentWhitelist.query.filter_by(login_id=login.lower()).first()
            if not user and not wl:
                warnings.append(
                    f'group {name!r}: login {login!r} is not a user or whitelisted student'
                )

        if explicit_entry is not None or explicit_internal is not None:
            if explicit_entry is None or explicit_internal is None:
                errors.append(f'group {name!r}: must set both entry_vlan_tag and internal_vlan_tag or neither')
                continue
            entry_in_pool = GROUP_ENTRY_VLAN_MIN <= explicit_entry <= GROUP_ENTRY_VLAN_MAX
            internal_in_pool = GROUP_INTERNAL_VLAN_MIN <= explicit_internal <= GROUP_INTERNAL_VLAN_MAX
            for tag, label, in_pool in ((explicit_entry, 'entry_vlan_tag', entry_in_pool),
                                         (explicit_internal, 'internal_vlan_tag', internal_in_pool)):
                if tag in used:
                    errors.append(f'group {name!r}: {label}={tag} collides with an existing VLAN')
                elif not in_pool:
                    warnings.append(f'group {name!r}: {label}={tag} is outside the configured group pool')
            entry = explicit_entry
            internal = explicit_internal
            used.add(entry); used.add(internal)
        else:
            entry = _alloc(GROUP_ENTRY_VLAN_MIN, GROUP_ENTRY_VLAN_MAX)
            internal = _alloc(GROUP_INTERNAL_VLAN_MIN, GROUP_INTERNAL_VLAN_MAX)
            if entry is None or internal is None:
                errors.append(f'group {name!r}: VLAN pool exhausted')
                continue

        plan['vlan_allocations'][name] = (entry, internal)
        plan['members_to_set_by_group'][name] = members

        existing = StudentGroup.query.filter_by(name=name).first()
        if existing:
            plan['groups_to_update'].append({
                'id': existing.id, 'name': name, 'display_name': display_name,
                'description': g.get('description'),
                'entry_vlan_tag': entry, 'internal_vlan_tag': internal,
            })
        else:
            plan['groups_to_add'].append({
                'name': name, 'display_name': display_name,
                'description': g.get('description'),
                'entry_vlan_tag': entry, 'internal_vlan_tag': internal,
            })

    return plan, errors, warnings


@student_groups_bp.route('/admin/groups/yaml/preview', methods=['POST'])
@login_required
@admin_required
def preview_yaml():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'multipart field "file" required'}), 400
    raw = file.read()
    try:
        groups = _parse_groups_yaml(raw)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    plan, errors, warnings = _build_plan(groups)

    token = hashlib.sha256(raw).hexdigest()
    return jsonify({
        'plan_token': token,
        'diff': {
            'groups_to_add': plan['groups_to_add'],
            'groups_to_update': plan['groups_to_update'],
            'members_to_set_by_group': plan['members_to_set_by_group'],
            'vlan_allocations': plan['vlan_allocations'],
        },
        'errors': errors,
        'warnings': warnings,
    })


@student_groups_bp.route('/admin/groups/yaml/apply', methods=['POST'])
@login_required
@admin_required
def apply_yaml():
    file = request.files.get('file')
    plan_token = request.form.get('plan_token', '')
    if not file:
        return jsonify({'error': 'multipart field "file" required'}), 400
    raw = file.read()
    expected = hashlib.sha256(raw).hexdigest()
    if plan_token != expected:
        return jsonify({
            'error': 'plan_token does not match file hash; re-run preview',
        }), 409

    try:
        groups = _parse_groups_yaml(raw)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    plan, errors, warnings = _build_plan(groups)
    if errors:
        return jsonify({'errors': errors, 'warnings': warnings}), 400

    created = []
    updated = []

    for entry in plan['groups_to_add']:
        g = StudentGroup(
            name=entry['name'],
            display_name=entry['display_name'],
            description=entry['description'],
            entry_vlan_tag=entry['entry_vlan_tag'],
            internal_vlan_tag=entry['internal_vlan_tag'],
            created_by=get_authenticated_user().id,
        )
        db.session.add(g)
        db.session.flush()
        created.append(g.name)

    for entry in plan['groups_to_update']:
        g = db.session.get(StudentGroup, entry['id'])
        g.display_name = entry['display_name']
        g.description = entry['description']
        if (g.entry_vlan_tag != entry['entry_vlan_tag']
                or g.internal_vlan_tag != entry['internal_vlan_tag']):
            if _active_vm_count(g.id) > 0:
                db.session.rollback()
                return jsonify({
                    'error': f"group {g.name!r}: cannot change VLANs while active VMs exist",
                }), 409
            g.entry_vlan_tag = entry['entry_vlan_tag']
            g.internal_vlan_tag = entry['internal_vlan_tag']
        updated.append(g.name)

    for group_name, logins in plan['members_to_set_by_group'].items():
        g = StudentGroup.query.filter_by(name=group_name).first()
        if not g:
            continue
        StudentGroupMember.query.filter_by(group_id=g.id).delete()
        seen = set()
        for login in logins:
            if login.lower() in seen:
                continue
            seen.add(login.lower())
            user = User.query.filter_by(login_id=login.lower()).first()
            if not user:
                continue
            existing = StudentGroupMember.query.filter_by(user_id=user.id).first()
            if existing and existing.group_id != g.id:
                db.session.delete(existing)
                db.session.flush()
            db.session.add(StudentGroupMember(group_id=g.id, user_id=user.id,
                                              added_by=get_authenticated_user().id))

    db.session.commit()
    return jsonify({
        'created': created,
        'updated': updated,
        'warnings': warnings,
    })
