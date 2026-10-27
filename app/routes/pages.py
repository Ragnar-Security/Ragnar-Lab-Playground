"""
Page rendering routes
Simple template rendering for login, register, and main pages
"""

from flask import Blueprint, make_response, render_template
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
    """Password reset page, including the landing page of admin-issued links.

    The link's token sits in the URL fragment, which browsers never send, so
    this route cannot see it; the page script reads it. no-referrer is
    defense in depth for the page's CDN requests: a fragment is never part
    of a Referer, but a later change to the link format must not leak it.
    """
    if get_authenticated_user() is not None:
        response = make_response(render_template('index.html'))
    else:
        response = make_response(render_template('reset_password.html'))
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response
