# Production Deployment - Quick Reference

## Quick Start

### 1. Run Setup Script
```bash
chmod +x setup-production.sh
./setup-production.sh
```

### 2. Create Site Configuration

```bash
cp .env.example .env
cp templates.example.yaml templates.yaml
cp config/headscale-acl.example.json config/headscale-acl.json
```

Keep these files untracked and replace the neutral examples with operator
values. For workstation scripts, also copy `scripts/site.env.example` to
`scripts/site.env`.

Example template mapping:
```yaml
templates:
  - name: "loadbalancer"
    template_locations:
      node1: 110
      node2: 210
    deployment_groups:
      - "homework1"
```

### 3. Deploy
```bash
docker compose -f docker-compose.prod.yml up -d --build
```

### 4. Verify
```bash
# Check status
docker compose -f docker-compose.prod.yml ps

# Check logs
docker compose -f docker-compose.prod.yml logs -f

# Health check through Caddy
curl https://lab.example.edu/auth/health
```

## Key Differences from Development

| Aspect | Development | Production |
|--------|------------|------------|
| **Server** | Flask dev server | Gunicorn (1 gevent worker) |
| **Runtime** | Python 3.12 | Python 3.12-slim; Flask 3.1.3; Werkzeug 3.1.9; gunicorn 22.0.0 |
| **Dockerfile** | `Dockerfile` | `Dockerfile.prod` |
| **Compose File** | `docker-compose.yml` | `docker-compose.prod.yml` |
| **Entry Point** | `run.py` | `run_prod.py` |
| **User** | root | appuser (non-root) |
| **Health Checks** | None | Enabled |
| **Resource Limits** | None | No CPU/memory limits; `nofile` is 65536 |
| **Log Rotation** | None | Automatic (10MB, 3 files) |
| **Database Backups** | Manual | Scripted |

## Production Commands

### Start/Stop
```bash
# Start
docker compose -f docker-compose.prod.yml up -d

# Stop
docker compose -f docker-compose.prod.yml down

# Restart
docker compose -f docker-compose.prod.yml restart
```

### Logs
```bash
# Follow logs
docker compose -f docker-compose.prod.yml logs -f vm-automation

# Last 100 lines
docker compose -f docker-compose.prod.yml logs --tail=100 vm-automation

# Error logs only
docker compose -f docker-compose.prod.yml logs vm-automation 2>&1 | grep -i error
```

### Database Backup

```bash
cd /opt/lab-playground
# Manual backup
scripts/backup-database.sh

# Install the daily 07:30 UTC systemd timer and its settings file
install -m 600 scripts/systemd/vm-automation-backup.env.example /etc/default/vm-automation-backup
$EDITOR /etc/default/vm-automation-backup   # set COMPOSE_DIR=/opt/lab-playground
cp scripts/systemd/vm-automation-backup.service scripts/systemd/vm-automation-backup.timer /etc/systemd/system/ && \
  systemctl daemon-reload && \
  systemctl enable --now vm-automation-backup.timer
```

The script writes verified compressed dumps to `/root/backups/`, retains
matching dumps for 14 days by default, and never prunes other files.

### Updates

Before the first update that changes `templates.yaml` from tracked to
operator-owned, save every operator file outside the checkout and add the
current deployment values for `TEMPLATES_FILE`, `SITE_NAME`,
`SITE_BRAND_TAG`, `SITE_ORG`, `VPN_LOGIN_SERVER` (with its scheme, such as
`https://vpn.example.edu`), `SITE_DOMAIN`, and `ACME_EMAIL` to `.env`. Do
this before `git pull`: `docker-compose.prod.yml` rejects a missing required
`${VAR:?}` key. Requires the Docker Compose v2 plugin. Validate with
`python3 scripts/preflight_env.py .env`.

```bash
cd /opt/lab-playground
install -d -m 700 /root/backups/site-config-cutover
cp -p templates.yaml data/students_merged.csv config/headscale-acl.json \
  /root/backups/site-config-cutover/
scripts/backup-database.sh
python3 scripts/preflight_env.py .env
git pull
cp -p /root/backups/site-config-cutover/templates.yaml templates.yaml
cp -p /root/backups/site-config-cutover/students_merged.csv data/students_merged.csv
cp -p /root/backups/site-config-cutover/headscale-acl.json config/headscale-acl.json
chmod 644 templates.yaml    # the container runs as uid 1000
docker compose -f ./docker-compose.prod.yml down && \
  docker compose -f ./docker-compose.prod.yml up -d --build
```

The save/restore is required only for that first cutover. Later pulls leave
the untracked files in place. To move a host from another repository, follow
"Migrating an existing checkout to this repository" in
`PRODUCTION_DEPLOYMENT.md`; keep the checkout directory name (or set
`COMPOSE_PROJECT_NAME`) so the database volume is reused.

`SELF_SERVICE_PASSWORD_RESET` defaults to `false`; `true` lets a student
reset a password with only a login and SIS ID. `docker-compose.prod.yml`
deliberately does not pass `SESSION_COOKIE_SECURE`/`SESSION_COOKIE_HTTPONLY`;
they are forced on in production.

### Resource Monitoring
```bash
# Real-time resource usage
docker stats

# Container status
docker compose -f docker-compose.prod.yml ps

# Disk usage
du -sh data/ /root/backups/
```

## Environment Variables

