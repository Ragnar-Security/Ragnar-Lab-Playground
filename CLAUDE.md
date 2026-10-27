# Claude / contributor notes for this repo

A Flask + SQLAlchemy + MySQL portal that orchestrates Proxmox VM lifecycles.
Students log in from an imported allowlist, pick a template, get VMs cloned +
tracked + auto-expired. Group-mode and the King-of-the-Hill (KOTH)
competitive arena layer on top.

This file is loaded into context at the start of every Claude Code session
in this directory. Keep it short, opinionated, and focused on things you
need to know before touching the code.

## Workflow — change → test → codex review → commit

For any **non-trivial change** (anything touching business logic, security
boundaries, the DB schema, or the production deployment), follow this loop
in order. Don't skip steps and don't reorder them.

1. **Make the changes.** Sequential or via subagent-driven implementation as
   the scope warrants.
2. **Run the tests.** `uv run python -m pytest tests/ --ignore=tests/integration`
   or the targeted subset for what you touched. Must be green before moving
   on. The full suite is ~325 tests and runs in ~4 minutes.
3. **Codex review.** Invoke the `codex:codex-rescue` subagent (via the
   `/codex:rescue` slash command, or Agent tool with `subagent_type:
   codex:codex-rescue`). Give it: a focused changeset summary, the key code
   paths to look at, and ask for findings ranked Critical / Important /
   Minor with file:line refs. Then **fix every Critical and Important
   finding** before moving on. Re-run the tests after the fixes.
4. **Commit.** Single descriptive commit (unless the user explicitly asks
   for per-task commits). Co-author line stays.

**Why this matters.** A previous session shipped 6 important issues that
317/317 unit tests had passed cleanly through (race in claim-event
ordering, unbounded base64 decode, missing PATCH bounds, NaN/Inf points,
YAML-bomb gap, race-window in flag dedup). Codex caught them all on first
review. Tests verify code correctness; codex review catches design and
threat-model gaps the tests can't see.

**Trivial exceptions.** Single-line typo fix, docstring tweak, README
edit — fine to skip the codex step. Anything else, run it.

## Test, build, and deploy commands

| Action | Command |
|--------|---------|
| Unit tests (full) | `uv run python -m pytest tests/ --ignore=tests/integration -q` |
| Unit tests (focused) | `uv run python -m pytest tests/test_<x>.py -q` |
| Local dev stack | `docker compose up -d` (uses `docker-compose.yml`) |
| Local prod-parity stack | `docker compose -f docker-compose.prod.yml up -d --build` |
| Apply a migration | `docker compose -f docker-compose.prod.yml exec vm-automation python scripts/migrate_<name>.py` (all idempotent) |
| Tail prod logs | `docker compose -f docker-compose.prod.yml logs -f vm-automation` |

Do **not** run `pytest` directly — always go through `uv run` so the
managed venv is used.

## Production architecture

The production stack is defined by `docker-compose.prod.yml`; each operator
supplies the deployment's hostnames, credentials, and infrastructure values:

- **caddy** (`caddy:2-alpine`) — reverse proxy, TLS via Let's Encrypt,
  serving the hostname from `SITE_DOMAIN`. **HTTP/3 is intentionally
  disabled** (`servers { protocols h1 h2 }` in `Caddyfile`) — H3
  cancellations generated 502s under classroom load. Re-enable only after
  validating QUIC stability at scale.
- **vm-automation** (built from `Dockerfile.prod`) — Flask app under
  gunicorn. **Single worker, gevent class.** This is by design:
  APScheduler (scoring tick) and the SSE event bus (KOTH live
  scoreboard) are in-process; a second worker would mean a second
  scheduler racing the first and SSE events fanning out to the wrong
  subscriber set. Concurrency comes from greenlets, not threads.
  Configured in `gunicorn.conf.py`.
- **db** (`mysql:8.0`) — `--max-connections=300` so the SQLAlchemy pool
  (50 + 50 overflow) plus migrations + backups don't hit the cap.
- **ulimits**: `nofile: 65536` on `vm-automation` (Docker default of 1024
  was the EMFILE crash root cause).

### Things that will bite you if you change them

1. **Don't bump `workers` above 1 in `gunicorn.conf.py`.** It will silently
   break SSE delivery and double-tick the scheduler. If you genuinely need
   multi-worker scaling, you need Redis pub/sub for the event bus and an
   out-of-process scheduler first.
2. **Don't switch `worker_class` back to `gthread` or `sync`.** SSE clients
   become OS threads instead of greenlets and the FD/memory budget falls
   apart at ~30 concurrent tabs.
3. **`run_prod.py` calls `gevent.monkey.patch_all()` at the very top** —
   before `import threading`, before `import sys` is even fully resolved.
   Don't move imports above it.
