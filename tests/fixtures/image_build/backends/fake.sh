# Test backend for build_image.sh. Selected with IMAGE_BUILD_BACKENDS_DIR
# pointing here and backend name "fake".
#
# Every contract function appends "<function> <args>" to $CALL_LOG.
#   FAKE_FAIL=<function>     that function returns FAKE_FAIL_RC (default 1) at once.
#   FAKE_MIDFAIL=<function>  a step in the middle of that function fails and
#                            the function carries on; only errexit stops it.
#                            Proves the core never calls a backend function
#                            where bash ignores set -e (if/||/&&).
# Its "cloud" is the fake-cloud command the tests put on PATH, reached only
# through the core's mutate/query helpers, so a dry run must leave no
# fake-cloud line in CALL_LOG (FAKE_MIDFAIL's step is the one exception).
#
# Config: FAKE_REGION (logged by backend_check_config as region=<value>).
BACKEND_CONFIG_PREFIX=FAKE_

_fake() {
  local fn="$1"
  shift
  printf '%s %s\n' "$fn" "$*" >> "$CALL_LOG"
  if [ "${FAKE_FAIL:-}" = "$fn" ]; then
    echo "fake: $fn failed" >&2
    return "${FAKE_FAIL_RC:-1}"
  fi
}

# Call as a plain statement: with errexit in force, a failure here ends the
# function; without it, the function goes on and reports success.
_midfail() {
  if [ "${FAKE_MIDFAIL:-}" = "$1" ]; then
    fake-cloud fail "$1"
  fi
}

backend_check_config() {
  _fake backend_check_config "region=${FAKE_REGION:-}" || return
  _midfail backend_check_config
  if [ -z "${FAKE_REGION:-}" ]; then
    echo "fake: FAKE_REGION is not set" >&2
    return 1
  fi
}

backend_resolve_base() {
  _fake backend_resolve_base "$@" || return
  _midfail backend_resolve_base
  query fake-base-id fake-cloud resolve "$1"
}

backend_start() {
  _fake backend_start "$@" "expires=$BUILD_EXPIRES_UTC" || return
  [ -s "$3" ] || die "fake: public key file $3 is missing"
  BUILD_VM_ID="vm-$1"
  mutate fake-cloud start "$1" "$2" "image-build=$RUN_ID" "image-build-expires=$BUILD_EXPIRES_UTC"
  _midfail backend_start
  BUILD_SSH_HOST=192.0.2.10
  if [ "$OS_FAMILY" = windows ]; then
    BUILD_SSH_USER=Administrator
  else
    BUILD_SSH_USER=builder
  fi
  BUILD_SSH_JUMP="${FAKE_JUMP:-}"
}

backend_save() {
  _fake backend_save "$@" || return
  _midfail backend_save
  mutate fake-cloud save "$BUILD_VM_ID" "$1"
  IMAGE_ID="img-$1"
  if [ "${FAKE_SAVE_CONSUMES_VM:-}" = 1 ]; then
    BUILD_VM_ID=""
  fi
}

backend_destroy() {
  _fake backend_destroy "$BUILD_VM_ID" || return
  [ -n "$BUILD_VM_ID" ] || return 0
  mutate fake-cloud destroy "$BUILD_VM_ID"
  BUILD_VM_ID=""
}

backend_leftovers() {
  _fake backend_leftovers || return
  _midfail backend_leftovers
  query "" fake-cloud leftovers
}

# Re-reads the tag and destroys only a build VM; anything else is refused.
backend_destroy_id() {
  local tag
  _fake backend_destroy_id "$@" || return
  _midfail backend_destroy_id
  tag="$(query image-build=ib-dry-run fake-cloud tag "$1")"
  case "$tag" in
    image-build=ib-*) ;;
    *) echo "fake: $1 is not a build VM (tag '$tag'); not destroyed" >&2
       return 1 ;;
  esac
  mutate fake-cloud destroy "$1"
}

backend_templates_yaml() {
  _fake backend_templates_yaml "$@" || return
  printf 'fake: {image: %s, disk_gb: %s}\n' "$1" "$DISK_GB"
}
