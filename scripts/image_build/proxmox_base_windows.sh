#!/usr/bin/env bash
#
# Create the Proxmox base template for Windows image builds,
# base-ws2022-<yyyymmdd>: Windows Server 2022 Standard Evaluation (Desktop
# Experience), installed unattended from the evaluation ISO, updated, and
# sysprepped. Runs on the build host, next to config.env, and reaches PVE_HOST
# as root over SSH (key auth), as build_image.sh does.
#
# Usage: scripts/image_build/proxmox_base_windows.sh [--apply] [options]
#   --iso VOLID         installation ISO
#                       (default local:iso/Windows_Server_2022_EVAL.iso)
#   --virtio-iso VOLID  virtio-win ISO (default local:iso/virtio-win-0.1.302.iso)
#   --iso-store STORE   storage with the `iso` content type for the run's own
#                       answer-file ISO, deleted afterwards (default local)
#   --vmid N            VMID in 8000-8499 (default: the lowest V with V and
#                       V+500 free, so the replica can sit at V+500)
#   --date YYYYMMDD     date in the name (default today, UTC)
#   --disk SIZE         system disk, e.g. 40G (default 40G); the Proxmox
#                       backend does not resize, so this is every image's
#                       disk size and the recipe's DISK_GB
#   --skip-updates      do not run Windows Update (the template then carries
#                       the ISO's build; say so wherever it is used)
#
# Dry run by default: reads the cluster and prints every command that would
# change it. --apply runs them. It never touches an existing guest: a VMID in
# use, a VMID outside 8000-8499, or a template name already taken is an
# error. Config: the PVE_ keys of config.env (IMAGE_BUILD_CONFIG overrides
# the path); it uses PVE_HOST, PVE_NODE, PVE_STORAGE, PVE_BUILD_BRIDGE and
# PVE_BUILD_VLAN. The guest-side files are in windows_base/ next to it.
#
# The template meets the README's base-template contract for Windows:
#   - Administrator is the only account; each clone gets its own random
#     password in the specialize pass (randomize-admin.ps1, never output);
#     no answer file holds a password;
#   - OpenSSH Server installed; key login for Administrator through
#     C:\ProgramData\ssh\administrators_authorized_keys (sshd's default
#     `Match Group administrators`), password login off, default shell
#     cmd.exe, tcp/22 open on every firewall profile;
#   - QEMU guest agent (and the virtio drivers) installed, starting
#     automatically;
#   - DHCP on its network interface, with no cached lease (released before
#     sysprep), so each clone asks for its own address;
#   - no SSH key, no host keys: sysprep /generalize /oobe /mode:vm, then
#     shut down. On a clone's first boot Windows runs specialize, reboots
#     once, runs OOBE unattended, and only then SetupComplete.cmd enables and
#     starts sshd, which writes the clone's own host keys, and deletes the
#     answer file and the password script. So an SSH session the builder
#     opens can no longer be cut by setup's own reboot.
#   - Windows Update applied until no update is left (unless --skip-updates),
#     superseded components removed.
# First boot installs from the ISO with an answer file on a per-run ISO
# (autounattend.xml with a random install password, plus the scripts); one
# automatic logon installs the virtio drivers and the guest agent; the rest is
# done through the guest agent, each long step as a SYSTEM scheduled task.
#
# A run that fails or is interrupted (INT, TERM, HUP) after it began to
# create the VM destroys that VM, but only after the node shows it is not a
# template and its description carries this run's construction marker
# (proxmox-base-build=<time>-<random>, written by qm create itself); any
# other VM at that VMID is named and left alone. The run's ISO is deleted on
# every exit, and an error names it if the node cannot confirm it is gone.
#
# Then prove the base (README "Base-template contract") with a test clone and
# replicate it: scripts/sync_template.sh <PVE_HOST> <vmid> <replica host>
#   <replica storage> <vmid+500>.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${IMAGE_BUILD_CONFIG:-$SCRIPT_DIR/config.env}"
FILES_DIR="$SCRIPT_DIR/windows_base"
# Copied onto the run's ISO; autounattend.xml is generated from its .in.
GUEST_FILES=(firstlogon.ps1 step.ps1 prepare.ps1 windows_update.ps1 cleanup.ps1 finalize.ps1
             sysprep-unattend.xml randomize-admin.ps1 SetupComplete.cmd)

