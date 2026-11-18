# Rolling out per-student internal VLANs

This runbook upgrades a running site to the release that gives every student
an internal VLAN of their own (`CHANGELOG.md`, "Unreleased"). Run it once, at
a term changeover, in this order. It uses `docs/JUMPBOX.md`,
`docs/TERM_CHANGEOVER.md` and `docs/STAGING.md`; every `<...>` value is a
placeholder for your site's own.

The examples assume the default `VLAN_BANDS`
(`entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4`).
With another layout, read each tag and address from your own bands
(`docs/JUMPBOX.md`, "Address plan"): student *i* has entry VLAN
`entry.first + i` and internal VLAN `internal.first + i`.

## Rules for the whole rollout

### Run the access check after every change

Run a student-access check after every section below. It reads the Headscale
nodes of the roster logins (users without nodes, staff and `zz-*` logins are
skipped) and prints
`student nodes: N; reach own /24: K; LOST own /24: L; also reach a foreign /24: F; filter entries on jumpbox: E`,
exiting 1 when `L > 0`. It tests only each student's **entry** /24; the
staging acceptance test and the smoke test (section 6) prove the internal
VLANs.

The expected results:

- **Baseline (section 1):** exit 0 and `L = 0`. Record `N` and `K`. A red
  baseline stops the rollout before anything changes.
- **After sections 2 and 3:** exit 0, and `N`, `K`, `L` exactly equal the
  baseline.
- **After section 5** (new roster, so `N` changes): exit 0 and `L = 0`.
  Record `N` and `K` as the post-changeover baseline.
- **After each policy apply in section 6 and after section 7:** exit 0 with
  the post-changeover counts.

**At the first result that differs, stop and roll back (section 8) every
section done so far.**

### Procedure R: restore Headscale as one set

Only rollback uses this (section 8, item 3). It puts back, **with Headscale
stopped**, the database and key file that the changeover's stage 1 replaced,
together with the policy file that stage 4 replaced. Headscale checks that
every user the policy names exists, so the outgoing database must never
start under the incoming policy, or the reverse. Run it on the Headscale
host, in `HEADSCALE_DIR`, after setting `HEADSCALE_HOST` in that shell to
exactly the value the apply script used and pasting `undo_file` from
`docs/TERM_CHANGEOVER.md` "Undo one file".

- Every replaced file was printed as `backup: <path>` (a copy exists; it is
  copied back over the live file, which must exist, keeping its owner and
  mode) or `absent-before: <path>` (there was no file; the one the stage
  created is deleted). Remote paths carry the `<HEADSCALE_HOST>:` prefix.
  `undo_file` requires exactly that prefix (IPv6-safe) and refuses, changing
  nothing, a line from another host or without one, a mode other than
  `check`/`apply`, and a `backup:` line whose backup or live file is missing.
- `DB_LINE` and `KEYS_LINE` are the database and key-file lines stage 1
  printed; `POLICY_LINE` is stage 4's `backup:` line. Leave a line empty if
  its stage printed none: `undo_file` leaves that file alone.

```bash
DB_LINE='<stage 1 database line, or empty>'; KEYS_LINE='<stage 1 key-file line, or empty>'; POLICY_LINE='<stage 4 line, or empty>'
HOST_FILE='<host policy file>'
undo_file check "$DB_LINE" "$PWD/headscale/data/db.sqlite" "$HEADSCALE_HOST" \
  && undo_file check "$KEYS_LINE" "$PWD/students_with_keys.csv" "$HEADSCALE_HOST" \
  && undo_file check "$POLICY_LINE" "$HOST_FILE" "$HEADSCALE_HOST" && test -f "$HOST_FILE" && echo lines-ok
[[ -n "$POLICY_LINE" ]] || sha256sum "$HOST_FILE"   # no stage-4 backup: must equal section 1's record
```

Expected: `lines-ok`, and without a stage-4 backup a hash equal to section
1's record. Otherwise stop: nothing has been modified. Then:

