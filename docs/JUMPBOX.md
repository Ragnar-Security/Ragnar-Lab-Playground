# Lab jumpbox

The jumpbox is one Ubuntu VM that routes for every lab VLAN. It is:

- the default gateway and DHCP server for each lab VLAN;
- a Tailscale subnet router that advertises `10.0.0.0/8` to the Headscale
  tailnet, so a student on the VPN reaches lab VMs directly;
- optionally, a Zeek/RITA sensor on the lab trunk.

Students do not need to log in to the jumpbox. If the jumpbox stops, every
lab VLAN loses DHCP and every student loses VPN access to the labs.

```
student laptop ──tailnet── Headscale (vpn.example.edu)
                               │
                 jumpbox (tag:jumpbox, advertises 10.0.0.0/8, no SNAT)
                 ├─ net0  management: vmbr0, VLAN 1, DHCP
                 └─ net1  trunk: vmbr0, untagged, firewall on
                     ├─ vlan100  10.100.0.1/24  ─ student entry network
                     ├─ vlan200  10.100.1.1/24  ─ student internal network
                     ├─ vlan300  10.100.2.1/24  ─ group entry network
                     └─ vlan400  10.100.4.1/24  ─ group internal network …
```

Three scripts build it. Each one runs where its comment says, and each is a
dry run unless you pass `--apply`:

| Script | Runs on | Does |
|--------|---------|------|
| `scripts/jumpbox_provision.sh` | the Proxmox node | creates the VM: cloud image, full disk, two NICs, cloud-init login |
| `scripts/jumpbox_configure.sh` | the jumpbox | netplan VLAN gateways, dnsmasq, forwarding sysctls, root disk growth; optionally Tailscale, key-only SSH, staff-only SSH, inter-VLAN isolation, Zeek/RITA |
| `scripts/jumpbox_accounts.sh` | the jumpbox | optional student SSH accounts: `create`, `lock-stale`, `unlock` |

`scripts/jumpbox_netgen.py` renders the netplan and dnsmasq files for
`jumpbox_configure.sh`. Settings come from `scripts/site.env`; see the
jumpbox section of `scripts/site.env.example`.

## Address plan

One setting, `VLAN_BANDS`, is the whole VLAN layout. The portal allocates
every VLAN from it and the jumpbox routes exactly it, so it must have the
same value in the portal's `.env` and in `scripts/site.env`. Each band is
`name:first-last:offset:third`, and VLAN tag *N* in a band gets the /24
`10.<N-offset>.<third>.0/24`. The jumpbox holds `.1`, and dnsmasq leases
`.50` to `.254` for 12 hours. The default:

```
VLAN_BANDS="entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4"
#           name:first-last:offset:third  ->  VLAN N = 10.<N-offset>.<third>.1/24
```

| Band | Default VLANs | Default network | Portal pool |
|------|---------------|-----------------|-------------|
| `entry` | 100–199 | `10.<N>.0.0/24` | per-student entry VLAN: student *i* gets `first+i` |
| `internal` | 200–299 | `10.<N-100>.1.0/24` | per-student internal VLAN: student *i* gets `first+i` |
| `group-entry` | 300–399 | `10.<N-200>.2.0/24` | group entry VLAN: the first free tag |
| `group-internal` | 400–500 | `10.<N-300>.4.0/24` | group internal VLAN: the first free tag |

A student's two networks share the second octet: entry VLAN 142 is
`10.142.0.0/24` and internal VLAN 242 is `10.142.1.0/24`. The roster holds
as many students as the `entry` band has tags (100 by default); the
changeover and the seed refuse a larger one.

The portal refuses to start unless the layout satisfies these rules, each
because something depends on it:

- `entry`, `internal`, `group-entry` and `group-internal` each appear
  exactly once: they are the pools the portal allocates from. Other bands
  may be added for the jumpbox to route; the portal never assigns them.
- No two bands share a tag or a subnet.
- `internal` has exactly as many tags as `entry`, because a student's pair
  is one index into both bands.
- `group-internal` has at least as many tags as `group-entry`, because each
  group takes one tag from each. The default's `group-internal` holds one
  tag more (400–500).
