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

It is opt-in. Without `LIVE_PROXMOX=1` and the variables below, every
Proxmox test here (`test_proxmox_contract.py`) is skipped, so `pytest tests/`
and CI skip it without opening a connection. The safety guards are
unit-tested without a cluster in `tests/test_live_contract_guards.py`, which
CI does run. The AWS and GCP runs have their own, separate opt-in; see
[Live AWS and GCP contract runs](#live-aws-and-gcp-contract-runs) below.

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

# Live AWS and GCP contract runs

The unit suite runs `AWSBackend` against moto and `GCPBackend` against a
fake REST session. Neither is evidence of how EC2 or Compute Engine really
behave. `test_aws_contract.py` and `test_gcp_contract.py` drive the app's own
backends, through the registry, against a real account and check:

- one full lifecycle per cloud: admission at a cap of one, router, slot
  network, launch from the configured image, identity (`inspect`), status,
  private-only addresses inside the slot's CIDR, an expiry update visible in
  `list_managed()`, a second admission refused at the cap, then teardown and
  an empty inventory;
- **AWS:** instance-level `SecurityGroups` populated and no public
  `Association` on a private launch; `RunInstances` `ClientToken`
  idempotency after a lost response (a client-side timeout); a deployment
  security group delete refused with `DependencyViolation` while attached,
  and surfaced to the caller; `stop_router` while the router is `pending`;
- **GCP:** the deployment firewall accepted unchanged; the aggregated-list
  name filter is exact and literal and the hyphenated label key parses;
  which warning codes Compute sends (`NO_RESULTS_ON_PAGE` must be the only
  one) and how an unreachable zone shows up; `/wait` returning an unfinished
  operation and blocking less than the backend's read cap; a read timeout
  while waiting fails the call and a retry completes the delete; how an
  insert into a missing subnetwork is refused (and that the backend raises
  `SlotNetworkMissing` before inserting); whether a reused `requestId`
  replays a failed operation; the router found by its labels but never
  listed as managed.

Where the provider's answer is the point of the check (status codes,
warning codes, `/wait` timings), the test records it as a `LiveObservation`
warning, which appears in pytest's warnings summary. A check whose race the
run could not reproduce (the router already past `pending`, a delete that
finished before its first `/wait`, an insert refused before any operation
existed) is reported as SKIPPED with the reason, never as passed.

The GCP network and router checks call the `VMBackend` methods
`ensure_slot`, `release_slot`, `cleanup_deployment`, `ensure_router_running`
and `stop_router`. They need a `GCPBackend` that implements its networks.

## Cloud safety rules

`tests/live/cloud_harness.py` (`CloudContractRun`) enforces these rules;
they are unit-tested against the in-memory backend in
`tests/test_live_cloud_contract_guards.py`, which CI does run:

1. **A dedicated live site.** `LAB_SITE_ID` must be `live-<name>` (at most
   20 characters, lower-case letters, digits and `-`), used by no portal.
   Every resource the backends create carries that site, and every
   inventory is filtered by it.
2. **One machine at a time.** `AWS_MAX_VMS` / `GCP_MAX_VMS` must be exactly
   `1`.
3. **An empty site to start with.** The run refuses to start while
   `list_managed()` reports anything for the site, and touches none of it.
   Teardown can therefore only remove what this run created.
4. **A 30-minute expiry.** Every instance is launched with the app's expiry
   tag or label 30 minutes out, so the portal's sweeper would recognise it.
5. **Claimed before launch.** Each instance is claimed before `create` is
   called, so one whose launch raised half-way is still destroyed.
6. **Teardown always runs,** pass or fail, in this order: destroy each
   instance and require positive absence; delete its deployment network;
   release the slot; release the admission; stop the router. Network deletes
   are retried for up to five minutes while the provider still reports them
   in use (EC2 frees a terminated instance's interface asynchronously).
7. **Zero resources left.** A fresh `list_managed()` of the site must be
   empty. If it is not, the run deletes exactly what that fresh inventory
   lists (`delete_resource`, which re-proves ownership) and requires a second
   empty inventory.

Any cleanup failure fails the test's teardown, even after the test body
failed, with the provider ids and the inventory commands below filled in.

The run only reads and pins `app.config` values it validated: the site, the
cap, `ENABLED_BACKENDS` and the cloud settings below are patched into
`app.config` for the session, so a stray `.env` value cannot widen the cap or
change the site. Credentials stay in boto3's standard chain (environment,
shared config, instance role) or Google Application Default Credentials and
are never printed.

## Cloud environment

Without its flag and every variable below, a provider's tests are skipped
at collection, before any provider module is imported or client built. The
flags are independent: `LIVE_AWS=1` alone runs the AWS tests (the Proxmox
`LIVE_PROXMOX` guard applies only to the Proxmox tests).

