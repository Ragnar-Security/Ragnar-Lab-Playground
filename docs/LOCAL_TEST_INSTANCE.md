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
   people's VMs. The code enables the reaper when the value is unset or **empty**
   (`ORPHAN_REAPER_ENABLED = (os.environ.get('ORPHAN_REAPER_ENABLED') or 'true').lower() == 'true'`,
   `app/config.py:105`), so an empty `.env` line is not "off". Use the supported literal `false`;
   unset or empty enables it. `docker-compose.local.example.yml` sets it as well, and you prove it
   with a first start that has no cluster access before the real host and token exist (steps 3-4).
2. **Test logins and a test network.** Deployed VMs are named `student-<login>-<template>`, and a
   deploy cleans up an existing guest with the same name. Use logins that no real user has (for
   example a `zz-` prefix). Put test users on a VLAN pair that production does not use anywhere: a
   build or lab network with its own DHCP, so the test VM gets an address you can reach. Step 2
   shows how to check the pair.
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
Proxmox API). Steps 1-4 use no real credentials; the real host and token appear only in steps 5-7.

1. **A separate checkout of the release production runs**, so you test what students get. The two
   example files this guide uses live on `main`, and a release tag cut before this guide merged
   does not contain them (v1.0.0 does not), so read them out of `origin/main`. Once the guide has
   merged, `main` is where they exist; before that, fetch the guide's branch
   (`origin/docs/local-test-instance`) instead.

   ```bash
   git clone --branch vX.Y.Z https://github.com/Ragnar-Security/Ragnar-Lab-Playground.git lp-local
   cd lp-local
   git fetch origin main
   git show origin/main:docker-compose.local.example.yml > docker-compose.local.yml
   git show origin/main:templates.local.example.yaml > templates.yaml    # then fill in the <...> values
   ```

   Both files are untracked in the checkout. Edit `templates.yaml` so it lists only the template you
   are testing, with your cluster's node, storage and template VMID.

2. **Choose the test VLAN pair and check it against production.** Pick an entry tag and an internal
   tag, `<test-vlan>` and `<unused-vlan>`, on the network you will test on. Production stores tags in
   three tables, and its allocator treats a tag found in any of them as taken
   (`app/vlan_allocator.py` `_used_tags`, `validate_vlan_free`): `users`, `student_whitelist` and
   `student_groups`, each with `entry_vlan_tag` and `internal_vlan_tag`, plus `koth_games` rows whose
   `status` is not `ended`. "No student uses it" is not enough: a group can hold the tag, and a tag
   used as an entry tag by one row can be an internal tag on another.

   The tester does not need, and should not be given, access to the production database. Ask someone
   who already has it (the production admin UI where it shows student and group VLANs, otherwise the
   database) to confirm that neither tag appears anywhere. Read-only SQL for that person to run:

   ```sql
   SELECT 'users' AS src, login_id AS ref, entry_vlan_tag, internal_vlan_tag FROM users
     WHERE entry_vlan_tag IN (<test-vlan>, <unused-vlan>) OR internal_vlan_tag IN (<test-vlan>, <unused-vlan>)
   UNION ALL
   SELECT 'student_whitelist', login_id, entry_vlan_tag, internal_vlan_tag FROM student_whitelist
     WHERE entry_vlan_tag IN (<test-vlan>, <unused-vlan>) OR internal_vlan_tag IN (<test-vlan>, <unused-vlan>)
   UNION ALL
   SELECT 'student_groups', name, entry_vlan_tag, internal_vlan_tag FROM student_groups
     WHERE entry_vlan_tag IN (<test-vlan>, <unused-vlan>) OR internal_vlan_tag IN (<test-vlan>, <unused-vlan>)
   UNION ALL
   SELECT 'koth_games', name, entry_vlan_tag, internal_vlan_tag FROM koth_games
     WHERE status <> 'ended'
       AND (entry_vlan_tag IN (<test-vlan>, <unused-vlan>) OR internal_vlan_tag IN (<test-vlan>, <unused-vlan>));
   ```

   Zero rows means the pair is free in production's database. Also confirm with whoever runs the
   network that the tags are not in use on the switches for something else. Step 8 applies the
   pair to the test login in the *local* database.