APPLY=0
ISO=local:iso/Windows_Server_2022_EVAL.iso
VIRTIO_ISO=local:iso/virtio-win-0.1.302.iso
ISO_STORE=local
VMID=""
DATE="$(date -u +%Y%m%d)"
DISK=40G
UPDATES=1
# More while building (updates are CPU-bound); the template keeps what the
# Windows recipe asks for. The builder sets both per build anyway.
BUILD_MEMORY=8192
BUILD_CORES=4
TEMPLATE_MEMORY=4096
TEMPLATE_CORES=2
MAX_UPDATE_ROUNDS=8
MIN_AVAILABLE_KIB=$((16 * 1024 * 1024))
BLOCK_MIN=8000
BLOCK_MAX=8499
REPLICA_OFFSET=500

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*" >&2; }
stamp() { date -u +%H:%M:%S; }

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --iso) ISO="${2:?--iso needs a volume ID}"; shift ;;
        --virtio-iso) VIRTIO_ISO="${2:?--virtio-iso needs a volume ID}"; shift ;;
        --iso-store) ISO_STORE="${2:?--iso-store needs a storage}"; shift ;;
        --vmid) VMID="${2:?--vmid needs a VMID}"; shift ;;
        --date) DATE="${2:?--date needs YYYYMMDD}"; shift ;;
        --disk) DISK="${2:?--disk needs a size}"; shift ;;
        --skip-updates) UPDATES=0 ;;
        -h|--help) sed -n '3,61p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'ERROR: unknown option %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

# --- config: only the PVE_ keys come back, as in build_image.sh ------------
[ -f "$CONFIG" ] || die "config $CONFIG does not exist"
assignments="$(
    # shellcheck source=/dev/null
    source "$CONFIG" >&2 || exit 1
    for name in $(compgen -v PVE_); do
        printf '%s=%q\n' "$name" "${!name}"
    done
)" || die "config $CONFIG could not be loaded"
eval "$assignments"
PVE_NODE="${PVE_NODE:-pve}"
for key in PVE_HOST PVE_NODE PVE_STORAGE PVE_BUILD_BRIDGE PVE_BUILD_VLAN; do
    [ -n "${!key:-}" ] || die "$key is not set in $CONFIG"
