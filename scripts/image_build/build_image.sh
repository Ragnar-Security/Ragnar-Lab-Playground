#!/usr/bin/env bash
# Build a VM image from a recipe on one backend (proxmox, aws or gcp).
#
# Usage:
#   build_image.sh [--dry-run] [--allow-dirty] <recipe.env> <backend>
#   build_image.sh --reap <backend> [--dry-run]
#
# Exit codes: 0 success, 2 usage or config error, 1 build failure.
#
# A build: start a build VM from the backend's base, upload the recipe's payload
# over SSH, run setup (SETUP_PASSES times, waiting for SSH after each because a
# setup may reboot), run verify, seal (the matching seal script from this
# directory), save the image, destroy the build VM. A trap destroys the build VM
# on every exit, so a failed or interrupted build leaves nothing running. Only a
# verified, sealed build is saved, logged and printed.
#
# --dry-run prints every command that would reach a VM or a cloud ("DRY-RUN:"
# on stderr) and runs none of them; queries print their command and return a
# placeholder. With --reap, --dry-run still runs the backend's read-only
# queries for real (a reap plan needs the real leftover list) and prints the
# destroys instead of running them.
#
# Stdout carries only the templates.yaml lines (or, for --reap, the reaped
# IDs); everything else goes to stderr.
#
# Inputs:
#   config.env  next to this script, or $IMAGE_BUILD_CONFIG. Untracked: it
#               holds hosts and cloud IDs. See config.env.example.
#   recipe.env  sourced. Required: IMAGE_PREFIX OS_FAMILY PAYLOAD_DIR SETUP
#               VERIFY BASE_PROXMOX BASE_AWS BASE_GCP DISK_GB AWS_INSTANCE_TYPE
#               GCP_MACHINE_TYPE PVE_MEMORY_MB PVE_CORES. Optional: SETUP_PASSES
#               (1), KEEP_SSH (no), GCP_IMAGE_FAMILY (IMAGE_PREFIX),
#               PAYLOAD_EXCLUDE (space-separated tar --exclude globs).
#               PAYLOAD_DIR is relative to the recipe; SETUP and VERIFY are
#               relative to PAYLOAD_DIR.
#   IMAGE_BUILD_TTL_MINUTES   expiry tagged on build resources (default 240).
#   IMAGE_BUILD_BACKENDS_DIR  where <backend>.sh lives (default: ./backends).
#
# Backend contract (<backend>.sh is sourced into this shell):
#   backend_check_config        non-zero, naming the missing variable, if the
#                               config lacks what this backend needs.
#   backend_resolve_base <pat>  prints exactly one base ID.
#   backend_start <run_id> <base_id> <pubkey_file>
#                               sets BUILD_VM_ID (as soon as anything exists),
#                               BUILD_SSH_HOST, BUILD_SSH_USER, BUILD_SSH_JUMP.
#   backend_save <image_name>   sets IMAGE_ID (a placeholder in a dry run);
#                               clears BUILD_VM_ID when the build VM itself
#                               became the image.
#   backend_destroy             idempotent; destroys only BUILD_VM_ID.
#   backend_leftovers           one ID per line, tagged and expired build VMs
#                               only. Must read through `query`.
#   backend_destroy_id <id>     re-checks the tag (through `query`), then
#                               destroys (through `mutate`).
#   backend_templates_yaml <image_id>  prints the templates.yaml lines.
# The base pattern passed to backend_resolve_base is the recipe's
# BASE_<BACKEND> (BASE_PROXMOX, BASE_AWS, BASE_GCP).
# Variables a backend may read: RUN_ID (ib-<yyyymmddhhmm>-<4 hex>),
# BUILD_EXPIRES_UTC (yyyymmddhhmm, UTC), IMAGE_NAME, IMAGE_BUILD_COMMIT,
# IMAGE_BUILD_RECIPE (recipe path within its repo), DRY_RUN (0|1), every
# recipe and config key.
# Helpers a backend uses: mutate <cmd...>, query <placeholder> <cmd...>,
# die <msg>. Tags on every build resource: image-build=$RUN_ID and
# image-build-expires=$BUILD_EXPIRES_UTC, never lab-playground-site.
set -euo pipefail
# Keep errexit inside $(...), where backends are often called.
shopt -s inherit_errexit 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="build_image.sh"

