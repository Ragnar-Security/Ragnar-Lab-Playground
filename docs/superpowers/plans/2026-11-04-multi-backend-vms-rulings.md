# Multi-backend VMs: rulings made during implementation

These decisions were made by the controller while the plan
(`2026-11-04-multi-backend-vms.md`) was executed, task by task, with
cross-family review: Opus or Sonnet built each task, Codex reviewed it. Each
entry gives what was decided, why, and what it costs if it turns out wrong.
The plan's own "Plan-level rulings" section covers the preflight rulings
(C1–C7, I1–I19, M1–M21).

## Process

- **The plan was drafted by GPT Daybreak.** The generic `task` agent is unpinned and defaulted to it. An Opus preflight and two Codex re-review rounds ran before Task 1. Cost if wrong: none.
- **Review routing:** work built on Opus or Sonnet was reviewed by Codex, and work built on Codex was reviewed by Opus.
- **Tasks 15–19 ran in a parallel worktree,** `feature/multi-backend-vms-policy`, and were merged back with merge commits. Cost if wrong: merge conflicts. None occurred.

## Proxmox (no behaviour change)

- **Cluster inventory.** `ProxmoxBackend.list_managed()` is a projection of the cluster index and reads no guest configs. Reconcile needs only presence, and the Proxmox orphan sweep keeps its own path that reads `Expires:` itself. Cost: nothing generic can read a Proxmox expiry from inventory.
- **Template flag.** `get_cluster_vm_index()` entries gain a `template` key, but only when the cluster row reports one. Cost: one extra key.
- **Guest status.** `status()` returns `running` for a readable guest and `missing` for an absent one, which preserves today's `/list` and the two-sighting reconcile. Cost: `/list` does not show the real stopped state, the same as today.
- **Admin bulk destroy after a stop error.** `BEST_EFFORT` confirms absence after a stop error and skips the DELETE when the guest is confirmed gone. This now applies to admin bulk destroy as well as failed-deploy cleanup. Cost: admin bulk reports "already gone" instead of a delete error.
- **Connections.** There is no process-wide Proxmox HTTP client; each lifecycle operation opens its own connection at the granularity the original code used. An idle password-auth ticket cannot renew, and a shared session is not synchronized. Cost: one login per operation, as today.
- **Outage at the start of an expiry pass.** If the Proxmox connection fails, Proxmox records are skipped with no retry count and no alert, as before. Cloud records and alerts continue. Cost: a Proxmox outage delays expiry, as before.
- **Behaviour now taken from the record.** The admin `/api/vms/<vmid>` endpoint returns 404 when no record exists. Status comes from `backend.status()`. Extend commits to the database first, then updates the provider best-effort.
- **Display extras.** The diagnostic text and uptime shown in the UI are optional hooks outside the `VMBackend` contract.
- **Optional parameters.** `create_vm(create_options=)` and `ProxmoxBackend.create(clone_semaphores=, on_claim=)` are optional. Every fixed call form is unchanged.
- **`/list` probes.** `/list` makes one existence probe per guest and uses cached IPs when a guest is unknown, the same probe counts as `bd75a88`.

## Cloud safety

- **Delete paths re-verify ownership.** Every cloud delete path does a fresh describe or GET first: site, kind, vmid/deployment/slot, network placement and VPC/network. A mismatch raises `DestroyRefused`; unverifiable identity raises `BackendError`. This overrules the plan's direct termination. Cost: one extra read per deletion.
- **Placement parity.** AWS checks VPC and slot subnet on instance mutations, matching GCP's lab network and slot subnetwork check. An ambiguous slot fails closed.
- **Multi-step AWS operations** re-verify ownership before every security-group mutation and after every router transitional wait.
- **Routers must be tagged.** The router must carry `lab-playground-site=<site>` and `lab-playground-kind=router`, or the portal refuses to start or stop it. Cost: the operator tags it once.
- **GCP absence and location.** These come from project-wide `aggregatedList` discovery by exact name. A single-zone 404 is never treated as absence, and an unreachable zone means UNKNOWN. Cost: one aggregated list per identity check.
- **GCP insert request ids.** The `requestId` is `uuid5(deployment_id, vmid, attempt)`. The attempt number advances only after a terminal operation failure plus confirmed absence, which overrules the plan's fixed id. Delete, subnetwork, firewall and router calls use random request ids.
- **AMI data disks.** Inherited EBS mappings are copied field by field with `DeleteOnTermination=True`. `Encrypted=False` is omitted so the account's encryption-by-default applies.
- **Admission lease renewal.** The admission lease is renewed from admission through the VMRecord commit. A lease lost during cloud success staging never aborts the commit of launched VMs; after the commit the router is re-ensured under the router lock. Cost: a brief window with the router stopped, which is then undone.
- **Owners without VLAN tags.** A cloud deploy refuses an owner without entry and internal VLAN tags, because slots are the tags.

## VPN and operations

- **One Headscale with a per-student policy,** not one VPN per backend.
- **KOTH reachability.** The networks of any KOTH game that has not ended are granted to every roster login. Base-policy rules are kept except the exact blanket `autogroup:member` rule. The jumpbox `--allow-vlans` flag exempts KOTH ranges. Cost: KOTH players need a policy re-run or the flag.
- **Policy export** runs `create_app(start_background_tasks=False)` and uses `docker exec` by container name. The apply sends SIGHUP before the read-back; that Headscale reloads its policy file on SIGHUP is unverified live.
- **Compose.** `docker-compose.prod.yml` passes the new non-secret settings through, with an empty value tested to behave as unset. Credentials come only from an operator-owned override.
- **GCP IAM.** The GCP service account needs `compute.securityAdmin` for deployment firewalls, because `networkAdmin` cannot manage firewall rules.
