# Proxmox backend for build_image.sh (sourced; the contract is in its header).
#
# A build full-clones the newest base template matching BASE_PROXMOX into the
# build block on $PVE_NODE, reaches it over SSH through root@$PVE_HOST, turns
# the build VM itself into the template and copies that to $PVE_REPLICA_NODE
# with sync_template.sh.
#
# VMIDs: a build VM is V in 8000-8499 on $PVE_NODE, and its replica is V+500
# (8500-8999) on $PVE_REPLICA_NODE; V is the lowest value with both halves
# free. No command here names a VMID >= 9000: the portal's allocator takes the
# cluster's highest VMID as a permanent high-water mark
# (app/proxmox_service.py).
#
# Tags live in the description: "image-build=<run-id>
# image-build-expires=<yyyymmddhhmm UTC>" while a VM is a build VM, and
# "image=<image-name>" once it is a template.
#
# Config: PVE_HOST and PVE_REPLICA_HOST are SSH addresses (root, key auth);
# PVE_NODE and PVE_REPLICA_NODE are the cluster's node names, as in
# templates.yaml. Needs python3 locally to read pvesh/qm JSON.

_PVE_BLOCK_MIN=8000
_PVE_BLOCK_MAX=8499
_PVE_LEFTOVER_MAX=8999
_PVE_REPLICA_OFFSET=500
_PVE_SYNC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/sync_template.sh"

# JSON from pvesh and qm, read locally. Exit 0/1 is the answer for a check,
# 2 a malformed reply.
_PVE_PY="$(cat <<'PY'
import base64, fnmatch, json, sys


def fail(msg, code=2):
    print("proxmox: " + msg, file=sys.stderr)
    sys.exit(code)


def load():
    try:
        return json.load(sys.stdin)
    except ValueError as e:
        fail("cannot parse JSON from Proxmox: %s" % e)


def vmid(r):
    return int(r.get("vmid", 0))


def tags(cfg):
    return str(cfg.get("description") or "").split()


mode, args = sys.argv[1], sys.argv[2:]
if mode == "free":
    lo, hi, off = map(int, args)
    used = {vmid(r) for r in load()}
    for v in range(lo, hi + 1):
        if v not in used and v + off not in used:
            print(v)
            sys.exit(0)
    fail("no free VMID pair: every V in %d-%d has V or V+%d in use" % (lo, hi, off), 1)
elif mode == "newest":
    node, pattern, limit = args[0], args[1], int(args[2])
    found = [r for r in load()
             if r.get("type") == "qemu" and int(r.get("template") or 0) == 1
             and r.get("node") == node and vmid(r) < limit
             and fnmatch.fnmatchcase(str(r.get("name", "")), pattern)]
    if not found:
        fail("no template on node %s matches '%s'" % (node, pattern), 1)

    # The last hyphen-separated field is the date or timestamp; the git SHA
    # before it must not decide.
    def key(r):
        last = str(r["name"]).rsplit("-", 1)[-1]
        return (last.isdigit(), int(last) if last.isdigit() else 0, vmid(r))
    print(vmid(max(found, key=key)))
elif mode == "ip":
    for iface in load():
        name = iface.get("name", "")
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        for a in iface.get("ip-addresses") or []:
            ip = a.get("ip-address", "")
            if a.get("ip-address-type") == "ipv4" and not ip.startswith(("127.", "169.254.")):
                print(ip)
                sys.exit(0)
    sys.exit(1)
elif mode == "state":
    node, want = args[0], int(args[1])
    match = [r for r in load() if vmid(r) == want]
    if not match:
        print("absent")
    elif match[0].get("type") != "qemu" or match[0].get("node") != node:
        print("foreign")
    else:
        print("stopped" if match[0].get("status") == "stopped" else "running")
elif mode == "inrange":
    node, lo, hi = args[0], int(args[1]), int(args[2])
    for v in sorted(vmid(r) for r in load()
                    if r.get("type") == "qemu" and r.get("node") == node and lo <= vmid(r) <= hi):
        print(v)
elif mode == "owned":
    sys.exit(0 if "image-build=" + args[0] in tags(load()) else 1)