- Every tag lies in 2–4094 and every derived address is valid
  (`0 <= N-offset <= 255`, `0 <= third <= 255`).
- With `aws` or `gcp` enabled, no tag is above 500: a cloud gives each VLAN
  tag its own network slot, and slot 500 is the highest.
- Every stored student pair (users and the whitelist) lies in the `entry`
  and `internal` bands.
- `VLAN_BANDS` equals the layout recorded in the database while any student
  pair is stored. The portal records its layout on its first start (and
  again whenever it changes with no pair stored). A changed layout can keep
  every tag inside its band yet move it to another subnet (a new offset or
  third octet), so startup refuses it, naming the recorded and the new
  layout, instead of cutting students off their labs.

The bands describe what the jumpbox serves. A group or KOTH game given an
explicit VLAN outside every band is allowed, and startup names each one in
a warning, but the jumpbox and the Headscale policy do not route it; that
VLAN is the operator's to route.

To change `VLAN_BANDS` while students are stored, do it between terms:

1. Destroy every lab, then stop the portal
   (`docker compose -f <PORTAL_COMPOSE> stop vm-automation`). The accept
   refuses while any VM record is live or a deployment is being admitted,
   because a running lab keeps its old addresses.
2. Set the new value in the portal's `.env` and in `scripts/site.env`. The
   accept records the value the portal's `.env` holds.
3. Accept it in a one-off container:
   `docker compose -f <PORTAL_COMPOSE> run --rm vm-automation python
   scripts/accept_vlan_layout.py`. This dry run prints the recorded and new
   layouts and every student network that moves, `old subnet -> new subnet`.
   Re-run it with `--apply` to record the new layout. The live-VM check and
   the write run in one transaction under the lock deployment admission
   takes, so no deployment can slip in between.
4. Start the portal, re-run `jumpbox_configure.sh --apply` on the jumpbox,
   and re-run `scripts/apply_headscale_policy.sh --apply`, which refuses
   while the portal's exported bands and `scripts/site.env` differ.

A portal process still running the old layout (one that was not stopped)
refuses every deployment after the accept with `portal VLAN layout differs
from the recorded layout — restart required`. With no student pair stored,
setting the same value in both files and step 4 are enough: the portal
records the new layout itself.

The record is created by `scripts/migrate_add_vlan_layout_record.py`. On a
database that already holds student pairs, the portal refuses to start until
that migration has seeded the layout those pairs were numbered under
(`docs/INTERNAL_VLAN_ROLLOUT.md`, section 2); a new database, or one with no
student pair, records the configured layout on its first start.

Templates with a `static_mac` can get a fixed address on every network of
one band, for example a domain controller at `.10` on each group-internal
network:

```
JUMPBOX_DHCP_RESERVATIONS="group-internal=02:00:00:00:00:01,dc,10"
```

The Proxmox bridge must carry these VLANs (`bridge-vlan-aware yes`, with
`bridge-vids` including every tag in `VLAN_BANDS`, 100–500 by default), and so must the switch ports between nodes.

## 1. Create the VM

A staging jumpbox puts `JUMPBOX_TRUNK_BRIDGE` on an isolated bridge; see `docs/STAGING.md`.

Set the provisioning values in `scripts/site.env`: `JUMPBOX_NODE`,
`JUMPBOX_STORAGE`, `JUMPBOX_VMID`, `JUMPBOX_SSH_PUBKEY`, and if they differ
from the defaults, the bridge, the management VLAN, the cloud image and the VM
shape. Choose a VMID outside the range the portal allocates. Then, on that
node:

```bash
scp scripts/site_env.sh scripts/site.env scripts/jumpbox_provision.sh root@node1:/root/jumpbox/
ssh root@node1 /root/jumpbox/jumpbox_provision.sh            # prints the qm commands
ssh root@node1 /root/jumpbox/jumpbox_provision.sh --apply
```

