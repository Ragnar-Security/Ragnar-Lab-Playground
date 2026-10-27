# Ragnar Lab Playground

A reusable Flask lab portal for deploying, managing, and destroying Proxmox virtual machines. Students use a web dashboard while operators control access, lab visibility, and the VM fleet.

## Features

### For Students
- **My Labs tab** -- unified view of active VMs with extend and destroy controls in one place
- **One-click deployment** -- select a lab, click Create, and the portal provisions all VMs in parallel with automatic retries
- **Lifecycle management** -- extend VM lifetime when less than 60 minutes remain, or destroy a deployment group when finished
- **Entry-point IP display** -- only the entry-point VM's IP is shown; internal topology stays hidden

### For Admins
- **Admin Dashboard** -- student management, VM overview, bulk operations, and alert testing from a single panel
- **Lab assignment restrictions** -- control which deployment groups each student can see (unrestricted by default; restrict by assigning specific students)
- **Student management** -- enable/disable accounts, toggle admin role, view per-student VM counts
- **Bulk operations** -- extend or destroy many VMs at once; bulk-assign students to labs
- **CSV import** -- import student whitelist from Canvas CSV exports; VLAN tags and Headscale keys are pre-assigned
- **Alert system** -- email, webhook (Slack/Discord), database, and file alerts for failed VM deletions and config corruption

### King of the Hill (KOTH) Mode
- **Competitive lab arena** -- one shared deployment of a `templates.yaml` group, multiple `StudentGroup`s play as teams, single-active-game enforced at the DB
- **Live scoreboard + network map** -- Cytoscape.js topology view, scoreboard with rank/score, event log, all updated via Server-Sent Events
- **Token-authenticated box claims** -- students `curl /koth/claim -d token=... -d box=$(hostname)` from inside rooted boxes; per-team submission tokens are rotatable
- **Service-uptime scoring** -- in-process port scanner ticks every 12s, awards points for each open service the team owns; exponential late-game multiplier
- **Admin-defined flags** -- per-game flags entered in the admin UI, submitted via `/koth/flag`, deduplicated per (flag, team)
- **Pause + hide controls** -- pause non-admin deployments while the game is in progress; hide scoreboard or map for end-of-game reveal
- **Deploy / redeploy** -- redeploy preserves scores/teams/history but re-clones VMs (new IPs, ownership clears)
- See [docs/KING_OF_THE_HILL.md](docs/KING_OF_THE_HILL.md) for the full guide. Operators enable KOTH on deployment groups in their own `templates.yaml`.

### Infrastructure
- **Weighted load balancing** -- distributes VMs across Proxmox nodes by CPU (30%), memory (30%), and free memory (40%)
- **Cross-node template sync** -- `sync_template.sh` replicates templates between nodes using vzdump/qmrestore (no NFS)
- **Automatic cleanup** -- a background task destroys expired VMs every minute; a record whose guest was deleted by hand in Proxmox is retired, with an admin alert, once fresh inventory has confirmed the absence for five minutes; a time-boxed orphan reaper removes expired guests that have no record
- **Per-student VLAN isolation** -- entry VLAN (100-200) and internal VLAN (200-300) per student
- **Startup config validation** -- templates.yaml is validated at boot; mismatches are caught before any VM is created

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Flask 2.3, Python 3.12 |
| Database | MySQL 8.0 via SQLAlchemy, SQLite for tests |
| Proxmox API | Proxmoxer 2.0 |
| Frontend | HTML5, Bootstrap 5, Vanilla JS (5 modules) |
| Fonts | DM Sans (UI), JetBrains Mono (code/data) |
| Configuration | `.env` + `templates.yaml` |
| Deployment | Docker, Docker Compose, Gunicorn |
| CI/CD | GitHub Actions (unit + integration + Docker Hub push) |
| Testing | pytest, Playwright |

## Project Structure