3. **Phase 1 of the start: no cluster access at all.** Write `.env` in the checkout, mode 600
   (`chmod 600 .env`), with a host that cannot reach any cluster and dummy credentials:

   ```ini
   FLASK_ENV=development
   PROXMOX_HOST=127.0.0.1
   PROXMOX_USER=root@pam
   PROXMOX_TOKEN_ID=localportal
   PROXMOX_TOKEN_SECRET=dummy
   PROXMOX_VERIFY_SSL=false
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

   Start it and prove the reaper is off, two ways. The container must hold the literal value, and the
   expiry loop's first pass (about 60 seconds after start) must log the disabled line:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
   docker compose -f docker-compose.yml -f docker-compose.local.yml exec -T vm-automation printenv ORPHAN_REAPER_ENABLED
   # expect exactly: false
   docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f vm-automation | grep -i reaper
   # expect: <timestamp> app.orphan_reaper INFO Orphan reaper disabled by ORPHAN_REAPER_ENABLED
   ```

   The log text comes from `app/orphan_reaper.py:55` (`logger.info("Orphan reaper disabled by
   ORPHAN_REAPER_ENABLED")`), and `app.orphan_reaper` is the logger name. The example override
   starts the app with INFO logging so the line prints. **If `printenv` prints anything but `false`,
   or you see a sweep line instead of the disabled line, stop here:** run
   `docker compose -f docker-compose.yml -f docker-compose.local.yml down` and fix the cause. Do not
   continue to step 5.

4. **Stop phase 1.** Keep the database (no `-v`); the seeded logins are reused.

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml down
   ```

5. **A dedicated, short-lived API token** (on a Proxmox node, as root). Never reuse production's
   token. Give it an expiry that covers the test session and no more; the command below expires it
   in eight hours:

   ```bash
   pveum user token add root@pam localportal --privsep 0 --expire "$(date -d '+8 hours' +%s)" --output-format json   # copy "value"
   ```

   `--privsep 0` gives the token the full privileges of `root@pam`, which is why it must expire and
   must be removed at teardown (Teardown, step 4). Expiry stops the token working; it does not
   remove it and it does not remove VMs the token created. If it lapses while a test VM exists, remove
   it and create a new one with a fresh `--expire`, put the new value in `.env`, and restart.

6. **Phase 2 of the start: the real host and token.** Edit `.env`: set `PROXMOX_HOST=<proxmox-api-host>`
   and `PROXMOX_TOKEN_SECRET=<token value>`. Add `PROXMOX_CA_BUNDLE=<pem>` instead of
   `PROXMOX_VERIFY_SSL=false` if the cluster's CA is available. Leave `ORPHAN_REAPER_ENABLED=false`
   and `ORPHAN_REAPER_DRY_RUN=true` exactly as they are. Recreate the container so it certainly picks
   up the new `.env` values:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --force-recreate
   docker compose -f docker-compose.yml -f docker-compose.local.yml exec -T vm-automation printenv ORPHAN_REAPER_ENABLED
   # expect exactly: false
   ```

