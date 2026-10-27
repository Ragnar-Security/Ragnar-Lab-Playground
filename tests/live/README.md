# Live Proxmox contract run

The unit suite mocks `proxmoxer`. This directory checks the real Proxmox API
against the shapes those mocks assume, using the app's own helpers:

- `cluster/resources` rows (`vmid`, `name`, `node`, `type`, `status`,
  `template`), which feed `get_cluster_vm_index`, the orphan reaper and
  VMID allocation;
- `nodes/{node}/status` memory fields `total`, `used`, `free` and
  `available`, which feed `get_node_resources` and admission control;
- a clone through `create_qemu_vm`: linked when the source is a template,
  falling back to a full clone if the storage rejects linked clones; a full
  clone onto `LIVE_CLONE_STORAGE` when configured;
- start, the guest agent's `network-get-interfaces` reply (optional), stop,
  and the task `status`/`exitstatus` that `wait_for_proxmox_task` reads;
- delete with task waiting, `confirm_proxmox_absence`, and the error a read
  of a deleted VMID raises (`status_code`, recognised by
  `is_proxmox_not_found`);
- cleanup after a failure injected mid-test.

It is opt-in. Without `LIVE_PROXMOX=1` and the variables below, every test
here is skipped, so `pytest tests/` and CI skip it without opening a
connection. The safety guards are unit-tested without a cluster in
`tests/test_live_contract_guards.py`, which CI does run.

## Safety rules

`tests/live/harness.py` (`ContractRun`) enforces these rules:

1. **Only VMIDs inside `LIVE_VMID_RANGE`.** The run refuses to start, and
   refuses each new clone, while any guest in the range has a name that does
   not start with `lab-contract-test-`.
2. **Never above the portal's VMID high-water mark.** On a cluster the
   portal deploys to, the run refuses to start when the range reaches above
   that mark (see "Choosing the VMID range").
3. **Recognisable guests.** Each guest is named
   `lab-contract-test-<random>` and gets the app's description line
   `Expires: <now + 30 minutes>`.
4. **Cleanup always runs.** The fixture's teardown destroys every guest the
   run created, whether the test passed or failed.
5. **It touches only its own guests.** The run starts, stops or destroys a
   VMID only if this run created it and fresh inventory still shows the exact
   name and node it gave that guest. Otherwise it leaves the VMID alone and
   reports it.

If a delete fails, the run prints copy-paste removal commands to stderr and
fails the test's teardown.

## Environment

| Variable | Required | Example | Meaning |
|----------|----------|---------|---------|
| `LIVE_PROXMOX` | yes | `1` | Opt in. Any other value skips. |
| `PROXMOX_HOST` | yes | `lab.example.edu` | API host. The app's own connection code is used. |
| `PROXMOX_USER` | token auth | `contract@pve` | User that owns the token. |
| `PROXMOX_TOKEN_ID` + `PROXMOX_TOKEN_SECRET`, or `PROXMOX_PASSWORD` | yes | | Credentials. |
| `PROXMOX_VERIFY_SSL` / `PROXMOX_CA_BUNDLE` | no | | TLS settings, same as the app. |
| `LIVE_NODE` | yes | `node1` or `node1,node2` | Node(s) to test. Each node gets its own cases. |
| `LIVE_TEMPLATE_VMID` | yes | `9000` or `node1:9000,node2:9001` | QEMU template on each node. The app clones on the template's node. |
| `LIVE_VMID_RANGE` | yes | `900-999` | Inclusive VMID range the run may use. Must lie below the portal's VMIDs; see below. |
| `LIVE_PORTAL_DATABASE_URL` | no | `mysql+pymysql://readonly:...@db-host/portal` | Portal database, read once per test to check the range against the VMID high-water mark. Use a read-only account. |
| `LIVE_SEPARATE_CLUSTER` | no | `1` | The cluster is one the portal never deploys to; skips the high-water check. Not allowed together with `LIVE_PORTAL_DATABASE_URL`. |
| `LIVE_CLONE_STORAGE` | no | `fast-zfs` or `node2:fast-zfs` | Enables the full-clone-onto-storage test for those nodes. |
| `LIVE_AGENT_TIMEOUT` | no | `90` | Seconds to wait for the guest agent when the template enables it. |

The app's config module also loads a `.env` file from the working directory.
Run from a checkout without a production `.env`, so that only the variables
you export are in effect.

Use a dedicated API token, not the portal's own credentials. It needs to
audit the cluster and nodes, clone the templates, allocate VMIDs in the
range, set memory, CPU, network and description options, manage power, use
the target storage, and (optionally) query the guest agent.

## Choosing the VMID range

The portal allocates the next VMID above the larger of the highest VMID in
the cluster and a high-water mark stored in its database, then stores the
new top as the mark. The mark never goes down: not when guests are deleted,
and not when the portal restarts. If a contract guest sits above every
student VMID while a student deploys, the portal allocates above the
contract guest and records that, so every later allocation stays raised
for good.

Therefore use one of these:

- **A range below the portal's allocation base**, on a cluster the portal
  also uses: below every VMID the portal has ever allocated (if student
  VMIDs start at 1000, for example, use `900-999`). Set
  `LIVE_PORTAL_DATABASE_URL` and the run reads the mark (the larger of
  `coordination_state.vmid_allocator` and `MAX(vm_records.vmid)`) and
  refuses a range that reaches above it. Without that variable the run can
  only see the cluster: it refuses a range that reaches above every other
  guest, which is a lower bound on the mark. That check cannot see a mark
  raised by guests that were since deleted, so read the mark yourself
  before choosing:

  ```sql
  SELECT GREATEST(
    COALESCE((SELECT value FROM coordination_state
              WHERE name = 'vmid_allocator'), 0),
    COALESCE((SELECT MAX(vmid) FROM vm_records), 0)) AS vmid_mark;
  ```

- **A separate cluster** that the portal never deploys to, with
  `LIVE_SEPARATE_CLUSTER=1`. Any range works there.

Leftover `lab-contract-test-*` guests are allowed in the range. The run skips
their VMIDs and never touches them.

## Running it

```bash
export LIVE_PROXMOX=1 PROXMOX_HOST=lab.example.edu PROXMOX_USER=contract@pve \
       PROXMOX_TOKEN_ID=contract PROXMOX_TOKEN_SECRET=... \
       LIVE_NODE=node1,node2 LIVE_TEMPLATE_VMID=node1:9000,node2:9001 \
       LIVE_VMID_RANGE=900-999 \
       LIVE_PORTAL_DATABASE_URL='mysql+pymysql://readonly:...@db-host/portal'
uv run python -m pytest tests/live -v -rs
```

A full run clones two guests per node (three with `LIVE_CLONE_STORAGE`), one
at a time, and takes several minutes. A warning that a missing VMID returned
HTTP 500 rather than 404 is expected: Proxmox reports a missing guest config
as a 500 with "does not exist", and the app classifies that text.

## Manual cleanup

The orphan reaper does **not** remove these guests: it only considers
`student-` and `group-` names. A guest that survives a run (for example a
killed process, or a delete the run reported as failed) stays until someone
removes it. The `Expires:` line tells you when it was meant to go.

Confirm the name first, then remove it on the node that hosts it:

```bash
qm config <vmid> | grep '^name: lab-contract-test-'
qm stop <vmid>; qm destroy <vmid> --purge
```

or through the API:

```bash
pvesh create /nodes/<node>/qemu/<vmid>/status/stop
pvesh delete /nodes/<node>/qemu/<vmid> --purge 1
```

For a delete it could not finish, the run prints these commands with the
VMID, node and name already filled in.