| Variable | AWS | GCP | Example | Meaning |
|----------|-----|-----|---------|---------|
| `LIVE_AWS` / `LIVE_GCP` | `LIVE_AWS` | `LIVE_GCP` | `1` | Opt in. Any other value skips. |
| `ENABLED_BACKENDS` | contains `aws` | contains `gcp` | `aws` | As for the app. |
| `LAB_SITE_ID` | yes | yes | `live-contract` | The dedicated live site. Must start with `live-`. |
| `AWS_MAX_VMS` / `GCP_MAX_VMS` | `AWS_MAX_VMS` | `GCP_MAX_VMS` | `1` | Must be `1`. |
| `AWS_REGION`, `AWS_AVAILABILITY_ZONE` | yes | | `us-east-1`, `us-east-1a` | Where the run launches. |
| `AWS_LAB_VPC_ID`, `AWS_LAB_CIDR` | yes | | `vpc-…`, `172.20.0.0/16` | The lab VPC and its slot base, as for the app. |
| `AWS_ROUTER_INSTANCE_ID` | yes | | `i-…` | A router tagged `lab-playground-site=<live site>` and `lab-playground-kind=router`. |
| `LIVE_AWS_IMAGE_ID`, `LIVE_AWS_INSTANCE_TYPE` | yes | | `ami-…`, `t3.micro` | What the run launches. The root disk is 20 GB. |
| `GCP_PROJECT`, `GCP_REGION`, `GCP_ZONE` | | yes | `my-lab-project`, `us-central1`, `us-central1-a` | Where the run launches. |
| `GCP_NETWORK`, `GCP_LAB_CIDR` | | yes | `lab-vpc`, `172.21.0.0/16` | The lab VPC network and its slot base, as for the app. |
| `GCP_ROUTER_INSTANCE` | | yes | `lab-router` | A router labelled `lab-playground-site=<live site>` and `lab-playground-kind=router`. |
| `LIVE_GCP_IMAGE`, `LIVE_GCP_MACHINE_TYPE` | | yes | `projects/debian-cloud/global/images/family/debian-12`, `e2-small` | What the run launches. The boot disk is 20 GB. |

The timeouts (`AWS_LAUNCH_TIMEOUT_SECONDS`, `AWS_TERMINATE_TIMEOUT_SECONDS`,
`GCP_OPERATION_TIMEOUT_SECONDS`) are the app's, and default as they do there.

Use a dedicated live VPC (or at least a dedicated router and site) and an
identity limited to it. The AWS run needs to describe, run, tag, start, stop
and terminate instances; create, tag and delete subnets and security groups
and change their rules. The GCP run needs the same on instances,
subnetworks and firewall rules, plus operation reads.

## Running the cloud contracts

Run one provider at a time, from a checkout without a production `.env`:

```bash
# AWS
export LIVE_AWS=1 ENABLED_BACKENDS=aws LAB_SITE_ID=live-contract AWS_MAX_VMS=1 \
       AWS_REGION=us-east-1 AWS_AVAILABILITY_ZONE=us-east-1a \
       AWS_LAB_VPC_ID=vpc-... AWS_LAB_CIDR=172.20.0.0/16 \
       AWS_ROUTER_INSTANCE_ID=i-... \
       LIVE_AWS_IMAGE_ID=ami-... LIVE_AWS_INSTANCE_TYPE=t3.micro
uv run python -m pytest tests/live/test_aws_contract.py -v -rs
```

```bash
# GCP
export LIVE_GCP=1 ENABLED_BACKENDS=gcp LAB_SITE_ID=live-contract GCP_MAX_VMS=1 \
       GCP_PROJECT=my-lab-project GCP_REGION=us-central1 GCP_ZONE=us-central1-a \
       GCP_NETWORK=lab-vpc GCP_LAB_CIDR=172.21.0.0/16 GCP_ROUTER_INSTANCE=lab-router \
       LIVE_GCP_IMAGE=projects/debian-cloud/global/images/family/debian-12 \
       LIVE_GCP_MACHINE_TYPE=e2-small
uv run python -m pytest tests/live/test_gcp_contract.py -v -rs
```

Each test launches at most one instance and removes it before the next
starts. Expect a few minutes per test; the GCP file is the longer one.

## Manual cloud cleanup

A killed process can leave resources in the live site. They carry the
site tag or label and an expiry 30 minutes out. List them (the run prints
these with the region, project and site filled in when its teardown fails):

```bash
# AWS
aws ec2 describe-instances --region <region> \
  --filters Name=tag:lab-playground-site,Values=<live site> \
            Name=instance-state-name,Values=pending,running,stopping,stopped
aws ec2 describe-security-groups --region <region> --filters Name=tag:lab-playground-site,Values=<live site>
aws ec2 describe-subnets --region <region> --filters Name=tag:lab-playground-site,Values=<live site>

# GCP
gcloud compute instances list --project <project> --filter='labels.lab-playground-site=<live site>'
gcloud compute firewall-rules list --project <project> --filter='name ~ ^lp-<live site>-d-'
gcloud compute networks subnets list --project <project> --filter='name ~ ^lp-<live site>-s-'
```

Remove instances first, then security groups or firewall rules, then
subnets: `aws ec2 terminate-instances`, `delete-security-group`,
`delete-subnet`; `gcloud compute instances delete`, `firewall-rules delete`,
`networks subnets delete`. Never remove the router; stop it instead. The
next run refuses to start until the site is empty again.
