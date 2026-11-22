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

- `scripts/sync_template.sh` takes an optional fifth argument, the target
  VMID, restricted to 8500-8999 so a replica can never land at VMID 9000 or
  above.
- VPN-agnostic access. `VPN_MODE` selects how students reach labs:
  `headscale` (the default, unchanged), `external` (a VPN the portal does
  not run, such as AWS Client VPN or a site-to-site link: the dashboard
  shows `VPN_NAME` with `VPN_INSTRUCTIONS_URL` and/or the plain-text
  `VPN_INSTRUCTIONS`, and never a Headscale key) or `none` (students are
  already on an internal network routed to the labs; no VPN instructions).
  Labs stay private in every mode. `docs/CLOUD_BACKENDS.md` has "Using
  another VPN" and "No VPN (internal network)".
- `AWS_CLIENT_CIDRS`: the networks each AWS deployment's security group
  admits, instead of the hard-coded tailnet. Unset is `100.64.0.0/10`, as
  before; it is required outside `VPN_MODE=headscale`. Startup and the
  preflight refuse `0.0.0.0/0`, anything broader than a `/8`, overlapping
  entries, more than 8, and an overlap with `AWS_LAB_CIDR`. It is read at
  startup: changing it on a live site means stopping and recreating every
  portal process (`docs/CLOUD_BACKENDS.md`, "Changing `AWS_CLIENT_CIDRS` on
  a live site").
- `AWS_ROUTER_INSTANCE_ID` and `GCP_ROUTER_INSTANCE` are optional. Empty
  means router-less: deploys never start or wait for a router, and the
  idle-stop job skips that backend.
- `scripts/preflight_env.py --summary` prints the active VPN mode and, per
  enabled backend, its router and the client networks its labs admit.
  Without the flag its output is unchanged.
- `LAB_BRIDGE` (default `vmbr0`) and `VMID_RANGE` (unset: unchanged
  allocation), which let a staging portal run on an isolated bridge in its own
  VMID range.
- `docs/STAGING.md`, the permanent staging stack and its two-client acceptance
  test.
- `scripts/audit_vlan_pairs.py`, which checks the exported VLAN pairs.
- `scripts/apply_headscale_policy.sh --apply` checks, after the reload, that
  the jumpbox lists every roster device as a peer (`tailscale status --json`
  over SSH to the new `JUMPBOX_SSH`; `JUMPBOX_PEER_WAIT`, default 60 seconds).
  If one is missing it restarts Headscale once, checks again, and fails closed,
  naming the devices. Without the `proxmox` backend there is no jumpbox: the
  check is skipped with a message and `JUMPBOX_SSH` is not needed. An export
  whose `enabled_backends` is empty is refused.
- `scripts/apply_headscale_policy.sh --preflight` checks, without changing
  anything or contacting Headscale, what `--apply` will need: on a Proxmox
  site, that `JUMPBOX_SSH` is set, answers over SSH and reports a Running
  Tailscale. `scripts/term_changeover.sh --apply` runs it before stage 1 and
  refuses on failure, so a missing or stale `JUMPBOX_SSH` can no longer stop
  the changeover after the roster and database have been replaced.
- `scripts/headscale_peer_watchdog.sh`, run from a systemd timer every 5
  minutes (`docs/JUMPBOX.md`, "Headscale peer watchdog"): the same peer check,
  with no policy write. When the jumpbox is Running but roster devices are
  missing, it restarts Headscale once, at most once per
  `HEADSCALE_WATCHDOG_MIN_RESTART_INTERVAL` (default 1800 seconds) unless a
  healthy run came in between, and fails loudly when that does not help or the
  jumpbox's Tailscale is down.
- `jumpbox_configure.sh` writes a dnsmasq drop-in,
  `/etc/systemd/system/dnsmasq.service.d/60-jumpbox-limits.conf`
  (`LimitNOFILE=65536`): with all 401 lab VLANs dnsmasq holds about 1,600
  sockets and would not start under systemd's default limit of 1,024.
- Every `jumpbox_configure.sh --apply` installs the lab egress table,
  `inet jumpbox_lab_egress` (its own unit, `jumpbox-lab-egress.service`): lab
  VLANs may forward only to the tailnet (`tailscale0`). Traffic from a lab VLAN
  to the management NIC or any other interface is counted and dropped. It needs
  `nft`; `--apply` refuses without it.

### Changed

- The tailnet range `100.64.0.0/10` is a reserved lab-CIDR range only under
  `VPN_MODE=headscale`.
- Outside `VPN_MODE=headscale`, `/auth/me` and the other user payloads no
  longer carry `headscale_key`.
- The release bundle also carries `app/backends/network_slots.py`, which
  `scripts/preflight_env.py` now loads for the client-network checks.
- The jumpbox serves each student's internal VLAN (200-299 by default,
  `10.<N-100>.1.0/24`), and the entry band ends at 199.
