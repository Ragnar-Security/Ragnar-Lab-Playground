# Permanent staging stack

A staging copy of the lab, built once and kept: an isolated lab bridge, a staging jumpbox, a
staging Headscale with two student clients, and a staging portal. It exists for one purpose: to
prove, before a release reaches students, that each student's VPN client reaches only that
student's machines. Section 7's acceptance test is that proof, and a pass at a pull request's head
is the gate for merging changes to the lab network.

Commands run on a Proxmox node use only what a node ships with (`pvesh`, `pveum`, `qm`,
`python3`). Do not install packages on a production node to follow this guide. Commands on the
portal host and the staging VMs say where they run.

Targets Proxmox VE 9.2 (`pveversion` prints `pve-manager/9.2.x`) and Headscale v0.27.1. Re-check
the privilege names in section 5 after a Proxmox upgrade.

## 1. Values, boundary and preconditions

### Values

| Placeholder | Meaning |
|---|---|
| `<STAGING_NODE>` | the Proxmox node that hosts every staging guest |
| `<STAGING_BRIDGE>` | the isolated lab bridge on that node; its name must not start with `vmbr0` |
| `<STAGING_JUMPBOX_VMID>` | the staging jumpbox |
| `<STAGING_VPN_VMID>` | the staging VPN VM (Headscale and the two student clients) |
| `<STAGING_TEMPLATE_VMID>` | the dedicated staging template both catalog entries clone |
| `<STAGING_VMID_RANGE>` | the staging portal's `VMID_RANGE`, `<range min>-<range max>` |
| `<MGMT_VLAN>` | the management VLAN on `vmbr0`, where the jumpbox and the VPN VM have their management NICs |
| `<STAGING_PORTAL_HOST>` | the host running the staging portal |
| `<STAGING_VPN_ADDR>` | the VPN VM's DHCP address on the management VLAN |
| `<STAGING_JUMPBOX_ADDR>` | the staging jumpbox's DHCP address on the management VLAN |
| `<MGMT_GATEWAY>` | the management VLAN's gateway |
| `<STAGING_NODE_ADDR>` | `<STAGING_NODE>`'s own address, where its API listens on port 8006 |
| `<STAGING_POOL>` | the Proxmox pool that holds the staging template |
| `<STORAGE>` | the Proxmox storage for the staging VMs' disks |
| `<CLOUD_IMAGE>` | the Ubuntu 24.04 cloud image on the node (`scripts/site.env.example`, `JUMPBOX_CLOUD_IMAGE`) |
| `<PUBKEY_FILE>` | the operator's SSH public key file on the node |

The real values live in the operator's notes, never in this repository.

### Boundary

There is no dedicated staging node; the isolated bridge lives on an existing production node. So
staging is **network-isolated, not resource-isolated**:

- **Isolated:** lab traffic. The staging bridge has no physical port, so a staging lab VM's only
  router is the staging jumpbox. The jumpbox's lab egress table (installed by every
  `jumpbox_configure.sh --apply`, section 4) lets traffic routed from a lab VLAN leave only
  through `tailscale0`, toward the students' devices. It drops what a lab VM routes toward the
  management VLAN, the Proxmox API or the internet: without that table, the jumpbox forwarded a
  staging lab VM's packets out of its management NIC (section 7 step 8 checks this). VLAN
  isolation (`--isolate-vlans`) drops what a lab VM routes toward another lab VLAN (step 7).
  Staff-only SSH closes the jumpbox's own SSH to lab VMs (step 8). Staging uses its own
  Headscale and its own portal database.
- **Shared with production:** `<STAGING_NODE>`'s CPU, memory and storage (production's admission
  sees staging guests as used memory); the cluster's VMID namespace; the Proxmox task log and
  cluster API; and the management VLAN, where the staging jumpbox and VPN VM have their management
  NICs.
- **Consequence:** production's orphan reaper can delete an expired `student-zz-*` staging guest,
  because production holds no record of it. That is harmless, since a staging deployment lives for
  one run.

The staging portal's Proxmox API token is limited to staging paths (section 5), so a staging bug
cannot act on a production guest.

### Range size

One acceptance run deploys four VMs (two students × `srv01` + `dc01`). Destroyed records keep their
VMIDs until a reset (section 7), so a range of *n* VMIDs holds ⌊*n*/4⌋ runs between resets. Choose
*n* ≥ 100, which gives 25 runs. Record the chosen *n* and ⌊*n*/4⌋ in the operator's notes.

### Preconditions

On a node, before anything is created. VMIDs are a cluster-wide namespace, so every check reads
`/cluster/resources`: a node's own `qm list` does not show a VMID that is in use on another node.
Each check prints its own `ok`, and all thirteen must print. Otherwise choose other values with the
site owner.

