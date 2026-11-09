# Image builder

`build_image.sh` builds a VM image from a recipe on one backend: Proxmox, AWS
or GCP. It is plain bash plus each platform's own CLI. There is no Packer,
Vagrant, Ansible or Terraform, and no disk conversion (no `import-image`, no
Google image import). Each backend builds from its own base, so one recipe
yields one native image per platform.

This directory is in a public repo. It holds no hosts, account or project IDs,
image IDs or keys. Those go in `config.env`, which is untracked.

## What it does

```
build_image.sh [--dry-run] [--allow-dirty] <recipe.env> <backend>
build_image.sh --reap <backend> [--dry-run]
```

`<backend>` is `proxmox`, `aws` or `gcp`. A build runs these steps in order:

1. Read the recipe and the config, and check them. Name the image.
2. Resolve the base image or template for the backend.
3. Start a build VM from it, with a fresh SSH key made for this run.
4. Upload the recipe's payload to the VM, over SSH.
5. Run the setup script, `SETUP_PASSES` times. After each pass, wait for SSH.
6. Run the verify script. A failure stops the build; nothing is saved.
7. Seal the VM (`seal_linux.sh` or `seal_windows.ps1`). A failure stops the
   build; nothing is saved.
8. Save the image on the backend.
9. Append a line to the build log.
10. Destroy the build VM and print the `templates.yaml` lines.

A trap destroys the build VM on every exit, including an error, Ctrl-C and
SIGTERM. If the destroy itself fails, the script says so and tells you to run
`--reap`. Only a build that passed verify and seal is saved, logged and
printed.

**It does not:** edit `templates.yaml` (it prints lines for you to paste),
push anything, touch the portal's database, or copy an image between clouds.
It never overwrites an existing image.

### Output and exit codes

Stdout carries only the `templates.yaml` lines (for `--reap`, the IDs it
reaped). Everything else, including every `DRY-RUN:` line, goes to stderr.

| Exit | Meaning |
|---|---|
| 0 | Success |
| 1 | A build or reap failed while running. This includes errors a backend raises after the config passed: a malformed `BASE_AWS` or `BASE_GCP`, no image matching the base, a malformed `AWS_INSTANCE_TYPE`, a bad public key, a VMID outside the Proxmox block, a failed setup, verify or seal, no SSH within 900 s, a destroy that was refused. |
| 2 | The core rejected the input before any VM started: bad usage or option, unknown backend, unreadable recipe, a missing or invalid recipe key (`OS_FAMILY`, `KEEP_SSH`, the integer keys, `PAYLOAD_DIR`, `SETUP`/`VERIFY`), a missing `BASE_<BACKEND>` key, a recipe outside a git repo, a dirty repo, an invalid `IMAGE_BUILD_TTL_MINUTES`, an invalid image name, or a config the backend's `backend_check_config` rejects. |
| 130, 143 | Interrupted (INT) or terminated (TERM); the trap still ran |

### Image names and the build log

The image is named `<IMAGE_PREFIX>-<sha7>-<yyyymmddhhmm>` (UTC). `sha7` is the
first seven characters of the HEAD commit of the git repo that holds the
recipe. The name may contain only `a-z`, `0-9` and `-`, must start with a
letter, and is at most 63 characters, so `IMAGE_PREFIX` is at most 42.

The recipe's repo must be clean, or the build stops with exit 2.
`--allow-dirty` overrides that for a self-test; the commit is then recorded as
`<sha7>-dirty`. Do not use it for a real build.

Each saved image adds one tab-separated line to
`coursework/deploy/image-build/BUILD-LOG.tsv` in the recipe's repo:
`date, backend, image_id, image_name, base_id, recipe, commit, verify`.
A dry run only prints the line.

## Config keys

Copy `config.env.example` to `config.env` (untracked) and fill in the section
for your backend. Set `IMAGE_BUILD_CONFIG=/path/to/file` to use another file.
The backend checks its own keys and names any that are missing or malformed.

