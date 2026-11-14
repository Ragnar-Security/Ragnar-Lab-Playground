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

### Added

- Lab VMs on Amazon EC2 and Google Compute Engine beside Proxmox.
  `ENABLED_BACKENDS` selects the backends; a deployment group's `backend:`
  (or a machine's) selects where its labs go, with the machine spec in an
  `aws:` or `gcp:` block. Every cloud resource carries the `LAB_SITE_ID`
  ownership tag, launches without a public address into a private slot
  subnet with no direct TCP/UDP route to public addresses (the provider's
  DNS resolver stays reachable until blocked separately), and is capped by
  `AWS_MAX_VMS` / `GCP_MAX_VMS`. Per-deployment security groups and firewall
  rules isolate deployments; one subnet router per cloud is started on
  demand and stopped when idle. Operator runbook: `docs/CLOUD_BACKENDS.md`.
- The orphan reaper sweeps each enabled cloud's site inventory as well as
  Proxmox, deleting only on positive evidence that nothing owns a resource.
- A per-student Headscale policy (`scripts/apply_headscale_policy.sh`, dry run
  by default): each roster login reaches only its own and its groups' lab
  networks, and the non-ended KOTH arenas, on every backend.
- `jumpbox_configure.sh --isolate-vlans [--allow-vlans LIST]` drops traffic
  routed between lab VLANs on the jumpbox (`docs/JUMPBOX.md`).
- Opt-in live contract runs against real AWS and GCP accounts
  (`LIVE_AWS=1`, `LIVE_GCP=1`; `tests/live/README.md`).

### Changed

- `vm_records` carries each VM's backend identity (`backend`,
  `provider_id`, `deployment_id`, `network_slot`); every lifecycle path
  dispatches through the record's backend.
- Proxmox settings are required only while `proxmox` is enabled, so a
  cloud-only portal can start.
- `docker-compose.prod.yml` passes the backend settings into the app
  container; an empty value behaves as unset.

### Fixed

- `jumpbox_configure.sh` no longer dies at its first diff with
  `unrecognized arguments` on Ubuntu 24.04's Python 3.12.3, the jumpbox's
  `python3`: `jumpbox_netgen.py` accepts options between its command and
  its files.
- `term_changeover.sh` keeps exported `HEADSCALE_BASE_POLICY` and
  `HEADSCALE_POLICY_FILE` values over `scripts/site.env`, as
  `apply_headscale_policy.sh` already did.
- A destroy, expiry or failed-deploy cleanup in the first ~10 s after a
  Proxmox clone is no longer refused with "guest identity could not be
  verified". The cluster listing reports a fresh guest's name empty (or the
  previous guest's, at a reused VMID) until pvestatd's next broadcast; when
  only the name disagrees, the guest's own config on its node now decides.
  A config naming another guest still refuses.
- Every Proxmox destroy (student, expiry, admin, failed-deploy cleanup) and
  the orphan reaper re-read the guest's identity immediately before the
  delete, after the stop (which may wait up to a minute), and the reaper also
  before the stop. A guest that took the VMID in that window is refused and
  kept; a guest already gone counts as destroyed. v1.0.0's destroy paths
  checked once, before the stop; its reaper trusted the sweep's listing.
- The idle cloud-router stop logs "Stopped the … router" only when it
  issued the stop, not when the router was already stopped or stopping.

### Upgrade notes

- **Back up the database first** (`scripts/backup-database.sh`) and record the
  running commit or image as the rollback target.
- **Run the new migration, with the earlier ones, before the new code
  starts.** `migrate_add_vm_backend.py` adds the four `vm_records` columns and
  an index; existing rows become `backend='proxmox'`. All are idempotent.

  ```bash
  for migration in migrate_koth_claim_mode.py migrate_add_session_version.py \
                   migrate_vm_deletion_retry.py migrate_add_password_reset_tokens.py \
                   migrate_add_coordination_tables.py migrate_add_alert_outbox.py \
                   migrate_add_vm_backend.py; do
    docker compose -f docker-compose.prod.yml exec -T -w /app vm-automation \
      python scripts/$migration
  done
  ```

- **Proxmox-only sites change nothing else in normal operation.** Two
  behaviours differ while the Proxmox API is unreachable: `/extend` saves the
  new expiry in the database and returns `success: true` with
  `extended_count: 0`, where v1.0.0 refused the whole extend (the guest's
  own `Expires:` line keeps the old time until an extend reaches Proxmox;
  the orphan reaper does not act on it while the record exists); and
  `/list` shows each VM as "state unknown, listing unverified", where
  v1.0.0 failed the request. `ENABLED_BACKENDS` unset or
  `proxmox` keeps today's behaviour. The new variables are `ENABLED_BACKENDS`
  and `LAB_SITE_ID`; `JUMPBOX_VLAN_BANDS` (default the jumpbox's bands); and,
  only when that cloud is enabled, `AWS_REGION`, `AWS_AVAILABILITY_ZONE`,
  `AWS_LAB_VPC_ID`, `AWS_LAB_CIDR`, `AWS_ROUTER_INSTANCE_ID`, `AWS_MAX_VMS`,
  `AWS_ROUTER_IDLE_MINUTES`, `AWS_LAUNCH_TIMEOUT_SECONDS`,
  `AWS_TERMINATE_TIMEOUT_SECONDS`, `GCP_PROJECT`, `GCP_REGION`, `GCP_ZONE`,
  `GCP_NETWORK`, `GCP_LAB_CIDR`, `GCP_ROUTER_INSTANCE`, `GCP_MAX_VMS`,
  `GCP_ROUTER_IDLE_MINUTES` and `GCP_OPERATION_TIMEOUT_SECONDS`
  (`.env.example`). A Proxmox node named `aws` or `gcp` is no longer
  supported: startup refuses such a `FIXED_NODE`, and admission never
  places a VM on a node of that name.
- **Enabling a cloud** follows `docs/CLOUD_BACKENDS.md`: operator-created
  VPC, router and IAM, a vCPU quota increase before the first class,
  complete lab images (labs have no direct TCP/UDP route to public
  addresses; the provider's DNS stays reachable until blocked separately,
  which the runbook requires before students use the cloud), and
  credentials through an instance role or an untracked compose override,
  never a tracked file.
- **Instructor steps after the upgrade, not part of it:** the Headscale
  policy (`scripts/apply_headscale_policy.sh --apply`) and jumpbox
  `--isolate-vlans`. The policy cuts the blanket `autogroup:member` rule down
  to its `autogroup:self` part, which also changes what students reach on
  Proxmox; move any other access it gave that must stay into explicit rules
  in `config/headscale-acl.json` first. Any other base rule whose `src`
  includes `autogroup:member` or `*` must grant only `autogroup:self`, or
  the render refuses to run.
- **Still one gunicorn worker.**
- **Rollback** to v1.0.0 needs no database restore (it ignores the new
  columns) **only while no cloud records and no cloud failed-deploy survivor
  lines exist**: v1.0.0 would treat a cloud record as a Proxmox guest and
  never destroy its machine, and would set a cloud line in
  `data/untracked-survivors.jsonl` / `.importing` aside as corrupt. Before
  rolling back, run both checks in the rollback step of
  `docs/PRODUCTION_DEPLOYMENT.md`; tear the clouds down
  (`docs/CLOUD_BACKENDS.md`, "Teardown and incident response") and drain the
  survivor files on the new release until both are clean.

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