RECIPE_REQUIRED=(IMAGE_PREFIX OS_FAMILY PAYLOAD_DIR SETUP VERIFY BASE_PROXMOX BASE_AWS BASE_GCP
                 DISK_GB AWS_INSTANCE_TYPE GCP_MACHINE_TYPE PVE_MEMORY_MB PVE_CORES)
RECIPE_OPTIONAL=(SETUP_PASSES KEEP_SSH GCP_IMAGE_FAMILY PAYLOAD_EXCLUDE)
BACKEND_FUNCTIONS=(backend_check_config backend_resolve_base backend_start backend_save
                   backend_destroy backend_leftovers backend_destroy_id backend_templates_yaml)

DRY_RUN=0
QUERY_LIVE=0
CHILD_PID=""
TMP_DIR=""
STARTED=0
DESTROYED=0
LOGGED=0
STEP="startup"
BACKEND=""
BUILD_VM_ID=""
BUILD_SSH_HOST=""
BUILD_SSH_USER=""
BUILD_SSH_JUMP=""
IMAGE_ID=""

# --- helpers ----------------------------------------------------------------

log() { printf '[image-build] %s\n' "$*" >&2; }
die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
config_error() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }

usage() {
  cat >&2 <<'EOF'
Usage: build_image.sh [--dry-run] [--allow-dirty] <recipe.env> <backend>
       build_image.sh --reap <backend> [--dry-run]
EOF
  exit 2
}

# Run a command in the background and wait for it, so a signal is handled
# while it runs instead of after it returns. The trap kills CHILD_PID.
run_bg() {
  local rc=0
  "$@" <&0 &
  CHILD_PID=$!
  wait "$CHILD_PID" || rc=$?
  CHILD_PID=""
  return "$rc"
}

mutate() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY-RUN: %s\n' "$*" >&2
    return 0
  fi
  run_bg "$@"
}

query() {
  local placeholder="$1"
  shift
  if [ "$DRY_RUN" = 1 ] && [ "$QUERY_LIVE" != 1 ]; then
    printf 'DRY-RUN: %s\n' "$*" >&2
    printf '%s\n' "$placeholder"
    return 0
  fi
  "$@"
}

# POSIX single-quote one string for a remote shell.
sq() {
  local s="$1" q="'"
  s=${s//$q/$q\\$q$q}
  printf "'%s'" "$s"
}

is_posint() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }

# Format a Unix time as UTC yyyymmddhhmm (GNU date, then BSD date).
utc_stamp() {
  date -u -d "@$1" +%Y%m%d%H%M 2>/dev/null || date -u -r "$1" +%Y%m%d%H%M
}

