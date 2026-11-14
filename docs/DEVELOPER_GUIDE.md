# Developer Quick Start Guide

## Getting Started

### First Time Setup
```bash
# Clone or enter the project directory
cd /path/to/Ragnar-Lab-Playground

# Install dependencies (using uv)
uv pip install -r requirements.txt

# Copy environment template
cp .env.example .env

# Edit .env with your Proxmox credentials
nano .env
```

### Running the Application

```bash
# Start with uv
uv run python3 run.py

# Or with Docker
docker-compose up -d

# View logs
docker-compose logs -f vm-automation

# Stop Docker container
docker-compose down
```

## Project Structure at a Glance

```
project/
├── app/                 ← Application package
│   ├── __init__.py      ← Package initialization & app factory
│   ├── backends/        ← VMBackend contract + proxmox/aws/gcp adapters
│   ├── vm_provisioning.py ← Backend-generic create/destroy sequence
│   ├── config.py        ← Settings & env variables
│   ├── models.py        ← Database schema
│   ├── proxmox_service.py ← Proxmox API wrapper
│   ├── template_manager.py ← Template loading
│   ├── routes/          ← Web endpoints, one module per blueprint
│   └── tasks.py         ← Background jobs
├── templates/           ← HTML templates
├── static/              ← CSS & JavaScript files
├── tests/               ← Unit tests
├── data/                ← Database storage
├── run.py               ← Entry point (run this)
├── templates.yaml       ← VM template configuration
└── requirements.txt     ← Python dependencies
```

## Commonly Needed Tasks

### ❓ "Where do I add a new web endpoint?"
→ **app/routes.py**
```python
@api.route('/my-endpoint', methods=['GET'])
def my_endpoint():
    """Endpoint documentation"""
    return jsonify({'result': 'data'})
```

### ❓ "How do I track a new VM property?"
→ **app/models.py** (add column to VMRecord)
```python
new_field = db.Column(db.String(255))
```

### ❓ "I need to call a new Proxmox API"
→ **app/proxmox_service.py** (add function)
```python
def new_api_call(proxmox, node, vmid):
    """New API call"""
    return proxmox.nodes(node).qemu(vmid).new_endpoint.get()
```

### ❓ "How do I modify VM creation?"
→ **app/routes/vm_creation.py** - `create_vm()`, `deploy_bundle()`, `create_single_vm_task()` (provider-neutral)
→ **app/backends/<provider>.py** - `create()` for what one provider does (Proxmox clone: `create_qemu_vm()` / `create_lxc_container()` in `app/routes/vm_creation.py`)

### ❓ "I need to change how templates are loaded"
→ **app/template_manager.py**

### ❓ "Where are configuration values?"
→ **app/config.py** (edit values or .env file)

## Understanding the Code Flow

### Creating a VM

```
User clicks Create (templates/index.html)
        ↓
app/routes/vm_creation.py: create_vm()
  validate_group_backends() / resolve_group_backend()   (app/backends/template_spec.py)
        ↓
deploy_bundle() → create_single_vm_task() per machine
  backend_block() picks the machine's proxmox/aws/gcp spec → VMSpec
        ↓
app/vm_provisioning.py: create_vm()
  admit → (cloud) start router → (cloud) ensure slot network → backend.create()
        ↓
app/routes/vm_creation.py: log_vm_to_database()
  VMRecord with backend, provider_id, deployment_id, network_slot
```

### Deleting Expired VMs

```
app/tasks.py: check_and_destroy_expired_vms() (every 60 sec)
        ↓
VMRecord.expires_at has passed
        ↓
backend_for(record).inspect() → vm_provisioning.destroy_vm(ref, STRICT)
        ↓
record retired only when the guest is confirmed gone;
vm_provisioning.after_record_retired() releases unused cloud networks
        ↓
orphan reaper, then idle cloud routers stopped
```

## VM Backends

Every lifecycle path (create, list, extend, destroy, expiry, sweep) talks to a
provider only through `app/backends/`, so routes and tasks hold `VMSpec` /
`VMRef` values and never a provider client:

- **`app/backends/base.py`** — the `VMBackend` protocol (`admit`, `create`,
  `inspect`, `destroy`, `confirm_absent`, `ip_addresses`, `status`,
  `set_expiry`, `list_managed`, `delete_resource`, slot and router hooks) and
  the rules every backend shares, such as never deleting a guest whose
  identity does not match. It imports nothing from the app, so any module may
  import it.
- **`app/backends/__init__.py`** — the registry. `get_backend(name)` refuses a
  backend missing from `ENABLED_BACKENDS`; `backend_for(record)` dispatches by
  `VMRecord.backend`, and a row from before that column existed counts as
  `proxmox`. A record on a disabled backend is therefore kept, never
  destroyed, until the backend is enabled again.