```bash
docker compose stop headscale
undo_file apply "$DB_LINE" "$PWD/headscale/data/db.sqlite" "$HEADSCALE_HOST"
undo_file apply "$KEYS_LINE" "$PWD/students_with_keys.csv" "$HEADSCALE_HOST"
undo_file apply "$POLICY_LINE" "$HOST_FILE" "$HEADSCALE_HOST"
docker compose start headscale
for i in $(seq 1 30); do docker compose exec -T headscale headscale policy get > /dev/null 2>&1 && break; sleep 1; done
docker compose exec -T headscale headscale policy check --file '<HEADSCALE_POLICY_FILE>'   # must exit 0
docker compose exec -T headscale headscale users list                                      # the outgoing roster, staff, infrastructure
[[ -z "$POLICY_LINE" ]] || { docker compose exec -T headscale headscale policy get > /tmp/policy.readback \
  && python3 -c 'import json,sys; a,b=(json.load(open(p)) for p in sys.argv[1:]); sys.exit(a != b)' /tmp/policy.readback "$HOST_FILE" \
  && echo restored; }
```

Expected: Headscale starts, `policy check` exits 0, the user list is the
outgoing roster plus the staff and infrastructure users, and (with a stage-4
backup) `restored`. If Headscale does not start, `docker compose logs
headscale` names the user the policy lacks: the set is mixed, so stop and
escalate. A matching file proves only what Headscale serves, not what it has
loaded (`docs/CLOUD_BACKENDS.md`, "Apply the Headscale policy"). The peer
check comes after the roster and portal restore (section 8, item 3); the
access check that follows is the proof of what is enforced.

## 1. Preconditions and pre-change records

All of these must hold before continuing:

- The maintenance window is approved, and the staging acceptance test
  (`docs/STAGING.md`, section 7) has a recorded pass at the release you are
  deploying.
