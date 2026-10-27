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
                     ├─ vlan300  10.100.2.1/24  ─ group entry network
                     └─ vlan400  10.100.4.1/24  ─ group internal network …
```

Three scripts build it. Each one runs where its comment says, and each is a
dry run unless you pass `--apply`:

| Script | Runs on | Does |
|--------|---------|------|
| `scripts/jumpbox_provision.sh` | the Proxmox node | creates the VM: cloud image, full disk, two NICs, cloud-init login |
| `scripts/jumpbox_configure.sh` | the jumpbox | netplan VLAN gateways, dnsmasq, forwarding sysctls, root disk growth; optionally Tailscale, key-only SSH, Zeek/RITA |
| `scripts/jumpbox_accounts.sh` | the jumpbox | optional student SSH accounts: `create`, `lock-stale`, `unlock` |

`scripts/jumpbox_netgen.py` renders the netplan and dnsmasq files for
`jumpbox_configure.sh`. Settings come from `scripts/site.env`; see the
jumpbox section of `scripts/site.env.example`.

## Address plan

Each lab VLAN tag *N* gets one /24 network. The jumpbox holds `.1`, and
dnsmasq leases `.50` to `.254` for 12 hours:

| Band | VLANs | Network | Portal pool |
|------|-------|---------|-------------|
| `entry` | 100–200 | `10.<N>.0.0/24` | per-student entry VLAN (`app/vlan_allocator.py`) |
| `group-entry` | 300–399 | `10.<N-200>.2.0/24` | group entry VLAN (`GROUP_ENTRY_VLAN_MIN/MAX`) |
| `group-internal` | 400–500 | `10.<N-300>.4.0/24` | group internal VLAN (`GROUP_INTERNAL_VLAN_MIN/MAX`) |

The bands must cover every VLAN the portal can assign. The portal's bounds
are inclusive, so group-internal includes VLAN 500, and
`tests/test_jumpbox_scripts.py` fails if the defaults stop covering the
allocator's pools. Per-student internal VLANs (200–300) are not served by the
jumpbox. If you change the portal's `GROUP_*_VLAN_*` settings, set
`JUMPBOX_VLAN_BANDS` to match:

```
JUMPBOX_VLAN_BANDS="entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4"
#                   name:first-last:offset:third  ->  VLAN N = 10.<N-offset>.<third>.1/24
```

Templates with a `static_mac` can get a fixed address on every network of
one band, for example a domain controller at `.10` on each group-internal
network:

```
JUMPBOX_DHCP_RESERVATIONS="group-internal=02:00:00:00:00:01,dc,10"
```

The Proxmox bridge must carry these VLANs (`bridge-vlan-aware yes`, with
`bridge-vids` including 100–500), and so must the switch ports between nodes.

## 1. Create the VM

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
| `/etc/sysctl.d/90-jumpbox-router.conf` | `net.ipv4.ip_forward=1`, `net.ipv6.conf.all.forwarding=1`, `rp_filter=2` |

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
# On the Headscale host: a pre-auth key for a user that tagOwners lets own tag:jumpbox
headscale preauthkeys create --user <infrastructure-user> --expiration 1h
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

## Recommended security settings

### Headscale ACL: members reach the labs, not the jumpbox

`config/headscale-acl.example.json` lets every member reach `tag:jumpbox:*`,
which covers SSH and every other service on the jumpbox. Students need only
the advertised routes. Routed traffic is matched against its destination
(`10.x.x.x`), not against the router's tag, so this member rule keeps lab
access and closes the jumpbox itself:

```json
{
  "action": "accept",
  "src": ["autogroup:member"],
  "dst": ["autogroup:self:*", "10.0.0.0/8:*"]
}
```

Keep the staff rule as it is. Test the change with one student before applying
it to everyone.

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
and policy accept. It touches only `tcp dport 22`, in this order: accept on
`lo`; accept on the management interface (`JUMPBOX_MGMT_IF`); accept on
`tailscale0` from the two staff sets (`staff_v4`, `staff_v6`); count and drop
everything else. Forwarding and ICMP are untouched, so students' routed
traffic keeps working. The script validates the ruleset with `nft -c -f`
before it changes anything, and refuses when `JUMPBOX_SSH_STAFF_IPS` is empty
and the management interface cannot be determined, since nobody could then log
in. An empty staff list otherwise leaves SSH on loopback and the management
interface only.

Dropped packets include your own session if it arrives from an address that is
neither on the management interface nor listed. Keep a console open and test a
login from a listed device before closing it.

**Add a staff device:** append its tailnet addresses (`tailscale ip` on that
device) to `JUMPBOX_SSH_STAFF_IPS` and re-run with `--apply --ssh-staff-only`.
The table is replaced in one load.

**Roll back:**

```bash
sudo systemctl disable --now jumpbox-ssh-filter.service   # ExecStop deletes the table
```

This does not enable key-only SSH; that stays a separate choice
(`--ssh-keys-only`), which you cannot take while a staff account signs in with
a password.

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
| A VLAN works but a new group's does not | the group's VLAN is outside `JUMPBOX_VLAN_BANDS`; re-run configure after widening the bands |
| `netplan try` reverted | you did not confirm within the timeout, or the change cut your session; read the dry-run diff and re-run |
