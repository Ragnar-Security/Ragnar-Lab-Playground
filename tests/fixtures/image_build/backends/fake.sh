# Test backend for build_image.sh. Selected with IMAGE_BUILD_BACKENDS_DIR
# pointing here and backend name "fake".
#
# Every contract function appends "<function> <args>" to $CALL_LOG. Setting
# FAKE_FAIL=<function name> makes that function fail. Its "cloud" is the
# fake-cloud command the tests put on PATH, reached only through the core's
# mutate/query helpers, so a dry run must leave no fake-cloud line in CALL_LOG.

_fake() {
  local fn="$1"
  shift
  printf '%s %s\n' "$fn" "$*" >> "$CALL_LOG"
  if [ "${FAKE_FAIL:-}" = "$fn" ]; then
    echo "fake: $fn failed" >&2
    return 1
  fi
}

backend_check_config() {
  _fake backend_check_config || return 1
  if [ -z "${FAKE_REGION:-}" ]; then
    echo "fake: FAKE_REGION is not set" >&2
    return 1
  fi
}

backend_resolve_base() {
  _fake backend_resolve_base "$@" || return 1
  query fake-base-id fake-cloud resolve "$1"
}

backend_start() {
  _fake backend_start "$@" "expires=$BUILD_EXPIRES_UTC" || return 1
  [ -s "$3" ] || die "fake: public key file $3 is missing"
  BUILD_VM_ID="vm-$1"
  mutate fake-cloud start "$1" "$2" "image-build=$RUN_ID" "image-build-expires=$BUILD_EXPIRES_UTC"
  BUILD_SSH_HOST=192.0.2.10
  if [ "$OS_FAMILY" = windows ]; then
    BUILD_SSH_USER=Administrator
  else
    BUILD_SSH_USER=builder
  fi
  BUILD_SSH_JUMP="${FAKE_JUMP:-}"
}

backend_save() {
  _fake backend_save "$@" || return 1
  mutate fake-cloud save "$BUILD_VM_ID" "$1"
  IMAGE_ID="img-$1"
  if [ "${FAKE_SAVE_CONSUMES_VM:-}" = 1 ]; then
    BUILD_VM_ID=""
  fi
}

backend_destroy() {
  _fake backend_destroy "$BUILD_VM_ID" || return 1
  [ -n "$BUILD_VM_ID" ] || return 0
  mutate fake-cloud destroy "$BUILD_VM_ID"
  BUILD_VM_ID=""
}

backend_leftovers() {
  _fake backend_leftovers || return 1
  query "" fake-cloud leftovers
}

backend_destroy_id() {
  _fake backend_destroy_id "$@" || return 1
  mutate fake-cloud destroy "$1"
}

backend_templates_yaml() {
  _fake backend_templates_yaml "$@" || return 1
  printf 'fake: {image: %s, disk_gb: %s}\n' "$1" "$DISK_GB"
}
