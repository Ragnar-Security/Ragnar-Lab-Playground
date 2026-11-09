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
# image-build-expires=<yyyymmddhhmm UTC>" from the clone onwards (qm clone
# sets it; while the clone is still running the new guest may not carry it
# yet, being held as `lock: clone`, and a destroy that finds no tag refuses
# it), and "image=<image-name>" once it is a template. A destroy re-checks the
# tag on the node, in the same remote command, immediately before `qm stop`
# and again before `qm destroy`, and never destroys a template.
#
# Config: PVE_HOST and PVE_REPLICA_HOST are SSH addresses (root, key auth);
# PVE_NODE and PVE_REPLICA_NODE are the cluster's node names, as in
# templates.yaml ([a-z0-9-]). Every value that reaches a remote shell is
# checked against a strict pattern by backend_check_config, and every remote
# argument is quoted by pve(). Needs python3 locally to read pvesh/qm JSON.

# The config keys this backend reads (the core imports only these).
BACKEND_CONFIG_PREFIX=PVE_

_PVE_BLOCK_MIN=8000
_PVE_BLOCK_MAX=8499
_PVE_LEFTOVER_MAX=8999
_PVE_REPLICA_OFFSET=500
# A node name as templates.yaml takes it; the config check uses the same.
_PVE_NODE_CHARS='[a-z0-9-]+'
_PVE_SYNC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/sync_template.sh"

# JSON from pvesh and qm, read locally. Exit 0/1 is the answer for a check,
# 2 a malformed reply.
_PVE_PY="$(cat <<'PY'
import base64, fnmatch, json, re, sys


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
elif mode == "inrange":
    # Build VMs only: a template is never a leftover (destroy refuses one).
    node, lo, hi = args[0], int(args[1]), int(args[2])
    for v in sorted(vmid(r) for r in load()
                    if r.get("type") == "qemu" and r.get("node") == node
                    and int(r.get("template") or 0) == 0 and lo <= vmid(r) <= hi):
        print(v)
elif mode == "istemplate":
    print(1 if int(load().get("template") or 0) == 1 else 0)
elif mode == "expired":
    # A reapable VM carries exactly one image-build tag in the core's RUN_ID
    # format (ib-<yyyymmddhhmm>-<4 hex>), and one expiry in the past.
    now, t = int(args[0]), tags(load())
    runs = [x[len("image-build="):] for x in t if x.startswith("image-build=")]
    tagged = len(runs) == 1 and re.fullmatch(r"ib-[0-9]{12}-[0-9a-f]{4}", runs[0]) is not None
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

# One argument for the node's root shell: bare when it holds only characters
# no shell reads, single-quoted otherwise.
_pve_q() {
  if [[ "$1" =~ ^[A-Za-z0-9._/:=,@+%-]+$ ]]; then
    printf '%s' "$1"
  else
    sq "$1"
  fi
}

# Every Proxmox command runs on the build node as root, each argument quoted.
pve() {
  local arg line=""
  for arg in "$@"; do
    line+="${line:+ }$(_pve_q "$arg")"
  done
  ssh -n -o BatchMode=yes root@"$PVE_HOST" "$line"
}

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

_pve_is_template() {
  _pve_config "$1" | _pve_py istemplate
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

# The node-side destroy of VMID $1: one sh script, so the tag is checked by
# the node itself immediately before `qm stop` and again before `qm destroy`,
# with no round trip in which the VMID could be freed and reused. $2 is a
# shell condition on $w (the description's words, one per line); $3 says why
# a VM failing it is refused. A VM that does not exist is fine (exit 0): qm
# must report exactly that, /etc/pve/nodes must be readable, and no node may
# hold the VMID. One that fails $2, is a template, or is not a VM on this node
# is refused (exit 3). A config that cannot be read for any other reason (the
# cluster filesystem down, say) fails the destroy (exit 4).
_pve_destroy_script() {
  cat <<EOF
set -e
v=$1
check() {
  if ! c=\$(qm config \$v 2>/dev/null); then
    e=\$(qm config \$v 2>&1 >/dev/null) || true
    case "\$e" in
      *"/\$v.conf' does not exist"*) ;;
      *)
        echo "proxmox: cannot read the config of VM \$v (\${e:-qm config failed}); not destroyed" >&2
        exit 4 ;;
    esac
    if ! ls -d /etc/pve/nodes >/dev/null 2>&1; then
      echo "proxmox: /etc/pve/nodes is not readable (cluster filesystem down?); VM \$v not destroyed" >&2
      exit 4
    fi
    if [ -n "\$(ls /etc/pve/nodes/*/qemu-server/\$v.conf /etc/pve/nodes/*/lxc/\$v.conf 2>/dev/null)" ]; then
      echo "proxmox: VMID \$v is not a VM on this node; not destroyed" >&2
      exit 3
    fi
    echo "proxmox: VM \$v does not exist; nothing to destroy" >&2
    exit 0
  fi
  if printf '%s\n' "\$c" | grep -qx 'template: 1'; then
    echo "proxmox: VM \$v is a template; not destroyed" >&2
    exit 3
  fi
  w=\$(printf '%s\n' "\$c" | sed -n 's/^description: //p' | tr ' ' '\n')
  if ! { $2; }; then
    echo "proxmox: VM \$v $3; not destroyed" >&2
    exit 3
  fi
}
check
qm stop \$v
check
qm destroy \$v --purge 1
EOF
}