done
for key in PVE_HOST PVE_NODE PVE_STORAGE PVE_BUILD_BRIDGE; do
    [[ "${!key}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "$key must match [A-Za-z0-9][A-Za-z0-9._-]* (got '${!key}')"
done
[[ "$PVE_BUILD_VLAN" =~ ^[1-9][0-9]{0,3}$ && "$PVE_BUILD_VLAN" -le 4094 ]] \
    || die "PVE_BUILD_VLAN must be 1-4094 (got '$PVE_BUILD_VLAN')"

for opt in "--iso=$ISO" "--virtio-iso=$VIRTIO_ISO"; do
    [[ "${opt#*=}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*:iso/[A-Za-z0-9._+-]+\.[iI][sS][oO]$ ]] \
        || die "${opt%%=*} must be a <storage>:iso/<file>.iso volume ID (got '${opt#*=}')"
done
[[ "$ISO_STORE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "--iso-store must be a storage name (got '$ISO_STORE')"
[[ "$DATE" =~ ^20[0-9]{6}$ ]] || die "--date must be YYYYMMDD (got '$DATE')"
[[ "$DISK" =~ ^([1-9][0-9]*)G$ ]] || die "--disk must look like 40G (got '$DISK')"
DISK_GIB="${BASH_REMATCH[1]}"
if [ -n "$VMID" ]; then
    [[ "$VMID" =~ ^[0-9]{4}$ ]] && [ "$VMID" -ge "$BLOCK_MIN" ] && [ "$VMID" -le "$BLOCK_MAX" ] \
        || die "--vmid must be in $BLOCK_MIN-$BLOCK_MAX (got '$VMID'); never 9000 or above"
fi
command -v python3 >/dev/null || die "python3 is required"
for f in autounattend.xml.in "${GUEST_FILES[@]}"; do
    [ -f "$FILES_DIR/$f" ] || die "$FILES_DIR/$f is missing"
done

NAME="base-ws2022-$DATE"
GEN_ISO_FILE="$NAME-answer.iso"
GEN_ISO="$ISO_STORE:iso/$GEN_ISO_FILE"

# --- remote layer ----------------------------------------------------------
# One argument for the node's root shell: bare when it holds only characters
# no shell reads, single-quoted otherwise.
q() {
    if [[ "$1" =~ ^[A-Za-z0-9._/:=,@+%-]+$ ]]; then
        printf '%s' "$1"
    else
        printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
    fi
}
line_of() {
    local arg line=""
    for arg in "$@"; do line+="${line:+ }$(q "$arg")"; done
    printf '%s' "$line"
}
# Reads, always run. pve_in passes this script's stdin through.
pve() { ssh -n -o BatchMode=yes -o ServerAliveInterval=30 root@"$PVE_HOST" "$(line_of "$@")"; }
pve_in() { ssh -o BatchMode=yes -o ServerAliveInterval=30 root@"$PVE_HOST" "$(line_of "$@")"; }
# Changes: printed in a dry run, run with --apply.
run() {
    if [ "$APPLY" -eq 1 ]; then say "+ $(line_of "$@")"; pve "$@"; else say "would run: $(line_of "$@")"; fi
}

py() { python3 -c "$1" "${@:2}"; }

# --- guest layer (through the QEMU guest agent, as SYSTEM) ------------------
# PowerShell from stdin, run in the guest; prints the agent's JSON. The
# script travels as -EncodedCommand, so no shell or cmd.exe quoting applies.
gexec() {
    local enc
    # No progress records: over the agent they arrive as CLIXML on stderr.
    enc="$({ printf "\$ProgressPreference = 'SilentlyContinue'\n"; cat; } \
        | py 'import base64, sys; print(base64.b64encode(sys.stdin.read().encode("utf-16-le")).decode())')"
    pve qm guest exec "$VMID" --timeout "$1" -- powershell.exe -NoProfile -NonInteractive \
        -ExecutionPolicy Bypass -EncodedCommand "$enc"
}

# The agent's JSON for one finished command: print its output, fail unless
# it exited 0.
exec_ok() {
    py '
import json, sys
r = json.load(sys.stdin)
for k in ("out-data", "err-data"):
    if r.get(k):
        sys.stderr.write(r[k] if r[k].endswith("\n") else r[k] + "\n")
if r.get("exited") not in (1, True):
    sys.exit("guest command did not finish within the timeout")
sys.exit(0 if r.get("exitcode") == 0 else "guest command exited %s" % r.get("exitcode"))
'
}

# stdout of a guest command that exited 0; non-zero (and no output) otherwise.
exec_out() {
    py '
import json, sys
r = json.load(sys.stdin)
if r.get("exited") not in (1, True) or r.get("exitcode") != 0:
    sys.exit(1)
sys.stdout.write(r.get("out-data", ""))
'
}

wait_agent() {
    local limit="$1" start=$SECONDS
    say "$(stamp) waiting up to $((limit / 60)) min for the guest agent"
    until pve qm guest cmd "$VMID" ping >/dev/null 2>&1; do
        [ $((SECONDS - start)) -lt "$limit" ] || die "no guest agent on VM $VMID after $((limit / 60)) minutes"
        sleep 15
    done
    say "$(stamp) guest agent answers ($((SECONDS - start)) s)"
}

# PowerShell that prints this boot's identity: the record ID of the newest
# "operating system started" event (Kernel-General 12) in the System log.
# Each boot writes one and normal log rotation keeps record IDs growing, so
# any change is a reboot (LastBootUpTime is not: it moves when the clock is
# stepped, and a fast reboot can land within the same minute). The one
# exception is finalize.ps1, which clears the System log; it must stay the
# last step, because no step after it may rely on BOOT_PS.
BOOT_PS="[string](Get-WinEvent -MaxEvents 1 -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=12}).RecordId"

# Wait until the guest has settled after a boot: Windows servicing
# (TrustedInstaller) can restart it on its own shortly after an update
# reboot, and a step started before that is killed with it. Settled: the
# same boot and TrustedInstaller stopped on three reads 30 s apart.
settle() {
    local start=$SECONDS boot="" quiet=0 cur
    say "$(stamp) waiting for the guest to settle (no servicing restart)"
    while [ "$quiet" -lt 3 ]; do
        [ $((SECONDS - start)) -lt 3600 ] || die "VM $VMID did not settle within 60 minutes (Windows servicing still running or rebooting)"
        sleep 30
        cur="$(printf '"%s" + " " + (Get-Service TrustedInstaller).Status\n' "\$($BOOT_PS)" \
               | gexec 30 2>/dev/null | exec_out 2>/dev/null | tr -d '\r\n')" || cur=""
        if [[ "$cur" =~ ^([0-9]+)\ Stopped$ ]] && [ "${BASH_REMATCH[1]}" = "$boot" ]; then
            quiet=$((quiet + 1))
        else
            quiet=0
            if [[ "$cur" =~ ^([0-9]+)\  ]]; then
                [ -z "$boot" ] || [ "${BASH_REMATCH[1]}" = "$boot" ] || say "$(stamp) the guest booted again (Windows servicing)"
                boot="${BASH_REMATCH[1]}"
                [[ "$cur" == *Stopped ]] && quiet=1
            fi
        fi
    done
    say "$(stamp) settled on boot $boot ($((SECONDS - start)) s)"
}

# Run windows_base/<name>.ps1 in the guest as a SYSTEM scheduled task
# (step.ps1) and wait for it; prints its output and returns its exit code,
# or 99 if the guest rebooted while the step ran (the step was killed).
run_step() {
    local name="$1" limit="$2" start=$SECONDS st="" rc boot now
    say "$(stamp) step $name (up to $((limit / 60)) min)"
    boot="$(printf '%s\n' "$BOOT_PS" | gexec 30 | exec_out | tr -d '\r\n')" \
        && [[ "$boot" =~ ^[0-9]+$ ]] || die "cannot read the boot identity of VM $VMID"
    printf '& C:\\basebuild\\step.ps1 -Start -Name %s\n' "$name" | gexec 60 | exec_ok \
        || die "could not start step $name in VM $VMID"
    while :; do
        sleep 20
        # The agent can be briefly busy or unreachable: keep polling.
        st="$(printf '(Get-Content -LiteralPath C:\\basebuild\\%s.status) + "|" + (%s)\n' "$name" "$BOOT_PS" \
              | gexec 30 2>/dev/null | exec_out 2>/dev/null | tr -d '\r\n')" || st=""
        now="${st#*|}"
        st="${st%%|*}"
        case "$st" in
            "exit "*) break ;;
        esac
        if [[ "$now" =~ ^[0-9]+$ ]] && [ "$now" != "$boot" ]; then
            say "$(stamp) step $name: the guest rebooted while it ran (boot $boot -> $now); the step was killed"
            return 99
        fi
        [ $((SECONDS - start)) -lt "$limit" ] || die "step $name in VM $VMID still running after $((limit / 60)) minutes"
    done
    rc="${st#exit }"
    say "$(stamp) step $name: exit $rc ($((SECONDS - start)) s)"
    printf 'Get-Content -LiteralPath C:\\basebuild\\%s.out -ErrorAction SilentlyContinue\nGet-Content -LiteralPath C:\\basebuild\\%s.err -ErrorAction SilentlyContinue | ForEach-Object { "[stderr] $_" }\n' \
        "$name" "$name" | gexec 60 | exec_out | sed 's/^/    /' >&2 || true
    [[ "$rc" =~ ^[0-9]+$ ]] || die "step $name reported '$st'"
    return "$rc"
}

# Run a step that must exit 0; one killed by a servicing reboot runs again
# once the guest has settled (every step is safe to repeat).
run_step_ok() {
    local rc tries=0
    while :; do
        rc=0
        run_step "$@" || rc=$?
        case "$rc" in
            0) return 0 ;;
            99) tries=$((tries + 1)); [ "$tries" -lt 3 ] || die "step $1 was cut by a reboot three times"; settle ;;
            *) die "step $1 failed in VM $VMID (exit $rc)" ;;
        esac
    done
}

# Reboot the guest, wait for the agent, and let it settle.
reboot_guest() {
    run qm reboot "$VMID" --timeout 1800
    wait_agent 3600
    settle
}

# --- checks (read-only, in both modes) -------------------------------------
node="$(pve hostname)" || die "cannot reach root@$PVE_HOST over SSH"
[ "${node%%.*}" = "$PVE_NODE" ] || die "root@$PVE_HOST is node '${node%%.*}', but PVE_NODE is '$PVE_NODE'"

resources="$(pve pvesh get /cluster/resources --type vm --output-format json)"
# Prints the chosen VMID, or exits 1 naming why.
VMID="$(printf '%s' "$resources" | py '
import json, sys
want, name, lo, hi, off = sys.argv[1], sys.argv[2], *map(int, sys.argv[3:6])
r = json.load(sys.stdin)
used = {int(x["vmid"]) for x in r}
for x in r:
    if x.get("name") == name:
        sys.exit("a guest named %s already exists (VMID %s); pick another --date" % (name, x["vmid"]))
if want:
    v = int(want)
    if v in used or v + off in used:
        sys.exit("VMID %d or its replica %d is in use; this script never touches an existing guest" % (v, v + off))
    print(v)
    sys.exit(0)
for v in range(lo, hi + 1):
    if v not in used and v + off not in used:
        print(v)
        sys.exit(0)
sys.exit("no free VMID pair in %d-%d" % (lo, hi))
' "$VMID" "$NAME" "$BLOCK_MIN" "$BLOCK_MAX" "$REPLICA_OFFSET")" || die "no usable VMID (see above)"
[[ "$VMID" =~ ^[0-9]{4}$ ]] && [ "$VMID" -ge "$BLOCK_MIN" ] && [ "$VMID" -le "$BLOCK_MAX" ] \
    || die "VMID '$VMID' is outside $BLOCK_MIN-$BLOCK_MAX; refusing"
# The config file is the last word on whether a VMID exists.
if [ -n "$(pve sh -c "ls /etc/pve/nodes/*/qemu-server/$VMID.conf /etc/pve/nodes/*/lxc/$VMID.conf 2>/dev/null || true")" ]; then
    die "VMID $VMID is in use"
fi

avail_kib="$(pve sh -c "sed -n 's/^MemAvailable: *\([0-9]*\) kB/\1/p' /proc/meminfo")"
[[ "$avail_kib" =~ ^[0-9]+$ ]] || die "cannot read MemAvailable on $PVE_NODE"
[ "$avail_kib" -ge "$MIN_AVAILABLE_KIB" ] \
    || die "$PVE_NODE has $((avail_kib / 1024 / 1024)) GiB available; 16 GiB is the floor"

iso_path="$(pve pvesm path "$ISO")" || die "cannot resolve $ISO on $PVE_NODE"
virtio_path="$(pve pvesm path "$VIRTIO_ISO")" || die "cannot resolve $VIRTIO_ISO on $PVE_NODE"
pve test -f "$iso_path" || die "installation ISO $iso_path not found on $PVE_NODE"
pve test -f "$virtio_path" || die "virtio-win ISO $virtio_path not found on $PVE_NODE"
gen_iso_path="$(pve pvesm path "$GEN_ISO")" || die "storage $ISO_STORE has no iso content type"
pve test -d "$(dirname "$gen_iso_path")" || die "ISO directory $(dirname "$gen_iso_path") does not exist on $PVE_NODE"
if pve test -e "$gen_iso_path"; then
    die "$gen_iso_path already exists; remove it or pick another --date"
fi
pve sh -c 'command -v genisoimage' >/dev/null || die "genisoimage is not installed on $PVE_NODE"
say "hashing $iso_path (a few minutes over NFS)"
iso_sha="$(pve sha256sum "$iso_path" | cut -d' ' -f1)"
virtio_sha="$(pve sha256sum "$virtio_path" | cut -d' ' -f1)"

in_block="$(printf '%s' "$resources" | py '
import json, sys
for x in sorted(json.load(sys.stdin), key=lambda x: x["vmid"]):
    if 8000 <= int(x["vmid"]) <= 8999:
        print("  %s %s %s %s" % (x["vmid"], x.get("node"), x.get("name"), "template" if x.get("template") else x.get("status")))
')"

if [ "$APPLY" -eq 1 ]; then say "== Windows base template: APPLY =="; else say "== Windows base template: dry run (nothing will change) =="; fi
say "VM $VMID '$NAME' on $PVE_NODE: disk $DISK on $PVE_STORAGE, net0 $PVE_BUILD_BRIDGE VLAN $PVE_BUILD_VLAN (DHCP)"
say "  install:   $ISO (sha256 $iso_sha)"
say "  virtio:    $VIRTIO_ISO (sha256 $virtio_sha)"
say "  answer:    $GEN_ISO (this run's; deleted afterwards)"
say "  updates:   $([ "$UPDATES" -eq 1 ] && echo "Windows Update until none is left (at most $MAX_UPDATE_ROUNDS rounds)" || echo 'SKIPPED (--skip-updates)')"
say "  $PVE_NODE MemAvailable: $((avail_kib / 1024 / 1024)) GiB"
say "  guests in 8000-8999 now:${in_block:+$'\n'$in_block}$([ -n "$in_block" ] || echo ' none')"

# --- the run's answer-file ISO ---------------------------------------------
# autounattend.xml with this run's install password; never printed.
INSTALL_PASSWORD="$(py '
import secrets, string
alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!#%*-_=+"
while True:
    p = "".join(secrets.choice(alphabet) for _ in range(32))
    if any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p) \
       and any(not c.isalnum() for c in p):
        print(p)
        break
')"
answer_file() {
    PW="$1" py '
import os, sys
t = open(sys.argv[1], encoding="utf-8").read()
if "@@ADMIN_PASSWORD@@" not in t:
    sys.exit("no @@ADMIN_PASSWORD@@ in " + sys.argv[1])
sys.stdout.write(t.replace("@@ADMIN_PASSWORD@@", os.environ["PW"]))
' "$FILES_DIR/autounattend.xml.in"
}
answer_file "$INSTALL_PASSWORD" | py 'import sys, xml.dom.minidom; xml.dom.minidom.parseString(sys.stdin.read())' \
    || die "autounattend.xml.in does not produce well-formed XML"

# --- cleanup ---------------------------------------------------------------
# This run's construction marker. qm create writes it into the description,
# so the VM is recognised by what the node holds, not by a flag set after a
# call returns. The marker stays until the VM is a template.
RUN_TAG="proxmox-base-build=$(date -u +%Y%m%d%H%M%S)-$(py 'import secrets; print(secrets.token_hex(4))')"
[[ "$RUN_TAG" =~ ^proxmox-base-build=[0-9]{14}-[0-9a-f]{8}$ ]] || die "unexpected run marker '$RUN_TAG'"

# The node-side destroy of VM $VMID: one sh script, so the node itself checks
# the marker immediately before `qm stop` and again before `qm destroy`, with
# no round trip in which the VMID could change hands (as the Proxmox
# backend's destroy does). Exit 0: destroyed. 5: no such VMID anywhere.
# 3: refused (a template, another node's guest, or no marker). 4: the config
# could not be read. 6: qm stop or qm destroy failed (255: SSH failed).
cleanup_script() {
    cat <<EOF
set -e
v=$VMID
check() {
  if ! c=\$(qm config \$v 2>/dev/null); then
    e=\$(qm config \$v 2>&1 >/dev/null) || true
    case "\$e" in
      *"/\$v.conf' does not exist"*) ;;
      *) echo "cannot read the config of VM \$v (\${e:-qm config failed})" >&2; exit 4 ;;
    esac
    if ! ls -d /etc/pve/nodes >/dev/null 2>&1; then
      echo "/etc/pve/nodes is not readable (cluster filesystem down?)" >&2; exit 4
    fi
    if [ -n "\$(ls /etc/pve/nodes/*/qemu-server/\$v.conf /etc/pve/nodes/*/lxc/\$v.conf 2>/dev/null)" ]; then
      echo "VMID \$v is a guest on another node" >&2; exit 3
    fi
    exit 5
  fi
  if printf '%s\n' "\$c" | grep -qx 'template: 1'; then
    echo "VM \$v is a template" >&2; exit 3
  fi
  if ! printf '%s\n' "\$c" | grep -qxF 'name: $NAME' \\
     || ! printf '%s\n' "\$c" | sed -n 's/^description: //p' | tr ' ' '\n' | grep -qxF '$RUN_TAG'; then
    echo "VM \$v is not named $NAME or does not carry this run's marker $RUN_TAG" >&2; exit 3
  fi
}
check
qm stop \$v || exit 6
check
qm destroy \$v --purge 1 || exit 6
EOF
}