```bash
pvesh get /cluster/resources --type vm --output-format json | python3 -c '
import json, sys
vmids, (lo, hi), bridge = [int(v) for v in sys.argv[1:4]], map(int, sys.argv[4:6]), sys.argv[6]
used = {r["vmid"] for r in json.load(sys.stdin)}
top = max(used)
for v in vmids:
    if v not in used: print(f"vmid {v} unused ok")
    if v < top: print(f"vmid {v} below cluster max ok")
    if v < lo or v > hi: print(f"vmid {v} outside range ok")
if not any(lo <= v <= hi for v in used): print("range empty ok")
if hi < top: print("range below cluster max ok")
if hi - lo + 1 >= 100: print("range size ok")
if not bridge.startswith("vmbr0"): print("bridge name ok")  # never matched by section 2s vmbr0* check
' <STAGING_JUMPBOX_VMID> <STAGING_VPN_VMID> <STAGING_TEMPLATE_VMID> <range min> <range max> <STAGING_BRIDGE>
```

Expected: thirteen `ok` lines, three for each of the three staging VMIDs and four for the range and
the bridge. A staging guest that became the cluster's highest VMID would raise production's next
VMIDs for good (`app/proxmox_service.py`, `get_next_available_vmid`); the template sits outside the
range so it never moves the staging portal's in-range mark either. The bridge name must not start
with `vmbr0`, so that creating it can never show up as drift in section 2's `vmbr0*` comparison.

## 2. The isolated bridge

On `<STAGING_NODE>` as root. Only the new bridge is brought up. **Never `ifreload` the node**, which
would re-apply every production interface.

```bash
cp -a /etc/network/interfaces /root/interfaces.before-staging-bridge
ip -br link | grep -E '^vmbr0' > /root/vmbr0-link.before; ip -br addr | grep -E '^vmbr0' > /root/vmbr0-addr.before
cat /root/vmbr0-link.before /root/vmbr0-addr.before            # record every vmbr0* line
ifup -a --syntax-check > /root/syntax-check.before 2>&1; cat /root/syntax-check.before   # baseline, before the change
cat >> /etc/network/interfaces <<'EOF'

# Staging lab trunk: no physical port, never uplinked (docs/STAGING.md).
auto <STAGING_BRIDGE>
iface <STAGING_BRIDGE> inet manual
	bridge-ports none
	bridge-stp off
	bridge-fd 0
	bridge-vlan-aware yes
	bridge-vids 2-4094
EOF
diff <(ifup -a --syntax-check 2>&1) /root/syntax-check.before; ifquery <STAGING_BRIDGE>   # parse only
ifup -n -v <STAGING_BRIDGE> 2>&1 | grep -E 'DRY-RUN|running ops'                        # dry run
ifup <STAGING_BRIDGE>
ifquery --check <STAGING_BRIDGE>                               # running state matches the stanza
diff <(ip -br link | grep -E '^vmbr0') /root/vmbr0-link.before \
  && diff <(ip -br addr | grep -E '^vmbr0') /root/vmbr0-addr.before && echo vmbr0-unchanged
ip -br link show <STAGING_BRIDGE>; bridge link show | grep -c 'master <STAGING_BRIDGE>'
```

Expected:

- the syntax-check diff shows exactly one new line,
  `< warning: <STAGING_BRIDGE>: bridge-fd: value of out range "0": valid attribute range: 2-255`,
  and `ifquery` prints exactly the stanza;
- every dry-run line that names an interface names only `<STAGING_BRIDGE>`
  (`ip link add dev <STAGING_BRIDGE> type bridge`);
- `ifquery --check` reports the bridge and each `bridge-*` line `[pass]` (`bridge-vids` shows `[]`,
  as it does for `vmbr0`);
- `vmbr0-unchanged`, the bridge listed, and `0` member ports before any staging VM starts.

ifupdown2's parse-only check is `ifup -a --syntax-check` (`-s`); `ifquery` has no such option, and
its `-s` means `--syntax-help`. **The check exits 1 on an unmodified Proxmox node**: Proxmox writes
`bridge-fd 0` on every bridge, and ifupdown2 warns that 0 is out of range although it applies it
(`ifquery --check vmbr0` shows `bridge-fd 0 [pass]`). So compare against the baseline instead of
trusting the exit code. If the diff shows anything but the one new warning, restore the backup file
before running anything else. If any `vmbr0*` line differs after `ifup`, run
`ifdown <STAGING_BRIDGE>`, restore the backup file, compare again, and stop.