4. **Gunicorn `worker_int` / `worker_abort` / `worker_exit` hooks** all
   call `koth_scorer.stop_scheduler()`. This is the only thing standing
   between a graceful restart and minutes of `cannot schedule new futures
   after shutdown` log spam. If you add a hook, keep this call in it.
5. **Caddy uses `dynamic a` upstream resolution + active health checks.**
   When you redeploy `vm-automation`, the new container gets a fresh
   docker-network IP — Caddy re-resolves within ~10 s and probes
   `/auth/health` every 5 s. Don't switch to a static `vm-automation:5000`
   upstream or you'll go back to "no upstreams available" 503s on every
   redeploy.
6. **APScheduler is in-process.** `app/koth_scorer.py:start_scheduler` is
   guarded by a process-wide flag. Don't try to call it from outside the
   primary worker — `run_prod.py` uses `fcntl.flock` on
   `/tmp/vm_automation_primary_worker.lock` to ensure only one worker
   spins it up.
7. **A purge of the roster tables does not change the roster.**
   `scripts/init_db.py` re-seeds `student_whitelist` from
   `data/students_merged.csv` on EVERY container start, not only the first.
   `import_student_whitelist()` re-imports whenever the whitelist holds no
   real students. A purge therefore undoes itself on the next restart. This
   has happened: the previous term's students came straight back, and the
   SQL had reported success. **Replace the CSV first, then
   purge, then restart.** `scripts/term_changeover.sh` enforces that order.
8. **`data/students_merged.csv` holds live VPN credentials and is never
   committed.** It carries one reusable headscale pre-auth key per student
   plus SIS IDs and emails, which are FERPA-protected education records.
   Production reads it from the untracked `data/` directory of its checkout;
   `scripts/term_changeover.sh` delivers it over SSH. `.gitignore` excludes
   it, and every other CSV in `data/`. Never `git add -f` a roster, a Canvas
   export, or a key file: student data and credentials never go in git.
9. **The test student is gated behind `SEED_TEST_STUDENT=1`.** Only
   `docker-compose.test.yml` sets it, because the integration suite signs in
   as `teststudent` with a known password. Production must never seed that
   account. `create_test_student()` refuses to run without the variable.
10. **Admission, VMID allocation, VM lifecycle locks and the password-reset
    limiter are shared through the database; the scheduler and SSE bus are
    not.** `app/coordination.py` keeps node-memory reservations, VMID
    reservations plus the VMID high-water mark, and reset-failure stamps in
    tables. Each check-and-reserve runs in one transaction that locks a
    `coordination_state` row (`SELECT ... FOR UPDATE`). `vm_lifecycle_lock`
    holds MySQL `GET_LOCK('vm-lifecycle-<vmid>')` on a dedicated connection;
    on SQLite it is a process lock only, and during a database outage it
    falls back to the process lock so cleanup can still remove guests.
    These no longer limit the worker count. APScheduler and the SSE event
    bus still do (rules 1 and 6), so `workers` stays 1. Leases use
    `time.time()` epoch seconds, not local DateTimes.
    Node memory is `memory.available` (MemAvailable), never
    `memory.free`: ZFS ARC and page cache can leave MemFree low even when
    reclaimable memory is available. See `docs/LOAD_BALANCING.md`.
11. **Logout revokes through `User.session_version`.** Sessions are signed
    cookies, so clearing one never revoked its copies. Authorize through
    `get_authenticated_user()` / `login_required`; never trust
    `session['user_id']` directly.
12. **The orphan reaper destroys Proxmox guests.** `app/orphan_reaper.py`
    removes expired `student-`/`group-` guests that have no VMRecord. Keep
    the app naming and the `Expires:` line in clone descriptions stable, or
    it stops recognising (safe) or misjudges (unsafe) guests.
    `ORPHAN_REAPER_DRY_RUN=true` logs decisions only.
13. **Site-specific identities and infrastructure never belong in tracked
    files.** Real hosts, domains, usernames, node/storage names, VMIDs, and
    credentials live only in operator-owned `.env`, `templates.yaml`,
    `scripts/site.env`, `config/headscale-acl.json`, and
    `data/students_merged.csv`. Commit only their neutral example files.

### Changing the roster between semesters

Use one command. The dry run is the default and writes nothing:

```bash
scripts/term_changeover.sh <canvas-export>.csv            # report only
scripts/term_changeover.sh --apply <canvas-export>.csv    # back up, then change
```

It covers all three stages: the headscale users and keys, the mounted roster
file, and the production database. A second run of a finished changeover
reports "nothing to do" at every stage. `docs/TERM_CHANGEOVER.md` carries the
procedure, the rollback and the four failure modes behind these rules.

