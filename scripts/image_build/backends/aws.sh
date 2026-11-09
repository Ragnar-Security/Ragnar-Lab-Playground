# AWS EC2 backend for build_image.sh. Sourced by the core; the contract is in
# build_image.sh's header.
#
# Config (config.env):
#   AWS_BUILD_REGION     every call names it; a profile's default never decides
#   AWS_BUILD_SUBNET_ID  a public subnet of the build VPC (not the lab VPC)
#   AWS_BUILD_SG_ID      admits SSH from the build host only
# Recipe: BASE_AWS=<owner>:<AMI name glob> (owner: self, amazon or an account
# ID), AWS_INSTANCE_TYPE, DISK_GB, OS_FAMILY.
#
# Reads go through `query`. Everything that creates, changes or deletes a
# resource goes through `mutate`, and so do the waiters: they only wait on what
# a mutation made, so a dry run has nothing to wait for. Every call names
# --region and --output; a scalar is read with --query and --output text.
#
# The build instance and its root volume carry exactly image-build=<run ID>
# and image-build-expires=<yyyymmddhhmm UTC>. The image and its snapshots carry
# image-build-recipe and image-build-commit instead: the product never expires.

# The config keys this backend reads (the core imports only these).
BACKEND_CONFIG_PREFIX=AWS_

# One `aws ec2 wait image-available` gives up after 10 minutes (40 x 15 s);
# a Windows AMI can take longer, so it is re-run while the AMI is pending.
_AWS_IMAGE_WAIT_ROUNDS=6

# JSON for --tag-specifications: each resource type in $1 (space-separated)
# gets the key/value pairs that follow. Values are held to AWS's tag alphabet,
# which has no character JSON needs escaped.
_aws_tag_specs() {
  local types="$1" type tags="" sep="" out="" re='^[A-Za-z0-9 +=._:/@-]{1,256}$'
  shift
  while [ $# -gt 0 ]; do
    [[ "$2" =~ $re ]] || die "aws: tag $1='$2' has a character AWS does not allow in a tag value"
    tags+="$sep{\"Key\":\"$1\",\"Value\":\"$2\"}"
    sep=","
    shift 2
  done
  sep=""
  for type in $types; do
    out+="$sep{\"ResourceType\":\"$type\",\"Tags\":[$tags]}"
    sep=","
  done
  printf '[%s]\n' "$out"
}

# Write the first-boot user data that admits the build key.
#   Linux: cloud-config creates `builder` with the key and passwordless sudo.
#   Windows: EC2Launch v2 runs the <powershell> block as SYSTEM: it installs
#   OpenSSH Server, writes administrators_authorized_keys (readable only by
#   Administrators and SYSTEM, as sshd requires), starts sshd and opens tcp/22.
_aws_user_data() {
  local pubkey="$1" out="$2"
  if [ "$OS_FAMILY" = linux ]; then
    {
      printf '#cloud-config\n'
      printf 'users:\n'
      printf '  - name: builder\n'
      printf '    shell: /bin/bash\n'
      printf '    lock_passwd: true\n'
      printf '    sudo: "ALL=(ALL) NOPASSWD:ALL"\n'
      printf '    ssh_authorized_keys:\n'
      printf '      - "%s"\n' "$pubkey"
      printf 'ssh_pwauth: false\n'
    } > "$out"
  else
    {
      printf '<powershell>\n'
      printf "\$key = '%s'\n" "$pubkey"
      cat <<'EOF'
$ErrorActionPreference = 'Stop'
Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
$dir = Join-Path $env:ProgramData 'ssh'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$keys = Join-Path $dir 'administrators_authorized_keys'
Set-Content -LiteralPath $keys -Value $key -Encoding ascii
# S-1-5-32-544 Administrators, S-1-5-18 SYSTEM: language-independent names.
icacls.exe $keys /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls exited $LASTEXITCODE" }
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
if (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue) {
  Enable-NetFirewallRule -Name 'OpenSSH-Server-In-TCP'
} else {
  New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
</powershell>
EOF
    } > "$out"
  fi
}

# Terminate one instance after re-reading its tags.
#   own      the image-build tag must equal this run's RUN_ID.
#   expired  the image-build tag must be a run ID and image-build-expires must
#            be in the past (the reaper's re-check).
# An instance that is already terminated needs nothing more. A reap sets no
# RUN_ID, hence the defaults (the placeholder is only read in a build's dry run).
_aws_terminate() {
  local id="$1" mode="$2" row state tag expires now
  row="$(query "running"$'\t'"${RUN_ID:-}"$'\t'"${BUILD_EXPIRES_UTC:-}" \
    aws ec2 describe-instances --region "$AWS_BUILD_REGION" --instance-ids "$id" \
    --query "Reservations[0].Instances[0].[State.Name, Tags[?Key=='image-build'].Value | [0], Tags[?Key=='image-build-expires'].Value | [0]]" \
    --output text)"
  IFS=$'\t' read -r state tag expires <<< "$row"
  case "$mode" in
    own)
      [ "$tag" = "$RUN_ID" ] \
        || die "aws: $id carries image-build='$tag', not this run's $RUN_ID; not terminated"
      ;;
    expired)
      now="$(date -u +%Y%m%d%H%M)"
      [[ "$tag" =~ ^ib-[0-9]{12}-[0-9a-f]{4}$ ]] \
        || die "aws: $id carries image-build='$tag', not a build run ID; not terminated"
      [[ "$expires" =~ ^[0-9]{12}$ ]] && [ "$expires" -lt "$now" ] \
        || die "aws: $id has image-build-expires='$expires', not expired (now $now); not terminated"
      ;;
  esac
  case "$state" in
    terminated) return 0 ;;
    pending|running|stopping|stopped|shutting-down) ;;
    *) die "aws: $id is in state '$state'; not terminated" ;;
  esac
  mutate aws ec2 terminate-instances --region "$AWS_BUILD_REGION" --instance-ids "$id" \
    --query 'TerminatingInstances[0].CurrentState.Name' --output text >&2
  mutate aws ec2 wait instance-terminated --region "$AWS_BUILD_REGION" --instance-ids "$id" \
    --output json >&2
}