The VM gets 2 cores, 4 GB RAM and a 32 GB disk by default. Its disk is the
Ubuntu 24.04 cloud image resized before first boot, and cloud-init grows the
root filesystem into all of it. Do not install from the Ubuntu ISO with its
default storage layout: that installer gives the root LV only half the volume
group. A jumpbox built that way has run out of disk, and dnsmasq then could not
write its lease file. `jumpbox_configure.sh` grows such a root into the free
space (`lvextend -r -l +100%FREE`). If the disk is already 100% full, free
some space first (`journalctl --vacuum-size=200M`), because LVM needs room for
its metadata backup.

The script refuses to run on any node other than `JUMPBOX_NODE`, and it never
touches an existing VMID.

## 2. Configure routing and DHCP

Copy the scripts to the VM and run a dry run. It prints a diff of every file
it would write and every command it would run:

```bash
scp scripts/site_env.sh scripts/site.env scripts/jumpbox_configure.sh scripts/jumpbox_netgen.py \
    ubuntu@<jumpbox-mgmt-address>:jumpbox/
ssh -t ubuntu@<jumpbox-mgmt-address> sudo jumpbox/jumpbox_configure.sh
ssh -t ubuntu@<jumpbox-mgmt-address> sudo jumpbox/jumpbox_configure.sh --apply
```

It writes:

| File | Content |
|------|---------|
| `/etc/netplan/60-jumpbox-vlans.yaml` | management NIC on DHCP; trunk NIC unaddressed; one `vlan<N>` per lab VLAN with its `.1/24` |
| `/etc/dnsmasq.d/jumpbox.conf` | `interface=vlan*` with `bind-dynamic`; `no-dhcp-interface` for the management NIC; `no-resolv` plus `JUMPBOX_DNS_UPSTREAM`; one `dhcp-range` per VLAN |
| `/etc/systemd/system/dnsmasq.service.d/60-jumpbox-limits.conf` | `LimitNOFILE=65536`: dnsmasq opens a UDP and a TCP DNS socket for each VLAN's IPv4 and IPv6 link-local address, about 1,600 for the full band string, and fails with "Too many open files" at systemd's default soft limit of 1,024 |
| `/etc/sysctl.d/90-jumpbox-router.conf` | `net.ipv4.ip_forward=1`, `net.ipv6.conf.all.forwarding=1`, `rp_filter=2` |
| `/etc/nftables.d/jumpbox-lab-egress.nft`, `/etc/systemd/system/jumpbox-lab-egress.service` | the lab egress table, enabled and started before forwarding is switched on (below) |

Behaviour to know:

- **Interfaces.** The management NIC is the one that holds the default route,
  and the trunk is the only other physical NIC. Set `JUMPBOX_MGMT_IF` and
  `JUMPBOX_TRUNK_IF` if the script cannot tell. Proxmox's cloud-init may name
  the NICs `eth0`/`eth1` rather than `ens18`/`ens19`.
- **netplan try.** A changed netplan file is applied with
  `netplan try --timeout 120`. Run the script from an interactive SSH session
  on the management address and press Enter once you have confirmed you still
  have access; if you lose access, the change reverts by itself. Without a
  terminal the script refuses. `--no-try` uses `netplan apply` instead, with
  no automatic revert.
- **Validation first.** Before writing anything, the generated netplan is
  checked with `netplan generate` against a copy of `/etc/netplan`, and the
  dnsmasq file with `dnsmasq --test`.
- **One owner.** The script refuses if another netplan file already defines
  VLANs on the trunk, or if `/etc/dnsmasq.conf` or another file in
  `/etc/dnsmasq.d` configures DHCP ranges or listening. Two definitions of
  the same VLAN collide.
- **Idempotent.** A second `--apply` reports every file unchanged and runs
  nothing.
- **DNS is not exposed.** dnsmasq answers only on lab VLAN interfaces, so it
  is not a resolver on the management LAN or the tailnet, and it leaves
  systemd-resolved's stub listener alone.

### Lab egress: routed lab traffic leaves only to the tailnet

The jumpbox forwards whatever a lab VM hands it, and it does no NAT. Without a
filter, a lab VM can send packets through the jumpbox's management NIC into
the management LAN, the Proxmox API and the internet. Their replies usually
find no way back, but the packets arrive: on staging, a lab VM's SYNs to the
Proxmox API port and its NTP and HTTPS traffic were captured leaving the
jumpbox. The Headscale policy cannot stop this, since the traffic never
touches the tailnet.

