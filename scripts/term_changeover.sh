#!/usr/bin/env bash
# Run one roster changeover across Headscale and the production portal.
# Dry-run mode reads both hosts and makes no changes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/site_env.sh"

SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" \
    PORTAL_HOST PORTAL_DIR PORTAL_COMPOSE PORTAL_APP_CONTAINER \
    HEADSCALE_HOST HEADSCALE_DIR HEADSCALE_KEEP_USER HEADSCALE_STAFF \
    EMAIL_DOMAIN REMOTE_BACKUP_DIR KEY_DAYS HEADSCALE_SCRIPT HEADSCALE_DB
for name in PORTAL_HOST PORTAL_DIR PORTAL_COMPOSE PORTAL_APP_CONTAINER \
    HEADSCALE_HOST HEADSCALE_DIR HEADSCALE_KEEP_USER HEADSCALE_STAFF \
    EMAIL_DOMAIN REMOTE_BACKUP_DIR KEY_DAYS; do
    require_site_var "$name"
done

HEADSCALE_SCRIPT="${HEADSCALE_SCRIPT:-./headscale_term_changeover.sh}"
HEADSCALE_DB="${HEADSCALE_DB:-headscale/data/db.sqlite}"
for name in PORTAL_DIR HEADSCALE_DIR REMOTE_BACKUP_DIR; do
    [[ "${!name}" == /* ]] \
        || { printf 'ERROR: %s must be an absolute path\n' "$name" >&2; exit 1; }
done
for name in PORTAL_DIR PORTAL_COMPOSE PORTAL_APP_CONTAINER HEADSCALE_DIR \
    HEADSCALE_SCRIPT HEADSCALE_DB REMOTE_BACKUP_DIR; do
    [[ "${!name}" != *"'"* ]] \
        || { printf "ERROR: %s must not contain an apostrophe\n" "$name" >&2; exit 1; }
done
SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=10)
# The app container runs as uid/gid 1000 (Dockerfile.prod pins both); the
# mounted roster is delivered as mode 0640 with this group so it can read it.
APP_GID=1000
# The interpreter must live in the same path universe as this shell. Under WSL
# every path here is /mnt/c/..., and the Windows python.exe cannot open such a
# path. Under Git Bash the repository venv is the right answer. Both helpers
# this script runs use the standard library only, so either interpreter works.
# Prefer a POSIX python3 when one exists, and fall back to the venv.
if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif [[ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
    PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
else
    PYTHON="$(command -v python || true)"
fi
BUILD_MERGED="$SCRIPT_DIR/build_students_merged.py"
PURGE_SQL="$SCRIPT_DIR/purge_roster.sql"
LOCAL_MERGED="$REPO_ROOT/data/students_merged.csv"
APPLY=0
CANVAS=""
TMP_DIR=""
STAMP=""

usage() {
    cat <<'EOF'
Usage: scripts/term_changeover.sh [--apply] CANVAS_EXPORT.csv

Change the Headscale and portal rosters for a new semester.

The default dry run reads both real hosts and writes nothing. It reports:
  - Headscale users to remove and create.
  - Roster files to replace.
  - Whitelist rows to delete and seed.

Use --apply to create backups and perform the changeover. The script never
changes a Proxmox VM, a template, or a dojo repository.

Examples:
  scripts/term_changeover.sh ~/Downloads/canvas-gradebook.csv
  scripts/term_changeover.sh --apply ~/Downloads/2027-spring-gradebook.csv
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf -- "$TMP_DIR"
    fi
}
trap cleanup EXIT

while (($#)); do
    case "$1" in
        --apply)
            APPLY=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            [[ $# -eq 1 && -z "$CANVAS" ]] || die "Give exactly one Canvas export."
            CANVAS="$1"
            ;;
        -*)
            die "Unknown option: $1"
            ;;
        *)
            [[ -z "$CANVAS" ]] || die "Give exactly one Canvas export."
            CANVAS="$1"
            ;;
    esac
    shift
done

[[ -n "$CANVAS" ]] || { usage >&2; exit 2; }
# Canvas downloads to the Windows file system, so the instructor copies a path
# like C:\Users\<you>\Downloads\...csv out of Explorer. This shell is WSL, where
# that path does not exist. Translate it instead of failing. A backslash form
# also loses its separators in some shells, so convert the slashes first.
if [[ ! -f "$CANVAS" && "$CANVAS" =~ ^[A-Za-z]:[\\/] ]]; then
    if command -v wslpath >/dev/null 2>&1; then
        CANVAS="$(wslpath -u "${CANVAS//\\//}" 2>/dev/null || echo "$CANVAS")"
    else
        drive="${CANVAS:0:1}"
        CANVAS="/mnt/${drive,,}/${CANVAS:3}"
        CANVAS="${CANVAS//\\//}"
    fi
fi

[[ -f "$CANVAS" ]] || die "Canvas export not found: $CANVAS"
[[ -x "$PYTHON" ]] || die "Python not found: $PYTHON"
[[ -f "$BUILD_MERGED" ]] || die "Merge tool not found: $BUILD_MERGED"
[[ -f "$PURGE_SQL" ]] || die "Purge SQL not found: $PURGE_SQL"

# The helper needs a regular roster file. A Linux memory file keeps dry runs write-free.
HEADSCALE_MEMFD_RUNNER='import os,sys; fd=os.memfd_create("incoming-roster.csv"); os.write(fd,sys.stdin.buffer.read()); os.lseek(fd,0,0); os.set_inheritable(fd,True); os.execv("/bin/bash",["bash",sys.argv[1],f"/proc/self/fd/{fd}",*sys.argv[2:]])'

HEADSCALE_SUMMARY_PY='import base64,contextlib,csv,hashlib,io,json,sys
sys.path.insert(0, sys.argv[2])
from scripts.build_students_merged import read_canvas
with contextlib.redirect_stdout(sys.stderr):
    students = read_canvas(sys.argv[1])
if not students:
    raise SystemExit("Canvas export has no real students")
logins = [login for _, login in students]
if len(logins) != len(set(logins)):
    raise SystemExit("Canvas export has duplicate SIS Login IDs")
lines = sys.stdin.buffer.read().splitlines()
if len(lines) != 2:
    raise SystemExit("Headscale inventory returned an invalid payload")
users = json.loads(base64.b64decode(lines[0]))
key_text = base64.b64decode(lines[1]).decode("utf-8") if lines[1] else ""
keys = {}
for number, row in enumerate(csv.reader(io.StringIO(key_text)), 1):
    if not row:
        continue
    if len(row) != 3 or not row[0].strip() or not row[2].strip():
        raise SystemExit(f"Headscale key file has an invalid row at line {number}")
    login = row[0].strip().lower()
    if login in keys:
        raise SystemExit(f"Headscale key file repeats {login}")
    keys[login] = (row[1].strip(), row[2].strip())
roster = set(logins)
protected = {item.strip() for item in sys.argv[3].split(",") if item.strip()}
names = {item["name"] for item in users}
remove = names - roster - protected
create = roster - names
missing_keys = roster - set(keys)
extra_keys = set(keys) - roster
out = io.StringIO(newline="")
writer = csv.writer(out)
writer.writerow(["SIS User ID", "SIS Login ID", "email", "headscale_key"])
if not missing_keys:
    for uid, login in students:
        email, key = keys[login]
        writer.writerow([uid, login, email, key])
digest = hashlib.sha256(out.getvalue().encode("utf-8")).hexdigest() if not missing_keys else "unavailable"
print("\t".join(map(str, [len(students), len(remove), len(create), len(missing_keys), len(extra_keys), digest])))'

DB_SUMMARY_PY='import base64,contextlib,csv,io,sys
sys.path.insert(0, sys.argv[2])
from scripts.build_students_merged import read_canvas
with contextlib.redirect_stdout(io.StringIO()):
    students = read_canvas(sys.argv[1])
lines = sys.stdin.buffer.read().splitlines()
if len(lines) != 2:
    raise SystemExit("Portal inventory returned an invalid payload")
keys = {}
for row in csv.reader(io.StringIO(base64.b64decode(lines[0]).decode("utf-8"))):
    if row:
        keys[row[0].strip().lower()] = (row[1].strip(), row[2].strip())
expected = sorted((login, uid, keys.get(login, ("", ""))[1]) for uid, login in students)
db_text = base64.b64decode(lines[1]).decode("utf-8") if lines[1] else ""
actual = sorted(tuple(row.split("\t")) for row in db_text.splitlines() if row)
exact = actual == expected
print("\t".join(map(str, [len(actual), 0 if exact else len(actual), 0 if exact else len(expected), int(exact)])))'

# A missing host key is the usual first failure on a new machine. BatchMode
# turns it into "Host key verification failed", which does not say what to do.
# Name the host and give the exact command that fixes it.
check_host() {
    local host="$1" label="$2" probe="$3"
    if ! ssh "${SSH_OPTIONS[@]}" "$host" "$probe" >/dev/null 2>&1; then
        if ! ssh "${SSH_OPTIONS[@]}" "$host" true >/dev/null 2>&1; then
            die "Cannot reach $label on $host.
  Check the host key first. This shell may not have it yet:
      ssh-keyscan -H ${host#*@} >> ~/.ssh/known_hosts
  Then check that your key is loaded:
      ssh $host true"
        fi
        die "Reached $host, but $label did not answer.
  The host is up and your key works, so look at the service itself."
    fi
}

require_remote_access() {
    check_host "$HEADSCALE_HOST" "Headscale" \
        "cd '$HEADSCALE_DIR' && test -x '$HEADSCALE_SCRIPT' && docker compose exec -T headscale headscale users list -o json"
    check_host "$PORTAL_HOST" "the production portal" \
        "cd '$PORTAL_DIR' && test -f '$PORTAL_COMPOSE' &&
        docker compose -f '$PORTAL_COMPOSE' exec -T db sh -c \
        'export MYSQL_PWD=\"\$MYSQL_PASSWORD\"; mysqladmin ping -h localhost -u\"\$MYSQL_USER\" --silent'"
}

fetch_headscale_payload() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" "cd '$HEADSCALE_DIR' &&
        set -o pipefail &&
        docker compose exec -T headscale headscale users list -o json | base64 -w0 && printf '\n' &&
        if test -f students_with_keys.csv; then base64 -w0 students_with_keys.csv; fi && printf '\n'"
}

fetch_headscale_keys_b64() {
    local payload
    payload="$(ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" "cd '$HEADSCALE_DIR' &&
        if test -f students_with_keys.csv; then base64 -w0 students_with_keys.csv; fi")" \
        || return 1
    printf '%s\n' "$payload"
}

read_headscale_summary() {
    local summary
    summary="$(fetch_headscale_payload | "$PYTHON" -c "$HEADSCALE_SUMMARY_PY" \
        "$CANVAS" "$REPO_ROOT" "$HEADSCALE_KEEP_USER,$HEADSCALE_STAFF")" \
        || die "Could not compare the Canvas and Headscale rosters."
    IFS=$'\t' read -r ROSTER_COUNT USERS_REMOVE USERS_CREATE KEYS_MISSING KEYS_EXTRA DESIRED_HASH <<< "$summary"
}

run_headscale_helper() {
    local mode=""
    local runner_q keep_user_q staff_q domain_q key_days_q
    [[ "${1:-}" == "--apply" ]] && mode="--apply"
    printf -v runner_q '%q' "$HEADSCALE_MEMFD_RUNNER"
    printf -v keep_user_q '%q' "$HEADSCALE_KEEP_USER"
    printf -v staff_q '%q' "$HEADSCALE_STAFF"
    printf -v domain_q '%q' "$EMAIL_DOMAIN"
    printf -v key_days_q '%q' "$KEY_DAYS"
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' &&
        HEADSCALE_KEEP_USER=$keep_user_q HEADSCALE_STAFF=$staff_q \
        EMAIL_DOMAIN=$domain_q KEY_DAYS=$key_days_q \
        python3 -c $runner_q '$HEADSCALE_SCRIPT' $mode" < "$CANVAS"
}

verify_helper_count() {
    local output="$1"
    local helper_remove
    if [[ "$output" =~ to\ remove:\ ([0-9]+)\ users ]]; then
        helper_remove="${BASH_REMATCH[1]}"
    else
        die "The Headscale helper did not report its removal count."
    fi
    [[ "$helper_remove" == "$USERS_REMOVE" ]] \
        || die "Headscale counts disagree: helper=$helper_remove orchestrator=$USERS_REMOVE."
}

backup_headscale() {
    local db_backup="$REMOTE_BACKUP_DIR/headscale-db-$STAMP.sqlite"
    local keys_backup="$REMOTE_BACKUP_DIR/headscale-keys-$STAMP.csv"
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" "set -e
        umask 077
        mkdir -p '$REMOTE_BACKUP_DIR'
        chmod 700 '$REMOTE_BACKUP_DIR'
        cd '$HEADSCALE_DIR'
        docker compose stop headscale >/dev/null
        trap 'docker compose start headscale >/dev/null 2>&1 || true' EXIT
        test -s '$HEADSCALE_DB'
        cp '$HEADSCALE_DB' '$db_backup'
        chmod 600 '$db_backup'
        if test -f students_with_keys.csv; then
            cp students_with_keys.csv '$keys_backup'
            chmod 600 '$keys_backup'
        fi
        docker compose start headscale >/dev/null
        trap - EXIT
        test -s '$db_backup'
        for attempt in \$(seq 1 30); do
            docker compose exec -T headscale headscale users list -o json >/dev/null 2>&1 && exit 0
            sleep 1
        done
        exit 1" || die "The Headscale backup or restart failed."
    printf '  backup: %s:%s\n' "$HEADSCALE_HOST" "$db_backup"
    printf '  key backup: %s:%s\n' "$HEADSCALE_HOST" "$keys_backup"
}

portal_mysql() {
    local sql="$1"
    ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" "cd '$PORTAL_DIR' &&
        docker compose -f '$PORTAL_COMPOSE' exec -T db sh -c \
        'export MYSQL_PWD=\"\$MYSQL_PASSWORD\"; exec mysql --batch --skip-column-names -u\"\$MYSQL_USER\" \"\$MYSQL_DATABASE\"'" <<< "$sql"
}

portal_mysql_file() {
    local file="$1"
    ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" "cd '$PORTAL_DIR' &&
        docker compose -f '$PORTAL_COMPOSE' exec -T db sh -c \
        'export MYSQL_PWD=\"\$MYSQL_PASSWORD\"; exec mysql --batch -u\"\$MYSQL_USER\" \"\$MYSQL_DATABASE\"'" < "$file"
}

fetch_portal_whitelist_b64() {
    local rows
    rows="$(portal_mysql "SELECT login_id, sis_user_id, COALESCE(headscale_key, '') FROM student_whitelist ORDER BY login_id;")" \
        || return 1
    printf '%s' "$rows" | base64 -w0
    printf '\n'
}

read_db_summary() {
    local summary
    summary="$({ fetch_headscale_keys_b64; fetch_portal_whitelist_b64; } \
        | "$PYTHON" -c "$DB_SUMMARY_PY" "$CANVAS" "$REPO_ROOT")" \
        || die "Could not compare the portal whitelist with the roster."
    IFS=$'\t' read -r WHITELIST_CURRENT WHITELIST_DELETE WHITELIST_SEED WHITELIST_EXACT <<< "$summary"
}

file_hash_local() {
    "$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"
}

file_hash_portal() {
    ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" \
        "test -f '$PORTAL_DIR/data/students_merged.csv' && sha256sum '$PORTAL_DIR/data/students_merged.csv' | cut -d' ' -f1 || printf missing"
}

build_merged_file() {
    TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/term-changeover.XXXXXX")"
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" "cat '$HEADSCALE_DIR/students_with_keys.csv'" > "$TMP_DIR/students_with_keys.csv"
    chmod 600 "$TMP_DIR/students_with_keys.csv"
    "$PYTHON" "$BUILD_MERGED" "$CANVAS" "$TMP_DIR/students_with_keys.csv" "$TMP_DIR/students_merged.csv"
    local count
    count="$("$PYTHON" -c 'import csv,sys; print(sum(1 for _ in csv.DictReader(open(sys.argv[1], encoding="utf-8"))))' "$TMP_DIR/students_merged.csv")"
    [[ "$count" == "$ROSTER_COUNT" ]] || die "The merged file has $count rows, not $ROSTER_COUNT."
    [[ "$(file_hash_local "$TMP_DIR/students_merged.csv")" == "$DESIRED_HASH" ]] \
        || die "The merged file does not match the inspected roster."
}

replace_roster_files() {
    local new_file="$TMP_DIR/students_merged.csv"
    local local_hash="missing"
    local remote_hash
    [[ -f "$LOCAL_MERGED" ]] && local_hash="$(file_hash_local "$LOCAL_MERGED")"
    remote_hash="$(file_hash_portal)"

    if [[ "$local_hash" != "$DESIRED_HASH" ]]; then
        local local_backup="$REPO_ROOT/.git/term-changeover-backups/$STAMP/students_merged.csv"
        mkdir -p "$(dirname "$local_backup")"
        if [[ -f "$LOCAL_MERGED" ]]; then
            cp "$LOCAL_MERGED" "$local_backup"
            chmod 600 "$local_backup"
            printf '  local roster backup: %s\n' "$local_backup"
        fi
        cp "$new_file" "$LOCAL_MERGED"
    fi

    if [[ "$remote_hash" != "$DESIRED_HASH" ]]; then
        local remote_backup="$REMOTE_BACKUP_DIR/students_merged-$STAMP.csv"
        local remote_tmp="$PORTAL_DIR/data/.students_merged-$STAMP.tmp"
        ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" "set -e
            umask 077
            mkdir -p '$REMOTE_BACKUP_DIR'
            chmod 700 '$REMOTE_BACKUP_DIR'
            if test -f '$PORTAL_DIR/data/students_merged.csv'; then
                cp '$PORTAL_DIR/data/students_merged.csv' '$remote_backup'
                chmod 600 '$remote_backup'
            fi"
        printf '  portal roster backup: %s:%s\n' "$PORTAL_HOST" "$remote_backup"
        scp "${SSH_OPTIONS[@]}" "$new_file" "$PORTAL_HOST:$remote_tmp" >/dev/null
        # The roster carries reusable VPN keys: readable by its owner and by the
        # app container's group (APP_GID), not by other accounts on the host.
        ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" "set -e
            chgrp $APP_GID '$remote_tmp'
            chmod 640 '$remote_tmp'
            mv '$remote_tmp' '$PORTAL_DIR/data/students_merged.csv'
            test \"\$(sha256sum '$PORTAL_DIR/data/students_merged.csv' | cut -d' ' -f1)\" = '$DESIRED_HASH'"
    fi
}

backup_portal_database() {
    DB_BACKUP="$REMOTE_BACKUP_DIR/pre-term-changeover-$STAMP.sql"
    ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" "set -e
        umask 077
        cd '$PORTAL_DIR'
        mkdir -p '$REMOTE_BACKUP_DIR'
        chmod 700 '$REMOTE_BACKUP_DIR'
        docker compose -f '$PORTAL_COMPOSE' exec -T db sh -c \
          'export MYSQL_PWD=\"\$MYSQL_PASSWORD\"; exec mysqldump --single-transaction -u\"\$MYSQL_USER\" \"\$MYSQL_DATABASE\"' > '$DB_BACKUP'
        test -s '$DB_BACKUP'
        chmod 600 '$DB_BACKUP'" || die "The production database backup failed."
    printf '  database backup: %s:%s\n' "$PORTAL_HOST" "$DB_BACKUP"
}

wait_for_portal() {
    local status
    for _ in $(seq 1 90); do
        status="$(ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" \
            "docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' '$PORTAL_APP_CONTAINER'" 2>/dev/null || true)"
        [[ "$status" == "healthy" ]] && return 0
        sleep 1
    done
    die "The portal container did not become healthy."
}

purge_and_seed_database() {
    local before_vms after_vms counts
    before_vms="$(portal_mysql "SELECT vm_records.id FROM vm_records JOIN users ON users.id=vm_records.created_by_user_id WHERE users.is_admin=1 AND vm_records.destroyed=0 ORDER BY vm_records.id;")"
    backup_portal_database
    portal_mysql_file "$PURGE_SQL"

    counts="$(portal_mysql "SELECT (SELECT COUNT(*) FROM student_whitelist),(SELECT COUNT(*) FROM users WHERE is_admin=0),(SELECT COUNT(*) FROM users WHERE is_admin=1),(SELECT COUNT(*) FROM student_groups),(SELECT COUNT(*) FROM student_group_members),(SELECT COUNT(*) FROM student_lab_assignments),(SELECT COUNT(*) FROM koth_games);")"
    local white students admins groups members assignments games
    IFS=$'\t' read -r white students admins groups members assignments games <<< "$counts"
    [[ "$white" == 0 && "$students" == 0 && "$groups" == 0 && "$members" == 0 && "$assignments" == 0 && "$games" == 0 ]] \
        || die "The purge left student or KOTH rows."
    ((admins > 0)) || die "The purge left no administrator."

    after_vms="$(portal_mysql "SELECT vm_records.id FROM vm_records JOIN users ON users.id=vm_records.created_by_user_id WHERE users.is_admin=1 AND vm_records.destroyed=0 ORDER BY vm_records.id;")"
    [[ "$after_vms" == "$before_vms" ]] || die "The purge changed an administrator VM."

    # Replace the mounted CSV before this restart. Otherwise init_db restores the old roster.
    [[ "$(file_hash_portal)" == "$DESIRED_HASH" ]] \
        || die "The production roster changed before the restart."
    ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" "cd '$PORTAL_DIR' && docker compose -f '$PORTAL_COMPOSE' restart vm-automation >/dev/null"
    wait_for_portal
    read_db_summary
    [[ "$WHITELIST_EXACT" == 1 ]] \
        || die "The restarted portal did not seed the exact $ROSTER_COUNT-row whitelist."
}

printf 'Mode: %s\n' "$([[ "$APPLY" == 1 ]] && printf APPLY || printf 'DRY RUN (read-only)')"
printf 'Canvas export: %s\n\n' "$CANVAS"
require_remote_access

read_headscale_summary
helper_output="$(run_headscale_helper)" || die "The Headscale helper dry run failed."
verify_helper_count "$helper_output"

printf '[1/3] Headscale roster\n'
printf '  users to remove: %s\n' "$USERS_REMOVE"
printf '  users to create: %s\n' "$USERS_CREATE"
printf '  roster rows: %s\n' "$ROSTER_COUNT"
if ((USERS_REMOVE == 0 && USERS_CREATE == 0 && KEYS_MISSING == 0 && KEYS_EXTRA == 0)); then
    printf '  nothing to do: users and the protected key file match.\n'
elif ((APPLY == 0)); then
    printf '  would replace keys for all %s roster users.\n' "$ROSTER_COUNT"
else
    STAMP="$(date +%Y%m%d-%H%M%S)"
    backup_headscale
    run_headscale_helper --apply
    read_headscale_summary
    ((USERS_REMOVE == 0 && USERS_CREATE == 0 && KEYS_MISSING == 0 && KEYS_EXTRA == 0)) \
        || die "Headscale did not reach the requested state."
    printf '  verified: users and keys match the %s-row roster.\n' "$ROSTER_COUNT"
fi

[[ "$DESIRED_HASH" != "unavailable" ]] \
    || { printf '\n'; die "The Headscale key file cannot build a complete portal roster."; }

local_hash="missing"
[[ -f "$LOCAL_MERGED" ]] && local_hash="$(file_hash_local "$LOCAL_MERGED")"
remote_hash="$(file_hash_portal)"
printf '\n[2/3] Mounted roster file\n'
if [[ "$local_hash" == "$DESIRED_HASH" && "$remote_hash" == "$DESIRED_HASH" ]]; then
    printf '  nothing to do: local and production files match %s rows.\n' "$ROSTER_COUNT"
else
    [[ "$local_hash" == "$DESIRED_HASH" ]] || printf '  local file to replace: 1\n'
    [[ "$remote_hash" == "$DESIRED_HASH" ]] || printf '  production file to replace: 1\n'
    if ((APPLY == 1)); then
        [[ -n "$STAMP" ]] || STAMP="$(date +%Y%m%d-%H%M%S)"
        build_merged_file
        replace_roster_files
        [[ "$(file_hash_local "$LOCAL_MERGED")" == "$DESIRED_HASH" ]] \
            || die "The local roster verification failed."
        [[ "$(file_hash_portal)" == "$DESIRED_HASH" ]] \
            || die "The production roster verification failed."
        printf '  verified: both roster files match.\n'
    fi
fi

read_db_summary
printf '\n[3/3] Production database\n'
printf '  whitelist rows to delete: %s\n' "$WHITELIST_DELETE"
printf '  whitelist rows to seed: %s\n' "$WHITELIST_SEED"
if [[ "$WHITELIST_EXACT" == 1 ]]; then
    printf '  nothing to do: the whitelist has the exact %s rows.\n' "$ROSTER_COUNT"
elif ((APPLY == 0)); then
    printf '  would back up, purge, restart, and verify the portal.\n'
else
    [[ "$(file_hash_portal)" == "$DESIRED_HASH" ]] \
        || die "Refusing to purge before the production roster file is replaced."
    [[ -n "$STAMP" ]] || STAMP="$(date +%Y%m%d-%H%M%S)"
    purge_and_seed_database
    printf '  verified: the whitelist has the exact %s rows.\n' "$ROSTER_COUNT"
fi

printf '\n'
if ((APPLY == 0)); then
    if ((USERS_REMOVE == 0 && USERS_CREATE == 0 && KEYS_MISSING == 0 && KEYS_EXTRA == 0)) \
       && [[ "$local_hash" == "$DESIRED_HASH" && "$remote_hash" == "$DESIRED_HASH" && "$WHITELIST_EXACT" == 1 ]]; then
        printf 'Changeover complete. Every stage has nothing to do.\n'
    else
        printf 'No changes were made. Review the counts, then re-run with --apply.\n'
    fi
else
    printf 'Automated changeover complete. No VM, template, or dojo was changed.\n'
    printf 'Human action: Review data/students_merged.csv.\n'
    printf 'Human action: Commit and push that credential file only to this private repository.\n'
fi
