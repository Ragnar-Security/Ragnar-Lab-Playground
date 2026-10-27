#!/usr/bin/env bash
# Rotate students' reusable Headscale pre-auth keys, mid-term or at term end.
#
#   scripts/rotate_headscale_keys.sh                       # dry run: plan only
#   scripts/rotate_headscale_keys.sh --apply               # asks to confirm
#   scripts/rotate_headscale_keys.sh --apply --yes --only <login>
#   scripts/rotate_headscale_keys.sh --apply --yes --allow-partial     # exit 0 despite logins unknown to the portal
#   scripts/rotate_headscale_keys.sh --apply --yes --break-stale-lock  # take over a lock older than 6 hours
#
# The work happens in rotate_headscale_keys.py. This wrapper loads site.env the
# way the other operator scripts do, so all of them read one configuration and
# stop before contacting a host when a value is missing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/site_env.sh"

REQUIRED=(PORTAL_HOST PORTAL_DIR PORTAL_COMPOSE HEADSCALE_HOST HEADSCALE_DIR
    HEADSCALE_KEEP_USER HEADSCALE_STAFF KEY_DAYS REMOTE_BACKUP_DIR)

# Same interpreter choice as term_changeover.sh: under WSL a Windows python.exe
# cannot open /mnt/c paths. The helper needs the standard library only.
if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif [[ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
    PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
else
    PYTHON="$(command -v python || true)"
fi
[[ -n "$PYTHON" && -x "$PYTHON" ]] || { echo "ERROR: python3 not found" >&2; exit 1; }

for arg in "$@"; do
    [[ "$arg" == "-h" || "$arg" == "--help" ]] && exec "$PYTHON" "$SCRIPT_DIR/rotate_headscale_keys.py" --help
done

SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" "${REQUIRED[@]}"
for name in "${REQUIRED[@]}"; do
    require_site_var "$name"
done
for name in PORTAL_DIR HEADSCALE_DIR REMOTE_BACKUP_DIR; do
    [[ "${!name}" == /* ]] \
        || { printf 'ERROR: %s must be an absolute path\n' "$name" >&2; exit 1; }
done
export "${REQUIRED[@]}"

exec "$PYTHON" "$SCRIPT_DIR/rotate_headscale_keys.py" "$@"