- **`proxmox.py`** adapts the existing `app/proxmox_service.py` functions
  without rewriting them, so the existing patch points still govern its tests.
  Its router and slot hooks are no-ops. **`aws.py`** (boto3) and **`gcp.py`**
  (Compute REST through `google.auth`; never a gRPC client, which is not
  gevent-safe) share `cloud_common.py` (tags and names),
  `cloud_admission.py` (the per-cloud machine cap) and `network_slots.py`
  (slot-to-`/26` arithmetic).
- **`template_spec.py`** resolves which backend a deployment group runs on and
  each machine's spec block.

Adding a backend touches: its name in `KNOWN_BACKENDS` (`base.py`), a lazy
factory in `_DEFAULT_FACTORIES` (`__init__.py`; lazy so importing the package
never imports `app.config`), its required template keys in
`CLOUD_REQUIRED_KEYS` (`template_spec.py`), its settings and startup checks in
`app/config.py`, `.env.example` and `docker-compose.prod.yml`, and a case in
the shared contract suite `tests/backends/test_backend_contract.py`, whose
test bodies never change per backend. Operator-facing behaviour belongs in
`docs/CLOUD_BACKENDS.md`.

## Code Patterns Used

### Database Operations
```python
# Add a record
record = VMRecord(vmid=100, vm_name="test")
db.session.add(record)
db.session.commit()

# Query records
vm = VMRecord.query.filter_by(vmid=100).first()
vms = VMRecord.query.all()

# Update a record
vm.destroyed = True
db.session.commit()
```

### Proxmox API Calls
```python
# Connection
proxmox = get_proxmox_connection()

# LXC operations
proxmox.nodes(node).lxc(vmid).config.get()
proxmox.nodes(node).lxc.create(vmid=100, ...)

# QEMU operations
proxmox.nodes(node).qemu(vmid).config.get()
proxmox.nodes(node).qemu(vmid).status.start.post()
```

### Routes
```python
@api.route('/endpoint', methods=['POST'])
def endpoint():
    data = request.form.get('key')
    try:
        # Do something
        return jsonify({'success': True, 'result': value})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
```

## Common Errors & Solutions

### `ModuleNotFoundError: flask_sqlalchemy`
```bash
uv pip install Flask-SQLAlchemy==3.0.5
```

### `Address already in use` (port 5000)
```bash
# Kill existing process
lsof -ti:5000 | xargs kill -9

# Or use different port in run.py
```

### Database errors
```bash
# Reset the database (deletes the MySQL volume)
docker compose down -v
docker compose up -d
```

### Proxmox connection fails
- Check `app/config.py` values
- Verify `.env` file settings
- Test token authentication: `PROXMOX_TOKEN_ID` and `PROXMOX_TOKEN_SECRET`

## Debugging Tips

### Print debugging
```python
print(f"Debug: {variable_name}")
# Better: use logging (add later)
```

### Check database
```bash
# Connect to the database directly
docker compose exec db sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "SELECT * FROM vm_records;"'
```

### View logs
```bash
# With uv
uv run python3 run.py  # logs to console

# With Docker
docker-compose logs -f vm-automation
docker-compose logs -f vm-automation --tail 100
```

### Test Proxmox connection
```python
from app.proxmox_service import get_proxmox_connection
proxmox = get_proxmox_connection()
print(proxmox.nodes.get())  # Should list your nodes
```

## Adding Tests (Future)

When you want to add tests:

```python
# tests/test_proxmox_service.py
import unittest
from app.proxmox_service import get_next_available_vmid

class TestProxmoxService(unittest.TestCase):
    def test_next_vmid(self):
        # Mock test here
        pass
```

Then run with:
```bash
python3 -m unittest discover tests/
```

## Useful Commands

```bash
# Check syntax
python3 -m py_compile app/__init__.py app/routes.py app/models.py

# Run specific module
uv run python3 -c "from app.models import db; print('DB loaded')"

# Show imports
python3 -m modulefinder run.py

# Format code (install black first)
black app/ run.py

# Find all TODOs/FIXMEs
grep -r "TODO\|FIXME" app/
```

## Documentation Files

- **REFACTORING.md** - Detailed refactoring guide with examples
- **REFACTORING_SUMMARY.md** - Quick overview of changes
- **DOCKER.md** - Docker deployment guide
- **README.md** - General project documentation

## Quick Reference: Where to Edit

| Need to... | Edit | Search for |
|-----------|------|-----------|
| Add config option | app/config.py | `PROXMOX_` |
| Add DB column | app/models.py | `class VMRecord` |
| Add Proxmox call | app/proxmox_service.py | `def ` |
| Add or change a VM backend | app/backends/ | `KNOWN_BACKENDS`, `class VMBackend` |
| Add template feature | app/template_manager.py | `def ` |
| Add web endpoint | app/routes.py | `@api.route` |
| Add background task | app/tasks.py | `while True` |
| Change app startup | run.py | `create_app()` |

## Getting Help

1. Check the relevant module's docstring
2. Look at REFACTORING.md for examples
3. Search for similar code in the codebase
4. Review git history: `git log -p` (if using git)

---

**Happy coding!** The modular structure makes it easy to add features and fix bugs. 🚀
