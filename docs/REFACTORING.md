# Code Refactoring Documentation

## Overview
The application has been refactored from a single monolithic file into a modular, well-documented structure organized as a proper Python package. This makes the code more maintainable, testable, and easier to understand.

## Module Structure

### Package Organization
```
app/                          # Main application package
├── __init__.py              # Package initialization with create_app() factory
├── config.py                # Configuration management
├── models.py                # Database models
├── proxmox_service.py       # Proxmox API wrapper
├── template_manager.py      # Template loading logic
├── routes.py                # Flask endpoints and business logic
└── tasks.py                 # Background tasks (VM expiration)
run.py                        # Application entry point
```

### Core Modules

#### `run.py` - Application Entry Point
**Purpose:** Main application startup script  
**Key Operations:**
- `create_app()` - Initializes Flask application factory
- `start_background_tasks()` - Starts daemon threads

**What Changed:**
- New file replacing old app.py in root directory
- Now focuses only on app startup
- All business logic delegated to app package

---

#### `app/__init__.py` - Package Initialization
**Purpose:** Flask application factory function  
**Key Functions:**
- `create_app()` - Creates and configures Flask app
- Registers blueprints
- Initializes database

---

#### `app/config.py` - Configuration Management
**Purpose:** Centralized configuration for the entire application  
**Exports:**
- `PROXMOX_HOST`, `PROXMOX_USER`, `PROXMOX_PASSWORD` - Proxmox credentials
- `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET` - Token auth details
- `VM_UPTIME_MINUTES` - Default VM lifetime
- `DATABASE_URL` - Database connection string
- `Config` classes for different environments (development, production, testing)

**When to Edit:**
- Add new environment variables here
- Modify default values for VM configuration
- Add new Flask configuration options

---

#### `app/models.py` - Database Models
**Purpose:** Define SQLAlchemy ORM models for database tables  
**Key Classes:**
- `VMRecord` - Represents a VM record in the database
  - 16 columns tracking VM identification, resources, lifecycle
  - `to_dict()` method for JSON serialization
  - Comprehensive docstrings for each field

**When to Edit:**
- Add new fields to track additional VM properties
- Modify the database schema
- Add new model classes for other entities

**Example - Adding a field:**
```python
# In VMRecord class
owner_email = db.Column(db.String(255))  # Add email tracking
```

---

#### `app/proxmox_service.py` - Proxmox API Interactions
**Purpose:** Abstracts all Proxmox API calls into reusable functions  
**Key Functions:**
- `get_proxmox_connection()` - Create API connection (token or password auth)
- `get_vm_by_name()` - Find VM across all nodes
- `get_vm_details()` - Get VM status information
- `get_next_available_vmid()` - Find next available VM ID
- `get_available_node()` - Get first available Proxmox node

**When to Edit:**
- Add new Proxmox API calls (like CPU/memory updates)
- Modify how VMs are searched or queried
- Add snapshot or backup functionality
- Change authentication methods

**Example - Adding a new function:**
```python
def resize_vm(proxmox, node, vmid, new_memory, new_cores):
    """Resize a running VM"""
    proxmox.nodes(node).qemu(vmid).config.put(memory=new_memory, cores=new_cores)
```

---

#### `app/template_manager.py` - Template Configuration
**Purpose:** Load and manage VM templates from YAML  
**Key Functions:**
- `load_template_config()` - Load templates.yaml
- `get_template_by_name()` - Find template by display name
- `get_enabled_templates()` - Filter only enabled templates

**When to Edit:**
- Modify how templates are loaded
- Add validation for template configuration
- Support multiple template sources (files, API, database)

**Example - Adding template validation:**
```python
def validate_template(template):
    """Validate template has required fields"""
    required = ['display_name', 'type', 'memory', 'cores']
    return all(field in template for field in required)
```

---

#### `app/tasks.py` - Background Tasks
**Purpose:** Background jobs like automatic VM cleanup  
**Key Functions:**
- `check_and_destroy_expired_vms()` - Main background task loop
- `_check_expired_lxc()` - Check LXC containers for expiration
- `_check_expired_qemu()` - Check QEMU VMs for expiration
- `_mark_vm_destroyed()` - Update database when VM is destroyed

**Details:**
- Runs every 60 seconds
- Stops and deletes expired VMs
- Updates database records
- Comprehensive error handling

**When to Edit:**
- Add new scheduled tasks
- Change expiration check interval
- Add notification/alerting when VMs expire
- Add backup before deletion

**Example - Add pre-deletion backup:**
```python
def _create_backup_before_delete(proxmox, node, vmid, vm_type):
    """Create a backup before deleting VM"""
    backup_name = f"backup-{vmid}-{datetime.now().timestamp()}"
    if vm_type == 'qemu':
        proxmox.nodes(node).qemu(vmid).status.backup.post(backupid=backup_name)
```

---

