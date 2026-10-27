#!/usr/bin/env python3
"""
Proxmox VM Automation Web Interface
Main application entry point

This application provides a Flask-based web interface for managing Proxmox VMs.
It allows users to:
- Create VMs from templates
- List and manage existing VMs
- Set automatic expiration and destruction
- Track VM lifecycle in a database

Configuration:
- See config.py for configuration options
- See templates.yaml for available templates
- See .env for environment variables

Modules:
- config.py: Configuration management
- models.py: Database models
- proxmox_service.py: Proxmox API interactions
- template_manager.py: Template loading and management
- tasks.py: Background tasks
- routes.py: API endpoints
"""

import urllib3
import threading
from flask import Flask

# Suppress SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import configuration and modules
from config import config
from models import db
from routes import api
from tasks import check_and_destroy_expired_vms


def create_app(config_name=None):
    """
    Application factory function
    
    Creates and configures the Flask application with all blueprints and settings.
    
    Args:
        config_name (str, optional): Configuration name ('development', 'production', 'testing')
        
    Returns:
        Flask: Configured Flask application
    """
    app = Flask(__name__, template_folder='templates', static_folder='static')
    
    # Load configuration
    app.config.from_object(config)
    
    # Initialize database
    db.init_app(app)
    
    # Register blueprints
    app.register_blueprint(api)
    
    # Create database tables
    with app.app_context():
        db.create_all()
    
    return app


# Create the Flask app
app = create_app()


def start_background_tasks():
    """Start all background tasks as daemon threads"""
    # Start expiration checker thread
    expiration_thread = threading.Thread(
        target=check_and_destroy_expired_vms,
        daemon=True,
        name='ExpirationChecker'
    )
    expiration_thread.start()
    print("Background tasks started")


if __name__ == '__main__':
    # Initialize database
    with app.app_context():
        db.create_all()
        print("Database initialized")
    
    # Start background tasks
    start_background_tasks()
    
    # Run the Flask application
    print("Starting Proxmox VM Automation Server...")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
