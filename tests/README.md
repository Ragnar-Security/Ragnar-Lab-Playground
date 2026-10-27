# Test Suite

Comprehensive test suite for the Proxmox VM Automation system.

## Test Coverage

### Core Modules
- **test_models.py** - Database models (VMRecord, User, StudentWhitelist, AdminAlert)
- **test_auth_routes.py** - Authentication and authorization
- **test_api_routes.py** - API endpoints for VM management
- **test_template_manager.py** - Template configuration management
- **test_tasks.py** - Background tasks (NEW - retry logic, alerts, batch processing)

### New Test Coverage (Latest)
- ✅ VM stop/delete batch processing
- ✅ Deletion retry logic (5 attempts)
- ✅ Admin alert system (file, database, email, webhook)
- ✅ AdminAlert database model
- ✅ VMRecord deletion_retry_count field
- ✅ Discord/Slack webhook notifications
- ✅ SMTP email notifications

## Running Tests

### Option 1: Run Locally with UV (Recommended)

```bash
# Make sure UV is installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Run all tests
./run-tests.sh

# Or run specific test file
uv run pytest tests/test_tasks.py -v

# Run with coverage
uv run pytest tests/ -v --cov=app --cov-report=html
```

### Option 2: Run in Docker

```bash
# Development environment
docker compose exec vm-automation pytest tests/ -v

# With coverage
docker compose exec vm-automation pytest tests/ -v --cov=app --cov-report=term-missing
```

### Option 3: Run Directly with Python

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=app --cov-report=html
```

## Test Organization

```
tests/
├── conftest.py              # Shared fixtures and configuration
├── test_models.py           # Database model tests
├── test_auth_routes.py      # Authentication tests
├── test_api_routes.py       # API endpoint tests
├── test_template_manager.py # Template config tests
├── test_tasks.py           # Background task tests (NEW)
├── integration/             # Browser flows against a running stack
└── live/                    # Opt-in contract run against a real Proxmox cluster
```

`tests/live/` is skipped unless `LIVE_PROXMOX=1` and its environment are set,
so `pytest tests/` and CI skip it. See `tests/live/README.md`.

## Key Test Fixtures

- `app` - Flask application with test configuration
- `client` - Test client for making HTTP requests
- `create_test_templates_yaml()` - Test template configuration

## Coverage Report

After running tests with coverage, open the HTML report:

```bash
# Mac
open htmlcov/index.html

# Linux
xdg-open htmlcov/index.html

# Or just open in browser
firefox htmlcov/index.html
```

## Test Database

Tests use an in-memory SQLite database that's created and destroyed for each test session. No need to configure MySQL for testing.

## Continuous Integration

Tests can be run in CI/CD pipelines:

```yaml
# Example GitHub Actions
- name: Run tests
  run: |
    pip install -r requirements.txt
    pytest tests/ -v --cov=app --cov-report=xml
```

## Writing New Tests

1. Add test file in `tests/` directory
2. Import necessary fixtures from `conftest.py`
3. Use descriptive test names: `test_<feature>_<scenario>()`
4. Mock external dependencies (Proxmox API, SMTP, webhooks)

Example:

```python
def test_vm_creation_success(app, client):
    """Test successful VM creation"""
    with app.app_context():
        # Test code here
        pass
```

## Troubleshooting

### Import Errors
Make sure you're running from the project root directory.

### Database Errors
The test suite creates its own SQLite database. MySQL not required for tests.

### Missing Dependencies
Run: `uv pip install -r requirements.txt` or `pip install -r requirements.txt`
