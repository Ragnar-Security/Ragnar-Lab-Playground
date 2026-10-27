# Fresh Deployment Guide

## For Clean/First-Time Deployment

When deploying to a fresh environment (no existing database):

```bash
# 1. Clone/copy the project
cd /path/to/automation

# 2. Configure environment variables
cp .env.example .env
nano .env  # Set your Proxmox credentials, SECRET_KEY, etc.

# 3. Start the application
docker compose up -d

# 4. Wait for initialization (check logs)
docker compose logs -f vm-automation

# 5. Verify the application is running
curl http://localhost:8080
```

**Expected output in production logs:**
```
✓ Database initialized successfully
✓ Schema verified: vm_records.created_by_user_id exists
✓ Admin account created: admin
✓ Flask app created
Starting Proxmox VM Automation Server...
```

The test student is seeded only when `SEED_TEST_STUDENT=1`. Leave this
variable unset in production because the account uses a known password.

## For Upgrading Existing Deployment

If you already have an existing database and are adding authentication:

```bash
# 1. Stop the application
docker compose down

# 2. Backup your database (IMPORTANT!)
docker exec proxmox-vm-automation-mysql-1 mysqldump -u proxmox_user -p proxmox_automation > backup.sql

# 3. Update code
git pull  # or copy new files

# 4. Run migration
docker compose up -d
docker exec proxmox-vm-automation python migrate_db.py

# 5. Verify schema
docker compose logs vm-automation | grep "Schema verified"
```

## If Schema Issues Occur

If you see the error: `Unknown column 'vm_records.created_by_user_id'`

**Option 1: Drop and Recreate (Development/Testing)**
```bash
# This will DELETE all data!
docker compose down -v
docker compose up -d
```

**Option 2: Manual Migration (Production)**
```bash
# Backup first!
docker exec proxmox-vm-automation-mysql-1 mysqldump -u proxmox_user -p proxmox_automation > backup.sql

# Run migration
docker exec proxmox-vm-automation python migrate_db.py

# Restart application
docker compose restart vm-automation
```

**Option 3: Manual SQL (Advanced)**
```bash
# Connect to MySQL
docker exec -it proxmox-vm-automation-mysql-1 mysql -u proxmox_user -p

# Run this SQL
USE proxmox_automation;

ALTER TABLE vm_records 
ADD COLUMN created_by_user_id INT DEFAULT NULL,
ADD INDEX idx_created_by_user_id (created_by_user_id),
ADD FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL;

# Exit and restart
docker compose restart vm-automation
```

## Verification Steps

After deployment, verify everything is working:

```bash
# 1. Check containers are running
docker compose ps

# 2. Check logs for errors
docker compose logs vm-automation --tail 50

# 3. Verify schema
docker exec proxmox-vm-automation python -c "
from app import create_app, db
from sqlalchemy import inspect

app = create_app()
with app.app_context():
    inspector = inspect(db.engine)
    columns = [col['name'] for col in inspector.get_columns('vm_records')]
    print('✓ created_by_user_id exists' if 'created_by_user_id' in columns else '✗ Missing column')
"

# 4. Test admin login
curl -X POST http://localhost:8080/auth/login \
  -F "login_id=admin" \
  -F "password=<admin-password>"
```

## Common Issues & Solutions

### Issue: "Unknown column 'vm_records.created_by_user_id'"
**Cause:** Database has old schema from before authentication was added
**Solution:** Run migration or drop volume and recreate (see above)

### Issue: "No module named 'app'"
**Cause:** Application files not copied correctly
**Solution:** Rebuild container
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Issue: "Database not ready, retrying..."
**Cause:** MySQL container is still starting
**Solution:** Wait 30-60 seconds, it will connect automatically

### Issue: Admin login fails
**Cause:** Admin password not matching
**Solution:** Check .env for ADMIN_PASSWORD or reset:
```bash
docker exec proxmox-vm-automation python -c "
from app import create_app, db
from app.models import User

app = create_app()
with app.app_context():
    admin = User.query.filter_by(login_id='admin').first()
    admin.set_password('newpassword')
    db.session.commit()
    print('Password reset')
"
```

## Environment Variables Required

```env
# Database
MYSQL_ROOT_PASSWORD=root_password
MYSQL_DATABASE=proxmox_automation
MYSQL_USER=proxmox_user
MYSQL_PASSWORD=proxmox_password
MYSQL_HOST=mysql

# Proxmox
PROXMOX_HOST=<proxmox-host>
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=<choose-a-password>

# Application
SECRET_KEY=generate-with-python-secrets-token-hex-32
DEFAULT_VM_UPTIME_MINUTES=60
EXTEND_TIME_MINUTES=60

# Authentication
ADMIN_LOGIN_ID=admin
ADMIN_PASSWORD=secure_password_here
ADMIN_USERS=admin,ta1,ta2

# SEED_TEST_STUDENT is intentionally unset in production
```

## Database Schema Overview

### Tables Created:
- `users` - User accounts (admin and students)
- `student_whitelist` - Allowed Canvas login IDs
- `vm_records` - VM tracking with user ownership

### Key Fields in vm_records:
- `vmid` - Proxmox VM ID
- `vm_name` - VM name
- `student_name` - Student identifier (from login_id)
- `created_by_user_id` - Foreign key to users.id (enables ownership tracking)
- `created_at` - Creation timestamp
- `expires_at` - Expiration timestamp

## Post-Deployment Tasks

1. **Change admin password** via UI or CLI
2. **Import student whitelist** from Canvas CSV:
   ```bash
   docker cp students.csv proxmox-vm-automation:/app/
   docker exec proxmox-vm-automation python import_students.py students.csv
   ```
3. **Configure firewall** for the selected deployment. Production uses Caddy on ports 80 and 443.
4. **Install database backups** with `scripts/backup-database.sh` and the units under `scripts/systemd/`.
5. **Configure SSL/HTTPS** if exposing a non-production stack publicly.

## Monitoring

```bash
# Watch logs in real-time
docker compose logs -f vm-automation

# Check VM expiration monitoring
docker compose logs vm-automation | grep "VMs found in database"

# Check authentication
docker compose logs vm-automation | grep "Admin account"
```

## Rollback Plan

If deployment fails:

```bash
# 1. Stop new deployment
docker compose down

# 2. Restore from backup
docker compose up -d mysql
docker exec -i proxmox-vm-automation-mysql-1 mysql -u proxmox_user -p < backup.sql

# 3. Start application
docker compose up -d
```
