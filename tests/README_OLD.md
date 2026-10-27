# Test Suite

This directory contains unit and component tests for the Proxmox VM Automation Dashboard.

## Test Structure

- **test_template_manager.py** - Unit tests for YAML template loading and management
- **test_models.py** - Unit tests for database models (User, VMRecord)
- **test_auth_routes.py** - Component tests for authentication (login, register, logout)
- **test_api_routes.py** - Component tests for API routes (create, list, extend, destroy VMs)
- **conftest.py** - Shared test fixtures and utilities

## Running Tests

### Run All Tests
```bash
python -m pytest tests/
```

### Run Specific Test File
```bash
python -m pytest tests/test_template_manager.py
```

### Run with Coverage
```bash
python -m pytest tests/ --cov=app --cov-report=html
```

### Run Specific Test
```bash
python -m pytest tests/test_models.py::TestModels::test_user_creation
```

### Run with Verbose Output
```bash
python -m pytest tests/ -v
```

## Using unittest

Alternatively, you can run tests using Python's built-in unittest:

```bash
# Run all tests
python -m unittest discover tests/

# Run specific test file
python -m unittest tests.test_models

# Run specific test class
python -m unittest tests.test_models.TestModels

# Run specific test method
python -m unittest tests.test_models.TestModels.test_user_creation
```

## Test Coverage

The test suite covers:
- ✅ Template YAML loading and parsing
- ✅ Deployment group filtering and selection
- ✅ User model creation and password hashing
- ✅ VM record creation and querying
- ✅ User authentication (login, register, logout)
- ✅ Session management
- ✅ VM listing and filtering by user
- ✅ Deployment group operations (extend, destroy)
- ✅ Database relationships

## Adding New Tests

When adding new features, please add corresponding tests:

1. **Unit tests** - Test individual functions and methods in isolation
2. **Component tests** - Test API endpoints and route handlers
3. **Integration tests** - Test interactions between multiple components

### Example Test Template

```python
import unittest
from app import create_app
from app.models import db

class TestNewFeature(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        
        with self.app.app_context():
            db.create_all()
    
    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
    
    def test_something(self):
        # Your test code here
        self.assertTrue(True)
```

## Dependencies

Required for testing:
- pytest
- pytest-cov (for coverage reports)
- Flask testing utilities (included with Flask)

Install test dependencies:
```bash
pip install pytest pytest-cov
```
