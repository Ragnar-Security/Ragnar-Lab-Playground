"""
Page rendering routes
Simple template rendering for login, register, and main pages
"""

from flask import Blueprint, render_template
from ..auth import get_authenticated_user

# Create pages blueprint
pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
def index():
    """Main page - serve the web interface or redirect to login"""
    if get_authenticated_user() is None:
        return render_template('login.html')
    return render_template('index.html')


@pages_bp.route('/register')
def register_page():
    """Registration page"""
    if get_authenticated_user() is not None:
        return render_template('index.html')
    return render_template('register.html')


@pages_bp.route('/reset-password')
def reset_password_page():
    """Password reset page"""
    if get_authenticated_user() is not None:
        return render_template('index.html')
    return render_template('reset_password.html')