- The term is over. The admin dashboard shows no live VM record, and the
  cluster holds no production portal guest. On a Proxmox node (VMIDs and
  guests are read cluster-wide from `/cluster/resources`, never from one
  node's `qm list`):

  ```bash
  pvesh get /cluster/resources --type vm --output-format json | python3 -c '
  import json, re, sys
  for r in json.load(sys.stdin):
      name = r.get("name") or ""
      if re.match(r"(student|group)-", name) and not name.startswith("student-zz-"): print(r["vmid"], name)'
  ```

  It prints nothing. Staging's own `student-zz-*` guests are excluded;
  destroy them anyway before the changeover.
- **`JUMPBOX_SSH` is set** in production's `scripts/site.env`
  (`ubuntu@<jumpbox-mgmt-address>` or equivalent), and from the operator
  machine `ssh -o BatchMode=yes <JUMPBOX_SSH> tailscale status --json` prints
  JSON with `"BackendState": "Running"`. `term_changeover.sh --apply` runs
  the policy preflight (`apply_headscale_policy.sh --preflight`) before its
  first stage and refuses without this; `scripts/apply_headscale_policy.sh
  --preflight` on its own must end with `preflight passed`.
- **A free pair for the smoke test.** Section 6 imports one test login, and
  `scripts/import_students.py` gives it the pair after the current highest
  roster pair. So the new roster must leave one pair free; from the repository
  root, with `scripts/site.env` carrying the release's `VLAN_BANDS`:

  ```bash
  python3 -c 'import sys; sys.path.insert(0, "."); from scripts.build_students_merged import read_canvas, roster_max; n = len(read_canvas(sys.argv[1])); print(n, roster_max()); sys.exit(n >= roster_max())' <canvas-export>.csv
  ```

  It prints the roster size `n` and the pool size, and exits 0.
  - **The reservation:** pair `entry.first+n / internal.first+n`
    (`100+n / 200+n` by default) belongs to the smoke test from section 6b
    until its cleanup (6i). **No late roster import may run while the smoke
    test holds that pair**; `import_students.py` and the admin roster tools
    wait until 6i's item 8 verification has passed. After cleanup the
    released pair is free again, and the next import takes it.
  - If the roster fills the `entry` band, no pair is free. Stop and choose:
    skip section 6 (the staging pass then stands alone), or run it before
    seeding the full roster.
- **VLAN pair audit:** `docker exec <PORTAL_APP_CONTAINER> python
  scripts/export_network_slots.py > slots.json && python3
  scripts/audit_vlan_pairs.py slots.json` prints `ok`. On any problem, stop
  before any rollout change. Fix the offending rows: remove them, or renumber
  them to free pairs, with the audit's output naming each one. Then run the
  audit again; it must print `ok`. If the rows cannot be corrected, stop and
  escalate. There is no purge-first path: `term_changeover.sh` cannot stop
  before its policy stage.
- **Lab subnets clear of the management side.** On the jumpbox, with
  `jumpbox_netgen.py` copied to `jumpbox/`:

  ```bash
  ip -4 -j route show table all | python3 -c '
  import ipaddress, json, sys; sys.path.insert(0, "jumpbox"); import jumpbox_netgen as ng
  bands = ng.parse_bands(sys.argv[1])
  lab = [ipaddress.ip_network(ng.gateway(t, bands), strict=False) for t in ng.vlan_tags(bands)]
  bad = sorted({(r["dst"], r.get("dev", "")) for r in json.load(sys.stdin)
                if r["dst"] != "default" and not r.get("dev", "").startswith(("vlan", "tailscale"))
                and any(ipaddress.ip_network(r["dst"], strict=False).overlaps(n) for n in lab)})
  print(bad or "no-overlap"); sys.exit(1 if bad else 0)' '<VLAN_BANDS value>'
  ```

  Expected: `no-overlap`. Any route on the management NIC, `docker0` or
  another non-lab device that overlaps a generated lab subnet stops the
  rollout.
- **Jumpbox records, made before the backup so they are inside it:**
  - If the jumpbox carries a hand-loaded, log-only VLAN-to-VLAN table (called
    J1 below: `inet jumpbox_vlan_isolation` with log rules, prefix
    `jb-xvlan`), `sudo sh -c 'nft list table inet jumpbox_vlan_isolation >
    /root/j1-before-j2.nft'`. Otherwise record "no J1".
  - `ip -br addr | grep -c '^vlan' | sudo tee /root/vlan-count-before`.
  - Then take a Proxmox backup of the jumpbox VM.
- **Policy record:** on the Headscale host, `sha256sum <host policy file>`,
  kept with your change record. Procedure R compares against it when stage 4
  installed nothing.
- Run the access check and record the result as the baseline.

## 2. Deploy the portal

Deploy the release with `VLAN_BANDS` in `.env`: rename an existing
`JUMPBOX_VLAN_BANDS` line to `VLAN_BANDS`, or remove it to take the default,
and delete any `GROUP_ENTRY_VLAN_*` / `GROUP_INTERNAL_VLAN_*` line. Make the
same change in `scripts/site.env`. Leave `LAB_BRIDGE` and `VMID_RANGE`
absent. Recreate `vm-automation`.

Verify: `docker exec <PORTAL_APP_CONTAINER> python
scripts/export_network_slots.py | jq -r .config.vlan_bands` prints the band
string, and the portal started (it refuses to start while a stored student
pair lies outside the bands). Then run the access check.

## 3. Adopt the jumpbox

Follow `docs/JUMPBOX.md` "Adopting a jumpbox that was built by hand" from an
interactive console session, with `scripts/site.env` carrying the same
`VLAN_BANDS`. Never start `nftables.service`. Read the differences you
recorded when you adopted the staging jumpbox first.

The dry run lists the dnsmasq open-file drop-in
(`/etc/systemd/system/dnsmasq.service.d/60-jumpbox-limits.conf`): without it
dnsmasq does not start with 401 VLANs. It does not list the `apt-get
install` it runs if dnsmasq or the guest agent is missing. It also lists the
lab egress table (`/etc/nftables.d/jumpbox-lab-egress.nft` and
`jumpbox-lab-egress.service`), which every `--apply` installs. That table
removes any egress a hand-built jumpbox gave lab VMs toward the management
network or the internet. No lab may need outbound internet during a session;
package installs belong in the image build. The table needs `nft`.

- Before moving the hand-built files aside, compare them with the rendered
  netplan (`python3 jumpbox/jumpbox_netgen.py netplan --bands "<VLAN_BANDS
  value>" --mgmt-if <mgmt> --trunk-if <trunk>`). With the default bands, the
  only VLAN differences must be `vlan200` moving from `10.200.0.1/24` to
  `10.100.1.1/24`, the new `vlan201`-`vlan299`, and `vlan500`.
- After `--apply`:
  - `ip -br addr show vlan200` lists only `10.100.1.1/24`. If
    `10.200.0.1/24` is still there, run `sudo ip addr del 10.200.0.1/24 dev
    vlan200`, then `ip -br addr show vlan200` again; it must list only
    `10.100.1.1/24`.
  - `ip -br addr show vlan299` lists `10.199.1.1/24`.
  - `ip -br addr | grep -c '^vlan'` and `grep -c '^dhcp-range='
    /etc/dnsmasq.d/jumpbox.conf` both print the number of tags in
    `VLAN_BANDS` (`401` by default).
  - `systemctl show dnsmasq -p LimitNOFILESoft` prints
    `LimitNOFILESoft=65536`.
  - dnsmasq and the Tailscale daemon are active. The unit name depends on how
    Tailscale was installed: `tailscaled` (apt) or
    `snap.tailscale.tailscaled` (the snap that `jumpbox_configure.sh
    --tailscale` installs). `systemctl is-active dnsmasq; systemctl is-active
    tailscaled || systemctl is-active snap.tailscale.tailscaled` prints
    `active` twice, the second from whichever unit exists.
  - `sudo nft list table inet jumpbox_lab_egress` shows `iifname "vlan*"
    oifname "vlan*" return` and `iifname "vlan*" oifname != "tailscale0"
    counter drop`, and `systemctl is-enabled jumpbox-lab-egress.service`
    prints `enabled`.
- **Staff-only SSH (required).** `sudo jumpbox/jumpbox_configure.sh --apply
  --ssh-staff-only` (`docs/JUMPBOX.md`, "Staff-only SSH"), with
  `JUMPBOX_SSH_STAFF_IPS` listing the staff devices. A lab VM can otherwise
  reach the jumpbox's own SSH: that traffic is input to the jumpbox, not
  forwarding, so neither the egress table nor J2 covers it. Verify:
  - `sudo nft list table inet jumpbox_ssh` shows the final `counter drop`
    rule, and `systemctl is-enabled jumpbox-ssh-filter.service` prints
    `enabled`;
  - a staff login from a listed device works, and so does the operator
    machine's `ssh <JUMPBOX_SSH> true` over the management NIC; test both
    before closing the console;
  - no lab VM exists at this point, so section 6h proves that a lab VM
    cannot SSH to the jumpbox.
- Run the access check.

## 4. Audit the live `templates.yaml`

Every enabled individual group with a single machine sets
`internal_nic: false` on its entry point.

## 5. Run the changeover

Run `scripts/term_changeover.sh --apply <canvas-export>.csv`. Before stage 1
it runs the policy preflight. On a Proxmox site it must print `ready: the
jumpbox (<JUMPBOX_SSH>) answers and its Tailscale is Running.`; a failed
preflight stops with `nothing was changed`. Then its four stages run
(`docs/TERM_CHANGEOVER.md`), and stage 4 applies the policy for the new
roster. **Record every `backup:` and `absent-before:` line the run prints,
verbatim**, for a rollback:

- stage 1: the Headscale database (`backup:`) and key file (`backup:`, or
  `absent-before:` when there was none) — Procedure R's `DB_LINE` and
  `KEYS_LINE`;
- stage 2: the local and production roster CSVs (each `backup:` or
  `absent-before:`);
- stage 3: the MySQL dump (`backup:`, a `pre-term-changeover-*.sql`);
- stage 4: the policy `backup:` line (Procedure R's `POLICY_LINE`). If it
  scrolled away, use the newest `<policy file>.<YYYYmmddTHHMMSSZ>` on the
  Headscale host, as `backup: <HEADSCALE_HOST>:<that path>`.

Verify:

- Stage 4 printed `verified: the jumpbox lists every roster device as a
  peer.` That line is required: without it the stage failed, and no student
  may be told the VPN is open. If the stage restarted Headscale once and then
  verified, that is a pass; record it.
- The dry run of `scripts/term_changeover.sh <canvas-export>.csv` reports
  every stage, including stage 4, `nothing to do`.
- The section 1 audit, re-run, prints `ok`.
- The access check exits 0 with `LOST own /24: 0`.
- **Install the Headscale peer watchdog** on the operator host
  (`docs/JUMPBOX.md`, "Headscale peer watchdog"): the service and timer
  units, then `systemctl enable --now headscale-peer-watchdog.timer`. Its
  first run logs `ok: the jumpbox lists every roster device as a peer.`
  (`journalctl -u headscale-peer-watchdog.service`). Headscale 0.27.1 can
  leave the jumpbox without peers after the jumpbox's Tailscale reconnects;
  the watchdog restarts Headscale once when that happens. After a jumpbox
  reboot or Tailscale restart, `scripts/apply_headscale_policy.sh --apply`
  runs the same check at once.

## 6. Production smoke test with one `zz-` user

Run this after section 5, before students are told the VPN is open. It uses
the normal policy apply; nothing is swapped.

**a. A two-machine group.** Use an enabled individual group with an entry
point and a plain machine, if production has one. Otherwise add a temporary
enabled group `zz-internal-vlan-smoke`, shaped like
`templates.example.yaml`'s `two-machine-individual-example`, with a small
Linux template (with the QEMU guest agent) present on the production nodes.
Then run `cd <PORTAL_DIR> && docker compose -f <PORTAL_COMPOSE> restart
vm-automation` and wait until the container reports `healthy`.

**b. A unique login and its slot.** Every run uses a fresh login, so a rerun
never meets a leftover.

1. Set `S=zz-smoke-$(date -u +%m%d%H%M%S)-$RANDOM` and record it. Then check
   that it is absent everywhere before using it. All of these must hold, and
   any hit means pick a new `S`:
   - `SELECT COUNT(*) FROM users WHERE login_id='$S'` and `SELECT COUNT(*)
     FROM student_whitelist WHERE login_id='$S'` are both `0`;
   - `headscale users list -o json | jq --arg s "$S" '[.[] | select(.name ==
     $s)] | length'` prints `0`.
2. The free pair still exists: `docker exec <PORTAL_APP_CONTAINER> python
   scripts/export_network_slots.py | jq '[.users[].entry_vlan] | max'` prints
   less than the last tag of the `entry` band (at most `198` by default).

**c. The Headscale user, first.** On production Headscale, `headscale users
create "$S"`. Creating it before the policy names it means `headscale policy
check` never sees an identity that does not exist yet.

**d. The roster row.**

1. Write `<PORTAL_DIR>/data/$S.csv` containing `SIS User ID,SIS Login ID` /
   `0,$S`.
2. Run `docker exec <PORTAL_APP_CONTAINER> python scripts/import_students.py
   /app/data/$S.csv`, and note the pair it prints, `<E>/<I>`. `<E>` must
   equal `entry.first+n` from section 1, and `<I>` must be `<E>` plus
   `internal.first - entry.first` (`<E>/<E+100>` by default).
3. `rm <PORTAL_DIR>/data/$S.csv`.
4. Re-run the section 1 audit. It must print `ok`.

**e. Policy.** `scripts/apply_headscale_policy.sh` (dry run): the diff
removes nothing and adds exactly two rules, one with `"src": ["$S@"]` and
one with `"dst": ["$S@:*"]`. Any other change means stop and go to 6i. Then
run `--apply`; it must end with `verified: the jumpbox lists every roster
device as a peer.` Then run the access check.

**f. VPN client.** Create a one-hour pre-auth key for `$S`. Join one client
with `--accept-routes`, from a machine that is not on the staging tailnet.

**g. Deploy.** Register `$S` at `/register` and deploy the group.

**h. Pass criteria.** Pass only if all of these hold. With the default bands
the entry network is `10.<E>.0.0/24` and the internal one `10.<E>.1.0/24`;
with another layout use the addresses `docs/JUMPBOX.md` derives for `<E>` and
`<I>`.

- The production jumpbox's lease file holds the entry machine's two leases
  and the plain machine's one.
- The client reaches (ping and tcp/22) the entry machine on the entry
  network and both machines on the internal network, and both gateways
  (`10.<E>.0.1` and `10.<E>.1.1`) answer ping.
- For another roster student with entry `<X>`, neither `10.<X>.0.1` nor
  `10.<X>.1.1` answers.
- **No egress, no jumpbox SSH.** From the plain machine, on its Proxmox
  node, `qm guest exec <plain-machine-vmid> --timeout 20 -- sh -c 'ping -c2
  -W2 <mgmt-gateway>; nc -w3 -z <proxmox-node-mgmt-address> 8006'` gets no
  reply and no connection, and the counter in `sudo nft list table inet
  jumpbox_lab_egress` rises. `qm guest exec <plain-machine-vmid> --timeout
  20 -- sh -c 'nc -w3 -z 10.<E>.1.1 22'` fails too (staff-only SSH), and the
  `jumpbox_ssh` drop counter rises.

**i. Cleanup, on a pass and on a failure alike.** Skip any item whose part
never ran; the verification in item 8 always runs.

1. Destroy the deployment through the portal.
2. Delete `$S`'s node(s), then the user, in Headscale.
3. Run:

   ```bash
   docker exec <MYSQL_CONTAINER> sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u"$MYSQL_USER" "$MYSQL_DATABASE" -e "
     DELETE FROM student_whitelist WHERE login_id=\"'"$S"'\";
     UPDATE users SET entry_vlan_tag=NULL, internal_vlan_tag=NULL, disabled=1 WHERE login_id=\"'"$S"'\";"'
   ```

   The user row is kept, disabled and untagged, because its destroyed VM
   records reference it. Each run leaves one such row, which is not on the
   roster and so is never exported or granted.
4. Run `scripts/apply_headscale_policy.sh --apply`. Its diff removes exactly
   the two `$S` rules and adds nothing, and it ends with `verified: the
   jumpbox lists every roster device as a peer.` Then run the access check.
5. Re-run the section 1 audit. It must print `ok`.
6. If 6a added the temporary group, remove it and restart `vm-automation`
   as in 6a.
7. `rm -f <PORTAL_DIR>/data/zz-smoke*.csv`.
8. **Verify nothing remains.** All of these must hold:
   - `SELECT COUNT(*) FROM student_whitelist WHERE login_id LIKE
     'zz-smoke%'` is `0`;
   - `SELECT COUNT(*) FROM vm_records WHERE student_name LIKE 'zz-smoke%'
     AND destroyed = 0` is `0`;
   - `SELECT COUNT(*) FROM users WHERE login_id LIKE 'zz-smoke%' AND
     (entry_vlan_tag IS NOT NULL OR disabled = 0)` is `0`;
   - `headscale users list -o json | jq '[.[] | select(.name |
     startswith("zz-smoke"))] | length'` and the same filter on `headscale
     nodes list -o json` (by `.user.name`) both print `0`;
   - `headscale policy get | grep -c 'zz-smoke'` prints `0`;
   - `ls <PORTAL_DIR>/data/zz-smoke*.csv` finds nothing;
   - `grep -c zz-internal-vlan-smoke <PORTAL_DIR>/templates.yaml` prints
     `0`.
9. `scripts/term_changeover.sh <canvas-export>.csv` (dry run) reports every
   stage complete.

If 6h failed, roll back (section 8).

## 7. Inter-VLAN drop (J2), only after the J1 evidence gate

J2 is the drop table `jumpbox_configure.sh --isolate-vlans` installs
(`docs/JUMPBOX.md`, "VLAN isolation"). It comes after the changeover and is
gated on evidence, not on the window, so it may follow days later. **The J1
evidence gate**, all three of which must hold:

- J1 has been read after at least 7 days (counters and `journalctl -k --grep
  jb-xvlan`), and every cross-VLAN flow maps to staff or test traffic, with
  zero legitimate student or group flows.
- The site owner has accepted that reading in your change record.
- The `--allow-vlans` list for KOTH or staff VLANs is decided, possibly
  empty.

Until all three hold, J2 is not installed, and **no lab whose isolation
relies on J2 (such as a two-machine Active Directory lab) is enabled**. The
lab egress table from section 3 is already in place either way.

Record J1 first: `sudo sh -c 'nft list table inet jumpbox_vlan_isolation >
/root/j1-before-j2.nft'` if the J1 table exists, otherwise "no J1". On the
converged jumpbox, the dry run of `sudo jumpbox/jumpbox_configure.sh
--isolate-vlans [--allow-vlans <list>]` must report netplan, dnsmasq and the
lab egress table unchanged before `--apply --no-try --isolate-vlans
[--allow-vlans <list>]`.

J2 holds one counted drop, VLAN to VLAN. `--allow-vlans` exempts the listed
VLANs from it in both directions, and never opens the management side or
the internet, which the lab egress table keeps closed. Verify:

- `sudo nft list table inet jumpbox_vlan_isolation` shows `iifname "vlan*"
  oifname "vlan*" counter drop` (after any `--allow-vlans` accept rules).
- `systemctl is-enabled jumpbox-vlan-isolation.service` prints `enabled`.

Then run the access check. **Only after J2 is applied and the access check
shows no student reaching another student's networks may the labs that rely
on it be enabled.**

## 8. Rollback, if any check fails

First establish what ran: which of sections 2-7, and for section 5 which
changeover stages completed and which lines they printed. Each file a stage
replaced was printed as `backup:` (copied back on rollback) or
`absent-before:` (the file the stage created is deleted on rollback). Stage
1 printed lines only if it changed Headscale, stage 2 only for a roster file
it replaced, stage 3 a MySQL dump only if it purged and seeded, and stage 4 a
`backup:` line only if it installed a policy. Then undo, newest first, in
this order, skipping any item whose section never ran.

1. **Smoke test.** Run 6i in full, including its item 8 verification.
2. **J2.** If section 7 ran:

   ```bash
   sudo systemctl disable --now jumpbox-vlan-isolation.service   # ExecStop deletes the table
   sudo rm -f /etc/nftables.d/jumpbox-vlan-isolation.nft /etc/systemd/system/jumpbox-vlan-isolation.service
   sudo systemctl daemon-reload
   sudo nft list table inet jumpbox_vlan_isolation   # must fail: no such table
   ```

   If J1 existed in section 7, restore it with `sudo nft -f
   /root/j1-before-j2.nft`. Then `sudo nft list table inet
   jumpbox_vlan_isolation` shows J1's log-only rules and no drop rule. J1 is
   transient, as it was before.
3. **The changeover (section 5), if any stage of it ran.** Choose one
   direction, and record which in your change record:
   - **Back to the outgoing roster.** Follow `docs/TERM_CHANGEOVER.md`
     "Rollback" with every `backup:` and `absent-before:` line section 5
     recorded, in its order:
     1. Restore Headscale as one set with Procedure R: the database and key
        file (stage 1) and the policy file (stage 4), all with Headscale
        stopped. A key file printed `absent-before:` is deleted, not left
        behind. Then start Headscale, run `policy check`, and read
        `headscale users list` and the staff and infrastructure nodes. If
        stage 4 printed no backup, Procedure R checks that the live policy
        file matches section 1's record instead.
     2. Undo the local and production roster CSVs (stage 2) with
        `undo_file`, each from its own line: `backup:` copies the old file
        back, `absent-before:` deletes the incoming one.
     3. Restore MySQL from the dump, but only if stage 3 purged and seeded.
        Restart `vm-automation` and wait for `healthy`.
     4. Check. Run `scripts/headscale_peer_watchdog.sh` once: it reads the
        restored portal and logs `ok`, or restarts Headscale once and logs
        `recovered`. This release's `scripts/term_changeover.sh
        <outgoing-canvas-export>.csv` dry run reports stages 1-3 `nothing to
        do`, and after item 5 the access check matches the section 1
        baseline.

     Never restore the policy file alone, or start Headscale between
     restoring the database and the policy. If the policy must instead be
     re-rendered, do that only with the restored set running and the
     previous release deployed, from that release's checkout, after item 5.
   - **Forward to the incoming roster.** Fix the cause, then run
     `scripts/term_changeover.sh --apply <canvas-export>.csv` again.
     Completed stages are no-ops, the preflight runs first, and stage 4 must
     end with the verified peer line. Verify all four stages: the dry run
     reports every stage, including stage 4, `nothing to do`; the section 1
     audit prints `ok`; and the access check matches the post-changeover
     baseline. Nothing else in this list is undone in this direction unless
     the failure was in sections 2-3.
4. **Jumpbox network files.** If section 3 ran, from the console (the VLAN
   list in the `ip link del` line is the default bands' difference; use the
   VLANs section 3 added):

   ```bash
   sudo rm /etc/netplan/60-jumpbox-vlans.yaml /etc/dnsmasq.d/jumpbox.conf /etc/sysctl.d/90-jumpbox-router.conf
   sudo cp -a /root/jumpbox-before/netplan/. /etc/netplan/
   sudo cp -a /root/jumpbox-before/dnsmasq.conf /etc/dnsmasq.conf
   sudo cp -a /root/jumpbox-before/dnsmasq.d/. /etc/dnsmasq.d/
   sudo netplan apply
   for n in $(seq 201 299) 500; do sudo ip link del "vlan$n" 2>/dev/null; done   # netplan apply leaves removed VLANs up
   sudo ip addr del 10.100.1.1/24 dev vlan200 2>/dev/null
   sudo systemctl restart dnsmasq
   ```

   The lab egress table, the staff-only SSH filter and the dnsmasq open-file
   drop-in stay: they do not depend on the bands, and lab egress stays
   closed. Verify:
   - `ip -br addr | grep -c '^vlan'` equals `/root/vlan-count-before`.
   - `ip -br addr show vlan200` lists only `10.200.0.1/24`.
   - `systemctl is-active dnsmasq; systemctl is-active tailscaled ||
     systemctl is-active snap.tailscale.tailscaled` prints `active` twice.
   - `tailscale status` shows the subnet route.

   **Restore the section 1 Proxmox backup instead** if any of these happens:
   - management access cannot be regained from the console;
   - any check above fails after one retry;
   - `nft list tables` shows a table other than Tailscale's, `jumpbox_ssh`,
     `jumpbox_lab_egress` and `jumpbox_vlan_isolation`;
   - the cause of the failure cannot be attributed to the files above.

   A VM restore reboots the jumpbox, which clears J1 (reload it from
   `/root/j1-before-j2.nft`, which the backup contains) and the current DHCP
   leases. It also removes the lab egress table and the staff-only SSH
   filter, which postdate the backup. `jumpbox_configure.sh` refuses the
   restored hand-built box, so load the egress table on its own, from the
   console:

   ```bash
   python3 jumpbox/jumpbox_netgen.py lab-egress | sudo tee /etc/nftables.d/jumpbox-lab-egress.nft >/dev/null
   python3 jumpbox/jumpbox_netgen.py lab-egress-unit | sudo tee /etc/systemd/system/jumpbox-lab-egress.service >/dev/null
   sudo nft -c -f /etc/nftables.d/jumpbox-lab-egress.nft && sudo systemctl daemon-reload \
     && sudo systemctl enable --now jumpbox-lab-egress.service
   ```

   Decide whether to restore the SSH filter by hand before the next attempt.
5. **Portal.** If section 2 ran, redeploy the previous release. It reads the
   layout as `JUMPBOX_VLAN_BANDS`, so rename `VLAN_BANDS` back in `.env` and
   in the previous release's `scripts/site.env`, with the old value.
   Recreate `vm-automation`, and check that the export
   (`.config.jumpbox_vlan_bands` in that release) prints the old band
   string. If item 3 re-renders the policy from the previous release, do it
   now.
6. Run the access check. It must match the section 1 baseline after a
   backward rollback, or the post-changeover baseline after a forward
   recovery.

The release itself needs no database rollback: stored tags never change.
Only the changeover (section 5) changes the database, and item 3 restores
it. A half-rolled-back state, where the portal and the jumpbox use
different bands, only leaves internal VLANs unreachable; no student can
reach another student's networks.
