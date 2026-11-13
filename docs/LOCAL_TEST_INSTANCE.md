# Local test instance against a real Proxmox cluster

A throwaway copy of the portal, run with the development stack on a workstation or a lab VM, that
deploys real VMs on the same Proxmox cluster as production. Use it to try a new template, test a
`templates.yaml` change, or develop against real infrastructure without touching the production
portal.

It shares the cluster with production, so three things must be true **before** it gets real
credentials. Everything else in this guide is convenience.

## The three rules

1. **The orphan reaper is off, spelled `ORPHAN_REAPER_ENABLED=false`.** The reaper reads every guest
   in the cluster and deletes app-named guests that are past their `Expires:` line and have no row in
   *this instance's* database. A local database is empty, so a local reaper would delete other
   people's VMs. An unset or **empty** value turns it on (`app/config.py`); only the word `false`
   turns it off. `docker-compose.local.example.yml` sets it as well, and you check the log line
   before going further (step 4).
2. **Test logins and a test network.** Deployed VMs are named `student-<login>-<template>`, and a
   deploy cleans up an existing guest with the same name. Use logins that no real user has (for
   example a `zz-` prefix). Put test users on a VLAN no student uses: a build or lab network with its
   own DHCP, so the test VM gets an address you can reach.
3. **A test-only catalog.** List only the templates you are testing (`templates.local.example.yaml`),
   never the production catalog.

Also: leave `SMTP_SERVER` and `ALERT_WEBHOOK_URL` empty, use the development stack
(`docker-compose.yml`), never `docker-compose.prod.yml`, and never run from a checkout that holds
production data (`data/students_merged.csv`, `data/untracked-survivors.jsonl`).

## What it still shares with production

- **VMIDs.** It allocates above the cluster's highest VMID, like production. Production reads the
  cluster live when it allocates, so the two do not collide while a test VM exists, and nothing
  remains after you destroy it.
- **Capacity.** Test VMs use real memory and storage on the nodes.
- **Proxmox task log.** Each operation shows up there under the token's user.

## Stand it up

Run these on the machine that will host it (it needs Docker with Compose v2 and HTTPS access to the
Proxmox API).

1. **A separate checkout of the release production runs**, so you test what students get:

   ```bash
   git clone --branch vX.Y.Z https://github.com/Ragnar-Security/Ragnar-Lab-Playground.git lp-local
   cd lp-local
   cp docker-compose.local.example.yml docker-compose.local.yml   # from main if the tag predates it
   cp templates.local.example.yaml templates.yaml                  # then fill in the <...> values
   ```

2. **A dedicated API token** (on a Proxmox node, as root). Never reuse production's token.

   ```bash
   pveum user token add root@pam localportal --privsep 0 --output-format json   # copy "value"
   # Optional expiry: --expire <unix-time>. Remove it later with:
   # pveum user token remove root@pam localportal
   ```

3. **`.env`** in the checkout, mode 600 (`chmod 600 .env`):

   ```ini
   FLASK_ENV=development
   PROXMOX_HOST=<proxmox-api-host>
   PROXMOX_USER=root@pam
   PROXMOX_TOKEN_ID=localportal
   PROXMOX_TOKEN_SECRET=<token value>
   PROXMOX_VERIFY_SSL=false            # or PROXMOX_CA_BUNDLE=<pem> if the cluster's CA is available
   ORPHAN_REAPER_ENABLED=false
   ORPHAN_REAPER_DRY_RUN=true
   SECRET_KEY=<long random string>
   ADMIN_LOGIN_ID=localadmin
   ADMIN_PASSWORD=<random>
   SEED_ADMIN_ACCOUNT=true
   SEED_TEST_STUDENT=1
   TEST_STUDENT_LOGIN_ID=zz-playground-test
   TEST_STUDENT_PASSWORD=<random>
   ENABLE_LOAD_BALANCING=false
   FIXED_NODE=<node>
   SMTP_SERVER=
   ALERT_WEBHOOK_URL=
   ```

4. **Start it and confirm the reaper is off.** The expiry loop's first pass is about 60 seconds after
   start, and that pass logs the line:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
   docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f vm-automation | grep -i reaper
   # expect: ... app.orphan_reaper INFO Orphan reaper disabled by ORPHAN_REAPER_ENABLED
   ```

   If you see a sweep line instead, run `docker compose -f docker-compose.yml -f docker-compose.local.yml down`
   at once. For extra certainty on first use, start it once with `PROXMOX_HOST=127.0.0.1` (no cluster
   access), see the line, `down`, then set the real host and start again.

5. **Put the test user on the test network.** The admin API cannot set VLANs; set them in the local
   database only:

   ```bash
   docker exec mysql-db mysql -uvmuser -pvmpassword vmdatabase -e "
     UPDATE student_whitelist SET entry_vlan_tag=<test-vlan>, internal_vlan_tag=<unused-vlan> WHERE login_id='zz-playground-test';
     UPDATE users             SET entry_vlan_tag=<test-vlan>, internal_vlan_tag=<unused-vlan> WHERE login_id='zz-playground-test';"
   ```

6. **Open it.** It listens on `127.0.0.1:5001` on its host. From another machine use a tunnel:
   `ssh -L 5001:127.0.0.1:5001 <host>`, then browse to http://localhost:5001 and log in as
   `zz-playground-test` (or `localadmin`).

## Day-to-day

| Task | Command |
|---|---|
| Stop, keep the database | `docker compose -f docker-compose.yml -f docker-compose.local.yml down` |
| Stop and wipe the database | `... down -v` |
| Reload after editing `templates.yaml` | `... up -d` |
| Follow the log | `... logs -f vm-automation` |

The expiry loop destroys this instance's own VMs when their time runs out, exactly as in production.
Destroy test VMs from the UI (or `POST /destroy`) before wiping the database; a VM whose row is gone
is invisible to this instance and is only cleaned up by production's reaper once it expires.

## Notes

- **Ports in the UI.** The VM list renders connection details only for templates with an RDP
  `connect:` block; everything else shows "SSH via jumpbox". Put non-standard ports in the group
  `description`.
- **Destroy deletes the row.** Destroying a normal (non-KOTH) VM removes its database row rather than
  marking it destroyed, so it disappears from the instance entirely.
