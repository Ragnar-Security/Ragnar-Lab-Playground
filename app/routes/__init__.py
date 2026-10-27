"""
Routes package - Modular route organization
Combines all sub-blueprints into a single API blueprint
"""

from flask import Blueprint

# Import sub-blueprints
from .pages import pages_bp
from .templates import templates_bp
from .vm_creation import vm_creation_bp
from .node_resources import node_resources_bp
from .vm_listing import vm_listing_bp
from .vm_lifecycle import vm_lifecycle_bp
from .vm_status import vm_status_bp
from .admin import admin_bp
from .student_groups import student_groups_bp
from .koth import koth_bp

# Create main API blueprint
api = Blueprint('api', __name__)

# Register all sub-blueprints
# Note: These are registered without url_prefix to maintain existing routes
api.register_blueprint(pages_bp)
api.register_blueprint(templates_bp)
api.register_blueprint(vm_creation_bp)
api.register_blueprint(node_resources_bp)
api.register_blueprint(vm_listing_bp)
api.register_blueprint(vm_lifecycle_bp)
api.register_blueprint(vm_status_bp)
api.register_blueprint(admin_bp)
api.register_blueprint(student_groups_bp)
api.register_blueprint(koth_bp)