So every `--apply` installs `inet jumpbox_lab_egress`, with its own oneshot
unit `jumpbox-lab-egress.service`. At boot the unit loads the table before
`systemd-sysctl.service` switches IP forwarding on, and before the network
(`Before=systemd-sysctl.service network-pre.target`; it needs only `nft` and
the rule file mounted), so lab traffic is never forwarded unfiltered. It needs `nft`:
`--apply` refuses without it, and the dry run notes it. The table has a
forward hook at priority -10, policy accept, and two rules:

```
iifname "vlan*" oifname "vlan*" return
iifname "vlan*" oifname != "tailscale0" counter drop
```

Routed lab traffic may go only to the tailnet; everything else that enters on
a lab VLAN is counted and dropped. Lab VMs have no egress except the student's
tailnet: no lab needs outbound internet during a lab session, and build-time
package installs happen in the image builder, not in a lab session. Name
lookups still work, because dnsmasq on the jumpbox answers them (input, not
forwarding). VLAN-to-VLAN traffic is not this table's business: `return` ends
its chain, and the separate VLAN isolation table below (`--isolate-vlans`, J2)
decides it. From `tailscale0` to a VLAN, and the jumpbox's own DHCP and DNS,
are untouched.

**Verify:** from a lab VM, a ping to the management gateway gets no reply,
and the counter in `sudo nft list table inet jumpbox_lab_egress` rises.
Because the jumpbox does no NAT, a missing reply alone proves nothing; the
counter does, as does an empty `sudo tcpdump -ni <mgmt-if> src <lab-vm-address>`.

A lab VM can still reach the jumpbox itself (ping, and SSH on any of its
addresses), which is input, not forwarding. `--ssh-staff-only` (below) closes
SSH to everyone but staff; a changeover turns it on.

**Reload, never restart** the unit (as for VLAN isolation below). Rolling it
back is `sudo systemctl disable --now jumpbox-lab-egress.service` (ExecStop
deletes the table), but the next `--apply` installs it again.

### Adopting a jumpbox that was built by hand

A hand-built jumpbox usually keeps its VLANs in its own netplan files and its
DHCP ranges in `/etc/dnsmasq.conf`, so the dry run reports them as conflicts.
Read the dry-run diff first. In a quiet window, from an interactive session on
the management address:

```bash
sudo mkdir -p /root/jumpbox-before && sudo cp -a /etc/netplan /etc/dnsmasq.conf /etc/dnsmasq.d /root/jumpbox-before/
sudo mv /etc/netplan/<hand-built-vlan-files>.yaml /root/jumpbox-before/
sudo sh -c ': > /etc/dnsmasq.conf'
sudo jumpbox/jumpbox_configure.sh --apply       # netplan try: confirm within 120 s
```

Run the `mv` immediately before `--apply`: until the new file is in place, a
reboot would come up without the VLANs. If the hand-built file also defined
the management NIC, the generated file defines it too. Leases survive the
dnsmasq restart because they are kept in `/var/lib/misc/dnsmasq.leases`. If a
hand-built config differs from the plan above, for example a pool ending at
`.255` or ranges for VLANs that have no interface, the dry-run diff shows the
difference.

## 3. Join Headscale as the subnet router

```bash
# On the Headscale host: a tagged pre-auth key for a user that tagOwners lets own tag:jumpbox
headscale preauthkeys create --user <infrastructure-user-id> --expiration 1h --tags tag:jumpbox
# On the jumpbox: the key is read from stdin and never appears in argv
sudo jumpbox/jumpbox_configure.sh --apply --tailscale --authkey-stdin
```

Alternatively, export `JUMPBOX_TS_AUTHKEY` for that one command. Never put
the key in `site.env`. The script installs Tailscale (snap), writes the key to
a mode-0600 file that `tailscale up --auth-key=file:<path>` reads, and deletes
the file afterwards. It joins with:

```
--login-server=$HEADSCALE_URL --advertise-routes=10.0.0.0/8 --advertise-tags=tag:jumpbox
--snat-subnet-routes=false --stateful-filtering=false
```

With `--snat-subnet-routes=false`, lab VMs see each student's tailnet address
rather than the jumpbox's address. Return traffic to `100.64.0.0/10` must
therefore route back through the jumpbox, which it does because the jumpbox is
every lab VLAN's gateway. If the jumpbox has already joined, the script
re-asserts the routes, SNAT and filtering settings with `tailscale set` and
needs no key.

Create the key with `--tags tag:jumpbox`. With an untagged key, Headscale
v0.27 ignores the advertised tag when it resolves users, so the jumpbox
stays a node of its user: if that user is in `group:staff`, the jumpbox
gets the staff rule (`*:*`) and can open connections to every student
device. `headscale nodes list -o json` must show `tag:jumpbox` in the
node's `forced_tags`.

Approve the route in Headscale. Headscale 0.26 and later:

```bash
headscale nodes list-routes
headscale nodes approve-routes --identifier <jumpbox-node-id> --routes 10.0.0.0/8
```

Older versions use `headscale routes list` and `headscale routes enable -r <id>`.
Alternatively, add an `autoApprovers` entry to the policy so that a rebuilt
jumpbox is approved without a manual step:

```json
"autoApprovers": { "routes": { "10.0.0.0/8": ["tag:jumpbox"] } }
```

### Headscale peer watchdog

Headscale v0.27.1 can leave the jumpbox with no peers: once after a policy
reload and once after the jumpbox's Tailscale reconnected (staging). The
jumpbox then routes for nobody, and no student reaches any lab, until Headscale
restarts. `scripts/apply_headscale_policy.sh --apply` checks for this after
every reload. A reconnect can happen at any time, though: a jumpbox reboot,
a Tailscale upgrade, or a network blip. `scripts/headscale_peer_watchdog.sh`
runs the same check on a timer and never writes or reloads the policy:

- If every roster device is a peer of the jumpbox, it logs `ok` and exits 0.
- If the jumpbox's Tailscale is not `Running`, it logs that and exits 1
  without touching Headscale, since a restart cannot fix it.
- If roster devices are missing, it restarts Headscale once and checks again.
  It restarts at most once per `HEADSCALE_WATCHDOG_MIN_RESTART_INTERVAL`
  seconds (default 1800). Within that interval a still-missing device only
  logs an error and exits 1, so a fault that a restart cannot fix never turns
  into a restart loop. A healthy run ends the interval, so a new fault after
  a recovery (a jumpbox reboot, say) is repaired at the next run. The time of
  the last restart is kept in `HEADSCALE_WATCHDOG_STATE` (default
  `~/.local/state/headscale-peer-watchdog/last-restart`).
- Without the proxmox backend it logs that the check is skipped.

It reads the same `scripts/site.env` as the policy script (`PORTAL_HOST`,
`PORTAL_APP_CONTAINER`, `HEADSCALE_HOST`, `HEADSCALE_DIR`, `JUMPBOX_SSH`), so
run it on the operator host that runs that script. As a systemd timer every
5 minutes, with `<checkout>` the repository checkout and `<user>` the account
whose SSH key reaches the three hosts:

```ini
# /etc/systemd/system/headscale-peer-watchdog.service
[Unit]
Description=Restart Headscale once if the jumpbox has lost its roster peers
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=<user>
Environment=SITE_ENV_FILE=<checkout>/scripts/site.env
ExecStart=<checkout>/scripts/headscale_peer_watchdog.sh
```

```ini
# /etc/systemd/system/headscale-peer-watchdog.timer
[Unit]
Description=Check the jumpbox's Headscale peers every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now headscale-peer-watchdog.timer
systemctl list-timers headscale-peer-watchdog.timer       # next run within 5 minutes
journalctl -u headscale-peer-watchdog.service -n 20        # "ok: the jumpbox lists every roster device as a peer."
```

A failed run leaves the service `failed`, which `systemctl --failed` and any
unit-failure alerting the host has will show. Treat it as "no student can
reach a lab".

## Recommended security settings

