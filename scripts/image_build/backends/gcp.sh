# GCP backend for build_image.sh (sourced; see the contract in its header).
#
# The build VM is a Compute Engine instance named after the run ID, in the
# build subnet with an ephemeral external IP and no service account. Its image
# is made from its boot disk (same name) after the instance is stopped.
#
# Config: GCP_BUILD_PROJECT, GCP_BUILD_ZONE, GCP_BUILD_SUBNET.
# Recipe: BASE_GCP=<image project>/<image family>, GCP_MACHINE_TYPE,
#         GCP_IMAGE_FAMILY (the family the new image joins; default IMAGE_PREFIX).
#
# The build key goes in from a file, so it never appears on a command line:
#   Linux    ssh-keys metadata "builder:<key>"; the guest agent creates the
#            sudoer `builder`. OS Login and project-wide keys are turned off
#            for the instance, so no other account lands in the image.
#   Windows  windows-startup-script-ps1 (runs at every boot): installs the
#            OpenSSH server, writes the key to administrators_authorized_keys,
#            enables the built-in Administrator (disabled on GCE images) with a
#            random password generated in the guest, starts sshd, opens tcp/22.
#
# Build VM IDs: backend_leftovers prints <zone>/<instance>, from every zone of
# the project. A build VM is an instance whose image-build label equals its own
# name (ib-<yyyymmddhhmm>-<4 hex>) and carries image-build-expires.

_GCP_RUN_ID_RE='^ib-[0-9]{12}-[0-9a-f]{4}$'
_GCP_NAME_RE='^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$'
_GCP_ZONE_RE='^[a-z]+-[a-z]+[0-9]+-[a-z]$'
# One OpenSSH public key line, with no quote (it is embedded in a PowerShell
# single-quoted string on Windows).
_GCP_PUBKEY_RE='^ssh-[a-z0-9-]+ [A-Za-z0-9+/=]+( [A-Za-z0-9@._:+=-]+)?$'

backend_check_config() {
  local key missing=0
  for key in GCP_BUILD_PROJECT GCP_BUILD_ZONE GCP_BUILD_SUBNET; do
    if [ -z "${!key:-}" ]; then
      echo "gcp: $key is not set" >&2
      missing=1
    fi
  done
  [ "$missing" = 0 ] || return 1
  if ! [[ "$GCP_BUILD_PROJECT" =~ ^[a-z][-a-z0-9]{4,28}[a-z0-9]$ ]]; then
    echo "gcp: GCP_BUILD_PROJECT '$GCP_BUILD_PROJECT' is not a project ID" >&2
    return 1
  fi
  if ! [[ "$GCP_BUILD_ZONE" =~ $_GCP_ZONE_RE ]]; then
    echo "gcp: GCP_BUILD_ZONE '$GCP_BUILD_ZONE' is not a zone (e.g. us-east1-b)" >&2
    return 1
  fi
  if ! [[ "$GCP_BUILD_SUBNET" =~ ^[-a-z0-9/._]+$ ]]; then
    echo "gcp: GCP_BUILD_SUBNET '$GCP_BUILD_SUBNET' is not a subnet name or path" >&2
    return 1
  fi
  # Unset under --reap (no recipe). Checked here so a bad family fails before
  # an hour of build, not at the save.
  if [ -n "${GCP_IMAGE_FAMILY:-}" ] && ! [[ "$GCP_IMAGE_FAMILY" =~ $_GCP_NAME_RE ]]; then
    echo "gcp: GCP_IMAGE_FAMILY '$GCP_IMAGE_FAMILY' must match $_GCP_NAME_RE" >&2
    return 1
  fi
}

# <project>/<family> -> selfLink of the family's newest non-deprecated image.
backend_resolve_base() {
  local pattern="$1" project family out rc=0
  project="${pattern%%/*}"
  family="${pattern#*/}"
  if [[ "$pattern" != */* ]] || [ -z "$project" ] || [ -z "$family" ] || [[ "$family" == */* ]]; then
    die "gcp: BASE_GCP '$pattern' must be <project>/<family>"
  fi
  out="$(query "projects/$project/global/images/family/$family" \
    gcloud compute images describe-from-family "$family" --project "$project" --format 'value(selfLink)')" || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
    die "gcp: no image in family $family of project $project (BASE_GCP=$pattern)"
  fi
  printf '%s\n' "$out"
}