To remove the bridge (the site owner's decision, only with no VM attached): `ifdown <STAGING_BRIDGE>`,
restore `/root/interfaces.before-staging-bridge`, then repeat the two `vmbr0*` diffs.

## 3. The staging VPN VM

On `<STAGING_NODE>` as root. It has one NIC on the management VLAN and is never on the lab trunk.
The `qm` lines mirror `scripts/jumpbox_provision.sh`:

```bash
qm create <STAGING_VPN_VMID> --name staging-vpn --ostype l26 --cores 2 --memory 2048 --agent enabled=1 --onboot 1 \
  --scsihw virtio-scsi-single --serial0 socket --vga serial0 --net0 virtio,bridge=vmbr0,tag=<MGMT_VLAN>
qm set <STAGING_VPN_VMID> --scsi0 <STORAGE>:0,import-from=<CLOUD_IMAGE>,discard=on --boot order=scsi0
qm resize <STAGING_VPN_VMID> scsi0 32G
qm set <STAGING_VPN_VMID> --ide2 <STORAGE>:cloudinit --ciuser ubuntu --sshkeys <PUBKEY_FILE> --ipconfig0 ip=dhcp
qm start <STAGING_VPN_VMID>
```

Its address, `<STAGING_VPN_ADDR>`, comes from the management VLAN's DHCP server; the VM keeps it
while it runs and renews. If it ever changes, every `http://<STAGING_VPN_ADDR>:8080` below changes
with it. On the VM, as root:

```bash
apt-get update && apt-get install -y docker.io docker-compose-v2 qemu-guest-agent
systemctl enable --now qemu-guest-agent
usermod -aG docker ubuntu        # section 6 runs the policy script as this user (HEADSCALE_HOST)
```

### Headscale

A Docker Compose project in `/opt/headscale` whose service is named `headscale` (the name
`scripts/apply_headscale_policy.sh` uses), pinned to the same Headscale version as production.
`/etc/headscale` is a **directory** bind mount, so the policy script's atomic rename reaches the
container.

```bash
mkdir -p /opt/headscale/config /opt/headscale/lib && chown -R ubuntu:ubuntu /opt/headscale && cd /opt/headscale
cat > docker-compose.yml <<'EOF'
services:
  headscale:
    image: headscale/headscale:0.27.1
    command: serve
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./config:/etc/headscale
      - ./lib:/var/lib/headscale
EOF
```

Start from the release's example configuration (`config-example.yaml` at the `v0.27.1` tag of
`juanfont/headscale`) saved as `config/config.yaml`, and set:

