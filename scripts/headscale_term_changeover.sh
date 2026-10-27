#!/bin/bash
# Swap the headscale roster from one term to the next. Run ON the headscale
# droplet, from /opt/headscale.
#
#   headscale_term_changeover.sh <roster.csv> [--apply]
#
# Without --apply it only reports. Nothing is deleted or created.
#
# The roster is a Canvas gradebook export. The login comes from the
# "SIS Login ID" column.
#
# WHAT IT PROTECTS
#   The user named in HEADSCALE_KEEP_USER owns persistent infrastructure.
#   HEADSCALE_STAFF lists staff accounts that survive a term change.
#   The script never removes these users.
#   The script refuses to run if it cannot find HEADSCALE_KEEP_USER.
#
# WHAT IT REMOVES
#   It removes every other user who is not in the incoming roster.
#   A user's nodes go when the user goes.
#   Thus, a stale laptop from last term does not remain.
#
# WHAT IT CREATES
#   One user per student in the roster, plus one REUSABLE pre-auth key each.
#   Reusable matters: a student reinstalls, or joins from a laptop and a VM,
#   and the same key has to work again.
#
# It writes students_with_keys.csv as login,email,preauth_key. That file holds
# live credentials. Keep it out of git.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"

# The orchestrator exports these values over SSH; preserve those explicit
# values if a site.env also exists on the Headscale host.
KEEP_OVERRIDE_SET="${HEADSCALE_KEEP_USER+x}"
KEEP_OVERRIDE="${HEADSCALE_KEEP_USER-}"
STAFF_OVERRIDE_SET="${HEADSCALE_STAFF+x}"
STAFF_OVERRIDE="${HEADSCALE_STAFF-}"
DOMAIN_OVERRIDE_SET="${EMAIL_DOMAIN+x}"
DOMAIN_OVERRIDE="${EMAIL_DOMAIN-}"
DAYS_OVERRIDE_SET="${KEY_DAYS+x}"
DAYS_OVERRIDE="${KEY_DAYS-}"
if [[ -f "$SITE_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090 -- the operator chooses this untracked path.
    source "$SITE_ENV_FILE" || exit 1
    set +a
fi
[[ -n "$KEEP_OVERRIDE_SET" ]] && HEADSCALE_KEEP_USER="$KEEP_OVERRIDE"
[[ -n "$STAFF_OVERRIDE_SET" ]] && HEADSCALE_STAFF="$STAFF_OVERRIDE"
[[ -n "$DOMAIN_OVERRIDE_SET" ]] && EMAIL_DOMAIN="$DOMAIN_OVERRIDE"
[[ -n "$DAYS_OVERRIDE_SET" ]] && KEY_DAYS="$DAYS_OVERRIDE"

require_var() {
    local name="$1"
    if [[ -z "${!name:-}" ]]; then
        printf 'ERROR: required configuration variable %s is unset; set it in the environment or scripts/site.env\n' "$name" >&2
        exit 1
    fi
}
for name in HEADSCALE_KEEP_USER HEADSCALE_STAFF EMAIL_DOMAIN KEY_DAYS; do
    require_var "$name"
done

ROSTER="${1:?usage: headscale_term_changeover.sh <roster.csv> [--apply]}"
APPLY=0
[[ "${2:-}" == "--apply" ]] && APPLY=1

KEEP_USER="$HEADSCALE_KEEP_USER"
IFS=',' read -r -a PROTECTED_STAFF <<< "$HEADSCALE_STAFF"
for index in "${!PROTECTED_STAFF[@]}"; do
  staff="${PROTECTED_STAFF[index]}"
  staff="${staff#"${staff%%[![:space:]]*}"}"
  staff="${staff%"${staff##*[![:space:]]}"}"
  [ -n "$staff" ] || { echo "HEADSCALE_STAFF contains an empty login" >&2; exit 1; }
  PROTECTED_STAFF[index]="$staff"
done
OUT="students_with_keys.csv"

H() { docker compose exec -T headscale headscale "$@"; }

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
[ -f "$ROSTER" ] || { echo "no roster at $ROSTER" >&2; exit 1; }

# --- read the roster -------------------------------------------------------
# Canvas adds a synthetic "Test Student" with no SIS User ID and a 40-character
# digest for a login. Drop it, and drop the "Points Possible" metadata row.
mapfile -t LOGINS < <(python3 - "$ROSTER" <<'PY'
import csv, sys
seen = []
for r in csv.DictReader(open(sys.argv[1], encoding='utf-8-sig')):
    login = (r.get('SIS Login ID') or '').strip().lower()
    name  = (r.get('Student') or '').strip().lower()
    uid   = (r.get('SIS User ID') or '').strip()
    if not login or 'points possible' in name:
        continue
    if name in ('student, test', 'test student'):
        continue
    if not uid and len(login) == 40 and all(c in '0123456789abcdef' for c in login):
        continue
    if login not in seen:
        seen.append(login)
print('\n'.join(seen))
PY
)
echo "roster: ${#LOGINS[@]} students"
[ "${#LOGINS[@]}" -gt 0 ] || { echo "roster produced no logins" >&2; exit 1; }

# --- inspect what is there now --------------------------------------------
USERS_JSON="$(H users list -o json 2>/dev/null)"
KEEP_ID="$(echo "$USERS_JSON" | jq -r --arg n "$KEEP_USER" '.[] | select(.name==$n) | .id')"
if [ -z "$KEEP_ID" ] || [ "$KEEP_ID" = "null" ]; then
  echo "REFUSING: no user named '$KEEP_USER'. It owns persistent infrastructure." >&2
  echo "Check the name before running this, or the lab network goes down." >&2
  exit 1
fi
echo "protected: $KEEP_USER (id $KEEP_ID)"
printf 'protected staff: %s\n' "${PROTECTED_STAFF[*]}"

# Doomed = every user who is not protected and is not in the incoming roster.
#
# Excluding the roster makes this script safe to run twice.
# A partial first run can leave new students in Headscale.
# This filter keeps the second run from deleting those students and their keys.
ROSTER_JSON="$(printf '%s\n' "${LOGINS[@]}" | jq -R . | jq -s .)"
STAFF_JSON="$(printf '%s\n' "${PROTECTED_STAFF[@]}" | jq -R . | jq -s .)"
mapfile -t DOOMED < <(echo "$USERS_JSON" | jq -r \
  --arg n "$KEEP_USER" --argjson keep "$ROSTER_JSON" --argjson staff "$STAFF_JSON" \
  '.[] | select(.name != $n) | select([.name] | inside($keep) | not) | select([.name] | inside($staff) | not) | "\(.id)|\(.name)"')
echo "to remove: ${#DOOMED[@]} users (roster, $KEEP_USER, and staff are excluded)"

if [ "$APPLY" -eq 0 ]; then
  echo
  echo "DRY RUN. Re-run with --apply to make these changes:"
  if [ "${#DOOMED[@]}" -gt 0 ]; then
    printf '  remove user %s\n' "${DOOMED[@]:0:5}"
    [ "${#DOOMED[@]}" -gt 5 ] && echo "  ... and $(( ${#DOOMED[@]} - 5 )) more"
  fi
  printf '  create user %s\n' "${LOGINS[@]:0:5}"
  [ "${#LOGINS[@]}" -gt 5 ] && echo "  ... and $(( ${#LOGINS[@]} - 5 )) more"
  exit 0
fi

# --- remove the previous term ---------------------------------------------
# headscale refuses to destroy a user that still owns nodes:
#   Cannot destroy user: user not empty: node(s) found
# So the nodes go first. This is wanted anyway: last term's laptops should
# not linger in the tailnet. Without this step only the
# users that already have no nodes are removed and everyone else survives.
for entry in "${DOOMED[@]}"; do
  id="${entry%%|*}"; name="${entry##*|}"

  mapfile -t NODE_IDS < <(H nodes list -o json 2>/dev/null \
                          | jq -r --arg u "$name" '.[] | select(.user.name==$u) | .id')
  for nid in "${NODE_IDS[@]}"; do
    [ -n "$nid" ] || continue
    H nodes delete --identifier "$nid" --force >/dev/null 2>&1 \
      || echo "    could not delete node $nid of $name"
  done

  if H users destroy --identifier "$id" --force >/dev/null 2>&1; then
    echo "  removed $name (${#NODE_IDS[@]} node(s))"
  else
    echo "  FAILED to remove $name (id $id)"
  fi
done

# --- create the new term ---------------------------------------------------
: > "$OUT"
chmod 600 "$OUT"
created=0
for login in "${LOGINS[@]}"; do
  uid="$(H users create "$login" --email "${login}@${EMAIL_DOMAIN}" -o json 2>/dev/null | jq -r '.id')"
  if [ -z "$uid" ] || [ "$uid" = "null" ]; then
    uid="$(H users list -o json 2>/dev/null | jq -r --arg n "$login" '.[] | select(.name==$n) | .id')"
  fi
  if [ -z "$uid" ] || [ "$uid" = "null" ]; then
    echo "  FAILED to create $login"; continue
  fi
  key="$(H preauthkeys create --user "$uid" --reusable --expiration "$KEY_DAYS" 2>/dev/null | tr -d '\r' | tail -1)"
  if [ -z "$key" ]; then echo "  FAILED to key $login"; continue; fi
  echo "${login},${login}@${EMAIL_DOMAIN},${key}" >> "$OUT"
  created=$((created+1))
done

echo
echo "created $created of ${#LOGINS[@]} students; keys in $(pwd)/$OUT"
echo "final user count: $(H users list -o json 2>/dev/null | jq 'length')"
echo "nodes remaining:  $(H nodes list -o json 2>/dev/null | jq 'length')"