load_backend() {
  local name="$1" dir="${IMAGE_BUILD_BACKENDS_DIR:-$SCRIPT_DIR/backends}" fn
  if ! [[ "$name" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || [ ! -f "$dir/$name.sh" ]; then
    local available=""
    if [ -d "$dir" ]; then
      available="$(cd "$dir" && for f in *.sh; do if [ -e "$f" ]; then printf '%s ' "${f%.sh}"; fi; done)"
    fi
    config_error "unknown backend '$name' (available in $dir: ${available:-none})"
  fi
  # shellcheck source=/dev/null
  source "$dir/$name.sh"
  for fn in "${BACKEND_FUNCTIONS[@]}"; do
    declare -F "$fn" >/dev/null || config_error "backend '$name' does not define $fn"
  done
  BACKEND="$name"
}

load_config() {
  local config="${IMAGE_BUILD_CONFIG:-$SCRIPT_DIR/config.env}"
  if [ -f "$config" ]; then
    # shellcheck source=/dev/null
    source "$config" || config_error "cannot read config $config"
  elif [ -n "${IMAGE_BUILD_CONFIG:-}" ]; then
    config_error "IMAGE_BUILD_CONFIG=$config does not exist"
  fi
  PVE_NODE="${PVE_NODE:-pve}"
  PVE_REPLICA_NODE="${PVE_REPLICA_NODE:-pve2}"
  if ! backend_check_config; then
    config_error "config for backend '$BACKEND' is incomplete (see above; config: $config)"
  fi
}

# --- remote layer ------------------------------------------------------------

SSH_OPTS=()
SSH_TARGET=""

ssh_setup() {
  [ -n "$BUILD_SSH_HOST" ] || die "backend_start set no BUILD_SSH_HOST"
  [ -n "$BUILD_SSH_USER" ] || die "backend_start set no BUILD_SSH_USER"
  SSH_TARGET="$BUILD_SSH_USER@$BUILD_SSH_HOST"
  SSH_OPTS=(-i "$TMP_DIR/key" -o IdentitiesOnly=yes -o BatchMode=yes
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
            -o ConnectTimeout=10 -o ServerAliveInterval=15 -o LogLevel=ERROR)
  if [ -n "$BUILD_SSH_JUMP" ]; then
    SSH_OPTS+=(-J "$BUILD_SSH_JUMP")
  fi
}

# One command line on the build VM, as the SSH user. Its output goes to stderr.
rssh() { mutate ssh -n "${SSH_OPTS[@]}" "$SSH_TARGET" "$1" >&2; }
rscp() { mutate scp "${SSH_OPTS[@]}" "$1" "$SSH_TARGET:$2" >&2; }

# A command as root (Linux) or in PowerShell (Windows).
remote_run() {
  if [ "$OS_FAMILY" = linux ]; then
    rssh "sudo bash -c $(sq "$1")"
  else
    # Windows OpenSSH hands the line to cmd.exe, which has no escape for '"'.
    [[ "$1" != *'"'* ]] || die "remote_run: a Windows command may not contain '\"': $1"
    rssh "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$1\""
  fi
}

# Run a payload script (path relative to PAYLOAD_DIR) as root / Administrator.
remote_script() {
  if [ "$OS_FAMILY" = linux ]; then
    remote_run "cd /tmp/image-build && bash ./$1"
  else
    rssh "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\image-build\\${1//\//\\}"
  fi
}

# Upload a directory's contents to /tmp/image-build (Linux) or C:\image-build
# (Windows), minus PAYLOAD_EXCLUDE. Extracted as the SSH user, so the seal
# script can be copied in later.
remote_upload() {
  local dir="$1" pattern excludes=() patterns=()
  read -r -a patterns <<< "${PAYLOAD_EXCLUDE:-}"
  # ${a[@]+...} keeps an empty array legal under set -u on bash 3.2.
  for pattern in ${patterns[@]+"${patterns[@]}"}; do
    excludes+=("--exclude=$pattern")
  done
  mutate tar -C "$dir" -czf "$TMP_DIR/payload.tgz" ${excludes[@]+"${excludes[@]}"} .
  if [ "$OS_FAMILY" = linux ]; then
    rssh "mkdir -p /tmp/image-build"
    rscp "$TMP_DIR/payload.tgz" /tmp/image-build/payload.tgz
    rssh "tar -xzf /tmp/image-build/payload.tgz -C /tmp/image-build && rm -f /tmp/image-build/payload.tgz"
  else
    remote_run 'New-Item -ItemType Directory -Force -Path C:\image-build | Out-Null'
    rscp "$TMP_DIR/payload.tgz" C:/image-build/payload.tgz
    remote_run 'tar.exe -xzf C:\image-build\payload.tgz -C C:\image-build; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; Remove-Item -Force C:\image-build\payload.tgz'
  fi
}

wait_ssh() {
  local timeout="$1" deadline
  if [ "$DRY_RUN" = 1 ]; then
    rssh "exit 0"
    return 0
  fi
  deadline=$((SECONDS + timeout))
  log "waiting up to ${timeout}s for SSH to $SSH_TARGET"
  until run_bg ssh -n "${SSH_OPTS[@]}" "$SSH_TARGET" "exit 0" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      die "no SSH to $SSH_TARGET after ${timeout}s"
    fi
    run_bg sleep 10
  done
}

# Ubuntu empties /tmp at boot, so a setup that rebooted the VM took the
# payload with it. Upload it again before the next pass or verify.
ensure_payload() {
  local rc=0
  [ "$OS_FAMILY" = linux ] || return 0
  rssh "test -d /tmp/image-build" || rc=$?
  case "$rc" in
    0) ;;
    1) log "/tmp/image-build is gone (a reboot clears /tmp); uploading the payload again"
       remote_upload "$PAYLOAD_PATH" ;;
    *) die "cannot check /tmp/image-build on $SSH_TARGET (ssh exit $rc)" ;;
  esac
}

