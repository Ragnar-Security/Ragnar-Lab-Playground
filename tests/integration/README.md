# Integration Tests

Browser-based integration tests using Playwright against a live test server.

## Setup

```bash
uv venv -q
uv pip install -q -r requirements.txt pytest
uv run --no-sync playwright install chromium
```

## Running

### Option 1: Against Docker test server
```bash
docker compose -f docker-compose.test.yml up -d --build
uv run --no-sync python -m pytest tests/integration/ --base-url http://localhost:5099 -q -p no:cacheprovider
docker compose -f docker-compose.test.yml down -v
```

### Option 2: Against local Flask server
```bash
# Terminal 1:
FLASK_ENV=testing DATABASE_URL=sqlite:///instance/test.db SECRET_KEY=test-key PROXMOX_HOST=fake PROXMOX_PASSWORD=fake uv run --no-sync python scripts/init_db.py
FLASK_ENV=testing DATABASE_URL=sqlite:///instance/test.db SECRET_KEY=test-key PROXMOX_HOST=fake PROXMOX_PASSWORD=fake uv run --no-sync python -m flask run --port 5099

# Terminal 2:
TEST_BASE_URL=http://localhost:5099 uv run --no-sync python -m pytest tests/integration/ -q -p no:cacheprovider
```

## What's Tested

- Login page loads with configured site branding
- Theme styling is applied
- Password minimum length enforcement (8 chars)
- Login/logout flow
- Dashboard loads with all admin tabs
- Assignment tab UI elements
- Student management tab
- All VMs tab with filters
- Admin assignment API access
- Admin-issued password reset link: created in the Students tab, used once in
  a fresh browser, then the student signs in with the new password
