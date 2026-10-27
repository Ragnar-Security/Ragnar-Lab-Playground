"""
Template and deployment group configuration routes.

The key design decision here is the restriction model: a deployment group is
unrestricted (visible to everyone) by default. The moment an admin assigns
even one student to that group, it becomes restricted -- only assigned students
see it. This is the opposite of a whitelist: you opt *in* to restriction, not
out of it. Admins always bypass the filter.
"""

from flask import Blueprint, jsonify
from ..auth import get_authenticated_user
from ..template_manager import load_template_config, get_enabled_deployment_groups, get_group_mode
from ..models import db, StudentLabAssignment, StudentGroupMember

# Create templates blueprint
templates_bp = Blueprint('templates', __name__)


def _group_mode_visible_to(user):
    """True if `user` may see any group-mode deployment_group."""
    if getattr(user, 'is_admin', False):
        return True
    return StudentGroupMember.query.filter_by(user_id=user.id).first() is not None


@templates_bp.route('/templates', methods=['GET'])
def get_templates_list():
    """
    Get list of enabled deployment groups available for VM creation.

    All groups are visible by default. If a group has ANY student assignments,
    only those assigned students can see it. Unassigned groups remain visible to all.
    Admins always see all enabled groups.
    """
    try:
        config = load_template_config()
        enabled_groups = get_enabled_deployment_groups(config)

        # Filter restricted groups for non-admin students
        user = get_authenticated_user()
        if user is not None and not user.is_admin:
            user_id = user.id
            # Find groups that have ANY assignments (these are restricted)
            restricted = db.session.query(
                StudentLabAssignment.deployment_group_name
            ).distinct().all()
            restricted_names = {r[0] for r in restricted}

            # Find this user's assignments
            user_assigned = StudentLabAssignment.query.filter_by(user_id=user_id).all()
            user_assigned_names = {a.deployment_group_name for a in user_assigned}

            # Show group if: unrestricted (no assignments exist) OR user is assigned
            enabled_groups = [
                g for g in enabled_groups
                if g.get('display_name') not in restricted_names
                or g.get('display_name') in user_assigned_names
            ]

            # Hide group-mode labs from users who are not in any group
            if not _group_mode_visible_to(user):
                enabled_groups = [g for g in enabled_groups if not get_group_mode(g)]

            # Hide koth_mode deployment groups from non-admin users
            enabled_groups = [g for g in enabled_groups if not g.get('koth_mode')]

        return jsonify({
            'success': True,
            'deployment_groups': [
                {
                    'display_name': g.get('display_name'),
                    'description': g.get('description', ''),
                    'template_count': len(g.get('templates', [])),
                    'uptime_minutes': g.get('uptime_minutes', 4320),
                    'extend_time_minutes': g.get('extend_time_minutes', 1440),
                    'group_mode': get_group_mode(g),
                    'koth_mode': bool(g.get('koth_mode')),
                    'templates': [
                        {
                            'name': t.get('name', f'vm{i+1}'),
                            'type': t.get('type', 'lxc'),
                            'memory': t.get('memory'),
                            'cores': t.get('cores'),
                            'storage': t.get('storage'),
                            'connect': t.get('connect')
                        }
                        for i, t in enumerate(g.get('templates', []))
                    ]
                }
                for g in enabled_groups
            ]
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error loading templates: {e}")
        return jsonify({'success': False, 'error': 'An unexpected error occurred'})