# Called under `||`, where errexit is off: every step chains explicitly.
remote_seal() {
  if [ "$OS_FAMILY" = linux ]; then
    rscp "$SCRIPT_DIR/seal_linux.sh" /tmp/image-build/seal_linux.sh || return
    rssh "sudo bash /tmp/image-build/seal_linux.sh $KEEP_SSH"
  else
    local keep=""
    if [ "$KEEP_SSH" = yes ]; then keep=" -KeepSsh"; fi
    rscp "$SCRIPT_DIR/seal_windows.ps1" C:/image-build/seal_windows.ps1 || return
    rssh "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\image-build\\seal_windows.ps1$keep"
  fi
}

# --- cleanup -----------------------------------------------------------------

cleanup() {
  local rc=$? drc=0
  trap '' INT TERM
  trap - EXIT
  set +e
  if [ -n "$CHILD_PID" ]; then
    kill "$CHILD_PID" 2>/dev/null
    wait "$CHILD_PID" 2>/dev/null
    CHILD_PID=""
  fi
  if [ "$STARTED" = 1 ] && [ "$DESTROYED" = 0 ]; then
    log "destroying build VM ${BUILD_VM_ID:-(none recorded)}"
    ( set -e; backend_destroy )
    drc=$?
    if [ "$drc" -ne 0 ]; then
      log "WARNING: could not destroy build VM ${BUILD_VM_ID:-?}; run: $PROG --reap $BACKEND"
      [ "$rc" -eq 0 ] && rc=1
    fi
  fi
  [ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"
  case "$rc" in
    0|1|2|130|143) ;;
    *) rc=1 ;;
  esac
  if [ "$rc" -ne 0 ] && [ "$STEP" != startup ]; then
    if [ "$LOGGED" = 1 ]; then
      log "FAILED during $STEP (exit $rc), after image $IMAGE_ID was saved and logged"
    else
      log "FAILED during $STEP (exit $rc); no image was recorded"
    fi
  fi
  exit "$rc"
}

# --- reap ----------------------------------------------------------------------

reap() {
  local ids id drc failed=0
  load_backend "$1"
  load_config
  QUERY_LIVE=1
  ids="$(backend_leftovers)" || die "backend_leftovers failed"
  if [ -z "$ids" ]; then
    log "nothing to reap on $BACKEND"
    return 0
  fi
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    if [ "$DRY_RUN" = 1 ]; then
      printf 'would destroy %s\n' "$id"
    fi
    # Plain statement with errexit off: in an `if`, bash would ignore the
    # subshell's own set -e.
    set +e
    ( set -e; backend_destroy_id "$id" ) </dev/null
    drc=$?
    set -e
    if [ "$drc" -eq 0 ]; then
      [ "$DRY_RUN" = 1 ] || printf 'destroyed %s\n' "$id"
    else
      log "could not destroy $id"
      failed=1
    fi
  done <<< "$ids"
  return "$failed"
}

# The trap is in place before anything can fail, so every exit (including a
# signal) destroys whatever was started and maps stray codes to 1.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- arguments -----------------------------------------------------------------

ALLOW_DIRTY=0
REAP_BACKEND=""
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --reap)
      [ $# -ge 2 ] || usage
      REAP_BACKEND="$2"
      shift ;;
    -h|--help) usage ;;
    -*) printf '%s: unknown option %s\n' "$PROG" "$1" >&2; usage ;;
    *) POSITIONAL+=("$1") ;;
  esac
  shift
done

if [ -n "$REAP_BACKEND" ]; then
  [ "${#POSITIONAL[@]}" -eq 0 ] && [ "$ALLOW_DIRTY" = 0 ] || usage
  reap "$REAP_BACKEND"
  exit $?
fi
[ "${#POSITIONAL[@]}" -eq 2 ] || usage
RECIPE_ARG="${POSITIONAL[0]}"
BACKEND_ARG="${POSITIONAL[1]}"
BASE_VAR="BASE_$(printf '%s' "$BACKEND_ARG" | tr 'a-z-' 'A-Z_')"