#### `app/routes.py` - API Endpoints
**Purpose:** All Flask routes and API endpoints  
**Key Endpoints:**
- `GET /` - Main web interface
- `GET /templates` - List available templates
- `POST /create` - Create new VM
- `GET /list` - List all VMs
- `POST /extend` - Extend VM lifetime
- `POST /destroy` - Destroy VM immediately
- `GET /time-remaining` - Get time until expiration
- `GET /api/vms` - Database API for VM list
- `GET /api/vms/<vmid>` - Database API for specific VM

**Highlights:**
- 500+ lines of endpoint code
- Well-organized helper functions
- Detailed error handling
- Clear separation of concerns

**When to Edit:**
- Add new endpoints
- Modify API responses
- Change validation logic
- Add new API features

**Example - Adding an endpoint:**
```python
@api.route('/api/vms/<int:vmid>/stats', methods=['GET'])
def get_vm_stats(vmid):
    """Get VM performance statistics"""
    vm = VMRecord.query.filter_by(vmid=vmid).first()
    if not vm:
        return jsonify({'error': 'VM not found'}), 404
    
    # Get stats from Proxmox
    # Return JSON response
```

---

## File Size Comparison

| File | Before | After |
|------|--------|-------|
| Root | 730 lines (app.py) | 50 lines (run.py) |
| app/ package | N/A | 7 modules (~100-150 lines each) |
| Total modules | 1 file | 8 files (1 root + 7 in package) |
| Lines per module | 730 | ~100-150 avg |
| Complexity | High | Low |
| Testability | Difficult | Easy |

---

## How to Find Code

**Need to find something?**

| Task | File |
|------|------|
| Add environment variable | `app/config.py` |
| Add database field | `app/models.py` |
| Change Proxmox API call | `app/proxmox_service.py` |
| Modify template loading | `app/template_manager.py` |
| Add background task | `app/tasks.py` |
| Add web endpoint | `app/routes.py` |
| Change Flask settings | `run.py` or `app/config.py` |

---

## Adding New Features

### Example 1: Add VM Resize Endpoint

1. **Add function to app/proxmox_service.py:**
```python
def resize_vm(proxmox, node, vmid, memory, cores):
    """Resize a VM"""
    proxmox.nodes(node).qemu(vmid).config.put(memory=memory, cores=cores)
```

2. **Add endpoint to app/routes.py:**
```python
@api.route('/resize', methods=['POST'])
def resize_vm():
    vm_id = request.form.get('vm_id')
    memory = request.form.get('memory')
    cores = request.form.get('cores')
    
    proxmox = get_proxmox_connection()
    vm = get_vm_by_name(proxmox, vm_id)
    
    from .proxmox_service import resize_vm
    resize_vm(proxmox, vm['node'], vm['vmid'], memory, cores)
    
    return jsonify({'success': True})
```

3. **Add JavaScript handler in static/js/script.js**
4. **Add HTML form in templates/index.html**

---

### Example 2: Add Email Notifications

1. **Add email config to config.py:**
```python
EMAIL_ENABLED = os.getenv('EMAIL_ENABLED', 'false') == 'true'
EMAIL_SMTP = os.getenv('EMAIL_SMTP')
```

2. **Create new module `notify.py`:**
```python
def send_vm_expiration_warning(vm_record):
    """Send email warning before VM expires"""
    # Implementation here
```

3. **Call from tasks.py before destroying VM:**
```python
def _destroy_vm(...):
    from notify import send_vm_expiration_warning
    send_vm_expiration_warning(vm_record)
```

---

## Testing

With the modular structure, testing is much easier:

```python
# test_proxmox_service.py
import unittest
from proxmox_service import get_next_available_vmid

class TestProxmoxService(unittest.TestCase):
    def test_get_next_vmid(self):
        # Mock Proxmox connection
        # Test ID generation logic
```

---

## Dependencies Between Modules

```
app.py
  ├── config.py
  ├── models.py
  │   └── Flask-SQLAlchemy
  ├── routes.py
  │   ├── models.py
  │   ├── proxmox_service.py
  │   ├── template_manager.py
  │   └── Flask
  ├── tasks.py
  │   ├── models.py
  │   └── proxmox_service.py
  ├── proxmox_service.py
  │   └── config.py
  └── template_manager.py
```

---

## Maintaining Code Quality

### Code Style
- All functions have docstrings
- Parameter types documented
- Return values clearly indicated

### Error Handling
- Try/except blocks in critical paths
- Meaningful error messages
- Graceful degradation

### Comments
- Comments explain "why", not "what"
- Complex logic has inline comments
- Module docstrings at top of file

### Organization
- Related functions grouped together
- Helper functions prefixed with `_`
- Clear naming conventions

---

## Migration from Old Code

If you need to find something from the old `app.py.old`:

1. **Grep for the function name:** `grep "def function_name" app.py.old`
2. **Check which module it's in** from the table above
3. **Import from that module** in your routes/app

---

## Future Improvements

With this modular structure, you can easily:

- Add API authentication (in routes.py)
- Implement caching (in proxmox_service.py)
- Add logging to all modules
- Create comprehensive unit tests
- Add CLI commands (create new cli.py)
- Implement webhooks/notifications (create notify.py)
- Add admin dashboard (modify routes.py)
- Support multiple Proxmox clusters (modify config.py)
