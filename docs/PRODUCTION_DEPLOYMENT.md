# Production Deployment Guide

This guide covers deploying the Proxmox VM Automation application in a production environment.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Pre-Deployment Checklist](#pre-deployment-checklist)
3. [Initial Setup](#initial-setup)
4. [Deployment](#deployment)
5. [Post-Deployment Verification](#post-deployment-verification)
6. [Monitoring and Maintenance](#monitoring-and-maintenance)
7. [Backup and Recovery](#backup-and-recovery)
8. [Security Considerations](#security-considerations)
9. [Troubleshooting](#troubleshooting)

## Prerequisites

- Docker Engine 20.10+ and the Docker Compose v2 plugin (`docker compose`; the
  legacy `docker-compose` v1 binary is not supported)
- Production host: 2 vCPU and approximately 2 GB RAM
- Network access to Proxmox cluster
- Domain name (optional, for HTTPS setup)
- SSL certificate (optional, for HTTPS)

## Pre-Deployment Checklist

Before deploying to production, ensure:

- [ ] Proxmox API credentials are ready and tested
- [ ] Network connectivity to Proxmox hosts is verified
- [ ] Templates are created and available on Proxmox nodes
- [ ] Server has adequate disk space (minimum 20GB free)
- [ ] Ports 80 and 443 are reachable for the Caddy frontend
- [ ] Backup strategy is planned and documented
- [ ] You have reviewed the security considerations

## Initial Setup

### 1. Clone or Copy Application Files

```bash
# If using git
git clone <your-repository-url> /opt/lab-playground
cd /opt/lab-playground

# Or copy files to production server
scp -r /path/to/automation user@production-server:/opt/lab-playground
```

### 2. Configure Environment Variables

```bash
# Copy the example environment file
cp .env.example .env

# Edit with your production values
nano .env
```

**Critical settings to configure:**

```bash
# Generate a strong secret key
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

# Set Proxmox connection details
PROXMOX_HOST=<proxmox-host>
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=<choose-a-password>

# Production refuses a missing or known-default admin password
ADMIN_PASSWORD=<choose-a-password>

# Site identity and public endpoint
SITE_NAME="Lab Playground"
SITE_BRAND_TAG=
SITE_ORG=
VPN_LOGIN_SERVER=https://vpn.example.edu
SITE_DOMAIN=lab.example.edu
ACME_EMAIL=admin@example.edu

# Deployment-group configuration path
TEMPLATES_FILE=templates.yaml

# Set strong database passwords
MYSQL_ROOT_PASSWORD=$(openssl rand -base64 32)
MYSQL_PASSWORD=$(openssl rand -base64 24)
```

Further settings (all documented in `.env.example`):

```bash
# Verify the Proxmox TLS certificate (default true). For a self-signed
# cluster set PROXMOX_CA_BUNDLE to a PEM file mounted into the container;
# use PROXMOX_VERIFY_SSL=false only over a private tunnel.
PROXMOX_VERIFY_SSL=true
PROXMOX_CA_BUNDLE=

# Student self-service password reset (default false = ask an admin).
# true allows reset with only login + SIS ID: low-entropy, see below
SELF_SERVICE_PASSWORD_RESET=false

# 1 behind the bundled Caddy (set by docker-compose.prod.yml); 0 if exposed directly
TRUSTED_PROXY_HOPS=1

# Session cookie: Secure/HttpOnly/SameSite=Lax by default in production
SESSION_COOKIE_SAMESITE=Lax
PERMANENT_SESSION_LIFETIME=7200
```

**Production startup refusals.** Any `FLASK_ENV` other than `development` or
`testing` is production, and the app refuses to start when:

- `SECRET_KEY` is missing, empty, a published example value, or shorter than
  32 characters;
- the database password (`DATABASE_URL` or `MYSQL_PASSWORD`) is empty or a
  published example value;
- `ADMIN_PASSWORD` is missing or a known weak default;
- `PROXMOX_CA_BUNDLE` is set but the file does not exist;
- `SESSION_COOKIE_SAMESITE` is not a valid value.

`docker-compose.prod.yml` additionally requires `SECRET_KEY`, `MYSQL_USER`,
`MYSQL_PASSWORD`, `MYSQL_DATABASE` and `MYSQL_ROOT_PASSWORD` to be set.

`SITE_BRAND_TAG`, `SITE_ORG`, and `VPN_LOGIN_SERVER` may be empty. Empty
branding fields are omitted, and an empty `VPN_LOGIN_SERVER` makes the UI
omit the `--login-server` flag. When set, `VPN_LOGIN_SERVER` must include its
scheme (`https://vpn.example.edu`, not `vpn.example.edu`). `SITE_DOMAIN` and
`ACME_EMAIL` are passed to Caddy by `docker-compose.prod.yml`.

`docker-compose.prod.yml` deliberately does not pass `SESSION_COOKIE_SECURE`
or `SESSION_COOKIE_HTTPONLY` through: the stack is always served over HTTPS
through Caddy, and both flags are forced on in production. Only
`SESSION_COOKIE_SAMESITE` and `PERMANENT_SESSION_LIFETIME` are configurable.

`SELF_SERVICE_PASSWORD_RESET` defaults to `false`. Setting it to `true` lets
anyone who knows a student's login and SIS ID reset that student's password
without an administrator, so leave it off unless that risk is acceptable.

Before starting or updating, validate the file:

```bash
python3 scripts/preflight_env.py .env
```

The script is stdlib-only and runs on the host (default file `./.env`). It
prints one `KEY: problem` line per issue, never the values, and exits 1 on
problems or 2 if the file is unreadable. It checks `SECRET_KEY`,
`MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD` and `ADMIN_PASSWORD` (present and not
a known default), `SESSION_COOKIE_SAMESITE`, and that `PROXMOX_CA_BUNDLE` is
readable on the host when verification is on. Mount the PEM into the container
at the same path it has on the host.

Registration and self-service reset both refuse roster rows with an empty SIS
ID. `init_db` fails immediately on configuration errors instead of retrying as
"Database not ready".

### 3. Configure Templates and Site-Owned Files

```bash
cp templates.example.yaml templates.yaml
cp config/headscale-acl.example.json config/headscale-acl.json
```

Edit the untracked `templates.yaml` to define your VM templates. The
application reads `TEMPLATES_FILE`, which defaults to `templates.yaml`
relative to the app root:

```yaml
templates:
  - name: "loadbalancer"
    template_locations:
      node1: 110
      node2: 210
    deployment_groups:
      - "homework1"
      - "homework2"

  - name: "webserver"
    template_locations:
      node1: 120
      node2: 220
    deployment_groups:
      - "homework3"
```

On the operator workstation, copy `scripts/site.env.example` to the
untracked `scripts/site.env` and set the remote host, checkout, Compose,
Headscale, backup, email-domain, and jumpbox values. Exported environment
variables override that file, and operations scripts fail fast when a
required value is missing.

Provision the untracked `data/students_merged.csv` through the roster
changeover workflow. It contains protected student records and live VPN
credentials and must never be committed.

### 4. Set Proper File Permissions

```bash
# Restrict access to sensitive files
chmod 600 .env
chmod 755 scripts/*.sh

# Ensure data directory exists
mkdir -p data backups
chmod 755 data backups
```

## Deployment

### Option 1: Using Production Docker Compose (Recommended)

```bash
# Build and start containers
docker compose -f docker-compose.prod.yml up -d --build

# Verify containers are running
docker compose -f docker-compose.prod.yml ps

# Check logs
docker compose -f docker-compose.prod.yml logs -f
```

### Option 2: Using Standard Docker Compose

```bash
# Use the standard docker-compose.yml
docker compose up -d --build

# Note: This uses Flask's development server
# Only use for testing/development
```

### First-Time Setup

After starting the containers, the system will automatically:

1. Initialize the database schema
2. Create the admin account from `ADMIN_LOGIN_ID` and `ADMIN_PASSWORD`
3. Start background VM monitoring

The test student is created only when `SEED_TEST_STUDENT=1`. Production must
leave this variable unset because that account has a known password.

Production startup refuses to seed the admin when `ADMIN_PASSWORD` is
missing or is a known weak default. Set a unique password before starting
the stack.

### Migrating an existing checkout to this repository

Use this once, when a production host currently runs a checkout of an older
repository and must switch to this one. Do not do it during a King of the Hill
game. Requires the Docker Compose v2 plugin (`docker compose`, not
`docker-compose`).

**Keep the compose project.** Compose derives the project name, and therefore
the database volume name, from the checkout directory name. Keep the existing
directory (for example `/opt/lab-playground`) and switch its remote in place.
If you must use a different directory, set `COMPOSE_PROJECT_NAME=<old-name>`
in `.env` first. Otherwise Compose creates a **new, empty database volume** and
the site appears to have lost all its data.

**1. Back up operator files and the database** to a private directory:

```bash
cd /opt/lab-playground
install -d -m 700 /root/backups/cutover
cp -p templates.yaml data/students_merged.csv config/headscale-acl.json \
  /root/backups/cutover/
scripts/backup-database.sh
git rev-parse --short HEAD    # rollback target
```

**2. Add the new `.env` keys first**, before fetching any compose file that
requires them:

- `SITE_NAME`, `SITE_BRAND_TAG`, `SITE_ORG`, `TEMPLATES_FILE`: the current
  site's values;
- `VPN_LOGIN_SERVER`: the full login server **including its scheme**, for
  example `https://vpn.example.edu`. A bare hostname is rendered into the
  student instructions as-is and will not work with `--login-server`;
- `SITE_DOMAIN` and `ACME_EMAIL`: consumed by Caddy;
- `PROXMOX_VERIFY_SSL`: the new default verifies the certificate. Keep
  `false` (or set `PROXMOX_CA_BUNDLE`) only if the cluster uses a
  self-signed certificate;
- `SELF_SERVICE_PASSWORD_RESET=true` only if the site wants the old
  behavior (see the warning in the environment section);
- `TRUSTED_PROXY_HOPS` is preset to `1` by `docker-compose.prod.yml`; do not
  set it unless the app is exposed without Caddy.

Then check the file:

```bash
python3 scripts/preflight_env.py .env
```

Fix everything it reports before continuing.

**3. Switch the remote and take the code:**

```bash
git remote rename origin old-origin
git remote add origin <new-repository-url>
git fetch origin
git checkout -f -B main origin/main
```

**4. Restore operator files.** The new repository does not track them, and
the container runs as uid 1000, so the catalog must be world-readable:

```bash
cp -p /root/backups/cutover/templates.yaml templates.yaml
cp -p /root/backups/cutover/students_merged.csv data/students_merged.csv
mkdir -p config
cp -p /root/backups/cutover/headscale-acl.json config/headscale-acl.json
chmod 644 templates.yaml
```

**5. Start and verify:**

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
curl -fsS https://<SITE_DOMAIN>/health
docker compose -f docker-compose.prod.yml exec vm-automation \
  python -c "from app.config import proxmox_tls_verify as v; print(v())"
```

The last command prints the effective Proxmox TLS verification setting
(`True`, `False`, or the CA bundle path); confirm it is what you intend. Log in
as an administrator and confirm the existing deployment groups, students and
VMs are present. If they are not, you started with the wrong project name:
`docker compose down`, fix the directory or `COMPOSE_PROJECT_NAME`, and
start again. To roll back, restore the old remote and the rollback commit and
rebuild.

### Redeploy an Existing Production Host

Run this procedure from the checkout directory, for example
`/opt/lab-playground`.

Do not redeploy during a King of the Hill game. The restart drops every
Server-Sent Events stream and restarts the scoring loop.

**1. Back up the database first.** Keep the rollback commit with it.

```bash
cd /opt/lab-playground
scripts/backup-database.sh
git rev-parse --short HEAD    # write this down; it is the rollback target
```

The script writes
`/root/backups/vmdatabase-YYYYMMDD-HHMMSS.sql.gz`. It rejects incomplete
dumps before publishing the file.

**One-time configuration cutover:** releases before the site-configuration
cutover tracked the real `templates.yaml`. The cutover removes that tracked
file, so save every operator file outside the checkout **before** merging:

```bash
install -d -m 700 /root/backups/site-config-cutover
cp -p templates.yaml data/students_merged.csv config/headscale-acl.json \
  /root/backups/site-config-cutover/
```

Before the merge, add the deployment's current values for all new keys to
the existing untracked `.env`. **Do this before pulling a compose file:**
`docker-compose.prod.yml` uses `${VAR:?}` for required keys, so Compose
aborts with a clear error if a new required key is not yet in `.env`.

```bash
TEMPLATES_FILE=templates.yaml
SITE_NAME=<current-site-name>
SITE_BRAND_TAG=<current-brand-tag>
SITE_ORG=<current-footer-organization>
VPN_LOGIN_SERVER=<current-vpn-login-scheme-and-host>   # e.g. https://vpn.example.edu
SITE_DOMAIN=<current-public-domain>
ACME_EMAIL=<current-acme-email>
```

Do not substitute the neutral example values for an existing site's values.
Keep the existing strong `ADMIN_PASSWORD`.

**2. Take the new code.**

```bash
git fetch origin
git merge --ff-only origin/main
```

On the first cutover only, restore the catalog as an untracked file before
running migrations or restarting:

```bash
cp -p /root/backups/site-config-cutover/templates.yaml templates.yaml
```

**3. Run every migration the release needs, before the new code starts.**
Each one is idempotent, so a re-run is safe and reports what it skipped.
Adding a column does not break the old code, so this order is correct.
Skipping one is not safe: `db.create_all()` never adds columns to an existing
table, and the new code reads `users.session_version` and the
`vm_records.deletion_*` columns on every request and expiry pass.

```bash
for migration in migrate_koth_claim_mode.py migrate_add_session_version.py \
                 migrate_vm_deletion_retry.py; do
  docker compose -f docker-compose.prod.yml exec -T -w /app vm-automation \
    python scripts/$migration
done
```

If the running image predates `scripts/` being copied into it, or predates
the migration itself, the file is not there yet. Inject it for this one run:

```bash
for migration in migrate_koth_claim_mode.py migrate_add_session_version.py \
                 migrate_vm_deletion_retry.py; do
  docker cp scripts/$migration proxmox-vm-automation-prod:/tmp/$migration
  docker compose -f docker-compose.prod.yml exec -T vm-automation python /tmp/$migration
  docker compose -f docker-compose.prod.yml exec -T -u root vm-automation rm -f /tmp/$migration
done
```

**4. Rebuild and restart.** Confirm that the target host has enough free
memory and CPU for the stack and the expected class load.

```bash
free -m
docker compose -f ./docker-compose.prod.yml down && \
  docker compose -f ./docker-compose.prod.yml up -d --build
```

**5. Verify.** All five checks must pass.

```bash
docker ps --format '{{.Names}}\t{{.Status}}'      # all three healthy
docker compose -f docker-compose.prod.yml logs --tail=25 vm-automation
curl -s https://lab.example.edu/auth/health      # {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' https://lab.example.edu/list      # 401
curl -s -o /dev/null -w '%{http_code}\n' https://lab.example.edu/koth/api/_diag  # 401
```

The last two must be `401`. A `200` means an authenticated route is open to
anyone.

For an authenticated check without a password, drive the app's own test
client inside the container. `PYTHONPATH=/app` is required, because the
script does not live in the application directory.

```bash
docker compose -f docker-compose.prod.yml exec -T -e PYTHONPATH=/app \
  vm-automation python - <<'PY'
from app import create_app
from app.models import User
app = create_app()
with app.app_context():
    admin = User.query.filter_by(is_admin=True).first()
c = app.test_client()
with c.session_transaction() as s:
    s['user_id'] = admin.id
    s['session_version'] = admin.session_version
print('_diag', c.get('/koth/api/_diag').status_code)
print('list ', c.get('/list').status_code)
PY
```

**6. Roll back** if any check fails.

```bash
git checkout <rollback-commit>

# Only if a migration must also be undone:
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d db
gunzip -c /root/backups/vmdatabase-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'exec mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'

docker compose -f docker-compose.prod.yml down && \
  docker compose -f docker-compose.prod.yml up -d --build
```

The migrations in this release only ADD columns and an index. The old code
ignores them. A code rollback alone is therefore sufficient, and the
database restore is only for a migration that changes existing data.

### Watch a King of the Hill game

`GET /koth/api/_diag` is admin-only and reports live numbers. Read it during
a game, not after.

| Field | Meaning | Act when |
|---|---|---|
| `tick.overruns` | Scoring passes that took longer than their own interval | Above 0. APScheduler drops runs and reports nothing useful. Raise `scan_interval_sec`, or use `beacon` claim mode. |
| `tick.max_sec` | Longest pass so far | Approaches `scan_interval_sec` |
| `events.subscribers` | Open scoreboard streams | Compare with the number of students |
| `events.total_dropped_frames` | Frames a slow client lost | Above 0 for a long period |
| `db_pool.checked_out` | Database connections in use | Approaches 100 |
| `game.boxes_without_ip` | Boxes with no address yet | Above 0 after the boxes have booted. Nothing can probe or claim those. |

## Post-Deployment Verification

### 1. Health Check

```bash
# Check container health
docker ps

# Verify the public endpoint through Caddy
curl https://lab.example.edu/auth/health

# Expected output: {"status":"ok",...}
```

The application container exposes port 5000 only to the Compose network.
Caddy is the public entry point on ports 80 and 443.

### 2. Application Access

Open a web browser and navigate to:
```
https://lab.example.edu
```

Login with the admin credentials and verify:
- [ ] Login works correctly
- [ ] VM list loads
- [ ] Can create a test VM
- [ ] Can view VM details
- [ ] Can destroy VMs
- [ ] Background expiration checker is running

### 3. Database Verification

```bash
# Connect to database
docker exec -it mysql-db-prod mysql -u root -p

# Verify tables
USE vmdatabase;
SHOW TABLES;
SELECT * FROM users;
SELECT * FROM vm_records LIMIT 5;
```

### 4. Log Verification

```bash
# Check application logs
docker logs proxmox-vm-automation-prod

# Check for errors
docker logs proxmox-vm-automation-prod 2>&1 | grep -i error

# Check database logs
docker logs mysql-db-prod
```

## Monitoring and Maintenance

### Log Management

Logs are automatically rotated (max 10MB per file, 3 files retained).

```bash
# View real-time logs
docker compose -f docker-compose.prod.yml logs -f vm-automation

# View last 100 lines
docker compose -f docker-compose.prod.yml logs --tail=100 vm-automation

# Export logs for analysis
docker logs proxmox-vm-automation-prod > app-logs-$(date +%Y%m%d).log
```

### Resource Monitoring

```bash
# Monitor container resource usage
docker stats

# Check disk usage
df -h /var/lib/docker
du -sh /opt/lab-playground/data
```

### Database Maintenance

```bash
# Optimize database tables (run monthly)
docker exec mysql-db-prod mysql -u root -p -e "
USE vmdatabase;
OPTIMIZE TABLE vm_records;
OPTIMIZE TABLE users;
"

# Check database size
docker exec mysql-db-prod mysql -u root -p -e "
SELECT table_schema AS 'Database', 
       ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS 'Size (MB)'
FROM information_schema.tables 
WHERE table_schema = 'vmdatabase'
GROUP BY table_schema;
"
```

### Updates and Upgrades

```bash
# Pull latest code
cd /opt/lab-playground
git pull

# Backup database before updating
scripts/backup-database.sh

# Rebuild and restart using the production procedure
docker compose -f ./docker-compose.prod.yml down && \
  docker compose -f ./docker-compose.prod.yml up -d --build

# Verify deployment
docker compose -f docker-compose.prod.yml logs -f
```

## Backup and Recovery

### Automated Database Backups

Run the production backup script from the checkout:

```bash
cd /opt/lab-playground
scripts/backup-database.sh
```

It dumps MySQL through `docker compose exec -T db`, verifies the
`mysqldump` completion trailer, and then publishes
`/root/backups/vmdatabase-YYYYMMDD-HHMMSS.sql.gz`. It removes only matching
dumps older than `RETENTION_DAYS`, which defaults to 14 days. It never
removes other files.

Install the repository's systemd units for the daily 07:30 UTC backup. The
service reads `COMPOSE_DIR`, `BACKUP_DIR`, and `RETENTION_DAYS` from
`/etc/default/vm-automation-backup`; copy the example and set `COMPOSE_DIR` to
your checkout:

```bash
install -m 600 scripts/systemd/vm-automation-backup.env.example /etc/default/vm-automation-backup
$EDITOR /etc/default/vm-automation-backup   # set COMPOSE_DIR
cp scripts/systemd/vm-automation-backup.service scripts/systemd/vm-automation-backup.timer /etc/systemd/system/ && \
  systemctl daemon-reload && \
  systemctl enable --now vm-automation-backup.timer
```

### Backup Important Files

Back up every operator-owned file, not only `.env`. The catalog, the
Headscale ACL, and the roster (which holds live VPN credentials) are untracked
and exist nowhere else. Keep the archive private.

```bash
# Create full backup (umask keeps the archive owner-only)
( umask 077 && tar -czf backup-$(date +%Y%m%d).tar.gz \
    .env \
    templates.yaml \
    config/headscale-acl.json \
    data/students_merged.csv \
    scripts/site.env \
    /root/backups/ )

# Store backup securely off-server
scp backup-*.tar.gz backup-server:/backups/proxmox-vm-automation/
```

Omit `scripts/site.env` if that file lives on a separate workstation. Do not
use `data/` wholesale unless you also want application-generated files.

### Recovery Procedure

```bash
cd /opt/lab-playground

# Stop the application, then start only MySQL
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d db

# Restore a compressed backup
gunzip -c /root/backups/vmdatabase-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'exec mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'

# Restart the full stack
docker compose -f docker-compose.prod.yml up -d
```

## Security Considerations

### 1. Network Security

Caddy is the only public entry point. It publishes ports 80 and 443 and
proxies to the application's Compose-network-only port 5000. Do not publish
the application container directly.

#### Database port

MySQL is published only on `127.0.0.1:3306` for host-local clients. Keep this
loopback binding. Docker-published ports bypass `ufw`, so never publish MySQL
on `0.0.0.0`; the loopback bind is the database's host-network boundary.

### 2. Access Control

- Change all default passwords immediately
- Use strong, unique passwords for database and admin accounts
- Implement regular password rotation policy
- Review user accounts periodically
- Remove test accounts in production

### 3. File Permissions

```bash
# Verify secure permissions
ls -la .env           # Should be 600 (rw-------)
ls -la templates.yaml # Should be 644 (rw-r--r--)
ls -ld data/          # Should be 755 (rwxr-xr-x)
```

### 4. Database Security

```bash
# Disable remote MySQL access if not needed
# Edit docker-compose.prod.yml and remove ports exposure:
# ports:
#   - "3306:3306"  # Comment this out

# Rebuild containers
docker compose -f docker-compose.prod.yml up -d
```

### 5. Regular Security Updates

```bash
# Update base images monthly
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --build

# Update Python dependencies
pip list --outdated
# Review and update requirements.txt as needed
```

## Troubleshooting

### Application Won't Start

```bash
# Check container status
docker ps -a

# View detailed logs
docker logs proxmox-vm-automation-prod --tail=200

# Common issues:
# - Database not ready: Wait 30 seconds and check again
# - Port conflict: Change APP_PORT in .env
# - Permission issues: Check file permissions
```

### Database Connection Issues

```bash
# Verify database is running
docker exec mysql-db-prod mysqladmin -u root -p ping

# Check connection from app container
docker exec proxmox-vm-automation-prod python -c "
import pymysql
conn = pymysql.connect(host='db', user='vmuser', password='vmpassword', database='vmdatabase')
print('Connection successful')
"

# Reset database connection
docker compose -f docker-compose.prod.yml restart vm-automation
```

### Proxmox Connection Issues

```bash
# Test Proxmox API from container
docker exec proxmox-vm-automation-prod python -c "
from proxmoxer import ProxmoxAPI
import os
px = ProxmoxAPI(
    os.getenv('PROXMOX_HOST'),
    user=os.getenv('PROXMOX_USER'),
    password=os.getenv('PROXMOX_PASSWORD'),
    verify_ssl=(os.getenv('PROXMOX_CA_BUNDLE') or os.getenv('PROXMOX_VERIFY_SSL', 'true').lower() != 'false')
)
print('Proxmox version:', px.version.get())
"
```

### High Memory Usage

Do not increase or decrease the Gunicorn worker count as a memory
workaround. Production must run exactly one gevent worker. APScheduler and
the SSE event bus are in-process, so additional workers duplicate the
scheduler and split SSE subscribers.

```bash
docker stats proxmox-vm-automation-prod
docker compose -f docker-compose.prod.yml logs --tail=100 vm-automation
docker compose -f docker-compose.prod.yml restart vm-automation
```

### Expired VMs Not Being Destroyed

```bash
# Check background task logs
docker logs proxmox-vm-automation-prod 2>&1 | grep -i "expiration\|expired"

# Restart the application container if the expiry loop is not running
docker compose -f docker-compose.prod.yml restart vm-automation
```

Do not invoke `check_and_destroy_expired_vms()` from a separate Python
process. Creating another application instance starts a second infinite
expiry loop and another APScheduler. Restart the existing container instead.

## Performance Tuning

### Gunicorn Workers

Production must run exactly one gevent worker, as configured in
`gunicorn.conf.py`. APScheduler and the SSE event bus both keep state in the
worker process. More than one worker runs competing schedulers and splits
SSE delivery across separate subscriber sets.

### Database Optimization

`docker-compose.prod.yml` currently configures MySQL for UTF-8 and 300
connections:

```yaml
command: --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci --max-connections=300
```

### Container Resource Limits

`docker-compose.prod.yml` does not set CPU or memory limits. It raises the
application container's `nofile` soft and hard limits to 65536 for
classroom-sized SSE fan-out.

## Support and Maintenance

### Routine Maintenance Schedule

**Daily:**
- Monitor container health
- Review error logs

**Weekly:**
- Check disk space usage
- Review VM creation/destruction logs
- Verify backups are running

**Monthly:**
- Update Docker images
- Optimize database tables
- Review and clean old logs
- Test backup restoration
- Review user accounts

**Quarterly:**
- Security audit
- Performance review
- Update documentation
- Review and update dependencies

### Getting Help

If you encounter issues:

1. Check this documentation
2. Review application logs
3. Check Docker container logs
4. Verify environment configuration
5. Test Proxmox API connectivity
6. Review GitHub issues (if using version control)

## Additional Resources

- [DEPLOYMENT.md](DEPLOYMENT.md) - Standard deployment guide
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) - Development setup
- [USER_AUTH_GUIDE.md](USER_AUTH_GUIDE.md) - Authentication system
- [DOCKER.md](DOCKER.md) - Docker configuration details

---

**Last Updated:** January 28, 2026  
**Version:** 1.0 (Production Release)
