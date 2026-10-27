"""
Test configuration and fixtures
Provides shared test utilities and fixtures
"""

import os
import pytest
import tempfile
import yaml
# Tests must never inherit an operator's untracked deployment catalog.
os.environ['TEMPLATES_FILE'] = os.path.join(
    os.path.dirname(__file__), 'fixtures', 'templates.yaml'
)

from app import create_app
from app.models import db


@pytest.fixture
def app():
    """Create application for testing"""
    # Create a temporary directory for the test database
    db_fd, db_path = tempfile.mkstemp()
    
    # Create app with test config
    app = create_app(testing=True)
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    app.config['WTF_CSRF_ENABLED'] = False
    
    # Create tables
    with app.app_context():
        db.create_all()
    
    yield app
    
    # Cleanup
    with app.app_context():
        db.session.remove()
        db.drop_all()
    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture
def client(app):
    """Create test client"""
    return app.test_client()


def create_test_templates_yaml():
    """Create a test templates.yaml configuration"""
    return {
        'deployment_groups': [
            {
                'display_name': 'test-homework',
                'description': 'Test Homework Assignment',
                'enabled': True,
                'uptime_minutes': 120,
                'extend_time_minutes': 60,
                'templates': [
                    {
                        'name': 'webserver',
                        'type': 'lxc',
                        'memory': 2048,
                        'cores': 2,
                        'storage': 20,
                        'storage_device': 'local-lvm',
                        'ostemplate': 'local:vztmpl/debian-11-standard_11.3-1_amd64.tar.zst'
                    },
                    {
                        'name': 'database',
                        'type': 'lxc',
                        'memory': 4096,
                        'cores': 4,
                        'storage': 40,
                        'storage_device': 'local-lvm',
                        'ostemplate': 'local:vztmpl/debian-11-standard_11.3-1_amd64.tar.zst'
                    }
                ]
            }
        ]
    }


def write_test_templates_file(path):
    """Write test templates configuration to file"""
    config = create_test_templates_yaml()
    with open(os.path.join(path, 'templates.yaml'), 'w') as f:
        yaml.dump(config, f)


@pytest.fixture
def mock_proxmox_task_success():
    """Mock successful Proxmox task completion"""
    return {'success': True, 'status': 'stopped'}


@pytest.fixture
def mock_proxmox_task_failure():
    """Mock failed Proxmox task"""
    return {'success': False, 'status': 'stopped', 'error': 'Clone failed'}