elif mode == "expired":
    now, t = int(args[0]), tags(load())
    tagged = any(x.startswith("image-build=") and len(x) > len("image-build=") for x in t)
    exp = [x[len("image-build-expires="):] for x in t if x.startswith("image-build-expires=")]
    ok = tagged and len(exp) == 1 and len(exp[0]) == 12 and exp[0].isdigit() and int(exp[0]) < now
    sys.exit(0 if ok else 1)
elif mode == "execok":
    r = load()
    if r.get("exited") in (1, True) and r.get("exitcode") == 0:
        sys.exit(0)
    detail = str(r.get("err-data") or r.get("out-data") or "").strip()
    fail("guest command failed (exited=%s exitcode=%s) %s"
         % (r.get("exited"), r.get("exitcode"), detail), 1)
elif mode == "encps":
    print(base64.b64encode(sys.stdin.read().encode("utf-16-le")).decode())
else:
    fail("unknown mode " + mode)
PY
)"

_pve_py() { python3 -c "$_PVE_PY" "$@"; }

# Every Proxmox command runs on the build node as root.
pve() { ssh -n -o BatchMode=yes root@"$PVE_HOST" "$@"; }

_pve_now() { date -u +%Y%m%d%H%M; }

# Die unless $1 is a VMID in 8000..$2; called before any command naming one.
_pve_guard_vmid() {
  if ! [[ "$1" =~ ^[1-9][0-9]{3}$ ]] || [ "$1" -lt "$_PVE_BLOCK_MIN" ] || [ "$1" -gt "$2" ]; then
    die "proxmox: VMID '$1' is outside the build block $_PVE_BLOCK_MIN-$2; refusing"
  fi
}

_pve_resources() { pve pvesh get /cluster/resources --type vm --output-format json; }

_pve_config() { pve pvesh get "/nodes/$PVE_NODE/qemu/$1/config" --output-format json; }

# --- read-only helpers, each run through `query` ----------------------------

_pve_newest_template() {
  _pve_resources | _pve_py newest "$PVE_NODE" "$1" 9000
}

_pve_free_vmid() {
  _pve_resources | _pve_py free "$_PVE_BLOCK_MIN" "$_PVE_BLOCK_MAX" "$_PVE_REPLICA_OFFSET"
}

_pve_wait_agent() {
  local tries=0
  until pve qm guest cmd "$1" ping >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
      echo "proxmox: no guest agent on VM $1 after 5 minutes" >&2
      return 1
    fi
    sleep 5
  done
}

# The first non-loopback IPv4 address the guest agent reports.
_pve_guest_ip() {
  local tries=0 json ip
  while :; do
    json="$(pve qm guest cmd "$1" network-get-interfaces 2>/dev/null)" || json=""
    ip="$(printf '%s' "$json" | _pve_py ip 2>/dev/null)" || ip=""
    if [ -n "$ip" ]; then
      printf '%s\n' "$ip"
      return 0
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
      echo "proxmox: VM $1 reported no IPv4 address after 5 minutes" >&2
      return 1
    fi
    sleep 5
  done
}

# Prints absent, foreign, stopped or running for VMID $1. A QEMU VM on
# $PVE_NODE whose description fails `_pve_py $2 $3` (owned <run-id> or
# expired <now>) is foreign, as is any guest elsewhere or of another type.
_pve_verdict() {
  local state cfg rc=0
  state="$(_pve_resources | _pve_py state "$PVE_NODE" "$1")"
  case "$state" in
    absent|foreign) printf '%s\n' "$state"; return 0 ;;
  esac
  cfg="$(_pve_config "$1")"
  printf '%s' "$cfg" | _pve_py "$2" "$3" || rc=$?
  case "$rc" in
    0) printf '%s\n' "$state" ;;
    1) echo foreign ;;
    *) return "$rc" ;;
  esac
}

_pve_leftovers() {
  local now ids vmid cfg rc
  now="$(_pve_now)"
  ids="$(_pve_resources | _pve_py inrange "$PVE_NODE" "$_PVE_BLOCK_MIN" "$_PVE_LEFTOVER_MAX")"
  for vmid in $ids; do
    cfg="$(_pve_config "$vmid")"
    rc=0
    printf '%s' "$cfg" | _pve_py expired "$now" || rc=$?
    case "$rc" in
      0) printf '%s\n' "$vmid" ;;
      1) ;;
      *) return "$rc" ;;
    esac
  done
}

