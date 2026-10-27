# Term changeover

This procedure replaces one semester roster across Headscale and the production portal.

The script changes no Proxmox VM or template.

The script never touches a dojo repository.

## Before you start

1. Export the Canvas gradebook as CSV.
2. Confirm that every real student has a SIS User ID.
3. Copy `scripts/site.env.example` to the untracked `scripts/site.env`.
4. Set `PORTAL_HOST`, `PORTAL_DIR`, `PORTAL_COMPOSE`,
   `PORTAL_APP_CONTAINER`, `HEADSCALE_HOST`, `HEADSCALE_DIR`,
   `HEADSCALE_KEEP_USER`, `HEADSCALE_STAFF`, `EMAIL_DOMAIN`, and
   `REMOTE_BACKUP_DIR`.
5. Run the script from this repository root.
6. Confirm that SSH reaches both production hosts.

Exported environment variables override `scripts/site.env`. The scripts have
no host, IP, or username defaults; they stop immediately and name a missing
required variable.

### The shell matters, and it bit us once

If a checkout is on a Windows disk, WSL and Git Bash can both open it but
resolve paths and interpreters differently. The script adapts to both.

**The path you paste.** A Canvas download may be on a Windows file system, so
you can copy a path like `C:\Users\<operator>\Downloads\gradebook.csv` out of
Explorer. WSL has no such path. The script converts a `C:\...` or `C:/...`
path to `/mnt/c/...` for you.

**The interpreter.** The script picks `python3` when the shell has one, and the
repository virtual environment when it does not. Do not force one. The Windows
`python.exe` cannot open a `/mnt/c/...` path, so a forced choice breaks one shell.

**The SSH keys.** WSL and Git Bash keep separate `~/.ssh` directories. A key that
works in one shell is unknown in the other. The script tells you the exact command
when a host key is missing:

```bash
ssh-keyscan -H <headscale-host> >> ~/.ssh/known_hosts
ssh <headscale-host> true
```

If the second command says `Permission denied (publickey)`, that shell's key is not
authorized yet. Append its `~/.ssh/id_ed25519.pub` to `~/.ssh/authorized_keys` on both
production hosts.

## Commands

Run the read-only inspection first.

```bash
scripts/term_changeover.sh <canvas-export>.csv
```

The dry run is the default.

It reads Headscale and the production portal.

It does not create temporary remote files.

It reports these counts:

- Headscale users to remove.
- Headscale users to create.
- Local and production roster files to replace.
- Whitelist rows to delete.
- Whitelist rows to seed.

Review every count before applying the change.

```bash
scripts/term_changeover.sh --apply <canvas-export>.csv
```

Run the dry command again after the apply command.

Every stage must report `nothing to do`.

## Stage order

### 1. Inspect and update Headscale

The script reads the current Headscale users and protected key file.

It preserves the infrastructure user from `HEADSCALE_KEEP_USER` and every
comma-separated staff user in `HEADSCALE_STAFF`.

It runs `headscale_term_changeover.sh` from `HEADSCALE_DIR` for changes.

The orchestrator skips that apply call when Headscale already matches.

This skip prevents a second run from rotating working keys.

Before a change, the script stops Headscale and copies its SQLite database.

It also copies the existing student key file.

The script starts Headscale before making roster changes.

It prints both backup paths.

After the change, it reads all users and keys again.

It fails unless the user and key sets match Canvas exactly.

### 2. Build and replace the mounted roster

The script downloads the protected key file to a local temporary directory.

It calls `scripts/build_students_merged.py` with the Canvas export and key file.

The merge tool requires one key for every student.

It also rejects keys that do not belong to the roster.

The script verifies the row count and file hash.

It backs up the local file under `.git/term-changeover-backups/`.

It backs up the production file under `REMOTE_BACKUP_DIR`.

It then replaces `data/students_merged.csv` locally and on the portal.

Production bind-mounts `./data` into `/app/data`.

The mounted CSV is therefore the seed source for the next restart.

The CSV contains live Headscale credentials and protected student records.

It must remain untracked. `.gitignore` excludes it and every other CSV under
`data/`; never force-add a roster, Canvas export, or key file. Transfer and
back it up only through the changeover procedure.

The production copy is delivered as mode `0640` with group `1000`. The app
container runs as `appuser`, uid and gid `1000` (pinned in `Dockerfile.prod`),
so the group bit lets it read the bind-mounted file while every other account
on the portal host is shut out. `0600` would need the file owned by uid
`1000`, which only root can arrange; a group needs only that the SSH account
owns the file and belongs to group `1000` (on a stock Ubuntu host the first
account is uid/gid `1000`) or is root. When `chgrp` fails the delivery stops
before the live file changes.

