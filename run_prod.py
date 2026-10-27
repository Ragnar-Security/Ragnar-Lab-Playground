#!/usr/bin/env python3
"""
Proxmox VM Automation - Production Entry Point
Production WSGI application entry point for Gunicorn

This module creates the Flask application and starts background tasks
in a production-safe manner using Gunicorn workers.
"""

# gevent monkey-patching MUST run before any stdlib import that touches
# socket / ssl / threading / select. The gunicorn gevent worker also
# patches, but doing it here too ensures the Flask app + APScheduler load
# under the patched stdlib regardless of how this module is imported.
from gevent import monkey
monkey.patch_all()

import threading
import sys
import os
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.tasks import check_and_destroy_expired_vms, initialize_vm_monitoring

# Flag to ensure background tasks only start once per worker
_background_tasks_started = False
_background_tasks_lock = threading.Lock()


def start_background_tasks(app):
    """Start all background tasks as daemon threads
    
    Uses a file lock to ensure only ONE worker across all processes
    starts the background task thread.
    
    Args:
        app: Flask application instance
    """
    global _background_tasks_started
    
    with _background_tasks_lock:
        if _background_tasks_started:
            logger.info("Background tasks already started in this worker")
            return
        
        # Use file lock to ensure only one worker starts background tasks
        # This prevents duplicate background threads across multiple Gunicorn workers
        import fcntl
        lock_file_path = '/tmp/vm_automation_primary_worker.lock'
        
        try:
            lock_file = open(lock_file_path, 'w')
            # Try to acquire exclusive lock (non-blocking)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            
            # Successfully acquired lock - this is the primary worker
            logger.info("Acquired primary worker lock - starting background tasks (PID: %s)", os.getpid())
            
            # Keep the lock file open to maintain the lock
            # When this process dies, the lock is automatically released
            app.config['_worker_lock_file'] = lock_file
            
        except IOError:
            # Another worker already has the lock
            logger.info("Another worker is running background tasks - skipping in this worker (PID: %s)", os.getpid())
            _background_tasks_started = True
            return
        
        def task_wrapper():
            """Wrapper to run background task with Flask app instance"""
            try:
                check_and_destroy_expired_vms(app)
            except Exception as e:
                logger.error(f"Background task error: {e}", exc_info=True)
        
        # Start expiration checker thread as a daemon
        # Daemon threads automatically terminate when the main process exits
        expiration_thread = threading.Thread(
            target=task_wrapper,
            daemon=True,
            name='VMExpirationChecker'
        )
        expiration_thread.start()
        logger.info("✓ Background cleanup thread started (PID: %s)", os.getpid())
        _background_tasks_started = True


# Create Flask app
logger.info("Creating Flask application...")
app = create_app()
logger.info("✓ Flask app created")

# Initialize VM monitoring from database
logger.info("Initializing VM monitoring...")
initialize_vm_monitoring(app)
logger.info("✓ VM monitoring initialized")

# Start background tasks
start_background_tasks(app)

# Log production startup
logger.info("Proxmox VM Automation Server ready (Production Mode)")
logger.info("Worker PID: %s", os.getpid())


if __name__ == '__main__':
    # This block is for testing only - production should use Gunicorn
    logger.warning("Running with Flask development server - NOT FOR PRODUCTION USE")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