# The Windows startup script, with the build key on its first line.
_gcp_windows_startup_script() {
  printf "\$buildKey = '%s'\n" "$1"
  cat <<'PS1'
$ErrorActionPreference = 'Stop'

$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ($cap.State -ne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
# The first start creates C:\ProgramData\ssh, sshd_config and the host keys.
Start-Service -Name sshd
Set-Service -Name sshd -StartupType Automatic

# sshd ignores administrators_authorized_keys unless only Administrators and
# SYSTEM can access it (SIDs, so a localised group name does not matter).
$auth = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
Set-Content -LiteralPath $auth -Value $buildKey -Encoding ascii
icacls.exe $auth /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $auth" }

# Administrator is disabled on GCE images. Enable it with a random password,
# generated here and never output (the seal replaces it again).
$admin = Get-LocalUser -Name 'Administrator'
if (-not $admin.Enabled) {
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+'
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $secure = New-Object System.Security.SecureString
    try {
        $byte = New-Object byte[] 1
        $limit = 256 - (256 % $alphabet.Length)
        while ($secure.Length -lt 32) {
            $rng.GetBytes($byte)
            if ($byte[0] -lt $limit) { $secure.AppendChar($alphabet[$byte[0] % $alphabet.Length]) }
        }
    } finally {
        $rng.Dispose()
    }
    Set-LocalUser -Name 'Administrator' -Password $secure
    Enable-LocalUser -Name 'Administrator'
}

if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
PS1
}

backend_start() {
  local run_id="$1" base="$2" pubkey="$3" key meta host
  local -a args
  # GCP labels and instance names are lowercase [a-z0-9_-] / [a-z0-9-]; the
  # leftover check relies on name == image-build label.
  if [ "$run_id" != "$RUN_ID" ] || ! [[ "$run_id" =~ $_GCP_RUN_ID_RE ]] || ! [[ "$run_id" =~ $_GCP_NAME_RE ]]; then
    die "gcp: run ID '$run_id' is not a valid build VM name and label"
  fi
  [[ "$BUILD_EXPIRES_UTC" =~ ^[0-9]{12}$ ]] || die "gcp: expiry '$BUILD_EXPIRES_UTC' is not yyyymmddhhmm"
  key="$(cat "$pubkey")"
  [[ "$key" =~ $_GCP_PUBKEY_RE ]] || die "gcp: public key $pubkey is not a single OpenSSH key line"

  args=(compute instances create "$run_id" --project "$GCP_BUILD_PROJECT" --zone "$GCP_BUILD_ZONE"
        --machine-type "$GCP_MACHINE_TYPE" --subnet "$GCP_BUILD_SUBNET" --image "$base"
        --boot-disk-size "${DISK_GB}GB" --boot-disk-auto-delete
        --no-service-account --no-scopes
        --labels "image-build=$RUN_ID,image-build-expires=$BUILD_EXPIRES_UTC")
  # The core's per-run temp dir (removed on exit) holds the metadata files.
  meta="$(dirname "$pubkey")"
  if [ "$OS_FAMILY" = windows ]; then
    _gcp_windows_startup_script "$key" > "$meta/gcp-startup.ps1"
    args+=(--metadata-from-file "windows-startup-script-ps1=$meta/gcp-startup.ps1")
    BUILD_SSH_USER=Administrator
  else
    printf 'builder:%s\n' "$key" > "$meta/gcp-ssh-keys"
    args+=(--metadata enable-oslogin=FALSE,block-project-ssh-keys=TRUE
           --metadata-from-file "ssh-keys=$meta/gcp-ssh-keys")
    BUILD_SSH_USER=builder
  fi
  args+=(--quiet)

  BUILD_VM_ID="$run_id"
  # gcloud prints a table on stdout, which carries only templates.yaml lines.
  mutate gcloud "${args[@]}" >&2
  host="$(query "nat-ip-of-$run_id" gcloud compute instances describe "$run_id" \
    --project "$GCP_BUILD_PROJECT" --zone "$GCP_BUILD_ZONE" \
    --format 'value(networkInterfaces[0].accessConfigs[0].natIP)')"
  [ -n "$host" ] || die "gcp: instance $run_id has no external IP"
  BUILD_SSH_HOST="$host"
  BUILD_SSH_JUMP=""
}

# Prints "<image-build>,<image-build-expires>" of an instance, or nothing when
# it does not exist. Any other failure fails.
_gcp_instance_labels() {
  local zone="$1" name="$2" placeholder="$3" err out rc=0
  err="$(mktemp "${TMPDIR:-/tmp}/gcp-describe.XXXXXX")"
  out="$(query "$placeholder" gcloud compute instances describe "$name" \
    --project "$GCP_BUILD_PROJECT" --zone "$zone" \
    --format 'csv[no-heading](labels.image-build,labels.image-build-expires)' 2>"$err")" || rc=$?
  if [ "$rc" -ne 0 ] && grep -qF "instances/$name' was not found" "$err"; then
    rm -f "$err"
    return 0
  fi
  cat "$err" >&2
  rm -f "$err"
  [ "$rc" -eq 0 ] || die "gcp: cannot read instance $zone/$name (exit $rc)"
  printf '%s\n' "$out"
}

backend_save() {
  local name="$1" recipe
  [ -n "$BUILD_VM_ID" ] || die "gcp: no build VM to save"
  # Label values: lowercase [a-z0-9_-], at most 63. The exact path goes in
  # the description.
  recipe="$(printf '%s' "$IMAGE_BUILD_RECIPE" | tr 'A-Z' 'a-z')"
  recipe="${recipe//[^a-z0-9_-]/_}"
  recipe="${recipe:0:63}"
  # stop returns once the instance is TERMINATED, so the disk is consistent.
  mutate gcloud compute instances stop "$BUILD_VM_ID" --project "$GCP_BUILD_PROJECT" \
    --zone "$GCP_BUILD_ZONE" --quiet >&2
  mutate gcloud compute images create "$name" --project "$GCP_BUILD_PROJECT" \
    --source-disk "$BUILD_VM_ID" --source-disk-zone "$GCP_BUILD_ZONE" \
    --family "${GCP_IMAGE_FAMILY:-$IMAGE_PREFIX}" \
    --labels "image-build-recipe=$recipe,image-build-commit=$IMAGE_BUILD_COMMIT" \
    --description "image-build $IMAGE_BUILD_RECIPE@$IMAGE_BUILD_COMMIT run $RUN_ID" --quiet >&2
  IMAGE_ID="projects/$GCP_BUILD_PROJECT/global/images/$name"
}

backend_destroy() {
  local labels label
  [ -n "$BUILD_VM_ID" ] || return 0
  labels="$(_gcp_instance_labels "$GCP_BUILD_ZONE" "$BUILD_VM_ID" "$RUN_ID,$BUILD_EXPIRES_UTC")"
  if [ -n "$labels" ]; then
    label="${labels%%,*}"
    [ "$label" = "$RUN_ID" ] \
      || die "gcp: instance $BUILD_VM_ID carries image-build='$label', not $RUN_ID; not deleted"
    mutate gcloud compute instances delete "$BUILD_VM_ID" --project "$GCP_BUILD_PROJECT" \
      --zone "$GCP_BUILD_ZONE" --quiet >&2
  fi
  BUILD_VM_ID=""
}

backend_leftovers() {
  local out now name zone label expires
  now="$(date -u +%Y%m%d%H%M)"
  out="$(query "" gcloud compute instances list --project "$GCP_BUILD_PROJECT" \
    --filter 'labels.image-build:*' \
    --format 'csv[no-heading](name,zone.basename(),labels.image-build,labels.image-build-expires)')"
  while IFS=, read -r name zone label expires; do
    if [[ "$label" =~ $_GCP_RUN_ID_RE ]] && [ "$name" = "$label" ] && [[ "$zone" =~ $_GCP_ZONE_RE ]] \
       && [[ "$expires" =~ ^[0-9]{12}$ ]] && [ "$expires" -lt "$now" ]; then
      printf '%s/%s\n' "$zone" "$name"
    fi
  done <<< "$out"
}

backend_destroy_id() {
  local id="$1" zone name labels label expires now
  zone="${id%%/*}"
  name="${id#*/}"
  if ! [[ "$zone" =~ $_GCP_ZONE_RE ]] || ! [[ "$name" =~ $_GCP_RUN_ID_RE ]]; then
    die "gcp: '$id' is not a <zone>/<build VM> ID; not deleted"
  fi
  labels="$(_gcp_instance_labels "$zone" "$name" "$name,000000000000")"
  if [ -z "$labels" ]; then
    echo "gcp: $id no longer exists" >&2
    return 0
  fi
  label="${labels%%,*}"
  expires="${labels#*,}"
  now="$(date -u +%Y%m%d%H%M)"
  if ! { [ "$label" = "$name" ] && [[ "$expires" =~ ^[0-9]{12}$ ]] && [ "$expires" -lt "$now" ]; }; then
    die "gcp: $id carries image-build='$label' image-build-expires='$expires'; not an expired build VM, not deleted"
  fi
  mutate gcloud compute instances delete "$name" --project "$GCP_BUILD_PROJECT" --zone "$zone" --quiet >&2
}

backend_templates_yaml() {
  printf 'gcp: {image: %s, machine_type: %s, disk_gb: %s}\n' "$1" "$GCP_MACHINE_TYPE" "$DISK_GB"
}