7. **Confirm the reaper is still off against the real cluster.** Wait for the next pass (about 60
   seconds) and look for the same line:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f vm-automation | grep -i reaper
   # expect: <timestamp> app.orphan_reaper INFO Orphan reaper disabled by ORPHAN_REAPER_ENABLED
   ```

   A sweep line here means run `docker compose -f docker-compose.yml -f docker-compose.local.yml down`
   at once and run Teardown steps 4 and 5.

8. **Put the test user on the test network.** The admin API cannot give the same pair to a login's
   whitelist row and its user row: `validate_vlan_free` refuses a tag already held by any user,
   whitelist row, group, or live KOTH game, including the same login's other row. In the **local**
   database the pair is only checked for uniqueness within each table, so set it with SQL, using the
   pair you cleared with production in step 2:

   ```bash
   docker exec mysql-db mysql -uvmuser -pvmpassword vmdatabase -e "
     UPDATE student_whitelist SET entry_vlan_tag=<test-vlan>, internal_vlan_tag=<unused-vlan> WHERE login_id='zz-playground-test';
     UPDATE users             SET entry_vlan_tag=<test-vlan>, internal_vlan_tag=<unused-vlan> WHERE login_id='zz-playground-test';
     SELECT 'student_whitelist' AS src, login_id, entry_vlan_tag, internal_vlan_tag FROM student_whitelist WHERE login_id='zz-playground-test'
     UNION ALL
     SELECT 'users', login_id, entry_vlan_tag, internal_vlan_tag FROM users WHERE login_id='zz-playground-test';"
   ```

   The final `SELECT` must show the pair on both rows. Production is untouched: this is the local
   database only.

9. **Open it.** It listens on `127.0.0.1:5001` on its host. From another machine use a tunnel:
   `ssh -L 5001:127.0.0.1:5001 <host>`, then browse to http://localhost:5001 and log in as
   `zz-playground-test` (or `localadmin`). Note the VMID and name of every VM you deploy; Teardown
   checks the cluster for them.

## Day-to-day

| Task | Command |
|---|---|
| Stop, keep the database | `docker compose -f docker-compose.yml -f docker-compose.local.yml down` |
| Reload after editing `templates.yaml` | `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d` |
| Reload after editing `.env` | `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --force-recreate` |
| Follow the log | `docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f vm-automation` |

Wiping the database (`down -v`) is not a day-to-day command. It is Teardown step 3, and only after
the cluster has no VM left from this instance.

The expiry loop destroys this instance's own VMs when their time runs out, exactly as in production.
It does this only while the portal is running and the token is valid.

## Teardown

Mandatory when the test is over, in this order. The database holds the only record of which guests
this instance created; wipe it while a test VM still exists and that VM becomes an orphan nobody is
tracking (production's reaper would eventually delete it, but only once it expires, and the VM uses
real capacity until then).

1. **Destroy every test VM through the portal** (the UI, or `POST /destroy`), while it is still
   running with a valid token.

2. **Verify nothing is left**, locally and on the cluster.

   ```bash
   docker exec mysql-db mysql -uvmuser -pvmpassword vmdatabase -e "SELECT vmid, vm_name, node, destroyed FROM vm_records;"
   ```

   Expect no rows (destroying a normal VM deletes its row). On a Proxmox node, check by name and by
   each VMID you noted in step 9 (replace `<vmid1>|<vmid2>`):

   ```bash
   pvesh get /cluster/resources --type vm --output-format json | grep -Eo '"name" *: *"student-zz-[^"]*"'
   pvesh get /cluster/resources --type vm --output-format json | grep -Eo '"vmid" *: *(<vmid1>|<vmid2>)[,}]'
   ```

   Both must print nothing. A VM that is still listed: destroy it from the portal again, or, if the
   portal cannot, stop and destroy that guest by hand in Proxmox and only then continue.

3. **Stop the stack and wipe its volumes**:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml down -v
   ```

4. **Remove the Proxmox token** (on a node, as root). Do this even if it has already expired:

   ```bash
   pveum user token remove root@pam localportal
   pveum user token list root@pam
   ```

   `localportal` must not appear in the list.

5. **Remove the credentials from the checkout**: `shred -u .env` (or delete it and empty the
   trash, where `shred` does not exist). Delete the checkout if you will not reuse it; it also
   holds `docker-compose.local.yml` and `templates.yaml`, which contain your cluster's node and
   template names.

## Notes

- **Ports in the UI.** The VM list renders connection details only for templates with an RDP
  `connect:` block; everything else shows "SSH via jumpbox". Put non-standard ports in the group
  `description`.
- **Destroy deletes the row.** Destroying a normal (non-KOTH) VM removes its database row rather than
  marking it destroyed, so it disappears from the instance entirely.