# Destroy VM $VMID if, and only if, the node shows it carries RUN_TAG.
destroy_vm() {
    local rc=0
    if ! [[ "$VMID" =~ ^[0-9]{4}$ && "$VMID" -ge "$BLOCK_MIN" && "$VMID" -le "$BLOCK_MAX" ]]; then
        say "ERROR: VMID '$VMID' is outside $BLOCK_MIN-$BLOCK_MAX; not touched"
        return 1
    fi
    say "+ destroy VM $VMID on $PVE_NODE if it carries $RUN_TAG"
    pve sh -c "$(cleanup_script)" >&2 || rc=$?
    case "$rc" in
        0)
            VM_ARMED=0
            say "VM $VMID ('$NAME', $RUN_TAG) was destroyed: the run stopped before the template was finished." ;;
        5)
            VM_ARMED=0
            say "No VM $VMID exists on the cluster now; nothing was destroyed. If this run stopped while"
            say "  qm create was still running on $PVE_NODE, check again: a VM $VMID named $NAME whose"
            say "  description carries $RUN_TAG is this run's (qm config $VMID; qm destroy $VMID --purge 1)." ;;
        *)
            say "ERROR: VM $VMID was NOT destroyed (exit $rc; reason above). Inspect it on $PVE_NODE: qm config $VMID."
            say "       Remove it by hand (qm destroy $VMID --purge 1) only if it is not a template and its"
            say "       description carries this run's marker $RUN_TAG."
            return 1 ;;
    esac
}

