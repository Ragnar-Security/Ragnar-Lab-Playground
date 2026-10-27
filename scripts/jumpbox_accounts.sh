#!/usr/bin/env bash
#
# OPTIONAL student SSH accounts on the jumpbox. Runs ON the jumpbox, as root.
#
# Students do not need jumpbox accounts: Tailscale routes them straight to
# their lab VMs through the jumpbox. Create accounts only for a course that
# logs in to the jumpbox itself, and lock them when that course ends.
#
# Usage:
#   jumpbox_accounts.sh create <students.csv>
#       One account per "SIS Login ID" in a Canvas export, each with its own
#       random password that must be changed at first login. Passwords go to
#       JUMPBOX_CREDENTIALS_FILE (mode 0600, default ./jumpbox-credentials.csv)
#       and are never printed or passed on a command line.
#   jumpbox_accounts.sh lock-stale [--apply] [--keep user,user]
#       Lock (passwd -l) and expire (chage -E 0) every login account that has
#       never logged in. Expiry also blocks SSH key logins. Accounts that are
#       already locked are left alone. With --apply the affected names are
#       written to JUMPBOX_LOCK_LIST_DIR/locked-stale-accounts-<timestamp>.txt
#       (mode 0600, default dir /root), the input for unlock.
#   jumpbox_accounts.sh unlock <list-file> [--apply]
#       Undo lock-stale for every name in the list.
#
# lock-stale and unlock are dry runs unless --apply is given.

set -euo pipefail

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'; }

[ $# -ge 1 ] || { usage >&2; exit 2; }
MODE="$1"
shift
case "$MODE" in -h|--help) usage; exit 0 ;; esac
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

APPLY=0 KEEP="" ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --keep) [ $# -ge 2 ] || die "--keep needs a comma-separated list"; KEEP="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) [ -z "$ARG" ] || die "unexpected argument $1"; ARG="$1" ;;
    esac
    shift
done

run() {
    if [ "$APPLY" -eq 1 ]; then "$@"; else say "would run: $*"; fi
}

create_accounts() {
    local csv="$1" creds="${JUMPBOX_CREDENTIALS_FILE:-./jumpbox-credentials.csv}"
    local created=0 skipped=0 login_id password
    [ -f "$csv" ] || die "file not found: $csv"
    # Owner-only: this file holds the one-time initial passwords.
    (umask 077; touch "$creds")
    chmod 600 "$creds"
    say "Creating accounts from $csv (password change forced at first login)"
    while IFS=, read -r _ login_id || [ -n "$login_id" ]; do
        login_id="$(printf '%s' "$login_id" | tr -d '\r' | xargs)"
        [ -n "$login_id" ] && [ "$login_id" != "SIS Login ID" ] || continue
        if id "$login_id" >/dev/null 2>&1; then
            say "SKIP: $login_id already exists"
            skipped=$((skipped + 1))
            continue
        fi
        if ! useradd -m -s /bin/bash "$login_id"; then
            say "ERROR: could not create $login_id"
            continue
        fi
        password="$(openssl rand -base64 12)"
        # printf is a builtin, so the password is in no process listing.
        printf '%s:%s\n' "$login_id" "$password" | chpasswd
        chage -d 0 "$login_id"
        printf '%s,%s\n' "$login_id" "$password" >> "$creds"
        password=""
        say "CREATED: $login_id"
        created=$((created + 1))
    done < "$csv"
    say "Created: $created  Skipped: $skipped"
    say "Initial passwords: $creds (mode 0600). Deliver each one privately."
}

# Login accounts (UID 1000-59999 with a real shell) that never logged in and
# are not already locked.
stale_accounts() {
    local name uid shell status
    getent passwd | while IFS=: read -r name _ uid _ _ _ shell; do
        [ "$uid" -ge 1000 ] && [ "$uid" -lt 60000 ] || continue
        case "$shell" in */nologin|*/false|"") continue ;; esac
        # The operator's own account is never a candidate, even if lastlog
        # lost its records.
        case ",$KEEP,${SUDO_USER:-},${JUMPBOX_ADMIN_USER:-}," in *",$name,"*) continue ;; esac
        status="$(lastlog -u "$name" 2>/dev/null || true)"
        case "$status" in *"Never logged in"*) ;; *) continue ;; esac
        status="$(passwd -S "$name" 2>/dev/null || true)"
        case "$status" in "$name L "*) continue ;; esac
        printf '%s\n' "$name"
    done
}

lock_stale() {
    command -v lastlog >/dev/null || die "lastlog is not installed; cannot tell which accounts never logged in"
    local names list name count
    names="$(stale_accounts)"
    if [ -z "$names" ]; then
        say "No unlocked account has never logged in. Nothing to do."
        return 0
    fi
    count="$(printf '%s\n' "$names" | wc -l | tr -d ' ')"
    say "$count account(s) have never logged in:"
    # shellcheck disable=SC2086 # one name per line
    printf '  %s\n' $names
    if [ "$APPLY" -ne 1 ]; then
        say "Dry run only. Re-run with --apply to lock and expire them."
        return 0
    fi
    list="${JUMPBOX_LOCK_LIST_DIR:-/root}/locked-stale-accounts-$(date +%Y%m%d-%H%M%S).txt"
    (umask 077; : > "$list")
    for name in $names; do
        passwd -l "$name" >/dev/null
        chage -E 0 "$name"
        # Written per account, so a failure part-way still leaves an exact undo list.
        printf '%s\n' "$name" >> "$list"
    done
    say "Locked and expired $count account(s). Undo list: $list"
    say "Undo: $0 unlock $list --apply"
}

unlock() {
    local list="$1" name count=0
    [ -f "$list" ] || die "list file not found: $list"
    while IFS= read -r name || [ -n "$name" ]; do
        case "$name" in ""|\#*) continue ;; esac
        getent passwd "$name" >/dev/null || { say "SKIP: no such user $name"; continue; }
        run passwd -u "$name"
        run chage -E -1 "$name"
        count=$((count + 1))
    done < "$list"
    if [ "$APPLY" -eq 1 ]; then
        say "Unlocked $count account(s)."
    else
        say "Dry run only. Re-run with --apply to unlock $count account(s)."
    fi
}

case "$MODE" in
    create) [ -n "$ARG" ] || die "usage: $0 create <students.csv>"; create_accounts "$ARG" ;;
    lock-stale) [ -z "$ARG" ] || die "lock-stale takes no file argument"; lock_stale ;;
    unlock) [ -n "$ARG" ] || die "usage: $0 unlock <list-file> [--apply]"; unlock "$ARG" ;;
    -h|--help) usage ;;
    *) usage >&2; exit 2 ;;
esac