backend_check_config() {
  local rc=0
  if ! [[ "${AWS_BUILD_REGION:-}" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]]; then
    echo "aws: AWS_BUILD_REGION must be a region name such as us-east-1 (got '${AWS_BUILD_REGION:-}')" >&2
    rc=1
  fi
  if ! [[ "${AWS_BUILD_SUBNET_ID:-}" =~ ^subnet-[0-9a-f]+$ ]]; then
    echo "aws: AWS_BUILD_SUBNET_ID must be a subnet ID, subnet-... (got '${AWS_BUILD_SUBNET_ID:-}')" >&2
    rc=1
  fi
  if ! [[ "${AWS_BUILD_SG_ID:-}" =~ ^sg-[0-9a-f]+$ ]]; then
    echo "aws: AWS_BUILD_SG_ID must be a security group ID, sg-... (got '${AWS_BUILD_SG_ID:-}')" >&2
    rc=1
  fi
  if [ "$DRY_RUN" != 1 ] && ! command -v aws >/dev/null 2>&1; then
    echo "aws: the AWS CLI (aws, v2) is not on PATH" >&2
    rc=1
  fi
  return "$rc"
}

# <owner>:<glob>: the newest available AMI of that owner whose name matches.
backend_resolve_base() {
  local owner="${1%%:*}" glob="${1#*:}" id
  if [ "$owner" = "$1" ] || [ -z "$owner" ] || [ -z "$glob" ]; then
    die "aws: BASE_AWS must be <owner>:<AMI name glob> (got '$1')"
  fi
  # A comma would split the filter into two values.
  [[ "$glob" != *,* ]] || die "aws: BASE_AWS's name glob may not contain ',' (got '$glob')"
  id="$(query ami-dryrunbase aws ec2 describe-images --region "$AWS_BUILD_REGION" \
    --owners "$owner" --filters "Name=name,Values=$glob" Name=state,Values=available \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text)"
  if [ -z "$id" ] || [ "$id" = None ]; then
    die "aws: no available AMI named '$glob' owned by $owner in $AWS_BUILD_REGION"
  fi
  [[ "$id" =~ ^ami-[0-9a-z]+$ ]] || die "aws: describe-images returned '$id' for '$glob', not one AMI ID"
  printf '%s\n' "$id"
}