### Headscale ACL: each student reaches only their own labs

`config/headscale-acl.example.json` gives every member (`autogroup:member`)
`autogroup:self`, `tag:jumpbox:*` and the hosts and ranges it lists. Routed
traffic is matched against its destination (`10.x.x.x`), not against the
router's tag, so the policy is
where per-student access is decided. `scripts/apply_headscale_policy.sh`
renders it from the portal (dry run by default, `--apply` to install): each
roster login reaches only its own entry and internal networks, its groups'
networks and the running KOTH arenas, on Proxmox and on every enabled cloud. It keeps every
rule of your base policy except the blanket rule whose `src` is exactly
`["autogroup:member"]`, which it cuts down to its `autogroup:self`
destinations (students keep reaching their own devices), so a rule like
this one becomes `"dst": ["autogroup:self:*"]`:

```json
{
  "action": "accept",
  "src": ["autogroup:member"],
  "dst": ["autogroup:self:*", "10.0.0.0/8:*"]
}
```

Any other rule whose `src` includes `autogroup:member` or `*` must grant
only `autogroup:self`; the render refuses a base policy that has one. Keep
the staff rule, and any infrastructure access, as explicit rules in the
base file. `docs/CLOUD_BACKENDS.md` ("Apply the Headscale policy") has the
procedure, when to re-run it, and the rollback. Test with one student before
applying it to everyone.

### Key-only SSH

```bash
sudo jumpbox/jumpbox_configure.sh --apply --ssh-keys-only
```

This writes `/etc/ssh/sshd_config.d/10-jumpbox-keys-only.conf`
(`PasswordAuthentication no`, `KbdInteractiveAuthentication no`). sshd keeps
the first value it reads, so the file sorts before `50-cloud-init.conf`. The
script refuses unless `JUMPBOX_ADMIN_USER` (default: the sudo user) has an
authorized key, and unless that key file and its `.ssh` directory are not
group- or world-writable, which would make sshd ignore them. It checks the
result with `sshd -t` before reloading, and with `sshd -T` afterwards. Keep
your current session open until a new key login works.

Students with jumpbox accounts must have keys before you turn this on. Most
courses use no jumpbox accounts at all (below).

### Staff-only SSH

Students route *through* the jumpbox and never log in to it, yet without this
step SSH answers every tailnet device and every lab VLAN. A Headscale ACL
cannot fix that on its own: the jumpbox's gateway addresses (`10.x.x.1`) sit
inside the advertised `10.0.0.0/8` route, so a student's SSH to their own
gateway is matched as routed traffic to `10.0.0.0/8`, which the member rule
above has to allow. Close the port on the jumpbox itself:

```bash
# scripts/site.env: staff tailnet addresses, IPv4 and IPv6 mixed
JUMPBOX_SSH_STAFF_IPS="100.64.0.10,fd7a:115c:a1e0::a"
sudo jumpbox/jumpbox_configure.sh --ssh-staff-only            # dry run: shows the diff
sudo jumpbox/jumpbox_configure.sh --apply --ssh-staff-only
```

This writes `/etc/nftables.d/jumpbox-ssh.nft` and
`/etc/systemd/system/jumpbox-ssh-filter.service` (a oneshot that loads the file
before the network and `ssh.socket`/`ssh.service`) and enables the unit. The
file is its own table, `inet jumpbox_ssh`, with an input hook at priority -10
and policy accept. It touches only SSH ports (`tcp dport 22`, or a set such as
`{ 22, 2222 }`), in this order: accept on
`lo`; accept on the management interface (`JUMPBOX_MGMT_IF`); accept on
`tailscale0` from the two staff sets (`staff_v4`, `staff_v6`); count and drop
everything else. Forwarding and ICMP are untouched, so students' routed
traffic keeps working. The script validates the ruleset with `nft -c -f`
before it changes anything, and refuses when `JUMPBOX_SSH_STAFF_IPS` is empty
and the management interface cannot be determined, since nobody could then log
in. An empty staff list otherwise leaves SSH on loopback and the management
interface only.