A roster delivered by an older version of these scripts is world-readable
(`0644`). Tighten it once on the portal host; the container reads it either way:

```bash
chgrp 1000 /opt/lab-playground/data/students_merged.csv
chmod 640 /opt/lab-playground/data/students_merged.csv
```

### 3. Purge and seed the production database

The script first compares the live whitelist with the requested roster.

An exact whitelist makes this stage a no-op.

This rule protects students who register after the changeover.

A mismatch means the database still represents another roster.

The script prints the delete count and seed count.

Before the purge, the script creates a MySQL dump under
`REMOTE_BACKUP_DIR`. It prints the dump path and verifies that the file is
not empty.

The production `scripts/backup-database.sh` also dumps MySQL and verifies the
completion trailer. The term-changeover orchestrator creates its own
`pre-term-changeover-*.sql` dump because the rollback procedure names that
specific snapshot.

The script runs `scripts/purge_roster.sql` after the backup.

It verifies that the purge removed all roster-owned rows.

It also verifies that at least one administrator remains.

It compares the administrator VM list before and after the purge.

The script refuses to restart unless the mounted roster hash matches.

It then restarts only the portal application container.

The startup command runs `scripts/init_db.py` before Gunicorn.

The script waits for a healthy container.

It then compares every whitelist login, SIS ID, and Headscale key.

## Four failure traps

### Trap 1: A database purge is not a roster change

`init_db.py` reads `data/students_merged.csv` during every container start.

An empty whitelist causes the import again.

An old CSV once restored every previous-term student after a purge.

The script replaces and verifies the CSV before any restart.

### Trap 2: A second Headscale run can remove new students

A broad removal list included the incoming students during the first design.

The helper now excludes every incoming login from removal.

The orchestrator also skips the helper apply when users and keys already match.

Together, these rules make a completed second run a no-op.

### Trap 3: Headscale cannot delete a user with nodes

Headscale rejects `users destroy` while that user owns a node.

The helper deletes each departing user's nodes first.

The orchestrator reads the final user set after the helper exits.

A successful exit without the correct state still fails verification.

### Trap 4: Staff users look like old students

A removal list based only on the incoming roster also includes staff and
infrastructure users.

The helper protects `HEADSCALE_STAFF` and `HEADSCALE_KEEP_USER`.

The orchestrator independently checks the helper's removal count.

Stop if those counts disagree.

## Re-run safety

A completed dry run reports zero removals and zero creations.

It reports no roster file replacements.

It reports zero whitelist deletions and zero whitelist seeds.

It ends with `Changeover complete. Every stage has nothing to do.`

Do not use `--apply` unless the dry run reports expected changes.

A second completed apply also skips every destructive stage.

## Rollback

Use only the backup paths printed by the failed apply run. Replace each angle-bracket placeholder below with the corresponding value from `scripts/site.env`.

### Restore Headscale

Stop Headscale before restoring its SQLite database.

```bash
ssh <headscale-host>
cd <headscale-dir>
docker compose stop headscale
cp <remote-backup-dir>/headscale-db-YYYYMMDD-HHMMSS.sqlite headscale/data/db.sqlite
cp <remote-backup-dir>/headscale-keys-YYYYMMDD-HHMMSS.csv students_with_keys.csv
chmod 600 headscale/data/db.sqlite students_with_keys.csv
docker compose start headscale
docker compose exec -T headscale headscale users list
```

Read the user list after the start.

Confirm that the configured infrastructure and staff users still own their expected nodes.

### Restore the roster files

Restore the production CSV before restarting the portal.

```bash
ssh <portal-host>
cp <remote-backup-dir>/students_merged-YYYYMMDD-HHMMSS.csv \
  /opt/lab-playground/data/students_merged.csv
chgrp 1000 /opt/lab-playground/data/students_merged.csv
chmod 640 /opt/lab-playground/data/students_merged.csv
```

Restore the local CSV from the printed `.git/term-changeover-backups/` path.

Keep the restored roster and key set untracked.

### Restore MySQL

Restore the dump only when the purge or seed was wrong.

```bash
ssh <portal-host>
cd /opt/lab-playground
docker compose -f docker-compose.prod.yml exec -T db sh -c \
  'exec mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' \
  < <remote-backup-dir>/pre-term-changeover-YYYYMMDD-HHMMSS.sql
docker compose -f docker-compose.prod.yml restart vm-automation
```

Wait for the portal health check.

Run the dry command with the restored term export.

## Mid-term or end-of-term key rotation

Each student's reusable Headscale pre-auth key stays valid for `KEY_DAYS`.
A leaked key lets anyone join the tailnet as that student until it expires.
Rotate the keys at the end of a semester, or mid-term after a suspected
leak, without changing the roster:

```bash
scripts/rotate_headscale_keys.sh                          # dry run: the plan only
scripts/rotate_headscale_keys.sh --apply --only <login>   # try one account first
scripts/rotate_headscale_keys.sh --apply                  # asks you to type 'rotate'
scripts/rotate_headscale_keys.sh --apply --yes            # unattended
scripts/rotate_headscale_keys.sh --apply --yes --allow-partial      # see "Partial rotation"
scripts/rotate_headscale_keys.sh --apply --yes --break-stale-lock   # see "Rotation lock"
```

It needs `PORTAL_HOST`, `PORTAL_DIR`, `PORTAL_COMPOSE`, `HEADSCALE_HOST`,
`HEADSCALE_DIR`, `HEADSCALE_KEEP_USER`, `HEADSCALE_STAFF`, `KEY_DAYS`, and
`REMOTE_BACKUP_DIR`, and stops before contacting a host when one is unset.
`--apply` without `--yes` asks for confirmation at the terminal and refuses
when there is no terminal.

The roster is the portal's mounted `data/students_merged.csv`. Logins in
`HEADSCALE_KEEP_USER` and `HEADSCALE_STAFF` are never rotated, even when they
appear in that file. `--only` limits the run to the listed logins and refuses
a login that is not on the roster or is protected.

The dry run prints the students, their count of active keys, and the checks
below. It shows no key value and changes nothing. It fails, as `--apply`
would, when a student has no Headscale user or the running portal container
lacks `scripts/update_headscale_keys.py`.

`--apply` works in this order:

1. Create one new reusable key per student on Headscale, expiring in `KEY_DAYS`.
2. Write the new `data/students_merged.csv` locally, mode `0600`.
3. Back up the portal's roster file to
   `REMOTE_BACKUP_DIR/students_merged-pre-rotation-<stamp>-<pid>-<random>.csv`,
   then replace it with the same temp-and-`mv` and hash check as the term
   changeover, mode `0640` and group `1000` as described above.
4. Store the new keys in both `student_whitelist.headscale_key` and
   `users.headscale_key`, in one transaction, by running
   `scripts/update_headscale_keys.py` in the `vm-automation` container.
5. Expire the keys that were active before step 1.

Step 4 is not optional. The dashboard shows the key from the database, and
`init_db.py` reads the CSV only while the whitelist is empty, so a new CSV
alone would leave students looking at a key that step 5 is about to expire.

Old keys are expired last because a failure at any earlier step must leave
every student with a key that works. If the database update fails, the
script stops with a non-zero exit, and no key is expired. Fix the cause and
run `--apply` again: each run issues fresh keys and expires every key that
was active before it, including keys from a failed run.

Expiring a pre-auth key stops new registrations with it. It does not
disconnect a device that is already registered. A student needs the new key
only to add a device or reinstall, and the portal already shows it.

The key values travel only over SSH standard input and output. They never
appear in command arguments on the workstation or in the script's output.
Keep the regenerated `data/students_merged.csv` untracked, as always.

### Partial rotation

A student on the roster but absent from the portal database keeps the old
key, because expiring it would leave them with no key the portal can show.
That old key is still a live credential, so the run exits non-zero after
expiring the other students' old keys, and names the affected logins (never
keys). Add those students to the portal and re-run
`--apply --only <logins>`, or pass `--allow-partial` to accept the gap and
exit 0; the summary still names them.

### Rotation lock

`--apply` takes an exclusive lock on both hosts before it reads the roster and
holds it until the old keys are expired:
`HEADSCALE_DIR/.rotate.lock` on the Headscale host and
`PORTAL_DIR/data/.rotate.lock` on the portal. Each is a directory created with
`mkdir`, which is atomic, holding an `owner` file with the operator, workstation,
pid, start time, and a random token. A second rotation refuses to start and
names the holder. Every later step checks that the token is still its own, so a
run whose lock was taken over stops instead of interleaving. The locks are
removed when the run ends, whether it succeeded or failed. The dry run takes no
lock.

A run killed outright (power loss, `kill -9`) leaves its lock behind. Once the
lock directory is more than 6 hours old, measured on that host,
`--break-stale-lock` replaces it and prints whose lock it broke; it never breaks
a younger lock. To retry sooner, confirm that no rotation is running and remove
the directory by hand.

## Human decisions

Review the generated credential file, keep it untracked, and store any
off-host copy in an approved secret or backup system.

Review `templates.yaml` when the next term uses different lab groups.

Archive an old group outside Git instead of retaining site-specific
definitions in tracked files.

Decide separately whether deployment-specific jumpbox VM labels need
renaming. Do not delete a VM or template during a roster changeover.