# Delete the run's ISO (and its staging directory); the state is cleared only
# once the node confirms both are gone.
remove_iso() {
    say "+ rm -rf $stage_dir $gen_iso_path"
    if [ "$(pve sh -c "rm -rf -- $(q "$stage_dir") $(q "$gen_iso_path") && if [ -e $(q "$gen_iso_path") ] || [ -e $(q "$stage_dir") ]; then echo present; else echo absent; fi")" = absent ]; then
        ISO_ARMED=0
        return 0
    fi
    say "ERROR: the run's answer-file ISO $gen_iso_path (or $stage_dir) on $PVE_NODE was NOT deleted;"
    say "       it holds the install password. Delete it by hand: rm -rf $(q "$gen_iso_path") $(q "$stage_dir")."
    return 1
}

# Set before the change each guards, so an interruption at any point is
# covered; each is cleared only once its cleanup is confirmed.
VM_ARMED=0
ISO_ARMED=0
stage_dir="/tmp/$NAME-answer.$$"
on_exit() {
    local status=$?
    set +e
    trap '' INT TERM HUP
    if [ "$VM_ARMED" -eq 1 ]; then
        [ "$status" -ne 0 ] || status=1
        say ""
        say "ERROR: the run stopped before template $VMID was finished; cleaning up."
        destroy_vm
    fi
    if [ "$ISO_ARMED" -eq 1 ]; then
        remove_iso || { [ "$status" -ne 0 ] || status=1; }
    fi
    exit "$status"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# --- apply -----------------------------------------------------------------
say "  marker:    $RUN_TAG (in the description until the VM is a template)"
START=$SECONDS
if [ "$APPLY" -eq 1 ]; then
    # The paths were checked absent above: what is there from now on is ours.
    ISO_ARMED=1
    say "+ write $gen_iso_path (autounattend.xml, BASEBUILD.TAG, ${GUEST_FILES[*]})"
    pve mkdir -m 700 "$stage_dir"
    answer_file "$INSTALL_PASSWORD" | pve_in sh -c "cat > $(q "$stage_dir/autounattend.xml")"
    for f in "${GUEST_FILES[@]}"; do
        pve_in sh -c "cat > $(q "$stage_dir/$f")" < "$FILES_DIR/$f"
    done
    printf 'proxmox_base_windows.sh %s\n' "$RUN_TAG" | pve_in sh -c "cat > $(q "$stage_dir/BASEBUILD.TAG")"
    pve genisoimage -quiet -J -r -V BASEBUILD -o "$gen_iso_path" "$stage_dir"
    pve chmod 600 "$gen_iso_path"
    pve rm -rf "$stage_dir"
else
    say "would write $gen_iso_path from $stage_dir: autounattend.xml (password elided), BASEBUILD.TAG, ${GUEST_FILES[*]}"
    answer_file '<random, never printed>' | sed 's/^/    /' >&2
fi
unset INSTALL_PASSWORD

VM_ARMED="$APPLY"
# The RTC in UTC (Proxmox's default for Windows is the node's local time):
# Windows runs in UTC here, so it boots with the right clock instead of
# stepping it at the first time sync. A step moves LastBootUpTime, the
# image builder's Windows boot ID.
run qm create "$VMID" --name "$NAME" --ostype win11 --localtime 0 --cores "$BUILD_CORES" --memory "$BUILD_MEMORY" \
    --agent enabled=1 --scsihw virtio-scsi-single --scsi0 "$PVE_STORAGE:$DISK_GIB,discard=on" \
    --ide2 "$ISO,media=cdrom" --ide0 "$VIRTIO_ISO,media=cdrom" --ide3 "$GEN_ISO,media=cdrom" \
    --net0 "virtio,bridge=$PVE_BUILD_BRIDGE,tag=$PVE_BUILD_VLAN" --boot 'order=scsi0;ide2' \
    --description "$RUN_TAG $NAME under construction by proxmox_base_windows.sh; not a template yet"
run qm start "$VMID"

if [ "$APPLY" -eq 1 ]; then
    # Setup (about 10-20 minutes) ends with one automatic logon, whose
    # first-logon script installs the agent.
    wait_agent 5400
    fl="$(printf 'Get-Content -LiteralPath C:\\basebuild\\firstlogon.done\n' | gexec 30 | exec_out | tr -d '\r\n')" || fl=""
    printf 'Get-Content -LiteralPath C:\\basebuild\\firstlogon.log\n' | gexec 30 | exec_out | sed 's/^/    /' >&2 || true
    [ "$fl" = ok ] || die "the first-logon script did not finish cleanly ('$fl')"

    run_step_ok prepare 2700

    if [ "$UPDATES" -eq 1 ]; then
        round=0 failures=0 rc=0
        while :; do
            round=$((round + 1))
            [ "$round" -le "$MAX_UPDATE_ROUNDS" ] || die "Windows Update still had updates after $MAX_UPDATE_ROUNDS rounds"
            say "== Windows Update round $round"
            rc=0
            run_step windows_update 7200 || rc=$?
            case "$rc" in
                0) say "$(stamp) Windows Update: nothing left to install"; break ;;
                10) failures=0 ;;
                11) failures=0; reboot_guest ;;
                99) settle ;;
                *)
                    failures=$((failures + 1))
                    [ "$failures" -lt 2 ] || die "Windows Update failed in two rounds running"
                    say "Windows Update round $round failed; rebooting and trying again"
                    reboot_guest ;;
            esac
        done
    fi

    run_step_ok cleanup 5400
    # Servicing may finish at the next boot (or the one after); sysprep
    # refuses a pending reboot.
    reboots=0
    while :; do
        reboot_guest
        pending="$(gexec 30 <<'PS' | exec_out | tr -d '\r\n'