# --- mutations ----------------------------------------------------------------

# Run a command in the guest as root/SYSTEM and fail unless it exited 0.
# The arguments after the VMID reach the node's shell, so quote them for it.
_pve_guest_exec() {
  local vmid="$1" out
  shift
  out="$(mutate pve qm guest exec "$vmid" --timeout 120 -- "$@")"
  if [ "$DRY_RUN" != 1 ]; then
    printf '%s' "$out" | _pve_py execok
  fi
}

_pve_windows_key_script() {
  cat <<EOF
\$ErrorActionPreference = 'Stop'
\$dir = 'C:\ProgramData\ssh'
\$file = 'C:\ProgramData\ssh\administrators_authorized_keys'
New-Item -ItemType Directory -Force -Path \$dir | Out-Null
Set-Content -Path \$file -Value '$1' -Encoding Ascii
# sshd ignores this file unless only Administrators (S-1-5-32-544) and SYSTEM
# (S-1-5-18) can write it: full control for both, inheritance removed.
& icacls.exe \$file /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
if (\$LASTEXITCODE -ne 0) { exit \$LASTEXITCODE }
EOF
}

# <vmid> <verdict> <why>: stop and destroy an owned VM; refuse a foreign one.
_pve_teardown() {
  case "$2" in
    absent) echo "proxmox: VM $1 does not exist; nothing to destroy" >&2 ;;
    running)
      mutate pve qm stop "$1" >&2
      mutate pve qm destroy "$1" --purge 1 >&2 ;;
    stopped) mutate pve qm destroy "$1" --purge 1 >&2 ;;
    *)
      echo "proxmox: VM $1 $3; not destroyed" >&2
      return 1 ;;
  esac
}

# --- contract -------------------------------------------------------------------

backend_check_config() {
  local key missing=0
  for key in PVE_HOST PVE_NODE PVE_REPLICA_NODE PVE_REPLICA_HOST PVE_STORAGE \
             PVE_REPLICA_STORAGE PVE_BUILD_BRIDGE PVE_BUILD_VLAN; do
    if [ -z "${!key:-}" ]; then
      echo "proxmox: $key is not set" >&2
      missing=1
    fi
  done
  if [ -n "${PVE_BUILD_VLAN:-}" ] && ! [[ "$PVE_BUILD_VLAN" =~ ^[1-9][0-9]{0,3}$ ]]; then
    echo "proxmox: PVE_BUILD_VLAN must be a VLAN number (got '$PVE_BUILD_VLAN')" >&2
    missing=1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "proxmox: python3 is required" >&2
    missing=1
  fi
  if [ ! -f "$_PVE_SYNC" ]; then
    echo "proxmox: $_PVE_SYNC is missing" >&2
    missing=1
  fi
  return "$missing"
}

backend_resolve_base() {
  query "<$1>" _pve_newest_template "$1"
}

