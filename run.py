#!/usr/bin/env python3
"""
Proxmox VM Automation
Main application entry point

This is the entry point for running the application.
"""

import threading
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.tasks import check_and_destroy_expired_vms, initialize_vm_monitoring


def start_background_tasks(app):
    """Start all background tasks as daemon threads
    
    Args:
        app: Flask application instance
    """
    
    def task_wrapper():
        """Wrapper to run background task with Flask app instance"""
        check_and_destroy_expired_vms(app)
    
    # Start expiration checker thread
    expiration_thread = threading.Thread(
        target=task_wrapper,
        daemon=True,
        name='ExpirationChecker'
    )
    expiration_thread.start()
    print("✓ Background tasks started")


def main():
    """Main entry point"""
    # Create Flask app
    app = create_app()
    
    print("✓ Database initialized")
    print("✓ Flask app created")
    
    # Initialize VM monitoring from database
    initialize_vm_monitoring(app)
    
    # Start background tasks
    start_background_tasks(app)
    
    # Run the Flask application
    print("Starting Proxmox VM Automation Server...")
    print("  Access the web interface at: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)


if __name__ == '__main__':
    main()