(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
PS
)" || die "cannot read the pending-reboot state of VM $VMID"
        [ "$pending" = True ] || break
        reboots=$((reboots + 1))
        [ "$reboots" -lt 3 ] || die "VM $VMID still has a reboot pending after three reboots"
        say "$(stamp) a reboot is still pending; rebooting again"
    done
    run_step_ok finalize 1800

    say "$(stamp) sysprep /generalize /oobe /shutdown /mode:vm"
    gexec 60 <<'PS' | exec_ok || die "could not start sysprep in VM $VMID"
$ErrorActionPreference = 'Stop'
Get-ScheduledTask -TaskName 'basebuild-*' | Unregister-ScheduledTask -Confirm:$false
Remove-Item -Recurse -Force -LiteralPath 'C:\basebuild'
# Without this every clone boots with the template's cached lease (dnsmasq,
# not authoritative, ignores a request for another client's address, and
# Windows keeps an unexpired lease when its gateway answers): all clones on
# one VLAN would share one address. Released, a clone asks with its own MAC.
& ipconfig.exe /release | Out-Null
Start-Process -FilePath 'C:\Windows\System32\Sysprep\sysprep.exe' -ArgumentList @(
    '/generalize', '/oobe', '/shutdown', '/mode:vm', '/quiet',
    '/unattend:C:\Windows\System32\Sysprep\base-unattend.xml')