backend_start() {
  local run_id="$1" base="$2" key vmid
  key="$(cat "$3")"
  # The key reaches three shells (node, guest exec, guest); admit nothing a
  # shell would read.
  [[ "$key" =~ ^[A-Za-z0-9@.+/=_:-]+(\ [A-Za-z0-9@.+/=_:-]+)*$ ]] \
    || die "proxmox: public key $3 has an unexpected format"
  if ! [[ "$base" =~ ^[1-9][0-9]*$ && "$base" -lt 9000 ]] && [ "$DRY_RUN" != 1 ]; then
    die "proxmox: base '$base' is not a VMID below 9000"
  fi

  vmid="$(query "$_PVE_BLOCK_MIN" _pve_free_vmid)"
  _pve_guard_vmid "$vmid" "$_PVE_BLOCK_MAX"
  # Set before the clone: a clone that fails half-way is still destroyed.
  BUILD_VM_ID="$vmid"
  mutate pve qm clone "$base" "$vmid" --name "$run_id" --full 1 --storage "$PVE_STORAGE" >&2
  mutate pve qm set "$vmid" --memory "$PVE_MEMORY_MB" --cores "$PVE_CORES" \
    --net0 "virtio,bridge=$PVE_BUILD_BRIDGE,tag=$PVE_BUILD_VLAN" \
    --description "$(sq "image-build=$RUN_ID image-build-expires=$BUILD_EXPIRES_UTC")" >&2
  mutate pve qm start "$vmid" >&2
  query "" _pve_wait_agent "$vmid" >/dev/null

  if [ "$OS_FAMILY" = windows ]; then
    _pve_guest_exec "$vmid" powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
      -EncodedCommand "$(_pve_windows_key_script "$key" | _pve_py encps)"
    BUILD_SSH_USER=Administrator
  else
    _pve_guest_exec "$vmid" /bin/sh -c "$(sq "mkdir -p /home/builder/.ssh \
&& printf '%s\n' '$key' >> /home/builder/.ssh/authorized_keys \
&& chown -R builder: /home/builder/.ssh && chmod 700 /home/builder/.ssh \
&& chmod 600 /home/builder/.ssh/authorized_keys")"
    BUILD_SSH_USER=builder
  fi

  BUILD_SSH_HOST="$(query build-vm-ip _pve_guest_ip "$vmid")"
  BUILD_SSH_JUMP="root@$PVE_HOST"
}

backend_save() {
  local name="$1" vmid="$BUILD_VM_ID" replica rc=0
  _pve_guard_vmid "$vmid" "$_PVE_BLOCK_MAX"
  replica=$((vmid + _PVE_REPLICA_OFFSET))

  mutate pve qm shutdown "$vmid" --timeout 300 >&2 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "proxmox: shutdown of VM $vmid failed (exit $rc); stopping it" >&2
    mutate pve qm stop "$vmid" >&2
  fi
  # The build tag stays until the VM is a template, so a VM whose save fails
  # before that is still destroyed (by the core) or reaped.
  mutate pve qm set "$vmid" --name "$name" >&2
  mutate pve qm template "$vmid" >&2
  # The build VM is the template now: the core must not destroy it.
  BUILD_VM_ID=""
  # Before the copy, so the replica carries the same description.
  mutate pve qm set "$vmid" --description "$(sq "image=$name")" >&2

  rc=0
  mutate bash "$_PVE_SYNC" "$PVE_HOST" "$vmid" "$PVE_REPLICA_HOST" "$PVE_REPLICA_STORAGE" \
    "$replica" </dev/null >&2 || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "proxmox: copying template $vmid to $PVE_REPLICA_NODE as VMID $replica failed (exit $rc); template $vmid on $PVE_NODE is not logged: re-run scripts/sync_template.sh $PVE_HOST $vmid $PVE_REPLICA_HOST $PVE_REPLICA_STORAGE $replica, or delete it"
  fi
  IMAGE_ID="$PVE_NODE:$vmid,$PVE_REPLICA_NODE:$replica"
}

backend_destroy() {
  local vmid="$BUILD_VM_ID" verdict
  if [ -z "$vmid" ]; then
    return 0
  fi
  _pve_guard_vmid "$vmid" "$_PVE_BLOCK_MAX"
  verdict="$(query running _pve_verdict "$vmid" owned "$RUN_ID")"
  _pve_teardown "$vmid" "$verdict" "is no longer tagged image-build=$RUN_ID"
  BUILD_VM_ID=""
}

backend_leftovers() {
  query "" _pve_leftovers
}

backend_destroy_id() {
  local verdict
  _pve_guard_vmid "$1" "$_PVE_LEFTOVER_MAX"
  verdict="$(query running _pve_verdict "$1" expired "$(_pve_now)")"
  _pve_teardown "$1" "$verdict" "is not a tagged, expired build VM on $PVE_NODE"
}

backend_templates_yaml() {
  if ! [[ "$1" =~ ^([a-z0-9-]+):([0-9]+),([a-z0-9-]+):([0-9]+)$ ]]; then
    die "proxmox: image ID '$1' is not <node>:<vmid>,<node>:<vmid>"
  fi
  printf 'template_locations: {%s: %s, %s: %s}\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" \
    "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"
}