```
Ragnar-Lab-Playground/
├── app/                          # Flask application package
│   ├── __init__.py               # App factory
│   ├── auth.py                   # Authentication routes & decorators
│   ├── config.py                 # Configuration from env vars
│   ├── models.py                 # SQLAlchemy models
│   ├── proxmox_service.py        # Proxmox API wrapper
│   ├── tasks.py                  # Background tasks (cleanup, alerts)
│   ├── template_manager.py       # templates.yaml loader & validator
│   ├── utils.py                  # Shared utilities
│   └── routes/                   # Modular route blueprints
│       ├── __init__.py           # Blueprint aggregation
│       ├── admin.py              # Admin endpoints (students, assignments, bulk ops)
│       ├── node_resources.py     # Cluster node monitoring
│       ├── pages.py              # HTML page rendering
│       ├── templates.py          # Deployment group listing with restriction filter
│       ├── utils.py              # Route helper functions
│       ├── vm_creation.py        # Parallel VM creation
│       ├── vm_lifecycle.py       # Extend & destroy
│       ├── vm_listing.py         # VM list with orphan cleanup
│       └── vm_status.py          # Time-remaining queries
├── static/
│   ├── css/style.css             # Dark SOC-style theme
│   └── js/
│       ├── app.js                # Core application logic
│       ├── admin.js              # Admin dashboard UI
│       ├── auth.js               # Login/register flows
│       ├── utils.js              # Shared JS helpers
│       └── vm-operations.js      # VM create/extend/destroy UI
├── templates/                    # Jinja2 HTML templates
│   ├── index.html                # Main dashboard
│   ├── login.html                # Login page
│   ├── register.html             # Registration page
│   └── reset_password.html       # Password reset page
├── tests/
│   ├── conftest.py               # Shared test fixtures
│   ├── test_admin_dashboard.py   # Admin endpoint tests
│   ├── test_api_routes.py        # API route tests
│   ├── test_auth_routes.py       # Auth flow tests
│   ├── test_config.py            # Config validation tests
│   ├── test_lab_assignments.py   # Assignment restriction tests
│   ├── test_models.py            # Model unit tests
│   ├── test_tasks.py             # Background task tests
│   ├── test_template_manager.py  # Template loading tests
│   ├── test_vm_creation_parallel.py  # Parallel creation tests
│   └── integration/
│       ├── conftest.py           # Integration test fixtures
│       └── test_flows.py         # Playwright browser tests
├── scripts/
│   ├── sync_template.sh          # Cross-node template replication
│   ├── import_students.py        # Canvas CSV student import
│   ├── init_db.py                # Database initialization
│   ├── jumpbox_provision.sh      # Create the jumpbox VM on a Proxmox node (docs/JUMPBOX.md)
│   ├── jumpbox_configure.sh      # Jumpbox router: VLAN gateways, DHCP, Tailscale, optional Zeek/RITA
│   ├── jumpbox_netgen.py         # netplan/dnsmasq generator used by jumpbox_configure.sh
│   ├── jumpbox_accounts.sh       # Optional jumpbox SSH accounts: create, lock-stale, unlock
│   ├── backup-database.sh        # MySQL backup utility
│   ├── rotate_headscale_keys.sh  # Rotate students' Headscale pre-auth keys (dry run by default)
│   ├── update_headscale_keys.py  # Store rotated keys in the portal database (run in the app container)
│   └── migrate_*.py              # Database migration scripts
├── .github/workflows/ci.yml     # CI pipeline definition
├── docker-compose.yml            # Development environment
├── docker-compose.prod.yml       # Production environment
├── docker-compose.hub.yml        # Deploy from Docker Hub (no source needed)
├── docker-compose.test.yml       # Integration test environment
├── Dockerfile                    # Development image
├── Dockerfile.prod               # Production image (Gunicorn)
├── templates.example.yaml        # Example lab/deployment group definitions
├── requirements.txt              # Python dependencies
├── run.py                        # Dev entry point
├── run_prod.py                   # Production entry point
└── gunicorn.conf.py              # Gunicorn configuration
```

## Quick Start

The fastest path is Docker Compose:

```bash
# 1. Clone the repository
git clone git@github.com:Ragnar-Security/Ragnar-Lab-Playground.git
cd Ragnar-Lab-Playground

# 2. Create operator-owned configuration
cp .env.example .env
cp templates.example.yaml templates.yaml
# Edit both files for your Proxmox cluster and site

# 3. Start everything
docker compose up -d --build

# 4. Open http://localhost:5001
#    Set ADMIN_LOGIN_ID and ADMIN_PASSWORD explicitly before sharing the instance
```

## Deploying from Docker Hub

Deploy without cloning the repository. This stack serves plain HTTP and binds
the app to `127.0.0.1:5001` only, so it is not a complete production
deployment by itself: put a TLS reverse proxy in front of it (see `Caddyfile`
and `docker-compose.prod.yml` for the hardened setup) before exposing it:

1. Create a directory with your config files:
   ```bash
   mkdir lab-playground && cd lab-playground
   ```

2. Create your `.env` file with required settings (see `.env.example` in the repository)

3. Copy `templates.example.yaml` to `templates.yaml` and configure your labs

4. Download the compose file:
   ```bash
   curl -o docker-compose.yml https://raw.githubusercontent.com/Ragnar-Security/Ragnar-Lab-Playground/main/docker-compose.hub.yml
   ```

5. Deploy:
   ```bash
   docker compose up -d
   ```

The image is automatically built and pushed on every merge to main.

For production, use the hardened compose file:

```bash
./setup-production.sh          # interactive setup wizard
docker compose -f docker-compose.prod.yml up -d --build
curl http://localhost:5000/auth/health
```

## Site configuration

Real hostnames, IP addresses, usernames, credentials, node names, storage
pools, and VMIDs belong only in operator-owned untracked configuration. Start
from the committed examples; never put a deployment's values in a tracked
file.

### Untracked files

| File | Create from / purpose |
|------|------------------------|
| `.env` | Copy `.env.example`; application, database, Proxmox, proxy, and image settings |
| `templates.yaml` | Copy `templates.example.yaml`; deployment groups, node/storage mappings, and template VMIDs |
| `scripts/site.env` | Copy `scripts/site.env.example`; workstation operations and remote-host settings |
| `config/headscale-acl.json` | Copy `config/headscale-acl.example.json`; deployment-specific Headscale users and hosts |
| `data/students_merged.csv` | Generated roster with student records and live VPN credentials; never commit it |

When upgrading a deployment from a revision that tracked `templates.yaml`,
copy every operator file (`templates.yaml`, `data/students_merged.csv`, and
`config/headscale-acl.json`) into a `0700` directory outside the checkout
before pulling the cutover, then restore them. A pull that removes a tracked
file is not a configuration backup. The container runs as uid 1000, so
`templates.yaml` must be readable (`chmod 644`). Add the new `.env` keys
before pulling a compose file with new `${VAR:?}` requirements, run
`python3 scripts/preflight_env.py .env`, and keep the checkout directory name
(or set `COMPOSE_PROJECT_NAME`) so Compose reuses the database volume. The
full procedure is "Migrating an existing checkout to this repository" in
`docs/PRODUCTION_DEPLOYMENT.md`; it requires the Docker Compose v2 plugin.

### Application, proxy, and image environment

These values normally live in `.env`:

| Variable | Purpose / default |
|----------|-------------------|
| `TEMPLATES_FILE` | Deployment-group YAML path; defaults to `templates.yaml` relative to the app root |
| `SITE_NAME` | Displayed site title; defaults to `Lab Playground` |
| `SITE_BRAND_TAG` | Small label above the title; defaults to empty |
| `SITE_ORG` | Footer organization; defaults to empty |
| `VPN_LOGIN_SERVER` | Optional Tailscale/Headscale login-server URL, including its scheme (`https://vpn.example.edu`), shown in student instructions; when unset, the UI omits the `--login-server` flag |
| `SITE_DOMAIN` | Public hostname consumed by Caddy |
| `ACME_EMAIL` | ACME contact address consumed by Caddy |
| `ADMIN_PASSWORD` | Bootstrap admin password; production refuses one that is missing, a known default, or shorter than 16 characters when the account is about to be seeded (see `SEED_ADMIN_ACCOUNT`) |
| `SEED_ADMIN_ACCOUNT` | Whether `init_db` creates the generic `ADMIN_LOGIN_ID` account when it does not exist; defaults to `true`. Set `false` when staff use personal accounts listed in `ADMIN_USERS`; `ADMIN_PASSWORD` is then not required, in production or by `scripts/preflight_env.py`. It never deletes or changes an account that already exists. To retire an existing generic account, sign in as another admin and `PUT /admin/students/<id>` with `{"disabled": true}` (the same toggle as the Admin Dashboard's student list). Anything other than a recognised boolean fails startup |
| `PROXMOX_HOST` | Proxmox API hostname or IP |
| `PROXMOX_USER` / `PROXMOX_PASSWORD` | Password authentication (development) |
| `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` | API token authentication (production) |
| `PROXMOX_VERIFY_SSL` | Verify the Proxmox API TLS certificate; defaults to `true`. Set `false` only for a self-signed Proxmox reached over a private tunnel (startup logs a warning); prefer `PROXMOX_CA_BUNDLE` |
| `PROXMOX_CA_BUNDLE` | Path to a PEM CA certificate, inside the container, used to verify Proxmox; startup fails if the file is missing |
| `SECRET_KEY` | Flask session signing key. Production refuses to start when it is missing, empty, a value published in the repo's examples or docs, or shorter than 32 characters |
| `SELF_SERVICE_PASSWORD_RESET` | Student self-service password reset (roster SIS ID check, rate limited); defaults to `false`, which returns 404 and tells students to ask an administrator for a reset link. `true` lets anyone who knows a student's login and SIS ID reset that password; enable only if that risk is acceptable. Admin-issued reset links (see "Password recovery") work either way |
| `PASSWORD_RESET_TOKEN_TTL_MINUTES` | Lifetime of an admin-issued password reset link, in minutes; defaults to `60`. Values below `1` fall back to the default |
| `TRUSTED_PROXY_HOPS` | Number of reverse proxies whose `X-Forwarded-For` is trusted; `1` behind the bundled Caddy (set by `docker-compose.prod.yml`), `0` (default) when exposed directly |
| `SESSION_COOKIE_SECURE` | Secure flag on the session cookie; defaults to on in production, off otherwise. `docker-compose.prod.yml` deliberately does not pass it (forced on in production behind Caddy) |
| `SESSION_COOKIE_HTTPONLY` | HttpOnly flag; defaults to `true`. Not passed by `docker-compose.prod.yml`; forced on in production |
| `SESSION_COOKIE_SAMESITE` | `Lax` (default), `Strict` or `None`; an invalid value fails at startup |
| `PERMANENT_SESSION_LIFETIME` | Session lifetime in seconds; defaults to `7200` |
| `DATABASE_URL` | SQLAlchemy connection string; production refuses an empty or published database password (also checked for `MYSQL_PASSWORD`) |
| `ADMIN_LOGIN_ID` | Bootstrap admin login ID |
| `ADMIN_USERS` | Comma-separated admin login IDs |
| `ENABLE_LOAD_BALANCING` | Global load-balancing toggle |
| `FIXED_NODE` | Default node when load balancing is off |
| `CPU_THRESHOLD` / `MEMORY_THRESHOLD` | Global resource thresholds |
| `DISTRIBUTED_VM_LOCKS` | Serialize each VMID's stop/delete/retire across gunicorn workers with MySQL `GET_LOCK`; defaults to `false`, which uses an in-process lock only, enough for the single production worker. Set `true` before running more than one worker; each lock holder then uses a second database connection |
| `SMTP_*` / `ALERT_WEBHOOK_*` | Alert notification settings |
| `DOCKERHUB_USERNAME` | Docker Hub namespace |
| `IMAGE_NAME` | Image repository name; defaults to `lab-playground` |

CI reads `vars.DOCKERHUB_USERNAME` and `vars.IMAGE_NAME`, plus
`secrets.DOCKERHUB_TOKEN`. Publishing remains disabled unless
`vars.PUBLISH_DOCKERHUB == 'true'`.

### Workstation operations environment

The file is optional; operations scripts source it when present, while
exported environment variables take precedence. Scripts fail fast and name
any required value that is missing.

| Variable | Purpose |
|----------|---------|
| `PORTAL_HOST` | SSH destination for the portal host |
| `PORTAL_DIR` | Portal checkout directory |
| `PORTAL_COMPOSE` | Portal Compose filename |
| `PORTAL_APP_CONTAINER` | Portal application service/container |
| `HEADSCALE_HOST` | SSH destination for the Headscale host |
| `HEADSCALE_DIR` | Headscale deployment directory |
| `HEADSCALE_KEEP_USER` | Infrastructure user preserved at term changeover and never rotated |
| `HEADSCALE_STAFF` | Comma-separated staff users preserved at term changeover and never rotated |
| `EMAIL_DOMAIN` | Student email domain used while building the roster |
| `REMOTE_BACKUP_DIR` | Remote directory for changeover backups |
| `KEY_DAYS` | Lifetime of new student pre-auth keys (for example `150d`), used by changeover and key rotation |
| `HEADSCALE_URL` | Login-server URL the jumpbox joins |
| `JUMPBOX_NODE` | Proxmox node on which to create the jumpbox |
| `JUMPBOX_STORAGE` | Proxmox storage for the jumpbox |
| `JUMPBOX_VMID` | VMID of the jumpbox; outside the portal's VMID range |
| `JUMPBOX_SSH_PUBKEY` | Public-key file on the node, installed by cloud-init for `JUMPBOX_ADMIN_USER` (default `ubuntu`) |
| `JUMPBOX_*` (others) | Optional VM shape, NICs, VLAN bands, DHCP, Tailscale and monitoring settings, with defaults, listed in `scripts/site.env.example` and `docs/JUMPBOX.md` |
| `JUMPBOX_CREDENTIALS_FILE` | Output file (mode 0600, gitignored) for random per-user passwords generated by `jumpbox_accounts.sh create`; defaults to `./jumpbox-credentials.csv`. Users must change the password at first login |

### `templates.yaml` -- lab definitions and VM timing

All VM timing lives here (not in `.env`). Each deployment group defines:

```yaml
deployment_groups:
  - display_name: "Homework 2 - Active Recon"
    description: "Active reconnaissance lab."
    enabled: true
    uptime_minutes: 120        # initial VM lifetime
    extend_time_minutes: 60    # added on each extend
    load_balancing:
      enabled: true
      cpu_threshold: 80.0
      memory_threshold: 85.0
    templates:
      - name: "hw2-active-recon"
        type: "qemu"
        entry_point: true      # gets entry VLAN NIC; IP shown to student
        memory: 2048
        cores: 1
        template_locations:
          node1: 116           # VMID on node1
          node2: 117           # VMID on node2
```

The startup validator cross-checks `templates.yaml` against Proxmox to catch missing templates or misconfigured VMIDs before students deploy anything.

## Student Management

### Whitelist import (Canvas CSV)

```bash
python scripts/import_students.py data/students.csv
```

The CSV must include `SIS Login ID` and `Student` (full name) columns. The import script pre-assigns entry VLAN tags (100-200) and internal VLAN tags (200-300), plus optional Headscale auth keys.

### Registration flow

1. Admin imports student CSV into the whitelist table.
2. Student navigates to `/register` and enters their Canvas login ID, password, and optional email.
3. The system verifies the login ID exists in the whitelist before allowing registration.
4. On first login the student sees all enabled, unrestricted labs.

### Account lifecycle

Admins can disable a student account via `PUT /admin/students/<id>` with `{"disabled": true}`. Disabled students cannot log in or manage VMs until re-enabled.

### Rotating Headscale keys

At the end of a semester, or after a suspected leak, replace every student's
reusable Headscale pre-auth key without changing the roster:

```bash
scripts/rotate_headscale_keys.sh                          # dry run: prints the plan, changes nothing
scripts/rotate_headscale_keys.sh --apply --only <login>   # one account first
scripts/rotate_headscale_keys.sh --apply --yes            # everyone except HEADSCALE_STAFF/KEEP_USER
```

It creates the new keys, replaces `data/students_merged.csv` locally and on the
portal (mode `0640`, group `1000`: the app container's gid), updates both key
columns in the database, and only then expires the old keys, so a failure
part-way leaves every student with a working key. Registered devices stay
connected. `--apply` holds a lock on both hosts for the whole run, so a second
rotation refuses to start. A roster login missing from the portal database
keeps its old key and makes the run exit non-zero unless `--allow-partial` is
given. The portal container must include `scripts/update_headscale_keys.py`.
See `docs/TERM_CHANGEOVER.md`.

### Password recovery

The recommended path is an admin-issued reset link; it needs no mail server.
In the admin dashboard's Students tab, the key button on a student's row
creates a link and shows it once. Hand it to the student out of band (in
person, the course messaging system). The link:

- works once, and expires after `PASSWORD_RESET_TOKEN_TTL_MINUTES` (default 60);
- stops working when a newer link is created for the same student;
- signs the student out everywhere when used, like logout;
- carries its token in the URL fragment (`/reset-password#token=...`), which
  browsers never send to the server, so it stays out of gunicorn and Caddy
  access logs. The database stores only its SHA-256.

Failed link attempts count against the same per-address limit as
self-service resets. Run `scripts/migrate_add_password_reset_tokens.py` once
on an existing database before using the feature.

The older self-service reset (login ID plus roster SIS ID) stays available
behind `SELF_SERVICE_PASSWORD_RESET`; it is weaker because a SIS ID is not a
secret.

## Lab Assignment Restrictions

By default every enabled deployment group is visible to all students. When an admin assigns even one student to a group, that group becomes restricted -- only assigned students can see and deploy it. Unassigned groups remain visible to everyone.

This means admins can progressively lock down labs as the semester progresses:

1. **No assignments for a group** -- all students see it.
2. **At least one assignment exists** -- only assigned students see it.
3. **Bulk assign** -- use `POST /admin/assignments/bulk` with lists of user IDs and group names.
4. **Clear restrictions** -- use `POST /admin/assignments/clear` to remove all assignments for a group, making it visible to all again.

The filtering happens in `GET /templates` at query time; the database stores only (user_id, deployment_group_name) pairs.

## Admin Dashboard

The admin dashboard (visible when logged in as an admin) provides:

- **Student Management** -- list all users, view active VM counts, toggle admin role, disable/enable accounts, create one-time password reset links
- **VM Overview** -- list all VMs across all students with filters by student, group, node, and status
- **Bulk Destroy** -- select multiple VMs and destroy them in one operation
- **Bulk Extend** -- select multiple VMs and add time to their expiration
- **Lab Assignments** -- assign or unassign students to deployment groups, bulk assign, clear all assignments for a group
- **Alert Testing** -- send test alerts through email, webhook, database, and file channels to verify configuration

## Cross-Node Template Sync

Each Proxmox node must have a local copy of every template it will clone from. The `sync_template.sh` script automates replication:

```bash
./scripts/sync_template.sh <source_node> <source_vmid> <target_node> [target_storage]

# Example: copy template 116 from node1 to node2
./scripts/sync_template.sh node1 116 node2 local-zfs
```

The script performs a vzdump on the source node, transfers the backup via SCP, restores it on the target with the next available VMID, and converts it to a template. Update `templates.yaml` with the new VMID after syncing.

Prerequisites: SSH key-based access to both nodes, vzdump/qmrestore installed, sufficient disk space in `/var/lib/vz/dump`.

## Testing

### Unit tests (45 tests)

```bash
python -m pytest tests/ -v --ignore=tests/integration
```

Unit tests cover models, config validation, template loading, auth routes, API routes, admin dashboard, lab assignments, background tasks, and parallel VM creation. They use an in-memory SQLite database and mock the Proxmox API.

### Integration tests (16 tests)

```bash
# Start the test environment
docker compose -f docker-compose.test.yml up -d --build

# Wait for health check
curl -s http://localhost:5099/auth/health

# Run Playwright browser tests
python -m pytest tests/integration/test_flows.py --base-url http://localhost:5099 -v

# Tear down
docker compose -f docker-compose.test.yml down
```

Integration tests use Playwright to exercise real browser flows (login, registration, VM creation, admin operations) against a containerized instance with a SQLite database and mocked Proxmox host.

### CI/CD pipeline

GitHub Actions (`.github/workflows/ci.yml`) runs on every push to `main` or `features/*` and on pull requests to `main`:

1. **Unit Tests job** -- installs dependencies, runs the pure-Python test subset.
2. **Integration Tests job** -- builds the Docker test environment, installs Playwright + Chromium, runs browser tests, then tears down.
3. **Docker Push job** -- (main branch only) builds the production image and pushes to Docker Hub with `latest` and SHA tags. Requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets.

## API Reference

### Authentication (`/auth` prefix)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/register` | Register a new student account (whitelist required) |
| POST | `/auth/login` | Log in with credentials |
| POST | `/auth/logout` | Log out current session |
| POST | `/auth/reset-password` | Self-service reset with login ID and SIS ID (only with `SELF_SERVICE_PASSWORD_RESET=true`) |
| POST | `/auth/reset-password/link` | Set a new password with an admin-issued link (`{"token", "password"}`) |
| GET | `/auth/me` | Get current user info |
| GET | `/auth/check` | Check authentication status |
| GET | `/auth/health` | Health check for monitoring |

### Pages

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Main dashboard (redirects to login if unauthenticated) |
| GET | `/register` | Registration page |
| GET | `/reset-password` | Password reset page |

### VM Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/templates` | List enabled deployment groups (filtered by assignment restrictions) |
| POST | `/create` | Create VMs for a deployment group |
| GET | `/list` | List VMs grouped by student and deployment group |
| POST | `/extend` | Extend one deployment's lifetime (`deployment_scope=individual\|group` and `group_id` pick between same-named deployments; an ambiguous request is refused) |
| POST | `/destroy` | Destroy one deployment (same `deployment_scope` / `group_id` selection as `/extend`) |
| GET | `/time-remaining` | Check VM expiration time |
| GET | `/api/vms` | List VMs (flat, filterable by active status) |
| GET | `/api/vms/<vmid>` | Get detailed info for a single VM |
| GET | `/nodes/resources` | Cluster node resource usage (admin only). Memory fields: `memory_available_gb` (Linux MemAvailable; `total - used` if the node omits it), `memory_linux_free_gb` (Linux MemFree), and `memory_free_gb`, a **deprecated** alias that equals `memory_available_gb`, not MemFree. See [docs/LOAD_BALANCING.md](docs/LOAD_BALANCING.md#monitoring-node-resources) |

### Admin (all require admin role)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/admin/students` | List students (id, login_id, name, admin flag) |
| GET | `/admin/students/full` | List students with VM counts and full details |
| PUT | `/admin/students/<id>` | Update student (toggle admin, disable/enable) |
| POST | `/admin/students/<id>/reset-link` | Create a one-time password reset link (JSON body required; returned once) |
| GET | `/admin/assignments` | List lab assignments (filter by user_id or group) |
| POST | `/admin/assignments` | Assign a student to a deployment group |
| DELETE | `/admin/assignments/<id>` | Remove a specific assignment |
| POST | `/admin/assignments/bulk` | Bulk assign students to groups |
| POST | `/admin/assignments/clear` | Clear all assignments for a group |
| GET | `/admin/vms` | List all VMs with optional filters |
| POST | `/admin/bulk-destroy` | Destroy multiple VMs by record ID |
| POST | `/admin/bulk-extend` | Extend expiration for multiple VMs |
| POST | `/admin/test-alerts` | Send test alert notifications |

---

**Last Updated:** October 2026

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