PS
    # Sysprep that fails exits and leaves the VM running; one that succeeds
    # shuts it down. Six reads in a row (about two minutes) with the agent
    # answering and no sysprep.exe left mean it failed.
    t0=$SECONDS gone=0
    until [ "$(pve qm status "$VMID")" = "status: stopped" ]; do
        sp="$(printf '@(Get-Process -Name sysprep -ErrorAction SilentlyContinue).Count\n' | gexec 30 2>/dev/null \
              | exec_out 2>/dev/null | tr -d '\r\n')" || sp=""
        if [ "$sp" = 0 ]; then gone=$((gone + 1)); else gone=0; fi
        if [ "$gone" -ge 6 ] || [ $((SECONDS - t0)) -ge 2700 ]; then
            printf 'Get-Content -LiteralPath C:\\Windows\\System32\\Sysprep\\Panther\\setuperr.log -ErrorAction SilentlyContinue\n' \
                | gexec 60 | exec_out | sed 's/^/    /' >&2 || true
            die "sysprep in VM $VMID ended without shutting the VM down (setuperr.log above)"
        fi
        sleep 15
    done
    say "$(stamp) sysprep shut the VM down ($((SECONDS - t0)) s)"
else
    say "would wait for the guest agent (installed by the first automatic logon), then run as SYSTEM"
    say "  through the agent: step prepare; Windows Update rounds (reboot when asked); step cleanup;"
    say "  reboot; step finalize; sysprep /generalize /oobe /shutdown /mode:vm; wait for 'stopped'."
