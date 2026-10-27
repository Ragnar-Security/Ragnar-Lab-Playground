# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every release section has an `### Upgrade notes` subsection. It lists what an
operator must do when upgrading a running deployment: migrations, new or
changed environment variables, breaking changes, and anything to check
afterwards. The release workflow refuses to publish a version whose section
lacks it (`scripts/release_notes.py`), and the GitHub Release shows it.

## [Unreleased]

## [1.0.0] - 2026-10-27

The first public release. Earlier builds were never versioned; their
`/auth/health` did not report a version.

### Added

- Apache-2.0 license, `NOTICE`, `SECURITY.md`, and a secret-scanning
  configuration. Site-specific values (domains, hosts, staff, the lab
  catalog, branding) live only in untracked operator files, created from the
  committed examples (`.env.example`, `templates.example.yaml`,
  `scripts/site.env.example`, `config/headscale-acl.example.json`).
- Admin-issued, single-use password reset links, with one live link per
  student. `SEED_ADMIN_ACCOUNT` makes the generic admin account optional.
- Database-backed coordination tables for node-memory and VMID reservations,
  the VMID high-water mark and the password-reset limiter
  (`app/coordination.py`).
- An `alert_outbox` table: deletion-failure and orphan-reaped alerts are
  queued in the same commit as the state they report and delivered by the
  expiry loop, with retries and backoff.
- Failed-deploy guests that cannot be recorded during a database outage are
  written to a fallback file under `data/` and imported once the database is
  back, so the expiry loop still removes them.
- The expiry loop retires the record of a guest deleted by hand in Proxmox
  once fresh inventory has confirmed it missing.
- Jumpbox tooling that rebuilds the lab router: `jumpbox_provision.sh`,
  `jumpbox_configure.sh` (VLAN gateways, DHCP, optional Tailscale, SSH-key-only
  login, monitoring, and an optional staff-only SSH filter) and
  `jumpbox_accounts.sh`; see `docs/JUMPBOX.md`.
- Headscale pre-auth key rotation (`scripts/rotate_headscale_keys.sh`) and the
  term changeover procedure (`scripts/term_changeover.sh`).
- `/auth/health` reports `version` and, for release images, `revision`.
- Versioned release images on GHCR with build provenance and SBOM
  attestations, and a deploy bundle on each GitHub Release
  (`docs/RELEASING.md`).

### Changed

- Images install exactly the packages pinned in `uv.lock`, with hash
  checking; gunicorn is 23.0.0.
- The orphan reaper runs after the expiry pass and stops starting guests past
  a per-sweep time and guest budget, so it cannot hold up expiry. Alert
  delivery has its own per-pass budget.
- `/extend` and `/destroy` act on one deployment, chosen by
  `deployment_scope`, instead of every deployment sharing a name.

### Fixed

- An extension that committed while the expiry worker was processing the
  same VM could be destroyed anyway.
- The orphan reaper reported failures for guests that had already gone,
  because Proxmox answers a missing guest with HTTP 500, not 404.

### Security

- Logout revokes every copy of a session (`users.session_version`).
- Production refuses an admin password shorter than 16 characters while the
  admin account is seeded.
- MySQL is published on the loopback interface only.

### Upgrade notes

For operators upgrading a deployment that runs a pre-release build. A host
that runs a checkout of the older repository this one replaced follows
"Migrating an existing checkout to this repository" in
`docs/PRODUCTION_DEPLOYMENT.md` first.

- **Back up the database first** (`scripts/backup-database.sh`) and record the
  running commit or image as the rollback target.
- **Run all six migrations before the new code starts.** Each is idempotent
  and only adds columns, an index or tables, so the old code keeps working
  while they run. Skipping one is not safe: `db.create_all()` never adds
  columns to an existing table, and the new code reads them on every request.

  ```bash
  for migration in migrate_koth_claim_mode.py migrate_add_session_version.py \
                   migrate_vm_deletion_retry.py migrate_add_password_reset_tokens.py \
                   migrate_add_coordination_tables.py migrate_add_alert_outbox.py; do
    docker compose -f docker-compose.prod.yml exec -T -w /app vm-automation \
      python scripts/$migration
  done
  ```

  If the running image does not contain a migration yet, copy it in for the
  run; "Redeploy an Existing Production Host" in
  `docs/PRODUCTION_DEPLOYMENT.md` shows how.
- **New optional environment variables.** All have defaults; set them in
  `.env` only to change the default.
  - `ORPHAN_REAPER_MAX_PASS_SECONDS` (default 120) and
    `ORPHAN_REAPER_MAX_PASS_GUESTS` (default 10): per-sweep budget of the
    orphan reaper.
  - `DISTRIBUTED_VM_LOCKS` (default false): VM lifecycle locks through MySQL
    `GET_LOCK`. Keep it false while running one gunicorn worker.
  - `UNTRACKED_SURVIVORS_FILE` (default `data/untracked-survivors.jsonl`):
    where failed-deploy guests are recorded during a database outage. It must
    stay on the persistent `data/` mount.
  - `ALERT_DISPATCH_MAX_PASS_SECONDS` (default 20) and
    `ALERT_DISPATCH_MAX_PASS_ALERTS` (default 50): per-pass budget of alert
    delivery.
- **Breaking:** `POST /destroy` and `POST /extend` require
  `deployment_scope=individual` or `deployment_scope=group`; a request
  without it is refused. The bundled pages send it. A student whose browser
  still has the old JavaScript gets an error from the Extend and Destroy
  buttons until a hard refresh (Ctrl+Shift+R, or Cmd+Shift+R on macOS).
  Scripts that call these endpoints must add the field.
- **Still one gunicorn worker.** Do not raise `workers` in `gunicorn.conf.py`:
  the scheduler, the live scoreboard event bus, the reaper schedule and alert
  delivery are still per-process.
- **Rollback:** the old code ignores the added columns and tables, so going
  back to the previous build needs no database restore. The old code does not
  deliver alerts still pending in `alert_outbox`, and does not read the
  failed-deploy survivor files; see the rollback step in
  `docs/PRODUCTION_DEPLOYMENT.md`.

[Unreleased]: https://github.com/Ragnar-Security/Ragnar-Lab-Playground/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Ragnar-Security/Ragnar-Lab-Playground/releases/tag/v1.0.0