Rotating keys without a roster change is `scripts/rotate_headscale_keys.sh`
(dry run by default). It must update the database as well as the CSV, and
expires old keys only after the database update succeeds.


### Capacity planning

Do not copy sizing claims from an old deployment. Measure the operator's
target host under the expected class load.

The KOTH client rework substantially reduced browser-driven load. Every open
tab previously answered each `scan` event by re-fetching `/koth/api/state`.
The `scan` event now carries complete state and the client renders from it, so
a classroom costs roughly one bootstrap request per tab plus one serialized
frame per tick.

`/koth/api/_diag` (admin-only) exists — subscriber count,
dropped frames, tick duration + overrun count, boxes in guest-agent
backoff, DB pool checkouts. Read it *during* a game.

**Worker recycling is off** (`max_requests = 0`). This process owns the
APScheduler tick and the in-memory SSE bus, so a recycle drops every open
scoreboard and restarts scoring mid-game; at the old 5 req/s that fired
every ~17 minutes of play. If memory grows, fix the growth — do not
re-enable recycling.

## Repo layout (orienting)

```
app/                        # Flask app (blueprints, models, helpers)
  routes/                   # one module per blueprint
    koth.py / koth_admin.py / koth_player.py
  koth_scorer.py            # APScheduler tick: claim capture, probe, score
  koth_events.py            # SSE pub/sub — one serialization per publish
  koth_scoring.py           # pure scoring math
  koth_scanner.py           # TCP service probe + claim-beacon read
  concurrency.py            # bounded fan-out (gevent pool or threads)
  template_manager.py       # templates.yaml accessors
  models.py                 # SQLAlchemy models
  config.py                 # env-var-driven config + KOTH defaults
  __init__.py               # create_app() factory + boot recovery
docs/                       # user-facing docs
  KING_OF_THE_HILL.md       # KOTH user/admin guide
  PRODUCTION_DEPLOYMENT.md  # deploy guide (read this before changing prod stack)
scripts/                    # idempotent migration scripts (migrate_*.py)
static/                     # JS + CSS
templates/                  # Jinja templates
templates.example.yaml       # Example VM template + deployment-group catalog
gunicorn.conf.py            # production worker config — do not bump workers
run_prod.py                 # gunicorn entry point — monkey.patch_all() first line
docker-compose.prod.yml     # production stack
Caddyfile                   # reverse proxy config
tests/                      # unit tests; tests/integration runs against real Proxmox
```

## Conventions

- **Models**: `created_at` / `updated_at` defaults via `datetime.now()`
  (NOT `utcnow()`). Mixing the two created cooldown bugs in non-UTC tz —
  and again when `koth_games.started_at` was written with
  `utcnow()` while the scoring tick compared `ends_at` against `now()`,
  so time-boxed games auto-ended a whole tz offset late and left every VM
  running. Columns are naive LOCAL, everywhere, no exceptions.
  When emitting a timestamp to the browser, serialize it through
  `models._iso_local()` so it carries the host's real UTC offset.
  **Do not append a bare `Z`** — that was the old rule, it asserts UTC
  about a value that is local, and it masked the bug above by making the
  countdown look right while the server-side gate was wrong.
  The coordination tables' `expires_at` / `failed_at` are not DateTimes:
  they are `time.time()` epoch seconds in DOUBLE columns, compared across
  workers and never shown to anyone.
- **Migrations** must be idempotent (check `information_schema` before
  altering). Filename pattern: `scripts/migrate_<topic>.py`.
- **`templates.yaml`** is operator-owned, untracked, and bind-mounted
  read-only into prod. Editing it on the host + restarting the container
  picks up changes — no rebuild. `TEMPLATES_FILE` selects an alternate path.
- **Tests**: prefer `app.test_client()` (no real sockets). When mocking
  Proxmox API calls, mock at `proxmoxer.ProxmoxAPI` boundary.
- **Comments**: write WHY, not WHAT. Past-incident references are valuable
  ("we tripped this once; here's what to avoid"). Don't add
  end-of-file change-log style comments.

## Where to find more

- `docs/KING_OF_THE_HILL.md` — KOTH user-facing guide
- `docs/PRODUCTION_DEPLOYMENT.md` — full prod deploy + rollback playbook
- **Never delete a `VMRecord` because a Proxmox call failed.** The expiry
  daemon tracks VMs by database row, so a deleted row means the VM runs
  until a human notices. `_verify_vm_exists` returns
  `present | missing | config_error | unknown`, and only `missing` —
  confirmed by the cluster inventory not listing the vmid, from a ≤1s-old
  snapshot taken with `allow_stale=False` — may trigger cleanup. A deleted
  VM and a corrupted config give the *same* HTTP 500 "does not exist", so
  the config endpoint alone can never answer this question.