- The Headscale policy grants each student their own internal VLAN on every
  enabled backend.
- `VLAN_BANDS` is the one VLAN layout. It replaces `JUMPBOX_VLAN_BANDS` and
  the group pool settings, and everything else derives from it: the student
  entry and internal pools and their pair offset, the group pools, the roster
  cap (the `entry` band's size) in the seed, the importers and
  `term_changeover.sh`, `audit_vlan_pairs.py`, `migrate_add_vlan.py`, the
  network-slot export (key `config.vlan_bands`, was `jumpbox_vlan_bands`),
  the Headscale policy and the jumpbox's subnets. The default is unchanged
  (`entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2
  group-internal:400-500:300:4`): student pairs 100-199 and 200-299, and
  `migrate_add_vlan.py` still refuses to assign pairs when the pool is
  exhausted, before assigning any. The group entry pool is now exactly the
  `group-entry` band, 300-399; it used to end at 400, the first tag of the
  group-internal pool.
- The portal refuses to start when `VLAN_BANDS` does not parse, lacks one of
  `entry`, `internal`, `group-entry`, `group-internal` or repeats one, has
  overlapping bands, an `internal` band of a different size than `entry` (a
  student's pair is one index into both), a `group-internal` band smaller
  than `group-entry` (each group takes one tag of each), a tag outside
  2-4094 or an invalid address, or, with `aws` or `gcp` enabled, a tag above
  cloud slot 500. It also refuses while a stored user or whitelist pair lies
  outside the `entry`/`internal` bands, and while `VLAN_BANDS` differs from
  the layout recorded in the new `vlan_layout_record` table and any student
  pair is stored (a new offset can move in-band tags to other subnets); the
  message names both layouts. A database with no student pair records the
  layout on first start; one that already holds pairs refuses to start until
  `scripts/migrate_add_vlan_layout_record.py --bands <layout>` (or
  `--site-env <path>`) has seeded the layout those pairs were numbered
  under, validated against every stored pair.
  `scripts/accept_vlan_layout.py` (dry run by default) lists every student
  network that moves and, with `--apply`, records the new layout; its
  live-VM and admission check and the write run in one transaction under the
  deployment admission lock. Deployment admission refuses, under the same
  lock, when the recorded layout is not the process's own (`restart
  required`), so a portal left running on the old layout cannot place labs
  after an accept. A group or KOTH game with an explicit VLAN
  outside every band still starts, with a warning naming it.
- `scripts/build_students_merged.py` takes the VLAN layout explicitly,
  `--site-env <path>` or `--bands <VLAN_BANDS>`, and refuses to run without
  one; `term_changeover.sh` passes the `VLAN_BANDS` it loaded.
- `scripts/apply_headscale_policy.sh` (every mode) refuses on a Proxmox site
  while the portal's exported `VLAN_BANDS` differs from `VLAN_BANDS` in
  `scripts/site.env` (else the default), the layout the jumpbox routes.
  `jumpbox_configure.sh` reads `VLAN_BANDS` and refuses a leftover
  `JUMPBOX_VLAN_BANDS`, which would otherwise fall back to the default bands.
  `scripts/preflight_env.py` applies the same `VLAN_BANDS` rules (except the
  stored-pair check, which needs the database) and reports each removed
  setting still in the `.env`.
- `jumpbox_configure.sh --isolate-vlans` (J2) drops VLAN-to-VLAN forwarding
  only; lab egress moved to its own table (above), which does not wait for J2.
  `--allow-vlans` (KOTH) still exempts VLAN-to-VLAN traffic only.
- Group and KOTH internal networks are no longer reachable straight from the
  VPN; players reach them by pivoting through a host on the entry network.
  The Headscale policy grants a group its entry network only (both
  directions) and a KOTH game its entry network only, on every backend.
  A student's own entry and internal networks are unchanged (an
  individual-only site renders byte for byte as before).
- The jumpbox advertises one `/24` per VLAN in `VLAN_BANDS` instead of
  `10.0.0.0/8` (`jumpbox_netgen.py routes`), and a cloud router each band's
  slots instead of its whole base (`headscale_policy.py routes`). Headscale
  sends a client every advertised route that overlaps a network it is
  granted, so a covering route shadowed a player's own route through a
  pivot host.
- The lab egress table also drops traffic between `tailscale0` and the
  `group-internal` band's VLANs in both directions, plus the tags in the new
  `JUMPBOX_TAILNET_DENY_VLANS`; addresses in the new
  `JUMPBOX_TAILNET_STAFF_IPS` are exempt.

### Removed

- The image builder (`scripts/image_build/`), added after 1.1.0 and never in a
  release, moved to its own public repo, `Ragnar-Security/lab-image-builder`.
- `GROUP_ENTRY_VLAN_MIN`, `GROUP_ENTRY_VLAN_MAX`, `GROUP_INTERNAL_VLAN_MIN`,
  `GROUP_INTERNAL_VLAN_MAX` and `JUMPBOX_VLAN_BANDS`, replaced by
  `VLAN_BANDS`, and `app.config.get_group_vlan_ranges()`. A value left in the
  portal's environment is reported by a startup warning and otherwise ignored.

### Fixed

- Card text on the dark theme was near-invisible (dark text on the dark card
  background), which hid the VPN setup instructions on the student page;
  `.card` now sets the theme's text colour.

### Upgrade notes

- `VPN_MODE` defaults to `headscale`, `AWS_CLIENT_CIDRS` to the tailnet, and
  a configured router behaves exactly as before; existing security groups
  already match the default rule set.
- `docker-compose.prod.yml` passes the new `VPN_MODE`, `VPN_NAME`,
  `VPN_INSTRUCTIONS_URL`, `VPN_INSTRUCTIONS` and `AWS_CLIENT_CIDRS`; a site
  using its own compose file must add them to use the new modes.
- **Apply this release only at a term changeover, in this order.** The full
  operator runbook, with every check and the rollback, is
  `docs/INTERNAL_VLAN_ROLLOUT.md`; it uses `docs/JUMPBOX.md` and
  `docs/TERM_CHANGEOVER.md`. In outline:
  1. **Preconditions.** Before deploying, run
     `docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py | python3 scripts/audit_vlan_pairs.py -`.
     It must print `ok`; fix any offending rows first. Set `JUMPBOX_SSH` in
     production's `scripts/site.env` (Proxmox sites), and make sure the
     operator machine can SSH to the jumpbox and run `tailscale status --json`
     there.
  2. **Deploy the portal.** Note the pre-upgrade layout (the old
     `JUMPBOX_VLAN_BANDS`, or the old default when it was unset), then rename
     `JUMPBOX_VLAN_BANDS` to `VLAN_BANDS` in `.env` and in `scripts/site.env`
     (or delete the line in both to take the default); both files must agree.
     Leave `LAB_BRIDGE` and `VMID_RANGE` unset (`docker-compose.prod.yml`
     passes them through empty). Seed the pre-upgrade layout with
     `migrate_add_vlan_layout_record.py --bands '<pre-upgrade layout>'`, then
     accept the new one with `accept_vlan_layout.py` (dry run, then
     `--apply`). Stored tags do not change.
  3. **Adopt the jumpbox.** Re-run `jumpbox_configure.sh --apply`; a hand-built
     jumpbox is adopted first (`docs/JUMPBOX.md`). This installs the lab egress
     table, which removes any egress a hand-built jumpbox gave lab VMs toward
     the management network or the internet (no lab needs outbound internet
     during a session; package installs belong in the image builder). Then
     `jumpbox_configure.sh --apply --ssh-staff-only`, so a lab VM cannot SSH to
     the jumpbox. Verify both: the egress counter rises for a lab-VM ping to
     the management gateway, and a lab VM's SSH to the jumpbox is refused
     while staff SSH works.
  4. **Audit the templates.** Set `internal_nic: false` on every
     single-machine individual entry point.
  5. **Run the changeover.** `scripts/term_changeover.sh --apply` runs the
     policy preflight, then its four stages; stage 4 re-applies
     `scripts/apply_headscale_policy.sh` and must end with `verified: the
     jumpbox lists every roster device as a peer.` Install the Headscale peer
     watchdog timer.
  6. **J2 only after the J1 evidence gate.** `jumpbox_configure.sh --apply
     --no-try --isolate-vlans [--allow-vlans <list>]`, only after the
     log-only J1 reading (at least 7 days, every cross-VLAN flow accounted
     for) has been accepted, and with the agreed KOTH list.
  7. **AD labs only after J2.** No two-machine AD lab is enabled until J2 is
     live and the access check shows no student reaching another student's
     networks.
- Headscale 0.27.1 can leave the jumpbox without peers after the jumpbox's
  Tailscale reconnects, and then no student reaches any lab. The watchdog
  restarts Headscale once when that happens; `scripts/apply_headscale_policy.sh
  --apply` after any jumpbox reboot or Tailscale restart does the same check.
- **`VLAN_BANDS` replaces `JUMPBOX_VLAN_BANDS` and the `GROUP_*_VLAN_*`
  settings.** In the portal's `.env` and in `scripts/site.env`, rename
  `JUMPBOX_VLAN_BANDS` to `VLAN_BANDS` (same value), and delete every
  `GROUP_ENTRY_VLAN_*` / `GROUP_INTERNAL_VLAN_*` line. A site that customised
  the group pools must express them as the `group-entry` and `group-internal`
  bands of `VLAN_BANDS` (with the jumpbox routing the same bands); the old
  names are no longer read. `docker-compose.prod.yml` (and the development
  `docker-compose.yml`, which passed no band setting before) now pass `VLAN_BANDS`;
  a site with its own compose file must pass it instead of
  `JUMPBOX_VLAN_BANDS`. Run `scripts/migrate_add_vlan_layout_record.py` with
  the other migrations (`docs/PRODUCTION_DEPLOYMENT.md` step 3), giving it
  the pre-upgrade layout: the value of `JUMPBOX_VLAN_BANDS` in the
  pre-upgrade `.env`, or, where it was unset, the pre-upgrade default
  `entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4`
  (`--bands '<layout>'`, or `--site-env <pre-upgrade site.env>`, which reads
  `VLAN_BANDS`, then `JUMPBOX_VLAN_BANDS`, then that default). It is
  required while student pairs are stored; a site without one may omit it.
  Then accept the release's layout between terms with
  `scripts/accept_vlan_layout.py` (`docs/INTERNAL_VLAN_ROLLOUT.md`, section
  2). Startup then refuses while a stored
  student pair lies outside the `entry`/`internal` bands, or while
  `VLAN_BANDS` differs from the recorded layout with student pairs stored:
  change the layout between terms with `scripts/accept_vlan_layout.py`
  (`docs/JUMPBOX.md`, "Address plan"), or restore the old value. Callers of
  `build_students_merged.py` must add `--site-env` or `--bands`. Update the
  portal and the operator checkout together: the export's band key is now
  `vlan_bands`, and `apply_headscale_policy.sh` refuses an export without it.
- **Group and KOTH internal networks need a pivot from the VPN.** Labs whose
  student-facing hosts sit on a group or KOTH internal network must move
  those hosts to the entry network before this release is applied. Then:
  1. Remove `JUMPBOX_TS_ROUTES` from `scripts/site.env` (`jumpbox_configure.sh`
     refuses to run while it is set). Set `JUMPBOX_TAILNET_STAFF_IPS` to the
     staff devices that need the internal networks and, if you run KOTH, the
     portal host's tailnet address (its service probes and beacon claims
     reach every box). Set `JUMPBOX_TAILNET_DENY_VLANS` to any KOTH internal
     VLAN outside the `group-internal` band.
  2. `jumpbox_configure.sh --apply --tailscale` re-advertises the routes and
     installs the new lab egress table. If no `autoApprovers` entry covers
     `10.0.0.0/8` for `tag:jumpbox`, approve the new list:
     `headscale nodes approve-routes --identifier <id> --routes "$(python3
     scripts/jumpbox_netgen.py routes --bands "$VLAN_BANDS")"`, which also
     withdraws the old `10.0.0.0/8` approval.
  3. On each cloud router, `tailscale set --advertise-routes=<list>` with
     the list from `headscale_policy.py routes slots.json <aws|gcp>`, then
     approve it the same way.
  4. Re-render and apply the Headscale policy (`apply_headscale_policy.sh`).

## [1.1.0] - 2026-11-14

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
- `term_changeover.sh` reports every file it replaces as `backup: <path>` or,
  when there was no old file, `absent-before: <path>`. It used to print a
  key-file and a portal-roster backup path even when no backup was written,
  and nothing for a local roster created from nothing, so a rollback either
  failed on a missing backup or left the incoming file behind.
  `docs/TERM_CHANGEOVER.md` "Rollback" now undoes each line with one
  `undo_file` function (copy back, or delete); `docs/INTERNAL_VLAN_ROLLOUT.md`
  Procedure R uses it. The other lines changed form too: the database and
  MySQL dump lines are now `backup:`.
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

  The running v1.0.0 image does not contain `migrate_add_vm_backend.py`, so
  copy each script from the upgraded checkout into the running container and
  run it there (the scripts need only SQLAlchemy). Use plain `docker compose`
  on a host whose `.env` sets `COMPOSE_FILE`:

  ```bash
  for migration in migrate_koth_claim_mode.py migrate_add_session_version.py \
                   migrate_vm_deletion_retry.py migrate_add_password_reset_tokens.py \
                   migrate_add_coordination_tables.py migrate_add_alert_outbox.py \
                   migrate_add_vm_backend.py; do
    docker cp scripts/$migration proxmox-vm-automation-prod:/tmp/$migration
    docker compose exec -T vm-automation python /tmp/$migration
    docker compose exec -T -u root vm-automation rm -f /tmp/$migration
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
- **Operator steps after the upgrade, not part of it:** the Headscale
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

[Unreleased]: https://github.com/Ragnar-Security/Ragnar-Lab-Playground/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Ragnar-Security/Ragnar-Lab-Playground/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Ragnar-Security/Ragnar-Lab-Playground/releases/tag/v1.0.0