TTL_MINUTES="${IMAGE_BUILD_TTL_MINUTES:-240}"
is_posint "$TTL_MINUTES" || config_error "IMAGE_BUILD_TTL_MINUTES must be a positive integer (got '$TTL_MINUTES')"

# --- recipe --------------------------------------------------------------------

[ -f "$RECIPE_ARG" ] || config_error "recipe $RECIPE_ARG does not exist"
RECIPE_DIR="$(cd "$(dirname "$RECIPE_ARG")" && pwd)"
RECIPE_FILE="$(basename "$RECIPE_ARG")"
# Only the recipe may set its keys; a value inherited from the caller's
# environment must not stand in for a missing one.
unset "${RECIPE_REQUIRED[@]}" "${RECIPE_OPTIONAL[@]}" "$BASE_VAR" 2>/dev/null || true
# shellcheck source=/dev/null
source "$RECIPE_DIR/$RECIPE_FILE" || config_error "cannot read recipe $RECIPE_ARG"

for key in "${RECIPE_REQUIRED[@]}"; do
  [ -n "${!key:-}" ] || config_error "recipe $RECIPE_ARG does not set $key"
done
SETUP_PASSES="${SETUP_PASSES:-1}"
KEEP_SSH="${KEEP_SSH:-no}"
GCP_IMAGE_FAMILY="${GCP_IMAGE_FAMILY:-$IMAGE_PREFIX}"
PAYLOAD_EXCLUDE="${PAYLOAD_EXCLUDE:-}"
case "$OS_FAMILY" in linux|windows) ;; *) config_error "OS_FAMILY must be linux or windows (got '$OS_FAMILY')" ;; esac
case "$KEEP_SSH" in yes|no) ;; *) config_error "KEEP_SSH must be yes or no (got '$KEEP_SSH')" ;; esac
for key in SETUP_PASSES DISK_GB PVE_MEMORY_MB PVE_CORES; do
  is_posint "${!key}" || config_error "$key must be a positive integer (got '${!key}')"
done
PAYLOAD_PATH="$(cd "$RECIPE_DIR" && cd "$PAYLOAD_DIR" 2>/dev/null && pwd)" \
  || config_error "PAYLOAD_DIR $PAYLOAD_DIR does not exist next to $RECIPE_ARG"