| Backend | Key | Meaning |
|---|---|---|
| Proxmox | `PVE_HOST` | Address of the build node. Reached as root over SSH, key auth. Also the jump host to the build VM. |
| Proxmox | `PVE_NODE` | Node that runs the build VM (default `pve`). |
| Proxmox | `PVE_REPLICA_NODE` | Node that receives the template copy (default `pve2`). |
| Proxmox | `PVE_REPLICA_HOST` | Address of the replica node. |
| Proxmox | `PVE_STORAGE` | Storage for the build VM's disk on `PVE_NODE`. |
| Proxmox | `PVE_REPLICA_STORAGE` | Storage for the replica template. |
| Proxmox | `PVE_BUILD_BRIDGE` | Bridge of the provisioning network (has internet). |
| Proxmox | `PVE_BUILD_VLAN` | VLAN tag of that network, 1-4094. |
| AWS | `AWS_BUILD_REGION` | Region. Every call names it; a profile default never decides. |
| AWS | `AWS_BUILD_SUBNET_ID` | A public subnet in the build VPC, not the lab VPC. |
| AWS | `AWS_BUILD_SG_ID` | Security group that admits SSH from the build host only. |
| GCP | `GCP_BUILD_PROJECT` | Project ID. |
| GCP | `GCP_BUILD_ZONE` | Zone, for example `us-east1-b`. |
| GCP | `GCP_BUILD_SUBNET` | Subnet of the build network, separate from the lab network. |

The Proxmox backend needs `python3` on the machine running the build. The AWS
backend needs the AWS CLI v2 on `PATH` (not checked in a dry run). The GCP
backend uses `gcloud`.

`IMAGE_BUILD_TTL_MINUTES` (environment, default 240) sets how long a build's
resources live before `--reap` may remove them. `IMAGE_BUILD_BACKENDS_DIR`
(environment) changes where `<backend>.sh` is read from.

## Recipe keys

A recipe is a sourced `.env` file inside a git repo. Only the recipe may set
these keys; values in your shell environment are ignored.

| Key | Required | Meaning |
|---|---|---|
| `IMAGE_PREFIX` | yes | Start of the image name. At most 42 characters. |
| `OS_FAMILY` | yes | `linux` or `windows`. |
| `PAYLOAD_DIR` | yes | Directory to upload, relative to the recipe file. |
| `SETUP` | yes | Setup script, relative to `PAYLOAD_DIR`. |
| `VERIFY` | yes | Verify script, relative to `PAYLOAD_DIR`. |
| `BASE_PROXMOX` | yes | Proxmox: a template name glob. The newest match on `PVE_NODE` is used. |
| `BASE_AWS` | yes | AWS: `<owner>:<AMI name glob>`. Owner is `self`, `amazon` or an account ID. The newest available AMI wins. |
| `BASE_GCP` | yes | GCP: `<image project>/<image family>`. |
| `DISK_GB` | yes | Root disk size in GB. |
| `AWS_INSTANCE_TYPE` | yes | For example `t3.small`. |
| `GCP_MACHINE_TYPE` | yes | For example `e2-standard-2`. |
| `PVE_MEMORY_MB` | yes | Build VM memory on Proxmox. |
| `PVE_CORES` | yes | Build VM cores on Proxmox. |
| `SETUP_PASSES` | no | How many times to run `SETUP` (default 1). |
| `KEEP_SSH` | no | `yes` or `no` (default `no`). See "Host SSH". |
| `GCP_IMAGE_FAMILY` | no | Family the new GCP image joins (default `IMAGE_PREFIX`). |
| `PAYLOAD_EXCLUDE` | no | Space-separated `tar --exclude` globs applied to the payload. |

All of the required keys must be set for every backend, even one you are not
building on; the unused keys are just not read. The recipe must set the
`BASE_*` key of the backend you name, or the build stops with exit 2.
`SETUP` and `VERIFY` may use only letters, digits, space and `._+-/`, with no
`..` component, and the files must exist. `SETUP_PASSES`, `DISK_GB`,
`PVE_MEMORY_MB` and `PVE_CORES` must be positive integers.

The payload is uploaded to `/tmp/image-build` on Linux and `C:\image-build` on
Windows. Setup and verify run as root (Linux, through `sudo`) or in PowerShell
(Windows), with the payload directory as the working directory on Linux.

### Rules for setup scripts

- **A setup script that reboots must do so as its final action.** The core
  treats SSH exit 255 as a dropped session, waits for SSH again (up to 900 s)
  and carries on. It does not re-resolve the VM's address after the reboot, so
  the VM must come back at the same address. Anything the script does after
  `reboot` is not guaranteed to run.