### Required (Must Change)
```bash
SECRET_KEY=                    # Generate with: python3 -c 'import secrets; print(secrets.token_hex(32))'
PROXMOX_HOST=<proxmox-host>
PROXMOX_PASSWORD=<choose-a-password>
MYSQL_ROOT_PASSWORD=<choose-a-password>
MYSQL_PASSWORD=<choose-a-password>
ADMIN_PASSWORD=<choose-a-password>  # Production rejects missing/known defaults
SITE_DOMAIN=lab.example.edu
ACME_EMAIL=admin@example.edu
```

### Site Settings
```bash
TEMPLATES_FILE=templates.yaml  # Defaults to this app-root-relative path
SITE_NAME="Lab Playground"     # Default shown
SITE_BRAND_TAG=                # Optional small label above title
SITE_ORG=                      # Optional footer organization
VPN_LOGIN_SERVER=https://vpn.example.edu  # Optional; unset omits --login-server
```

`SITE_DOMAIN` and `ACME_EMAIL` are passed to Caddy. See the README's
**Site configuration** section for the image variables, operations variables,
and complete list of untracked files.

### Important Settings
```bash
DB_PORT=3306                   # Loopback-only host port for MySQL
DEFAULT_VM_UPTIME_MINUTES=480  # Default VM lifetime (8 hours)
EXTEND_TIME_MINUTES=240        # Extension time (4 hours)
ENABLE_LOAD_BALANCING=true     # Enable load balancing
CPU_THRESHOLD=80.0             # Max CPU % for node selection
MEMORY_THRESHOLD=85.0          # Max memory % for node selection
```

## Security Checklist

- [ ] Changed SECRET_KEY to random value
- [ ] Changed MYSQL_ROOT_PASSWORD
- [ ] Changed MYSQL_PASSWORD
- [ ] Set a unique `ADMIN_PASSWORD` before first production startup
- [ ] Left `SEED_TEST_STUDENT` unset in production
- [ ] Configured firewall rules
- [ ] Set up HTTPS (if exposing publicly)
- [ ] Configured backup automation
- [ ] Restricted database port access
- [ ] Reviewed file permissions (`.env` should be 600)
- [ ] Disabled debug mode

### Database port

MySQL is published only on `127.0.0.1:3306`. Docker-published ports bypass
`ufw`, so never publish the database on `0.0.0.0`.

## Troubleshooting

### Containers Won't Start
```bash
# Check logs
docker compose -f docker-compose.prod.yml logs

# Check public and loopback-only listeners
sudo lsof -nP -iTCP:80 -iTCP:443 -sTCP:LISTEN
sudo lsof -nP -iTCP:3306 -sTCP:LISTEN

# Reset everything
docker compose -f docker-compose.prod.yml down -v
docker compose -f docker-compose.prod.yml up -d --build
```

### Database Connection Failed
```bash
# Check database is ready
docker exec mysql-db-prod mysqladmin -u root -p ping

# Restart app container
docker compose -f docker-compose.prod.yml restart vm-automation
```

### Proxmox API Errors
```bash
# Test connection
docker exec proxmox-vm-automation-prod python -c "
from proxmoxer import ProxmoxAPI
import os
px = ProxmoxAPI(os.getenv('PROXMOX_HOST'), user=os.getenv('PROXMOX_USER'),
                password=os.getenv('PROXMOX_PASSWORD'),
                verify_ssl=(os.getenv('PROXMOX_CA_BUNDLE') or os.getenv('PROXMOX_VERIFY_SSL', 'true').lower() != 'false'))
print('Connection OK:', px.version.get())
"
```

### High Memory Usage

Keep Gunicorn at exactly one gevent worker. APScheduler and the SSE event
bus are in-process; additional workers duplicate the scheduler and split
SSE subscribers. Use `docker stats proxmox-vm-automation-prod` and the
application logs to identify memory growth instead of changing workers.

## Performance Tuning

### Gunicorn Workers

Production must run exactly one gevent worker, as configured in
`gunicorn.conf.py`. Concurrency comes from gevent greenlets.

### Database Connections

`docker-compose.prod.yml` configures MySQL with `--max-connections=300`.

### Container Resources

`docker-compose.prod.yml` sets no CPU or memory limits. It raises the
application container's `nofile` soft and hard limits to 65536.

## Monitoring Endpoints

### Health Check
```bash
curl https://lab.example.edu/auth/health
# Returns: {"status":"ok",...}
```

### Application Metrics
```bash
# Check VM count
docker exec mysql-db-prod mysql -u root -p -e "USE vmdatabase; SELECT COUNT(*) FROM vm_records;"

# Check active VMs
docker exec mysql-db-prod mysql -u root -p -e "USE vmdatabase; SELECT * FROM vm_records WHERE expires_at > NOW();"
```

## Backup and Recovery

### Backup

```bash
cd /opt/lab-playground
scripts/backup-database.sh
```

The database backup is
`/root/backups/vmdatabase-YYYYMMDD-HHMMSS.sql.gz`.

### Restore

```bash
cd /opt/lab-playground
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d db

gunzip -c /root/backups/vmdatabase-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'exec mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'

docker compose -f docker-compose.prod.yml up -d
```

## Maintenance Schedule

**Daily:**
- Monitor container health
- Review error logs

**Weekly:**
- Check disk space
- Verify backups

**Monthly:**
- Update Docker images
- Optimize database
- Security review

## Support

For detailed documentation, see:
- [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) - Complete deployment guide
- [DEPLOYMENT.md](DEPLOYMENT.md) - General deployment info
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) - Development setup

---
**Version:** 1.0  
**Last Updated:** October 23, 2026