backend_start() {
  local base="$2" pubkey_file="$3" pubkey root specs user_data iid host
  local key_re='^ssh-[a-z0-9-]+ [A-Za-z0-9+/=]+( [A-Za-z0-9@._:-]+)?$'
  [[ "$AWS_INSTANCE_TYPE" =~ ^[a-z0-9-]+\.[a-z0-9-]+$ ]] \
    || die "aws: AWS_INSTANCE_TYPE must look like t3.small (got '$AWS_INSTANCE_TYPE')"
  pubkey="$(cat "$pubkey_file")"
  [[ "$pubkey" =~ $key_re ]] || die "aws: $pubkey_file is not a one-line OpenSSH public key"

  # The root volume is sized on the AMI's own root device name.
  root="$(query /dev/sda1 aws ec2 describe-images --region "$AWS_BUILD_REGION" \
    --image-ids "$base" --query 'Images[0].RootDeviceName' --output text)"
  [[ "$root" =~ ^/dev/[a-z0-9]+$ ]] || die "aws: cannot read the root device of $base (got '$root')"

  specs="$(_aws_tag_specs "instance volume" image-build "$RUN_ID" image-build-expires "$BUILD_EXPIRES_UTC")"
  user_data="$(dirname "$pubkey_file")/aws-user-data"
  _aws_user_data "$pubkey" "$user_data"

  # --subnet-id, --security-group-ids and --associate-public-ip-address are
  # the CLI's documented single-instance form: it folds all three into the
  # primary network interface (they cannot be combined with
  # --network-interfaces). The client token makes a repeated call a no-op
  # instead of a second instance.
  iid="$(mutate aws ec2 run-instances --region "$AWS_BUILD_REGION" \
    --image-id "$base" --count 1 --instance-type "$AWS_INSTANCE_TYPE" \
    --subnet-id "$AWS_BUILD_SUBNET_ID" --security-group-ids "$AWS_BUILD_SG_ID" \
    --associate-public-ip-address \
    --block-device-mappings "[{\"DeviceName\":\"$root\",\"Ebs\":{\"VolumeSize\":$DISK_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
    --tag-specifications "$specs" \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled \
    --user-data "file://$user_data" \
    --client-token "$RUN_ID" \
    --query 'Instances[0].InstanceId' --output text)"
  if [ "$DRY_RUN" = 1 ]; then
    iid=i-dryrun
  fi
  [[ "$iid" =~ ^i-[0-9a-z]+$ ]] \
    || die "aws: run-instances returned '$iid', not an instance ID; the cleanup looks the instance up by this run's tag"
  BUILD_VM_ID="$iid"

  mutate aws ec2 wait instance-running --region "$AWS_BUILD_REGION" --instance-ids "$iid" \
    --output json >&2
  host="$(query 192.0.2.10 aws ec2 describe-instances --region "$AWS_BUILD_REGION" \
    --instance-ids "$iid" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
  [[ "$host" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
    || die "aws: $iid has no public IPv4 address (got '$host'); is $AWS_BUILD_SUBNET_ID a public subnet?"
  BUILD_SSH_HOST="$host"
  if [ "$OS_FAMILY" = windows ]; then
    BUILD_SSH_USER=Administrator
  else
    BUILD_SSH_USER=builder
  fi
  BUILD_SSH_JUMP=""
}

# create-image reboots the instance (the default) so the file systems are
# consistent, then the AMI is waited on until available.
backend_save() {
  local name="$1" specs ami state round=1
  specs="$(_aws_tag_specs "image snapshot" \
    image-build-recipe "$IMAGE_BUILD_RECIPE" image-build-commit "$IMAGE_BUILD_COMMIT")"
  ami="$(mutate aws ec2 create-image --region "$AWS_BUILD_REGION" \
    --instance-id "$BUILD_VM_ID" --name "$name" --tag-specifications "$specs" \
    --query ImageId --output text)"
  if [ "$DRY_RUN" = 1 ]; then
    ami=ami-dryrunimage
  fi
  [[ "$ami" =~ ^ami-[0-9a-z]+$ ]] || die "aws: create-image returned '$ami', not an AMI ID"
  until mutate aws ec2 wait image-available --region "$AWS_BUILD_REGION" --image-ids "$ami" \
    --output json >&2; do
    state="$(query pending aws ec2 describe-images --region "$AWS_BUILD_REGION" \
      --image-ids "$ami" --query 'Images[0].State' --output text)"
    if [ "$state" = available ]; then
      break
    fi
    if [ "$state" != pending ] || [ "$round" -ge "$_AWS_IMAGE_WAIT_ROUNDS" ]; then
      die "aws: AMI $ami is '$state' after $round wait round(s); it is not recorded. Deregister it and delete its snapshots (tag image-build-commit=$IMAGE_BUILD_COMMIT)."
    fi
    round=$((round + 1))
  done
  IMAGE_ID="$ami"
}

# The build instance, re-checked and terminated. With no instance ID recorded
# (run-instances failed, or was interrupted, after AWS may already have made
# the instance), whatever carries this run's image-build tag is looked up and
# terminated through the same re-check.
backend_destroy() {
  local rows id ids=()
  if [ -n "$BUILD_VM_ID" ]; then
    _aws_terminate "$BUILD_VM_ID" own
    BUILD_VM_ID=""
    return 0
  fi
  [ -n "${RUN_ID:-}" ] || return 0
  rows="$(query "" aws ec2 describe-instances --region "$AWS_BUILD_REGION" \
    --filters "Name=tag:image-build,Values=$RUN_ID" \
    Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[].Instances[].[InstanceId]' --output text)"
  read -r -d '' -a ids <<< "$rows" || true
  for id in ${ids[@]+"${ids[@]}"}; do
    [[ "$id" =~ ^i-[0-9a-f]+$ ]] || die "aws: looking up run $RUN_ID returned '$id', not an instance ID"
    echo "aws: $id was launched by this run but its ID was never recorded; terminating it" >&2
    _aws_terminate "$id" own
  done
}

# Build instances (tag key image-build) not yet terminated whose
# image-build-expires is in the past. One without a readable expiry is reported
# and left alone.
backend_leftovers() {
  local rows now id expires
  now="$(date -u +%Y%m%d%H%M)"
  rows="$(query "" aws ec2 describe-instances --region "$AWS_BUILD_REGION" \
    --filters Name=tag-key,Values=image-build \
    Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped \
    --query "Reservations[].Instances[].[InstanceId, Tags[?Key=='image-build-expires'].Value | [0]]" \
    --output text)"
  while IFS=$'\t' read -r id expires; do
    if [ -z "$id" ]; then
      continue
    fi
    if ! [[ "$expires" =~ ^[0-9]{12}$ ]]; then
      echo "aws: $id has image-build but no valid image-build-expires ('$expires'); left alone" >&2
      continue
    fi
    if [ "$expires" -lt "$now" ]; then
      printf '%s\n' "$id"
    fi
  done <<< "$rows"
}

backend_destroy_id() {
  [[ "$1" =~ ^i-[0-9a-f]+$ ]] || die "aws: '$1' is not an instance ID"
  _aws_terminate "$1" expired
}

backend_templates_yaml() {
  printf 'aws: {image_id: "%s", instance_type: "%s", disk_gb: %s}\n' "$1" "$AWS_INSTANCE_TYPE" "$DISK_GB"
}