**Ports.** The script collects every port sshd really listens on: the `port`
and `listenaddress host:port` lines of `sshd -T` (`0.0.0.0:2222`, `[::]:2222`;
a bare address means the `port` values apply) and the `ListenStream` entries of
`ssh.socket` (with socket activation sshd_config's `Port` is ignored). All of
them go into the rules. It fails closed: if sshd is installed but `sshd -T`
fails or reports no port, or `systemctl show ssh.socket` fails, `--apply`
refuses rather than assume 22 (a dry run warns). Only when neither sshd nor the
socket exists does it use 22. A listener on any port outside the rules would
bypass the filter, so **changing the SSH port requires re-running
`jumpbox_configure.sh --apply --ssh-staff-only`.**

**Requirements and lockout guard.** `--apply` needs `nft` (install the
`nftables` package) and always checks the ruleset with `nft -c -f` before
installing it. If you run it from an SSH session, the script takes the source
address from `SSH_CONNECTION` (it must be exactly `SRC SPORT DST DPORT`, valid
IPs and ports; IPv4-mapped IPv6 sources are folded to IPv4) and asks `ip route
get` which interface the route back leaves through. The session is kept only if
that is `lo`, the management interface, or `tailscale0` with a listed staff
source. If the interface cannot be determined, or the value is malformed, it
refuses. From the console, or if you can recover without SSH, pass
`--i-have-console`. Test a login from a listed device before closing your
console.

**Add a staff device:** append its tailnet addresses (`tailscale ip` on that
device) to `JUMPBOX_SSH_STAFF_IPS`, then `--apply --ssh-staff-only` again (or
`sudo systemctl reload jumpbox-ssh-filter.service` after editing the generated
file's sets, or `sudo nft -f /etc/nftables.d/jumpbox-ssh.nft`). Reload swaps
the table in one transaction, and a bad file leaves the old table in place.
**Never `systemctl restart` the unit**: stop deletes the table, so SSH is
unfiltered until start loads it again. The script uses `reload-or-restart` on
an active unit for the same reason.

**Roll back:**

```bash
sudo systemctl disable --now jumpbox-ssh-filter.service   # ExecStop deletes the table
```

This does not enable key-only SSH; that stays a separate choice
(`--ssh-keys-only`), which you cannot take while a staff account signs in with
a password.

### VLAN isolation

The jumpbox is every lab VLAN's gateway, so without this step a lab VM can
reach another student's lab VM on another VLAN by routing through it. The
Headscale policy cannot stop that: the traffic never leaves the jumpbox.
Traffic from a lab VLAN to anything else is the lab egress table's business
(section 2), which every `--apply` installs.

```bash
sudo jumpbox/jumpbox_configure.sh --isolate-vlans                         # dry run: shows the diff
sudo jumpbox/jumpbox_configure.sh --apply --no-try --isolate-vlans
sudo jumpbox/jumpbox_configure.sh --apply --no-try --isolate-vlans --allow-vlans 300-305
```

This writes `/etc/nftables.d/jumpbox-vlan-isolation.nft` and
`/etc/systemd/system/jumpbox-vlan-isolation.service` (a oneshot that loads the
file before IP forwarding is switched on and before the network, like the lab
egress unit) and enables the unit. The file is its own table,
`inet jumpbox_vlan_isolation`, with a forward hook at priority -10 and policy
accept, holding one rule: count and drop what enters **and** leaves through a
`vlan*` interface. Traffic between `tailscale0` and a VLAN (students, staff),
the jumpbox's own traffic, and traffic inside one VLAN, which never crosses
the jumpbox, are untouched. A group's entry and internal networks are joined
by the entry machine's second NIC, not by jumpbox routing, so group labs keep
working. The ruleset is checked with `nft -c -f` first, and the script
refuses when `nft` is missing. Have console or recovery access before the
first `--apply`; `--no-try` applies a pending netplan change without the
interactive `netplan try`.

**`--allow-vlans LIST`** (comma-separated tags and `first-last` ranges, tags
2-4094; only with `--isolate-vlans`) exempts those VLANs from the drop in
both directions: an exempt VLAN may exchange traffic with every lab VLAN. It
never opens the management side or the internet, which the lab egress table
keeps closed.
KOTH players arrive over the tailnet, which the table never touches, so set
it to the KOTH VLAN range only if KOTH boxes must reach each other across
VLANs, or team VLANs must reach the KOTH VLANs. Re-run with the new list
whenever the KOTH tags change; a run without `--allow-vlans` removes every
exemption.

**Verify:** from a student device, your own lab VM still answers; from a lab
VM, a VM on another VLAN does not, and the drop counter in
`sudo nft list table inet jumpbox_vlan_isolation` rises.

**Reload, never restart.** The script swaps a changed table with
`reload-or-restart` on the active unit (ExecReload loads the new table in one
transaction). **Never `systemctl restart` the unit**: stop deletes the table,
so the VLANs are unisolated until start loads it again.

**Roll back:**

```bash
sudo systemctl disable --now jumpbox-vlan-isolation.service   # ExecStop deletes the table
sudo rm /etc/nftables.d/jumpbox-vlan-isolation.nft /etc/systemd/system/jumpbox-vlan-isolation.service
sudo systemctl daemon-reload
sudo nft list table inet jumpbox_vlan_isolation               # must fail: no such table
```

## Optional: Zeek and RITA

```bash
sudo jumpbox/jumpbox_configure.sh --apply --monitoring
```

This installs Zeek from the openSUSE `security:zeek` repository and runs it
standalone on the trunk interface, with `10.0.0.0/8` as the local network and
`zeekctl cron` every 5 minutes. Logs expire after 7 days. It also installs
RITA (`JUMPBOX_RITA_VERSION`, default v5.1.1; Docker and ClickHouse) with
its release installer, and imports the Zeek logs into a rolling database
every hour at minute 20.

**Disk:** the RITA containers take about 8 GB, and Zeek logs and ClickHouse
data grow with lab traffic. The script refuses when `/` is smaller than
`JUMPBOX_MONITORING_MIN_GB` (default 64). Give a monitoring jumpbox at least
64 GB (`JUMPBOX_DISK_SIZE=64G`, or `qm resize` and then re-run configure) and
watch `df -h /`. A full root stops dnsmasq from recording leases.

## Optional: student SSH accounts

Students reach their VMs over the VPN routes and need no jumpbox login. Some
courses do want accounts on the jumpbox itself. For those:

```bash
sudo JUMPBOX_CREDENTIALS_FILE=/root/jumpbox-credentials.csv \
    jumpbox/jumpbox_accounts.sh create students.csv
```

Each account gets its own random password, which must be changed at first
login. The passwords are written only to the credentials file (mode 0600),
never to the terminal or a command line. Deliver each one privately.

When the course ends, lock the accounts that were never used:

```bash
sudo jumpbox/jumpbox_accounts.sh lock-stale                    # lists them
sudo jumpbox/jumpbox_accounts.sh lock-stale --apply [--keep alice,bob]
sudo jumpbox/jumpbox_accounts.sh unlock /root/locked-stale-accounts-<timestamp>.txt --apply
```

`lock-stale` selects login accounts (UID 1000–59999 with a real shell) that
`lastlog` reports as never logged in and that are not already locked. It locks
each one (`passwd -l`) and expires it (`chage -E 0`). Expiry also blocks SSH
key logins. The affected names go to a mode-0600 undo list in
`JUMPBOX_LOCK_LIST_DIR` (default `/root`), and `unlock` reverses exactly that
list.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Lab VMs get no address | `ip -br addr \| grep vlan` shows the `.1` gateways; `journalctl -u dnsmasq`; `df -h /`, because a full disk stops lease writes |
| Students cannot reach lab VMs | `tailscale status`; route approved (`headscale nodes list-routes`); ACL allows `10.0.0.0/8`; `sysctl net.ipv4.ip_forward` |
| A VLAN works but a new group's does not | the group's VLAN is outside `VLAN_BANDS` (an explicit tag; the startup log names it); widen the bands in both `.env` and `scripts/site.env`, then re-run configure |
| `netplan try` reverted | you did not confirm within the timeout, or the change cut your session; read the dry-run diff and re-run |
