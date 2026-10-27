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
chmod 644 /opt/lab-playground/data/students_merged.csv
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

## Human decisions

Review the generated credential file, keep it untracked, and store any
off-host copy in an approved secret or backup system.

Review `templates.yaml` when the next term uses different lab groups.

Archive an old group outside Git instead of retaining site-specific
definitions in tracked files.

Decide separately whether deployment-specific jumpbox VM labels need
renaming. Do not delete a VM or template during a roster changeover.