for key in SETUP VERIFY; do
  value="${!key}"
  if ! [[ "$value" =~ ^[A-Za-z0-9_][A-Za-z0-9._/-]*$ ]] || [[ "/$value/" == */../* ]]; then
    config_error "$key must be a plain relative path inside PAYLOAD_DIR (got '$value')"
  fi
  [ -f "$PAYLOAD_PATH/$value" ] || config_error "$key: $PAYLOAD_DIR/$value does not exist"
done

# --- source revision and names ------------------------------------------------

REPO_ROOT="$(git -C "$RECIPE_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || config_error "recipe $RECIPE_ARG is not inside a git repository"
REPO_PREFIX="$(git -C "$RECIPE_DIR" rev-parse --show-prefix)"
IMAGE_BUILD_RECIPE="$REPO_PREFIX$RECIPE_FILE"
FULL_SHA="$(git -C "$RECIPE_DIR" rev-parse HEAD)"
SHA7="${FULL_SHA:0:7}"
[[ "$SHA7" =~ ^[0-9a-f]{7}$ ]] || config_error "cannot read the recipe repo's commit (got '$FULL_SHA')"
IMAGE_BUILD_COMMIT="$SHA7"
if [ -n "$(git -C "$RECIPE_DIR" status --porcelain)" ]; then
  if [ "$ALLOW_DIRTY" = 1 ]; then
    log "WARNING: the recipe repo is dirty; building anyway (--allow-dirty)"
    IMAGE_BUILD_COMMIT="$SHA7-dirty"
  else
    config_error "the recipe repo $REPO_ROOT is dirty; commit first (or --allow-dirty for a self-test)"
  fi
fi

NOW_EPOCH="$(date -u +%s)"
STAMP="$(utc_stamp "$NOW_EPOCH")"
BUILD_EXPIRES_UTC="$(utc_stamp $((NOW_EPOCH + TTL_MINUTES * 60)))"
RUN_ID="ib-$STAMP-$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
IMAGE_NAME="$IMAGE_PREFIX-$SHA7-$STAMP"
if ! [[ "$IMAGE_NAME" =~ ^[a-z]([-a-z0-9]*[a-z0-9])?$ ]] || [ "${#IMAGE_NAME}" -gt 63 ]; then
  config_error "image name $IMAGE_NAME must be [a-z0-9-], start with a letter and be at most 63 characters (IMAGE_PREFIX at most 42)"
fi
BUILD_LOG="$REPO_ROOT/coursework/deploy/image-build/BUILD-LOG.tsv"

# --- backend and config ------------------------------------------------------------

load_backend "$BACKEND_ARG"
load_config
BASE_PATTERN="${!BASE_VAR:-}"
[ -n "$BASE_PATTERN" ] || config_error "recipe $RECIPE_ARG does not set $BASE_VAR (the base for backend $BACKEND)"

# --- build -----------------------------------------------------------------------

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/image-build.XXXXXX")"
ssh-keygen -q -t ed25519 -N '' -C "$RUN_ID" -f "$TMP_DIR/key" </dev/null >/dev/null

if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN: nothing that reaches a VM or a cloud is executed"
fi
log "run $RUN_ID: $IMAGE_BUILD_RECIPE@$IMAGE_BUILD_COMMIT on $BACKEND -> $IMAGE_NAME (expires $BUILD_EXPIRES_UTC UTC)"

STEP="resolve base"
BASE_ID="$(backend_resolve_base "$BASE_PATTERN")"
[ -n "$BASE_ID" ] && [[ "$BASE_ID" != *$'\n'* ]] || die "backend_resolve_base '$BASE_PATTERN' must print exactly one ID (got '$BASE_ID')"
log "base: $BASE_ID"

STEP="start build VM"
STARTED=1
backend_start "$RUN_ID" "$BASE_ID" "$TMP_DIR/key.pub"
ssh_setup
log "build VM ${BUILD_VM_ID:-?} at $SSH_TARGET${BUILD_SSH_JUMP:+ via $BUILD_SSH_JUMP}"
wait_ssh 900

STEP="upload payload"
remote_upload "$PAYLOAD_PATH"

for ((pass = 1; pass <= SETUP_PASSES; pass++)); do
  STEP="setup pass $pass/$SETUP_PASSES"
  log "$STEP: $SETUP"
  if [ "$pass" -gt 1 ]; then
    ensure_payload
  fi
  rc=0
  remote_script "$SETUP" || rc=$?
  case "$rc" in
    0) ;;
    255) log "SSH session dropped during setup (a reboot?); waiting for the VM" ;;
    *) die "setup pass $pass exited $rc" ;;
  esac
  wait_ssh 900
done

STEP="verify"
ensure_payload
log "verify: $VERIFY"
rc=0
remote_script "$VERIFY" || rc=$?
[ "$rc" -eq 0 ] || die "verify failed (exit $rc); the image is not saved"

STEP="seal"
log "seal (KEEP_SSH=$KEEP_SSH)"
rc=0
remote_seal || rc=$?
[ "$rc" -eq 0 ] || die "seal failed (exit $rc); the image is not saved"

STEP="save image"
backend_save "$IMAGE_NAME"
[ -n "$IMAGE_ID" ] || die "backend_save set no IMAGE_ID"
log "image: $IMAGE_ID"

STEP="write build log"
LOG_LINE="$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BACKEND" \
  "$IMAGE_ID" "$IMAGE_NAME" "$BASE_ID" "$IMAGE_BUILD_RECIPE" "$IMAGE_BUILD_COMMIT" pass)"
if [ "$DRY_RUN" = 1 ]; then
  printf 'DRY-RUN: append to %s: %s\n' "$BUILD_LOG" "$LOG_LINE" >&2
else
  mkdir -p "$(dirname "$BUILD_LOG")"
  if [ ! -s "$BUILD_LOG" ]; then
    printf 'date\tbackend\timage_id\timage_name\tbase_id\trecipe\tcommit\tverify\n' > "$BUILD_LOG"
  fi
  printf '%s\n' "$LOG_LINE" >> "$BUILD_LOG"
  LOGGED=1
fi

STEP="destroy build VM"
backend_destroy
DESTROYED=1

STEP="templates.yaml"
log "templates.yaml lines for $IMAGE_NAME:"
backend_templates_yaml "$IMAGE_ID"