- **Ubuntu clears `/tmp` at boot.** If `/tmp/image-build` has vanished, the
  core uploads the payload again before the next setup pass and before verify.
  Do not depend on state written under `/tmp` surviving a reboot.
- **The payload never ships.** Setup, verify (which holds solve steps) and any
  tarballs are deleted by the seal. The seal exits 1 if it finds them still
  present, and the build then saves nothing.

## Host SSH: `KEEP_SSH`

| Image is | Set | Effect |
|---|---|---|
| Shipped to students | `KEEP_SSH=no` (default) | Host SSH ships off. |
| Used as the BASE of a later build (for example a Docker-host image built upon) | `KEEP_SSH=yes` | Host SSH stays on, so the next build can log in. |

On Linux, `seal_linux.sh <yes|no>` always removes the payload, empties
`/home/builder/.ssh/authorized_keys`, runs `cloud-init clean --logs` when
cloud-init is present, empties `/etc/machine-id`, deletes the host keys, and
empties shell history. With `keep_ssh=no` it also runs `passwd -l builder` and
`systemctl disable ssh`. With `keep_ssh=yes` it skips both. Whenever it deletes
the host keys it installs a systemd drop-in
(`ssh.service.d/10-regen-hostkeys.conf`) that runs `ssh-keygen -A` before sshd
starts, so a clone gets new host keys on a base without cloud-init.

On Windows, `seal_windows.ps1` disables the `sshd` service unless `-KeepSsh`
is passed (which the core does for `KEEP_SSH=yes`).

## Windows recipes

- On AWS, set `DISK_GB` to at least 30.
- `seal_windows.ps1` deletes `C:\image-build` and
  `C:\ProgramData\ssh\administrators_authorized_keys`, then sets the local
  `Administrator` password to a random 32-character value generated in the
  guest. The value is held in a `SecureString` and is never printed or stored.
- **Sysprep is not run.** Whether and how to run it is to be decided per cloud
  during the first Windows build.
- The seal does not touch the firewall. The `tcp/22` allow rule that the build
  created stays enabled, even when `sshd` is disabled.
- A Windows command given to the core's remote runner may not contain a double
  quote character.

## The backend contract

Each `backends/<name>.sh` is sourced into the core and must define these
functions. The full text is in the header of `build_image.sh`.

| Function | Job |
|---|---|
| `backend_check_config` | Exit non-zero, naming the missing key, if the config lacks what the backend needs. Runs in a subshell. |
| `backend_resolve_base <pattern>` | Print exactly one base ID, from the recipe's `BASE_<BACKEND>`. |
| `backend_start <run_id> <base_id> <pubkey_file>` | Start the build VM with the key. Set `BUILD_VM_ID` as soon as anything exists, then `BUILD_SSH_HOST`, `BUILD_SSH_USER`, `BUILD_SSH_JUMP`. |
| `backend_save <image_name>` | Save the image and set `IMAGE_ID`. Clear `BUILD_VM_ID` if the build VM itself became the image. |
| `backend_destroy` | Idempotent. Destroys only `BUILD_VM_ID`. |
| `backend_leftovers` | Print one ID per line: tagged, expired build VMs only. Reads through `query`. |
| `backend_destroy_id <id>` | Re-check the tag through `query`, then destroy through `mutate`. |
| `backend_templates_yaml <image_id>` | Print the `templates.yaml` lines. |

Every function runs with `errexit` in force. A step that may fail must be
guarded inside the backend. Backends reach a platform only through two
helpers: `mutate` for anything that creates, changes or deletes, and `query`
for reads. That is what makes `--dry-run` safe.

## Tags and labels

Every build resource carries two tags (labels on GCP):

| Key | Value |
|---|---|
| `image-build` | The run ID, `ib-<yyyymmddhhmm>-<4 hex>` |
| `image-build-expires` | UTC `yyyymmddhhmm`, `IMAGE_BUILD_TTL_MINUTES` after the build started (default 4 hours) |

On Proxmox both go in the VM description, as two space-separated words. What
the saved image carries depends on the backend:

| Backend | The saved image carries |
|---|---|
| Proxmox | The description is replaced by `image=<image-name>` and nothing else: no build tags, no recipe or commit tag. The replica copy has the same description. |
| AWS | The AMI and its snapshots carry `image-build-recipe` and `image-build-commit` tags. |
| GCP | The image carries `image-build-recipe` and `image-build-commit` labels, and a description naming the recipe, commit and run ID. |