- `server_url: http://<STAGING_VPN_ADDR>:8080` and `listen_addr: 0.0.0.0:8080`;
- `policy.mode: file` and `policy.path: /etc/headscale/acl.json`;
- the default DERP map (the example's `derp.urls`).

The staging base policy is a copy of `config/headscale-acl.example.json` with `tag:jumpbox` owned
by `staging-infra@` and no staff members. It is kept in the operator's notes, never in the repo.
Copy it to `config/acl.json`, because Headscale in file mode refuses to start without the file. Then:

```bash
docker compose up -d
docker compose exec -T headscale headscale users create staging-infra
docker compose exec -T headscale headscale users create zz-a
docker compose exec -T headscale headscale users create zz-b
docker compose exec -T headscale headscale users list
```

The clients log in over plain HTTP: measured with Headscale v0.27.1 and Tailscale v1.104.7 (the
client containers below) and v1.102.3 (the jumpbox's snap), all without TLS.

### Two students

Two containers on the VPN VM, each with its own network namespace and its own `tailscaled`. Create
a pre-auth key for each with `headscale preauthkeys create --user <user-id> --expiration 1h`. The
key is written by the shell builtin `printf`, so it never appears in a process's argv:

```bash
for s in zz-a zz-b; do
  read -rs -p "pre-auth key for $s: " KEY; echo
  install -m 600 /dev/null /root/$s.env && printf 'TS_AUTHKEY=%s\n' "$KEY" > /root/$s.env; unset KEY
  docker run -d --name $s --hostname $s --restart unless-stopped --cap-add NET_ADMIN --device /dev/net/tun \
    -v $s-state:/var/lib/tailscale --env-file /root/$s.env -e TS_STATE_DIR=/var/lib/tailscale -e TS_USERSPACE=false \
    -e TS_EXTRA_ARGS="--login-server=http://<STAGING_VPN_ADDR>:8080 --accept-routes" tailscale/tailscale:v1.104.7
done
```

The client tag is pinned, never `latest`; change it deliberately and re-run section 7. Once
`headscale nodes list` shows both, run `shred -u /root/zz-a.env /root/zz-b.env`. The state volumes
keep the nodes. The two clients cannot reach each other: the staging policy gives a student only
`autogroup:self` on the tailnet.

## 4. The staging jumpbox: the production adoption's rehearsal

Use an operator-owned staging `site.env` kept outside the checkout (for example
`~/staging/site.env`, passed as `SITE_ENV_FILE=`). It sets:

- `JUMPBOX_NODE=<STAGING_NODE>`, `JUMPBOX_VMID=<STAGING_JUMPBOX_VMID>`, `JUMPBOX_NAME=staging-jumpbox`,
  `JUMPBOX_STORAGE=<STORAGE>`, `JUMPBOX_SSH_PUBKEY=<PUBKEY_FILE>`;
- `JUMPBOX_BRIDGE=vmbr0`, `JUMPBOX_MGMT_VLAN=<MGMT_VLAN>`, `JUMPBOX_TRUNK_BRIDGE=<STAGING_BRIDGE>`;
- `HEADSCALE_URL=http://<STAGING_VPN_ADDR>:8080`,
  `VLAN_BANDS="entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4"`
  (the same value as the staging portal's `.env`; `apply_headscale_policy.sh` refuses a difference);
- the policy keys of section 6.

Then follow `docs/JUMPBOX.md` sections 1-3 exactly, with `SITE_ENV_FILE=` pointing at that file on
each host the scripts run on:

1. `jumpbox_provision.sh` dry run, then `--apply`.
2. `jumpbox_configure.sh` dry run, then `--apply`, confirming `netplan try` with ENTER from the same
   SSH session on the management address. On a fresh VM the dry run does not list the
   `apt-get install dnsmasq qemu-guest-agent` that `--apply` runs.
3. `--apply --tailscale --authkey-stdin` with a tagged key from the staging Headscale
   (`headscale preauthkeys create --user <staging-infra-id> --expiration 1h --tags tag:jumpbox`).
4. `headscale nodes approve-routes --identifier <id> --routes "$(python3 scripts/jumpbox_netgen.py routes --bands "$VLAN_BANDS")"`
   (one `/24` per VLAN; `docs/JUMPBOX.md`, step 3).
5. `--apply --ssh-staff-only`, so a lab VM cannot SSH to the jumpbox. Staging has no staff
   devices, so `JUMPBOX_SSH_STAFF_IPS` stays empty and SSH answers only on the management NIC and
   loopback. Run it from an SSH session on the management address (its lockout guard keeps that
   session).
6. `--apply --no-try --isolate-vlans`.

After `--apply`:

- `ip -br addr | grep -c '^vlan'` and `grep -c '^dhcp-range=' /etc/dnsmasq.d/jumpbox.conf` both
  print `401`;
- `ip -br addr show vlan200` lists only `10.100.1.1/24`;
- `systemctl show dnsmasq -p LimitNOFILESoft` prints `LimitNOFILESoft=65536`: dnsmasq holds about
  1,600 sockets for 401 VLANs and does not start under the default 1,024;
- `systemctl is-active dnsmasq snap.tailscale.tailscaled` prints `active` twice (the script installs
  Tailscale as a snap, so there is no `tailscaled` unit);
- `sudo nft list table inet jumpbox_lab_egress` shows `iifname "vlan*" oifname "vlan*" return` and
  `iifname "vlan*" oifname != "tailscale0" counter drop`;
- `sudo nft list table inet jumpbox_ssh` ends with `counter drop`;
- `sudo nft list table inet jumpbox_vlan_isolation` shows `iifname "vlan*" oifname "vlan*" counter
  drop`;
- `systemctl is-enabled jumpbox-lab-egress.service jumpbox-ssh-filter.service
  jumpbox-vlan-isolation.service` prints `enabled` three times.

**Record every difference from the docs.** A generic error is fixed in this repository; a
site-specific one goes in the operator's notes.

## 5. The staging portal

Follow `docs/LOCAL_TEST_INSTANCE.md` (development stack, local override, reaper proven off), but
check out the **branch or release under test**, and use the token below instead of that guide's
step 5 token.

### Least-privilege API token

On a node as root. Each role grants only what its path needs. The template is in a staging pool, so
the token can clone it but cannot change or delete it.

`<STAGING_TEMPLATE_VMID>` is a **dedicated staging template**: a copy made for staging, never a
production template, so nothing in production depends on its pool membership. Record its current
pool before the change; it must print `none`, and if it prints anything else, stop and choose a
dedicated template:

```bash
pvesh get /cluster/resources --type vm --output-format json | python3 -c '
import json, sys
print(next((r.get("pool") or "none" for r in json.load(sys.stdin) if r["vmid"] == int(sys.argv[1])), "absent"))
' <STAGING_TEMPLATE_VMID>                                      # record the output
pveum pool add <STAGING_POOL> --comment "staging portal templates (docs/STAGING.md)"
pveum pool modify <STAGING_POOL> --vms <STAGING_TEMPLATE_VMID>
pveum role add StagingClone   --privs "VM.Clone VM.Audit"
pveum role add StagingGuest   --privs "VM.Allocate VM.Audit VM.Config.CPU VM.Config.Memory VM.Config.Network VM.Config.Options VM.PowerMgmt VM.GuestAgent.Audit"
pveum role add StagingStorage --privs "Datastore.AllocateSpace Datastore.Audit"
pveum role add StagingNode    --privs "Sys.Audit"
pveum role add StagingBridge  --privs "SDN.Use"
pveum user add staging-portal@pve --comment "staging portal (docs/STAGING.md)"
pveum acl modify /pool/<STAGING_POOL> --users staging-portal@pve --roles StagingClone
for id in $(seq <range min> <range max>); do pveum acl modify /vms/$id --users staging-portal@pve --roles StagingGuest; done
pveum acl modify /storage/<STORAGE> --users staging-portal@pve --roles StagingStorage
pveum acl modify /nodes/<STAGING_NODE> --users staging-portal@pve --roles StagingNode
pveum acl modify /sdn/zones/localnetwork/<STAGING_BRIDGE> --users staging-portal@pve --roles StagingBridge
pveum user token add staging-portal@pve portal --privsep 0 --expire <end of term, epoch>
```

- The token inherits exactly the user's ACLs (`--privsep 0` on a user that has no other rights).
- Its secret goes only into the staging `.env` (`PROXMOX_USER=staging-portal@pve`,
  `PROXMOX_TOKEN_ID=portal`, `PROXMOX_TOKEN_SECRET=…`, mode 600). Rotate it at its expiry.
- Proxmox VE 9 has no `VM.Monitor`; `VM.GuestAgent.Audit` is what lets the portal read guest
  addresses. `SDN.Use` on a local bridge exists from Proxmox VE 8. Confirm each name with
  `pveum role list` before creating the roles.

### Prove the token cannot touch a non-staging guest

Use only reads, so a mistake changes nothing.

```bash
pveum user token permissions staging-portal@pve portal      # lists only the five path kinds above
install -m 600 /dev/null /root/staging-token.hdr
printf 'Authorization: PVEAPIToken=staging-portal@pve!portal=%s\n' "$(read -rs -p 'secret: ' S; echo "$S")" > /root/staging-token.hdr
API=https://localhost:8006/api2/json; C="curl -sS -o /dev/null -w %{http_code}\n --cacert /etc/pve/pve-root-ca.pem -H @/root/staging-token.hdr"
$C $API/nodes/<STAGING_NODE>/qemu/<STAGING_JUMPBOX_VMID>/status/current   # expect 403
$C $API/nodes/<STAGING_NODE>/qemu/<any production VMID>/status/current     # expect 403
$C $API/nodes/<STAGING_NODE>/qemu/<STAGING_TEMPLATE_VMID>/status/current   # expect 200
curl -sS --cacert /etc/pve/pve-root-ca.pem -H @/root/staging-token.hdr "$API/cluster/resources?type=vm" \
  | python3 -c 'import json, sys; print(*sorted(r["vmid"] for r in json.load(sys.stdin)["data"]))'
                                                                           # only the template and in-range VMIDs
shred -u /root/staging-token.hdr
```

A `200` on either 403 line means stop: delete the token
(`pveum user token remove staging-portal@pve portal`) and fix the ACLs. The portal's own cluster view
is limited to these guests too, which is harmless in range mode: it allocates only inside the range.

### Its `.env`

On top of `docs/LOCAL_TEST_INSTANCE.md`'s file:

```ini
FIXED_NODE=<STAGING_NODE>
ENABLE_LOAD_BALANCING=false
LAB_BRIDGE=<STAGING_BRIDGE>
VMID_RANGE=<STAGING_VMID_RANGE>
VLAN_BANDS="entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4"
ENABLED_BACKENDS=proxmox
VPN_LOGIN_SERVER=http://<STAGING_VPN_ADDR>:8080
ORPHAN_REAPER_ENABLED=false
```

That guide's step 2 (clearing a VLAN pair against production) and step 8 (setting the pair by SQL)
do not apply: the isolated bridge carries no production VLAN. `data/students_merged.csv` holds
`zz-a` and `zz-b`, in that order, so they are seeded as 100/200 and 101/201:

```csv
Student,SIS User ID,SIS Login ID
Staging A,zz-a,zz-a
Staging B,zz-b,zz-b
```

`templates.yaml` has one enabled individual group, `staging-two-machine`, with two distinct entries:

- `srv01` (`entry_point: true`) and `dc01` (plain);
- both point at `<STAGING_TEMPLATE_VMID>`, a small Linux template on `<STAGING_NODE>` with `sshd`,
  the QEMU guest agent and `nc` (OpenBSD netcat);
- `template_locations` names `<STAGING_NODE>` only, and `storage_locations` names `<STORAGE>`.

## 6. The staging policy

The staging `site.env` also sets:

- `PORTAL_HOST=<STAGING_PORTAL_HOST>`, `PORTAL_APP_CONTAINER=proxmox-vm-automation`
  (`docker-compose.yml`, `container_name`). The script reaches the portal over SSH even when it runs
  on the portal host itself, so that host must accept the operator's key;
- `HEADSCALE_HOST=<staging VPN VM ssh>`, `HEADSCALE_DIR=/opt/headscale`;
- `HEADSCALE_BASE_POLICY=<staging base policy path>`, `HEADSCALE_POLICY_FILE=/etc/headscale/acl.json`;
- `JUMPBOX_SSH=ubuntu@<STAGING_JUMPBOX_ADDR>`, where `--apply`, its `--preflight` and the watchdog
  check the jumpbox's peers.

```bash
SITE_ENV_FILE=~/staging/site.env scripts/apply_headscale_policy.sh            # dry run
SITE_ENV_FILE=~/staging/site.env scripts/apply_headscale_policy.sh --apply
```

Expected: `verified: the policy file Headscale serves matches the rendered one.`, then
`verified: the jumpbox lists every roster device as a peer.` Re-run after every staging roster or
VLAN change.

The second line is `--apply` checking, with `tailscale status --json` on the jumpbox, that every
roster device Headscale knows is one of the jumpbox's peers. On the first build it listed none after
the reload: the jumpbox's packet filter already held both students' rules, yet every client's
traffic to the lab went unanswered until `docker compose restart headscale` on the VPN VM. So when a
device is missing, the script restarts Headscale once, checks again, and fails, naming the devices,
if one is still missing. It happened a second time after the jumpbox's Tailscale reconnected
(`tailscale down`, then `up`): the jumpbox ran with no peers until the script restarted Headscale.
So also run `--apply` after the jumpbox reboots or its Tailscale restarts. A stopped jumpbox
(`BackendState` other than `Running`) still lists its last peers, so the script counts it as having
none. To check by hand on the jumpbox:
`tailscale status --json | python3 -c 'import json, sys; print(sorted(p["HostName"] for p in (json.load(sys.stdin).get("Peer") or {}).values()))'`
lists `zz-a` and `zz-b`, and `tailscale status --json` shows `"BackendState": "Running"`.

### Headscale peer watchdog

Install `scripts/headscale_peer_watchdog.sh` as in `docs/JUMPBOX.md` "Headscale peer watchdog",
on `<STAGING_PORTAL_HOST>`, the host that runs the policy script, with
`SITE_ENV_FILE=<staging site.env>`. Its journal shows `ok: the jumpbox lists every roster device
as a peer.` every 5 minutes. To prove it, restart the staging jumpbox's Tailscale
(`sudo snap restart tailscale`). Within one timer interval the journal shows either `ok`, or the
missing devices, one Headscale restart and `recovered: the jumpbox lists every roster device as a
peer after one Headscale restart.`; the section 7 step 5 checks then pass again.

## 7. The acceptance test

Repeatable. Every `pvesh` and `qm` line runs on `<STAGING_NODE>` as root, `docker exec zz-*` on the
VPN VM, and the lease and `nft` lines on the staging jumpbox.

1. Record `git -C <checkout> rev-parse HEAD` on the portal host. Section 6's dry run reports nothing
   to do, and section 6's peer check by hand on the jumpbox lists `zz-a` and `zz-b`.
2. `zz-a` and `zz-b` each register (first use) and deploy `staging-two-machine`.
3. List the staging guests:

   ```bash
   pvesh get /cluster/resources --type vm --output-format json | python3 -c '
   import json, re, sys
   for r in json.load(sys.stdin):
       if re.match(r"student-zz-(a|b)-", r.get("name") or ""): print(r["vmid"], r["name"], r["node"])'
   ```

   It lists exactly four guests, each inside `<STAGING_VMID_RANGE>` and on `<STAGING_NODE>`. For about
   10 s after a clone, `/cluster/resources` reports a new guest without its name, so run it again if
   a guest is missing right after the deploy.
   `qm config <vmid> | grep ^net` shows `bridge=<STAGING_BRIDGE>` on every NIC:
   - `srv01`: `net0` on the entry tag and `net1` on the internal tag;
   - `dc01`: `net0` on the internal tag and no `net1`.
4. In `/var/lib/misc/dnsmasq.leases` on the staging jumpbox, each `srv01` holds two leases and each
   `dc01` one (match the MACs from step 3). A's addresses are in `10.100.0.0/24` and
   `10.100.1.0/24`; B's in `10.101.0.0/24` and `10.101.1.0/24`.
5. For each of A's three addresses, `docker exec zz-a ping -c2 -W2 <addr>` and
   `docker exec zz-a nc -w3 <addr> 22 </dev/null` both succeed. Both fail for each of B's three.
   Then the same with the roles reversed.
6. **Reverse rule** (lab → the owner's own device). This exercises the reverse rule for the internal
   VLAN.
   1. Record the clients' tailnet addresses: `TA=$(docker exec zz-a tailscale ip -4)` and
      `TB=$(docker exec zz-b tailscale ip -4)`.
   2. In each client, start a TCP listener on port 18080 that answers with its own name:
      `docker exec -d zz-a sh -c 'while :; do echo reverse-ok-zz-a | nc -l -p 18080; done'`, and the
      same for `zz-b`.
   3. From A's `dc01`,
      `qm guest exec <A dc01 vmid> --timeout 10 -- sh -c 'nc -w3 <TA> 18080 </dev/null'` must return
      `reverse-ok-zz-a` in `out-data`. The same command against `<TB>` must return nothing.
   4. From B's `dc01`, `<TB>` must return `reverse-ok-zz-b` and `<TA>` nothing.
   5. Stop both listeners: `docker exec zz-a pkill -f 'nc -l -p 18080'`, and the same for `zz-b`.
7. Read the J2 counter (`sudo nft list table inet jumpbox_vlan_isolation` on the staging jumpbox).
   From A's `dc01` (`qm guest exec <vmid> -- ping -c2 -W2 <B srv01 internal addr>`), there is no
   reply, and the counter rises.
8. **No egress except the tailnet, and no jumpbox SSH.** From A's `dc01`, each of these must fail:

   ```bash
   qm guest exec <A dc01 vmid> --timeout 20 -- sh -c 'ping -c2 -W2 <MGMT_GATEWAY>'
   qm guest exec <A dc01 vmid> --timeout 20 -- sh -c 'nc -w3 -z <STAGING_NODE_ADDR> 8006'
   qm guest exec <A dc01 vmid> --timeout 20 -- sh -c 'nc -w3 -z <STAGING_VPN_ADDR> 8080'
   qm guest exec <A dc01 vmid> --timeout 20 -- sh -c 'ping -c2 -W2 1.1.1.1; nc -w3 -z 1.1.1.1 443'
   qm guest exec <A dc01 vmid> --timeout 20 -- sh -c 'nc -w3 -z 10.100.1.1 22; nc -w3 -z <STAGING_JUMPBOX_ADDR> 22'
   ```

   The counter in `sudo nft list table inet jumpbox_lab_egress` must rise by at least the first four
   probes' packets, and the `jumpbox_ssh` drop counter by the last one's. A failure alone proves
   nothing here: the jumpbox does no NAT, so before the table existed these probes also failed for
   want of a return route, while `tcpdump -ni <jumpbox mgmt NIC> src <dc01 addr>` on the jumpbox
   showed every packet leaving. The counter is the evidence; so is a capture that stays empty.
9. Record the date, commit, four VMIDs, `TA`, `TB` and each result in the operator's staging log.
   Then destroy both deployments; step 3's query prints nothing.

### Group and KOTH internal networks

Repeatable, after the steps above. A group's and a KOTH game's internal network is reached only by
pivoting through a host on its entry network: the policy grants the entry network only, the
routers advertise one route per network, and the jumpbox drops the tailnet's traffic to and from
the `group-internal` band. A student's own entry and internal networks are unchanged (step 5 above
is the proof that they still work).

Preparation, once per staging rebuild:

- The staging `site.env` leaves `JUMPBOX_TAILNET_STAFF_IPS` and `JUMPBOX_TAILNET_DENY_VLANS` empty
  (staging has no staff device and runs its KOTH draft in the group bands).
- `templates.yaml` gains a second group, `staging-two-machine-group`: the same two entries as
  `staging-two-machine`, with `group_mode: true`.
- In the admin Groups tab, create the group `zz-team` with members `zz-a` and `zz-b`. With the
  default bands it takes VLANs 300/400: entry `10.100.2.0/24`, internal `10.100.4.0/24`.
- Section 6 again, so the policy holds the group.

1. **Routes.** On the VPN VM, `docker compose exec -T headscale headscale nodes list-routes`
   shows the staging jumpbox with 401 approved and 401 serving routes, and no `10.0.0.0/8`. In
   each client, `docker exec zz-a ip route show table 52 | grep '^10\.'` lists exactly
   `10.100.0.0/24`, `10.100.1.0/24` (A's own) and `10.100.2.0/24` (the group's entry), and for
   `zz-b` `10.101.0.0/24`, `10.101.1.0/24` and `10.100.2.0/24`. Neither lists `10.100.4.0/24`.
2. `zz-a` deploys `staging-two-machine-group`. The group `srv01` holds a lease in `10.100.2.0/24`
   and one in `10.100.4.0/24`; the group `dc01` one in `10.100.4.0/24`.
3. **Entry works, internal does not, from the VPN.** From each client, `ping -c2 -W2` and
   `nc -w3 <addr> 22 </dev/null` succeed against the group `srv01`'s entry address, and fail against
   its internal address and against the group `dc01`. `docker exec zz-a ip route get <dc01 addr>`
   does not name `tailscale0`. On an Active Directory pair, also `nc -w3 -z <dc01 addr> 88` and
   `445` from the VPN: both must fail.
4. **The jumpbox layer on its own.** Simulate a policy mistake: add a rule to the staging base
   policy granting `zz-b@` `10.100.4.0/24:*`, and apply section 6. `zz-b` now lists
   `10.100.4.0/24` in table 52, yet `docker exec zz-b nc -w3 <dc01 addr> 22 </dev/null` still
   fails, and on the staging jumpbox the `iifname "tailscale0" oifname @tailnet_internal counter`
   in `sudo nft list table inet jumpbox_lab_egress` rises. Remove the rule and apply section 6
   again; table 52 loses the route.
5. **Staff exemption.** Set `JUMPBOX_TAILNET_STAFF_IPS` to `zz-b`'s tailnet address, run
   `jumpbox_configure.sh --apply` on the staging jumpbox, repeat step 4's grant: `zz-b` now reaches
   `dc01` on port 22. Revert both (empty `JUMPBOX_TAILNET_STAFF_IPS`, `--apply`, policy without the
   rule) and confirm step 3 again.
6. **The pivot.** Through a TUN tunnel whose agent runs on the group `srv01`, for example
   ligolo-ng with its proxy in the `zz-a` container: start the tunnel, then in `zz-a`
   `ip route add 10.100.4.0/24 dev <tunnel interface>` (the main table). `ip route get <dc01 addr>`
   names the tunnel interface, and `nc -w3 <dc01 addr> 22 </dev/null` (or `88`/`445` on an Active
   Directory pair) succeeds. Remove the route and stop the tunnel.
7. **KOTH.** In `/admin/koth`, create a draft game on `staging-two-machine-group` with entry VLAN
   350 and internal VLAN 450, and apply section 6. Both clients' table 52 gains `10.150.2.0/24` and
   not `10.150.4.0/24`; the rendered policy names `10.150.2.0/24` in each login's forward rule
   only, and `10.150.4.0/24` nowhere. Delete the draft and apply section 6 again.
8. Record the date, commit and each result in the staging log, then destroy the group deployment.

### Stop, start, reset, remove

Portal commands run in the staging checkout on `<STAGING_PORTAL_HOST>`.

- **Stop:** destroy staging deployments through the portal;
  `docker compose -f docker-compose.yml -f docker-compose.local.yml stop` on the portal host;
  `qm shutdown <STAGING_JUMPBOX_VMID>` and `qm shutdown <STAGING_VPN_VMID>`. The bridge stays.
- **Start:** `qm start` both VMs; `docker compose -f docker-compose.yml -f docker-compose.local.yml start`;
  section 6's dry run reports nothing to do.
- **Reset:**
  1. Destroy the staging deployments, and check that the step 3 query finds no `student-zz-*` guest.
  2. **Before the first reset**, record what `down -v` removes:

     ```bash
     docker compose -f docker-compose.yml -f docker-compose.local.yml config --volumes   # expect exactly: db_data
     docker volume inspect "$(docker compose -f docker-compose.yml -f docker-compose.local.yml config --format json | jq -r .name)_db_data" \
       --format '{{.Name}} {{.Mountpoint}}'
     ```

     Only the staging database volume goes. `./data` and `templates.yaml` are bind mounts and stay.
     Record both outputs in the staging log; if `config --volumes` lists anything else, stop and
     find out why first.
  3. Run `docker compose -f docker-compose.yml -f docker-compose.local.yml down -v`, which wipes the
     staging database and with it the in-range high-water mark. Reset whenever ⌊*n*/4⌋ runs have
     used the range; the next deploy would otherwise fail with `VMIDRangeExhausted`.
  4. Run `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d` (`init_db.py`
     reseeds `zz-a`/`zz-b`), then section 6 again.
  5. On the jumpbox, `jumpbox_configure.sh --apply` must report every file unchanged.
- **Remove** (the site owner's decision only): `qm destroy` both VMs and the template; remove the token,
  user, roles and pool (`pveum user delete staging-portal@pve`, `pveum role delete Staging…`,
  `pveum pool delete <STAGING_POOL>` after removing the template from it); then remove the bridge as
  in section 2.
