#!/usr/bin/env bash
# Seal a Linux build VM just before its image is saved.
#
# Usage: seal_linux.sh <yes|no>      (keep_ssh)
#
# Removes the uploaded payload, empties the build key, and strips the identity
# the image would otherwise share with the build VM. Fails (exit 1) if any
# payload file is still present, so the build stops before the image is saved.
# Every path is prefixed with ${SEAL_ROOT:-} so tests can run against a temp tree.
set -euo pipefail

keep_ssh="${1:-}"
case "$keep_ssh" in
  yes|no) ;;
  *) echo "seal_linux.sh: keep_ssh must be 'yes' or 'no' (got '${keep_ssh}')" >&2; exit 2 ;;
esac

R="${SEAL_ROOT:-}"
shopt -s nullglob

# Disable and mask one systemd unit. A unit that does not exist (ssh.socket on
# a base older than Ubuntu 22.10) is skipped; any other failure fails the seal.
ssh_unit_off() {
  local unit="$1" err
  if ! err="$(systemctl disable "$unit" 2>&1)"; then
    case "$err" in
      *"does not exist"*)
        echo "seal_linux.sh: $unit does not exist; skipped" >&2
        return 0 ;;
      *)
        echo "seal_linux.sh: systemctl disable $unit failed: $err" >&2
        return 1 ;;
    esac
  fi
  systemctl mask "$unit"
}

# 1. The payload: setup, verify (solve steps), tarballs.
rm -rf "$R/tmp/image-build"

# 2. The build key, and the account's password. Locked even on a base
#    (keep_ssh=yes): with UsePAM (Ubuntu's default) sshd still admits a
#    public key to a password-locked account, so the next build logs in.
if [ -f "$R/home/builder/.ssh/authorized_keys" ]; then
  truncate -s 0 "$R/home/builder/.ssh/authorized_keys"
fi
passwd -l builder

# 3. Host SSH ships off (this session survives until the VM is stopped).
#    Since Ubuntu 22.10 ssh.socket owns port 22 and starts ssh.service on
#    demand, so both units are disabled and masked; both Debian/Ubuntu units
#    also refuse to start while sshd_not_to_be_run exists. RHEL's unit is
#    `sshd` and is not handled here.
if [ "$keep_ssh" = no ]; then
  ssh_unit_off ssh.service
  ssh_unit_off ssh.socket
  touch "$R/etc/ssh/sshd_not_to_be_run"
fi

# 4. cloud-init state, so the clone runs first-boot again.
if command -v cloud-init >/dev/null 2>&1; then
  cloud-init clean --logs
fi

# 5. Machine identity.
truncate -s 0 "$R/etc/machine-id"

# 6. Host keys, plus a drop-in that regenerates them at boot on a base without
#    cloud-init. ssh.service's own `ExecStartPre=/usr/sbin/sshd -t` fails with
#    no host keys, and a drop-in's ExecStartPre= lines run after the unit's, so
#    the list is reset and the check re-added after ssh-keygen.
rm -f "$R"/etc/ssh/ssh_host_*
mkdir -p "$R/etc/systemd/system/ssh.service.d"
printf '[Service]\nExecStartPre=\nExecStartPre=/usr/bin/ssh-keygen -A\nExecStartPre=/usr/sbin/sshd -t\n' \
  > "$R/etc/systemd/system/ssh.service.d/10-regen-hostkeys.conf"

# 7. Shell history.
for f in "$R"/root/.bash_history "$R"/home/*/.bash_history; do
  truncate -s 0 "$f"
done

# 8. Self-check: the payload must be gone.
if [ -e "$R/tmp/image-build" ]; then
  echo "seal_linux.sh: /tmp/image-build still exists after removal" >&2
  exit 1
fi
leftover=""
for dir in "$R/tmp" "$R/root" "$R/home"; do
  [ -d "$dir" ] || continue
  leftover="$leftover$(find "$dir" \( -name images.tar -o -name verify.sh -o -name setup.sh \) 2>/dev/null)"
done
if [ -n "$leftover" ]; then
  echo "seal_linux.sh: payload files survive:" >&2
  echo "$leftover" >&2
  exit 1
fi
echo "sealed"