The image has no expiry tag, because the product does not expire.

**Build resources never carry `lab-playground-site`.** The multi-backend
portal's AWS and GCP backends (branch `feature/multi-backend-vms`, PR #80, not
yet merged; see "Safety model and ownership" in its `docs/CLOUD_BACKENDS.md`)
tag the instances and other taggable resources they create with
`lab-playground-site=<LAB_SITE_ID>`, and filter inventory, reuse and deletes
by it. Its GCP networks carry no labels; they are identified by their
`lp-<site-id>-...` names instead. A build resource never carries the tag, so
the portal never lists or deletes a build instance. The builder, for its part,
selects only `image-build`-tagged build VMs when it cleans up or reaps; it
never deletes a saved image. On Proxmox the existing sweeper
(`app/orphan_reaper.py`) reaps untracked guests whose description has an
`Expires:` line past its grace period; a build description holds
`image-build=` and `image-build-expires=` words and no `Expires:` line, so the
sweeper keeps it.

### What a destroy or a reap will delete

A resource is deleted only after the backend re-reads its tag immediately
before the delete.

- **Cleanup of the current build** (the trap, and `backend_destroy`) requires
  the `image-build` value to equal this run's `RUN_ID`. Otherwise it refuses.
- **`--reap`** requires all of these:
  - the `image-build` value matches `ib-<12 digits>-<4 hex>` exactly;
  - the resource has one `image-build` tag (AWS tags and GCP labels hold one
    value per key; on Proxmox the description may hold only one
    `image-build=` word);
  - `image-build-expires` is 12 digits and earlier than now (UTC);
  - on GCP, the label also equals the instance's own name;
  - on Proxmox, also: the VM is on `PVE_NODE`, its VMID is in `8000`-`8999`,
    and it is not a template.

A resource that has the tag but fails these checks is never deleted. On AWS,
one whose `image-build-expires` is missing or malformed is reported and left
out of the list. One that is listed but whose tag is not a run ID is refused
at the delete, and `--reap` reports it and exits 1.

## Backends

### Proxmox

The build full-clones the newest template matching `BASE_PROXMOX` on
`PVE_NODE` (by the date or timestamp in the last hyphen-separated field of its
name, VMID below 9000). It starts the clone, injects a per-run SSH key through
the guest agent, and reaches it over SSH through `root@PVE_HOST`. After seal,
it shuts the VM down, renames it to the image name, turns the build VM itself
into the template, and copies that to `PVE_REPLICA_NODE` with
`scripts/sync_template.sh`.

**VMIDs.**

| Role | Node | VMID |
|---|---|---|
| Build VM and template | `PVE_NODE` | `8000`-`8499` (the lowest value whose replica ID is also free) |
| Replica | `PVE_REPLICA_NODE` | build VMID + 500, so `8500`-`8999` |

**Never create or clone a guest at VMID 9000 or above.** The portal's VMID
allocator takes the cluster's highest VMID as a permanent high-water mark
(`app/proxmox_service.py`): it allocates above the larger of the cluster's
current highest VMID and the highest value it has stored before. One guest at
9000+ therefore moves every later portal VMID above it, and once the portal has
allocated above it, deleting the guest does not undo that. The backend refuses any
VMID outside its block, and `sync_template.sh`'s optional fifth argument
(`target_vmid`) accepts only `8500`-`8999`.

`--reap proxmox` lists only non-template VMs on `PVE_NODE` in `8000`-`8999`
that meet the criteria in "What a destroy or a reap will delete". The
node-side destroy repeats the tag check before `qm stop` and again before
`qm destroy`, and refuses a template.

**A template left by a failed save must be removed by hand.** If the build VM
became a template but the conversion call reported failure, or the copy to the
replica failed, the build is not logged. The error message names the VMID:
delete it with `qm destroy <vmid> --purge` on the node, or finish the copy
with the `sync_template.sh` command the error prints.

**Base-template contract.** The template named by `BASE_PROXMOX` must have:

- a user `builder` with passwordless `sudo`;
- OpenSSH server running;
- `qemu-guest-agent` running (the build waits up to 5 minutes for it);
- DHCP on its network interface;
- **no SSH key baked in.** The build writes a per-run key into
  `/home/builder/.ssh/authorized_keys` through the guest agent, and the seal
  empties that file.

