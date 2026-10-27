# Development Guide

This guide covers local development setup for the Proxmox VM Automation application.

## Table of Contents

1. [Development Setup](#development-setup)
2. [Development vs Production](#development-vs-production)
3. [Running Development Environment](#running-development-environment)
4. [Development Workflow](#development-workflow)
5. [Testing](#testing)
6. [Debugging](#debugging)
7. [Code Structure](#code-structure)

## Development Setup

### Prerequisites

- Docker and Docker Compose installed
- Python 3.12+ (for local testing without Docker)
- Access to Proxmox cluster
- Git (for version control)

### Initial Setup

```bash
# Clone or navigate to repository
cd /path/to/automation

# Copy the example environment file
cp .env.example .env

# Edit .env with your Proxmox credentials
nano .env
# Set PROXMOX_HOST, PROXMOX_PASSWORD

# Configure templates
nano templates.yaml
```

### Start Development Environment

```bash
# Start with Docker Compose (recommended)
docker compose up -d --build

# Check logs
docker compose logs -f

# Access application
# Open browser: http://localhost:5000
```

## Development vs Production

| Aspect | Development | Production |
|--------|------------|------------|
| **Dockerfile** | `Dockerfile` | `Dockerfile.prod` |
| **Compose File** | `docker-compose.yml` | `docker-compose.prod.yml` |
| **Entry Point** | `run.py` | `run_prod.py` |
| **Web Server** | Flask dev server | Gunicorn (1 gevent worker) |
| **Auto-reload** | Yes (volume mounts) | No |
| **Debug Mode** | Can enable | Disabled |
| **Env File** | `.env` from `.env.example` | `.env` (production values) |
| **Database Password** | Simple | Strong/random |
| **Secret Key** | Simple | Strong/random |
| **Resource Limits** | None | No CPU/memory limits; `nofile` is 65536 |
| **Health Checks** | Optional | Enabled |
| **Running User** | root | appuser (non-root) |
| **Logging** | Console | Gunicorn + rotation |

## Running Development Environment

### Using Docker Compose (Recommended)

```bash
# Start containers
docker compose up -d

# View logs in real-time
docker compose logs -f vm-automation

# Restart after code changes
docker compose restart vm-automation

# Or rebuild if dependencies changed
docker compose up -d --build

# Stop containers
docker compose down

# Stop and remove volumes (fresh start)
docker compose down -v
```

### Using Local Python (Alternative)

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export $(cat .env | xargs)

# Start MySQL (if not using Docker)
# or configure DATABASE_URL to point to existing MySQL

# Initialize database
python scripts/init_db.py

# Run application
python run.py

# Access at http://localhost:5000
```

### Hot Reload with Volume Mounts

The development `docker-compose.yml` can be enhanced for hot reload:

```yaml
services:
  vm-automation:
    # ... existing config ...
    volumes:
      - ./app:/app/app                    # Mount app code
      - ./templates:/app/templates        # Mount templates
      - ./static:/app/static              # Mount static files
      - ./data:/app/data
      - ./.env:/app/.env:ro
      - ./templates.yaml:/app/templates.yaml:ro
    environment:
      - FLASK_ENV=development
      - FLASK_DEBUG=1                     # Enable debug mode
```

After adding volume mounts, restart:
```bash
docker compose down
docker compose up -d
```

Now code changes will be reflected immediately without rebuild!

## Development Workflow

### Making Code Changes

1. **Edit files** in your preferred editor
2. **If using volume mounts**: Changes are live
3. **If not using volume mounts**: Restart container
   ```bash
   docker compose restart vm-automation
   ```
4. **Check logs** for errors
   ```bash
   docker compose logs -f vm-automation
   ```

### Testing Changes

```bash
# Test API endpoints
curl http://localhost:5000/auth/health

# Test VM creation
curl -X POST http://localhost:5000/api/vms/create \
  -H "Content-Type: application/json" \
  -d '{
    "deployment_group": "test",
    "hostname_prefix": "test-vm",
    "count": 1
  }'

# Check VM list
curl http://localhost:5000/api/vms/list
```

### Database Access

```bash
# Connect to database
docker exec -it mysql-db mysql -u vmuser -pvmpassword vmdatabase

# Useful queries
SELECT * FROM users;
SELECT * FROM vm_records WHERE expires_at > NOW();
SELECT * FROM vm_records ORDER BY created_at DESC LIMIT 5;

# Clear test data
DELETE FROM vm_records WHERE created_by_user_id = (SELECT id FROM users WHERE login_id = 'teststudent');
```

### Viewing Logs

```bash
# Application logs
docker compose logs -f vm-automation

# Database logs
docker compose logs -f db

# All logs
docker compose logs -f

# Last 50 lines
docker compose logs --tail=50 vm-automation

# Search for errors
docker compose logs vm-automation 2>&1 | grep -i error
```

## Testing

### Manual Testing Checklist

- [ ] Login with admin account
- [ ] Login with student account
- [ ] Create VM from deployment group
- [ ] List VMs (verify grouping by student)
- [ ] Extend VM lifetime
- [ ] Destroy deployment group
- [ ] Admin destroy with student_name
- [ ] Check IP addresses (should be 10.1.x.x only)
- [ ] Verify load balancing across nodes
- [ ] Test VM expiration (wait for test VM to expire)

### Testing VM Expiration

With development settings (8 minute lifetime):

```bash
# Create a test VM
# Login at http://localhost:5000
# Create VM with any deployment group

# Watch logs for expiration
docker compose logs -f vm-automation | grep -i expir

# VM should be destroyed after 8 minutes
# Check logs: "Destroyed expired VM..."
```

### Testing Load Balancing

```bash
# Create multiple VMs rapidly
for i in {1..5}; do
  curl -X POST http://localhost:5000/api/vms/create \
    -H "Content-Type: application/json" \
    -b "session=..." \
    -d "{\"deployment_group\":\"test\",\"hostname_prefix\":\"lb-test\",\"count\":1}"
  sleep 2
done

# Check distribution across nodes
docker exec mysql-db mysql -u vmuser -pvmpassword -e "
  USE vmdatabase;
  SELECT node, COUNT(*) as vm_count 
  FROM vm_records 
  WHERE deployment_group = 'test'
  GROUP BY node;
"
```

### Automated Testing

```bash
# Run tests (if implemented)
pytest tests/

# Run with coverage
pytest --cov=app tests/

# Run specific test
pytest tests/test_auth.py::test_login
```

## Debugging

### Enable Debug Mode

Edit `.env`:
```bash
FLASK_ENV=development
FLASK_DEBUG=true
```

Restart:
```bash
docker compose restart vm-automation
```

### Interactive Debugging

Add breakpoint in code:
```python
import pdb; pdb.set_trace()
```

Attach to container:
```bash
docker attach proxmox-vm-automation
```

### Common Issues

#### Port Already in Use
```bash
# Check what's using port 5000
sudo lsof -i :5000

# Kill process
sudo kill -9 <PID>

# Or change port in .env
APP_PORT=5001
```

#### Database Connection Failed
```bash
# Check database is running
docker compose ps

# Restart database
docker compose restart db

# Wait for database to be ready
docker compose logs db | grep "ready for connections"
```

#### Template Not Found
```bash
# Verify templates.yaml is correct
cat templates.yaml

# Check template exists on Proxmox
curl -k -u "$PROXMOX_USER:$PROXMOX_PASSWORD" \
  "https://$PROXMOX_HOST:8006/api2/json/nodes/node1/qemu/110/config"
```

#### VMs Not Being Destroyed
```bash
# Check background task is running
docker compose logs vm-automation | grep "Background task"

# Manually trigger cleanup
docker exec proxmox-vm-automation python -c "
from app import create_app
from app.tasks import check_and_destroy_expired_vms
app = create_app()
check_and_destroy_expired_vms(app)
"
```

## Code Structure

```
automation/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── auth.py              # Authentication routes
│   ├── routes.py            # Main API routes
│   ├── models.py            # Database models
│   ├── config.py            # Configuration
│   ├── proxmox_service.py   # Proxmox API integration
│   ├── tasks.py             # Background tasks
│   └── template_manager.py  # Template configuration
├── templates/               # HTML templates
│   ├── index.html          # Main interface
│   ├── login.html          # Login page
│   └── register.html       # Registration page
├── static/
│   ├── css/style.css       # Styles
│   └── js/script.js        # Frontend JavaScript
├── scripts/
│   └── init_db.py          # Database initialization
├── tests/                   # Test files
├── run.py                   # Development entry point
├── run_prod.py             # Production entry point
├── Dockerfile              # Development Dockerfile
├── Dockerfile.prod         # Production Dockerfile
├── docker-compose.yml      # Development compose
├── docker-compose.prod.yml # Production compose
├── templates.yaml          # VM template configuration
├── .env                    # Environment configuration
└── requirements.txt        # Python dependencies
```

### Key Modules

**app/__init__.py**
- Flask app factory
- Database initialization
- Blueprint registration

**app/auth.py**
- User registration and login
- Session management
- Admin privilege checking

**app/routes.py**
- VM creation API
- VM listing (grouped by student)
- VM destruction
- VM lifetime extension

**app/proxmox_service.py**
- Proxmox API wrapper
- Node resource monitoring
- Weighted load balancing
- VM cloning and configuration

**app/tasks.py**
- Background VM expiration checker
- Automatic VM cleanup
- VM monitoring initialization

**app/template_manager.py**
- Load and parse templates.yaml
- Template location mapping
- Deployment group configuration

## Making Changes

### Adding a New API Endpoint

1. **Add route in app/routes.py**
```python
@main.route('/api/test', methods=['GET'])
@login_required
def test_endpoint():
    return jsonify({'success': True, 'message': 'Test'})
```

2. **Test with curl**
```bash
curl http://localhost:5000/api/test
```

3. **Add frontend call in static/js/script.js**
```javascript
fetch('/api/test')
  .then(response => response.json())
  .then(data => console.log(data));
```

### Adding a Database Model

1. **Define model in app/models.py**
```python
class NewModel(db.Model):
    __tablename__ = 'new_model'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
```

2. **Update scripts/init_db.py** to create table

3. **Restart with fresh database**
```bash
docker compose down -v
docker compose up -d
```

### Modifying Templates

1. **Edit templates.yaml**
```yaml
templates:
  - name: "new-template"
    template_locations:
      node1: 300
```

2. **Restart application**
```bash
docker compose restart vm-automation
```

## Switching Between Development and Production

### To Development
```bash
# Stop production
docker compose -f docker-compose.prod.yml down

# Start from the example environment
cp .env.example .env

# Start development
docker compose up -d --build
```

### To Production
```bash
# Stop development
docker compose down

# Restore production environment
# (make sure you have strong passwords!)
nano .env

# Start production
docker compose -f docker-compose.prod.yml up -d --build
```

### Production Ports

The development server listens on port 5000. The production application
port is internal to the Compose network; Caddy is the public entry point on
ports 80 and 443.

## Tips and Best Practices

1. **Use version control**: Commit changes frequently
2. **Never commit .env**: It's in .gitignore for a reason
3. **Test before pushing**: Verify changes work in development
4. **Keep dependencies updated**: change `pyproject.toml`, run `uv lock`, then regenerate `requirements.txt` with `uv export --frozen --no-dev --no-emit-project -o requirements.txt` (never hand-edit it)
5. **Document changes**: Update relevant .md files
6. **Short VM lifetimes**: Use 8 minutes in dev for quick testing
7. **Check logs often**: Catch errors early
8. **Backup before major changes**: Use `scripts/backup-database.sh`

## Additional Resources

- [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) - Production setup
- [PRODUCTION_QUICK_START.md](PRODUCTION_QUICK_START.md) - Production quick reference
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) - Existing developer guide
- [README.md](README.md) - Project overview

---
**Last Updated:** January 28, 2026