# --- contract -------------------------------------------------------------------

backend_check_config() {
  local key bad=0
  for key in PVE_HOST PVE_NODE PVE_REPLICA_NODE PVE_REPLICA_HOST PVE_STORAGE \
             PVE_REPLICA_STORAGE PVE_BUILD_BRIDGE PVE_BUILD_VLAN; do
    if [ -z "${!key:-}" ]; then
      echo "proxmox: $key is not set" >&2
      bad=1
    fi
  done
  # Each of these reaches a root shell on a node.
  for key in PVE_HOST PVE_NODE PVE_REPLICA_NODE PVE_REPLICA_HOST PVE_STORAGE \
             PVE_REPLICA_STORAGE PVE_BUILD_BRIDGE; do
    if [ -n "${!key:-}" ] && ! [[ "${!key}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
      echo "proxmox: $key must match [A-Za-z0-9][A-Za-z0-9._-]* (got '${!key}')" >&2
      bad=1
    fi
  done
  # templates.yaml names the nodes: a name it cannot take fails here, not
  # after the image is saved and logged.
  for key in PVE_NODE PVE_REPLICA_NODE; do
    if [ -n "${!key:-}" ] && ! [[ "${!key}" =~ ^${_PVE_NODE_CHARS}$ ]]; then
      echo "proxmox: $key must match $_PVE_NODE_CHARS, as in templates.yaml (got '${!key}')" >&2
      bad=1
    fi
  done
  if [ -n "${PVE_BUILD_VLAN:-}" ] && { ! [[ "$PVE_BUILD_VLAN" =~ ^[1-9][0-9]{0,3}$ ]] \
       || [ "$PVE_BUILD_VLAN" -gt 4094 ]; }; then
    echo "proxmox: PVE_BUILD_VLAN must be a VLAN number 1-4094 (got '$PVE_BUILD_VLAN')" >&2
    bad=1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "proxmox: python3 is required" >&2
    bad=1
  fi
  if [ ! -f "$_PVE_SYNC" ]; then
    echo "proxmox: $_PVE_SYNC is missing" >&2
    bad=1
  fi
  return "$bad"
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
  [[ "$run_id" =~ ^ib-[0-9]{12}-[0-9a-f]{4}$ ]] || die "proxmox: unexpected run ID '$run_id'"
  # Recipe values (absent in a reap, so not checked with the config).
  [[ "$PVE_MEMORY_MB" =~ ^[1-9][0-9]{0,8}$ && "$PVE_CORES" =~ ^[1-9][0-9]{0,8}$ ]] \
    || die "proxmox: PVE_MEMORY_MB and PVE_CORES must be positive integers"
  if ! [[ "$base" =~ ^[1-9][0-9]*$ && "$base" -lt 9000 ]] && [ "$DRY_RUN" != 1 ]; then
    die "proxmox: base '$base' is not a VMID below 9000"
  fi

  vmid="$(query "$_PVE_BLOCK_MIN" _pve_free_vmid)"
  _pve_guard_vmid "$vmid" "$_PVE_BLOCK_MAX"
  # Set before the clone: a clone that fails half-way, leaving a tagged guest,
  # is still destroyed.
  BUILD_VM_ID="$vmid"
  # Tagged by the clone command itself. While the clone runs, the guest may
  # not carry the description yet (`lock: clone`); a destroy that finds no
  # tag refuses it (fails closed, so --reap or a hand cleanup is needed).
  mutate pve qm clone "$base" "$vmid" --name "$run_id" --full 1 --storage "$PVE_STORAGE" \
    --description "image-build=$RUN_ID image-build-expires=$BUILD_EXPIRES_UTC" >&2
  mutate pve qm set "$vmid" --memory "$PVE_MEMORY_MB" --cores "$PVE_CORES" \
    --net0 "virtio,bridge=$PVE_BUILD_BRIDGE,tag=$PVE_BUILD_VLAN" >&2
  mutate pve qm start "$vmid" >&2
  query "" _pve_wait_agent "$vmid" >/dev/null

  if [ "$OS_FAMILY" = windows ]; then
    _pve_guest_exec "$vmid" powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
      -EncodedCommand "$(_pve_windows_key_script "$key" | _pve_py encps)"
    BUILD_SSH_USER=Administrator
  else
    _pve_guest_exec "$vmid" /bin/sh -c "mkdir -p /home/builder/.ssh \
&& printf '%s\n' '$key' >> /home/builder/.ssh/authorized_keys \
&& chown -R builder: /home/builder/.ssh && chmod 700 /home/builder/.ssh \
&& chmod 600 /home/builder/.ssh/authorized_keys"
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
  rc=0
  mutate pve qm template "$vmid" >&2 || rc=$?
  if [ "$rc" -ne 0 ]; then
    # The conversion may have happened even though the call failed.
    if [ "$(query 0 _pve_is_template "$vmid")" = 1 ]; then
      BUILD_VM_ID=""
      die "proxmox: 'qm template $vmid' reported failure (exit $rc), but VM $vmid is now a template; it is neither replicated nor logged: delete it on $PVE_NODE (qm destroy $vmid) or finish the save by hand"
    fi
    die "proxmox: 'qm template $vmid' failed (exit $rc); VM $vmid is not a template"
  fi
  # The build VM is the template now: the core must not destroy it.
  BUILD_VM_ID=""
  # Before the copy, so the replica carries the same description.
  rc=0
  mutate pve qm set "$vmid" --description "image=$name" >&2 || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "proxmox: setting the description of template $vmid on $PVE_NODE to image=$name failed (exit $rc); the template still carries its build tags (image-build=$RUN_ID) and is neither replicated nor logged: set it by hand (qm set $vmid --description image=$name) and copy it with scripts/sync_template.sh, or delete it (qm destroy $vmid --purge)"
  fi

  rc=0
  mutate bash "$_PVE_SYNC" "$PVE_HOST" "$vmid" "$PVE_REPLICA_HOST" "$PVE_REPLICA_STORAGE" \
    "$replica" </dev/null >&2 || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "proxmox: copying template $vmid to $PVE_REPLICA_NODE as VMID $replica failed (exit $rc); template $vmid on $PVE_NODE is not logged: re-run scripts/sync_template.sh $PVE_HOST $vmid $PVE_REPLICA_HOST $PVE_REPLICA_STORAGE $replica, or delete it"
  fi
  IMAGE_ID="$PVE_NODE:$vmid,$PVE_REPLICA_NODE:$replica"
}

backend_destroy() {
  local vmid="$BUILD_VM_ID" owned
  if [ -z "$vmid" ]; then
    return 0
  fi
  _pve_guard_vmid "$vmid" "$_PVE_BLOCK_MAX"
  [[ "$RUN_ID" =~ ^ib-[0-9]{12}-[0-9a-f]{4}$ ]] || die "proxmox: unexpected run ID '$RUN_ID'"
  owned='printf "%s\n" "$w" | grep -qxF image-build='"$RUN_ID"
  mutate pve sh -c "$(_pve_destroy_script "$vmid" "$owned" "is not tagged image-build=$RUN_ID")" >&2
  BUILD_VM_ID=""
}

backend_leftovers() {
  query "" _pve_leftovers
}

backend_destroy_id() {
  local now cfg rc=0 expired
  _pve_guard_vmid "$1" "$_PVE_LEFTOVER_MAX"
  now="$(_pve_now)"
  cfg="$(query '{"description": "image-build=ib-000000000000-0000 image-build-expires=000000000000"}' \
    _pve_config "$1")"
  printf '%s' "$cfg" | _pve_py expired "$now" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "proxmox: VM $1 is not a tagged, expired build VM on $PVE_NODE; not destroyed" >&2
    return 1
  fi
  # Re-checked on the node, in the destroy command itself: one image-build tag
  # in RUN_ID format, and one 12-digit expiry in the past.
  expired='[ "$(printf "%s\n" "$w" | grep -c "^image-build=")" = 1 ] && printf "%s\n" "$w" | grep -qx "image-build=ib-[0-9]\{12\}-[0-9a-f]\{4\}" && e=$(printf "%s\n" "$w" | sed -n "s/^image-build-expires=//p") && [ "$(printf "%s\n" "$e" | grep -cx "[0-9]\{12\}")" = 1 ] && [ "$e" -lt '"$now"' ]'
  mutate pve sh -c "$(_pve_destroy_script "$1" "$expired" "is not a tagged, expired build VM")" >&2
}

backend_templates_yaml() {
  if ! [[ "$1" =~ ^(${_PVE_NODE_CHARS}):([0-9]+),(${_PVE_NODE_CHARS}):([0-9]+)$ ]]; then
    die "proxmox: image ID '$1' is not <node>:<vmid>,<node>:<vmid>"
  fi
  printf 'template_locations: {%s: %s, %s: %s}\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" \
    "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"
}