For a Windows base, the user is `Administrator` and the guest agent must run.
OpenSSH Server must be installed, and the `sshd` service must be running and
set to start automatically. The backend only injects the key, through the
guest agent, into `C:\ProgramData\ssh\administrators_authorized_keys`; it
never installs or starts `sshd`.

### AWS

The build runs one instance from the newest available AMI matching `BASE_AWS`,
in `AWS_BUILD_SUBNET_ID`, with a public IPv4 address, IMDSv2 required, and a
`DISK_GB` gp3 root volume that is deleted on termination. The key goes in as
user data: on Linux, cloud-init creates `builder` with passwordless sudo and the
key; on Windows, EC2Launch runs a script that installs OpenSSH Server, writes
`administrators_authorized_keys`, starts `sshd` and opens `tcp/22`. The image
is made with `create-image`, which reboots the instance, and is waited on
until it is `available`.

The instance and its root volume carry the two build tags. The AMI and its
snapshots carry `image-build-recipe` and `image-build-commit`.
`--reap aws` scans `AWS_BUILD_REGION` only. `templates.yaml` gets
`aws: {image_id, instance_type, disk_gb}`.

### GCP

The build runs one instance, named after the run ID, from the newest
non-deprecated image in the `BASE_GCP` family. It has an external IP, no
service account and no scopes. On Linux the key goes in as `ssh-keys` metadata
for `builder`, with OS Login and project-wide keys turned off for the instance.
On Windows a startup script installs OpenSSH Server, writes the key, enables
the built-in `Administrator` with a random password generated in the guest,
starts `sshd` and opens `tcp/22`. The instance is stopped, then the image is
made from its boot disk and joins the `GCP_IMAGE_FAMILY` family. The build
labels are on the instance; the image carries `image-build-recipe` and
`image-build-commit` labels.

**`--reap gcp` scans all zones of the project**, not only `GCP_BUILD_ZONE`. See
"What a destroy or a reap will delete" for the criteria. IDs are printed as
`<zone>/<instance>`.
`templates.yaml` gets `gcp: {image, machine_type, disk_gb}`.

## Dry run and reaping

**Always run `--dry-run` first.** It prints every command that would reach a VM
or a cloud as a `DRY-RUN:` line on stderr and runs none of them. Reads print
their command and return a placeholder, so IDs in a dry run are placeholders.
It still validates the recipe, the config and the repo state, and names the
image.

```
scripts/image_build/build_image.sh --dry-run path/to/recipe.env aws
scripts/image_build/build_image.sh path/to/recipe.env aws
```

**`--reap <backend>`** removes build VMs that a killed build left behind
(`kill -9`, a lost laptop, a power cut). It lists the backend's leftovers:
expired build VMs that meet the criteria in "What a destroy or a reap will
delete". For each one the backend re-checks the tag, then
destroys it. It prints `destroyed <id>` for each, and exits 1 if any could not
be destroyed. With `--dry-run`, `--reap` still runs the read-only queries for
real, because the plan needs the real list, and prints `would destroy <id>` and
the destroy commands instead of running them. Run `--reap <backend>
--dry-run` first, as for a build. `--reap` takes no recipe and no
`--allow-dirty`.

A VM stays safe from `--reap` until its expiry (default 4 hours after the
build started), so do not reap a build you are still running.

## Deleting an old image

The builder never deletes images. Keep one previous image per machine (one
recipe on one backend), and delete the older ones by hand once
`templates.yaml` no longer points at them. Check the ID before you run
anything; deletion is not reversible.

**Proxmox.** A build makes two templates. Destroy both:

```
ssh root@<PVE_HOST> qm destroy <vmid> --purge
ssh root@<PVE_REPLICA_HOST> qm destroy <vmid+500> --purge
```

The image ID the build printed is `<node>:<vmid>,<replica node>:<vmid+500>`.

**AWS.** List the snapshots first, then deregister the AMI and delete each
snapshot:

```
aws ec2 describe-images --region <region> --image-ids <ami-id> \
  --query 'Images[0].BlockDeviceMappings[].Ebs.SnapshotId' --output text
aws ec2 deregister-image --region <region> --image-id <ami-id>
aws ec2 delete-snapshot --region <region> --snapshot-id <snap-id>
```

**GCP.**

```
gcloud compute images delete <image-name> --project <project>
```