fi

# The ISOs go: a clone needs none of them.
run qm set "$VMID" --delete ide0,ide2,ide3
run qm set "$VMID" --memory "$TEMPLATE_MEMORY" --cores "$TEMPLATE_CORES" --boot order=scsi0
if [ "$APPLY" -eq 1 ]; then
    if pve qm config "$VMID" | grep -E '^(ide[0-9]|sata[0-9]|unused[0-9]*):'; then
        die "VM $VMID still has a CD drive or an unused volume (above)"
    fi
fi
run qm template "$VMID"
# A template is finished: from here a failure is reported, never destroyed.
VM_ARMED=0
run qm set "$VMID" --description "$NAME: Windows Server 2022 Standard Evaluation (Desktop Experience) from $(basename "$iso_path") sha256 $iso_sha, virtio-win $(basename "$virtio_path") sha256 $virtio_sha, $([ "$UPDATES" -eq 1 ] && echo 'Windows Update applied' || echo 'NO Windows Update'), built $(date -u +%Y-%m-%d) by scripts/image_build/proxmox_base_windows.sh. Sysprepped (generalize, oobe, mode:vm). Administrator only, random unrecorded password. OpenSSH Server, key login only, no keys; sshd starts after first-boot setup. QEMU guest agent. Evaluation licence." \
    || die "template $VMID is finished, but setting its description failed; it still reads 'under construction' with $RUN_TAG: set it by hand (qm set $VMID --description ...)"
if [ "$APPLY" -eq 1 ]; then
    remove_iso || exit 1
else
    say "would run: rm -f $gen_iso_path"
fi

say ""
if [ "$APPLY" -ne 1 ]; then
    say "Dry run only. Re-run with --apply to create template $VMID."
    exit 0
fi
say "Template $VMID '$NAME' is ready on $PVE_NODE ($(( (SECONDS - START) / 60 )) min). Next:"
say "  1. Prove it: full-clone it to a test VMID in the block, boot it, check the contract, destroy the clone."
say "  2. Replicate: scripts/sync_template.sh $PVE_HOST $VMID <replica host> <replica storage> $((VMID + REPLICA_OFFSET))"
printf '%s\n' "$VMID"
