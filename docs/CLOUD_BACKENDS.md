# Cloud backends: AWS and Google Cloud

The portal can place labs on Proxmox, on Amazon EC2, and on Google Compute
Engine. `ENABLED_BACKENDS` (default `proxmox`) selects which; a deployment
group's `backend:` selects where each lab goes. This runbook is for the
operator who adds a cloud: what the portal creates and deletes, the base
infrastructure you create once by hand, the settings, the Headscale policy and
jumpbox isolation that go with it, and how to take it all down again.

Every value in this file is a placeholder: `<aws-region>`, `<aws-vpc-id>`,
`<gcp-project>`, `<site-id>` and so on. Real account, project, VPC, image and
router identifiers belong only in your untracked `.env` and `templates.yaml`
(repository rule 13 in `CLAUDE.md`).

The cloud base infrastructure (VPC, router subnet, router instance, route,
firewall rules, IAM) is created by the operator from this document, not by
Terraform. Everything per deployment (instances, slot subnets, security groups
and firewall rules) is created and deleted by the portal and scales to zero.

## Safety model and ownership

**`LAB_SITE_ID` is the ownership boundary.** Every cloud resource the portal
creates carries it, and every inventory, reuse and delete is filtered by it:

| Resource | AWS identity | GCP identity |
|---|---|---|
| Lab instance | tags `lab-playground-site`, `-vmid`, `-deployment`, `-expires`, `-slot`, `-kind=instance`; `Name` is the portal VM name | name `lp-<site-id>-<vmid>` plus the same labels; the portal VM name is in `description` |
| Root disk | EBS volume tagged like its instance (no expiry), `kind=instance-volume`, `DeleteOnTermination` | boot disk with `autoDelete: true` |
| Slot network | subnet tagged `site`, `slot`, `kind=slot-network`, `lab-playground-created`; `Name` `lp-<site-id>-s-<slot>` | subnetwork named exactly `lp-<site-id>-s-<slot>` in `GCP_NETWORK` |
| Deployment network | security group `lp-<site-id>-d-<key>` tagged `site`, `deployment`, `kind=deployment-network`, `created` | firewall rule named exactly `lp-<site-id>-d-<key>` whose source and target tags are that name |
| Router (operator-made) | tags `lab-playground-site=<site-id>`, `lab-playground-kind=router` | labels `lab-playground-site=<site-id>`, `lab-playground-kind=router` |

`<key>` is the first 16 characters of the deployment id. GCP networks carry no
labels, so their names are their identity; anything that only shares the
`lp-<site-id>-` prefix is never listed or deleted.

Rules the code keeps, and you must keep too:

- **Identity is re-read before every action.** A destroy, an expiry update
  and every sweeper delete first read the resource again and act only if
  every identity tag or label still matches. A changed identity is refused
  (`DestroyRefused`) and nothing is mutated.
- **Absence needs positive evidence.** Only `InvalidInstanceID.NotFound`, a
  `terminated` instance, or (GCP) a complete project-wide aggregated list
  without the name counts as gone. Any other failure is "unknown", and an
  unknown never retires a `vm_records` row whose machine may still bill.
- **Routers must be tagged (AWS) or labelled (GCP)
  `lab-playground-site=<LAB_SITE_ID>` and `lab-playground-kind=router`.**
  The configured id or name alone never proves which machine it is: an
  untagged or foreign router makes every router start and stop fail, so every
  cloud deploy fails before anything is launched.
- **No public lab address, no NAT, no internet.** Lab instances launch with
  `AssociatePublicIpAddress: false` (AWS; slot subnets also get
  `MapPublicIpOnLaunch=false`) or no `accessConfigs` (GCP). There is no NAT
  gateway or Cloud NAT, and the slot subnets have no internet route. A lab
  image must therefore be complete: nothing can be downloaded at boot or
  during the lab.
- **Caps and expiry bound the bill.** `AWS_MAX_VMS` / `GCP_MAX_VMS` are
  required when the backend is enabled; admission counts live records plus
  unexpired reservations in one locked transaction and refuses past the cap.
  Every machine carries its expiry and is destroyed by the expiry loop like a
  Proxmox guest.
- **Leftovers are swept conservatively.** After the expiry pass, the orphan
  reaper lists each enabled cloud's site inventory and deletes an instance
  only when no record or create reservation names it and its expiry plus
  `ORPHAN_REAPER_GRACE_MINUTES` (default 60) has passed, and a network only
  when nothing uses it and its creation time plus that grace has passed.
  `ORPHAN_REAPER_DRY_RUN=true` logs each decision (`would delete …`) without
  deleting. The Proxmox and cloud passes share one budget
  (`ORPHAN_REAPER_MAX_PASS_SECONDS`, `ORPHAN_REAPER_MAX_PASS_GUESTS`).
- **Never share a site id.** Two portals with the same `LAB_SITE_ID` would
  each sweep the other's resources. The live contract runs use their own
  `live-<name>` site, which no portal may use, and refuse to start unless that
  site's inventory is empty.

## Shared address plan

Every student and every group owns two VLAN tags, an entry tag and an
internal tag (`app/vlan_allocator.py`). On every backend a tag is the identity
of a **network slot**. A cloud deployment is refused for an owner that lacks
either tag, exactly as on Proxmox. The entry-point machine goes in the
owner's entry slot and every other machine in the owner's internal slot; a
group deployment uses the group's two slots.

A cloud slot *N* (0–500) is the `/26` at `base + N × 64`
(`app/backends/network_slots.py`):

| Backend | Base | Slot 101 | Slot 400 | Slot 500 (highest) |
|---|---|---|---|---|
| Proxmox | `JUMPBOX_VLAN_BANDS` | `10.101.0.0/24` | `10.100.4.0/24` | `10.200.4.0/24` |
| AWS | `AWS_LAB_CIDR`, default `172.20.0.0/16` | `172.20.25.64/26` | `172.20.100.0/26` | `172.20.125.0/26` |
| GCP | `GCP_LAB_CIDR`, default `172.21.0.0/16` | `172.21.25.64/26` | `172.21.100.0/26` | `172.21.125.0/26` |

```
AWS_LAB_CIDR 172.20.0.0/16 (inside the lab VPC's CIDR)
172.20.0.0 ─────────────────────────── 172.20.125.63   172.20.255.240/28
│ slot 0 │ slot 1 │ … │ slot 101 │ … │ slot 500 │  …  │ router subnet  │
│  /26   │  /26   │   │172.20.25.64│ │172.20.125.0│   │ (example, kept │
└────────┴────────┴───┴──────────┴───┴──────────┘     │ clear of slots)│
   created on first use, deleted when unused          └────────────────┘
```

- **Forbidden overlaps.** Startup (and `scripts/preflight_env.py`) refuses a
  base that overlaps `10.0.0.0/8` (Proxmox labs), `100.64.0.0/10` (the
  tailnet), `172.17.0.0/16` (Docker's default bridge, present on the jumpbox)
  or the other enabled cloud's base, and a base smaller than a `/17` (slots
  0–500 need 32,064 addresses).
- **One shared VPC per cloud.** A VPC costs nothing on either cloud. All
  students share it; isolation comes from the per-deployment security group
  or firewall rule and the Headscale policy, not from separate VPCs.
- **Deployment networks.** A deployment's first launch creates its security
  group (AWS: all traffic to and from the group itself and `100.64.0.0/10`,
  both directions, nothing else; the default allow-all egress is revoked) or
  firewall rule (GCP: ingress from instances with that deployment's network
  tag only). It is deleted after the deployment's last record retires.
- **Slot networks.** A slot's subnet or subnetwork is created by the first
  deployment that needs it and deleted once no live record and no unexpired
  admission on that backend uses it. One the lifecycle leaves behind is
  reaped by the sweeper.

## AWS one-time setup

Create these once, in `<aws-region>`. None of them carries a
`lab-playground-kind` other than the router's `router`, so the portal never
lists or deletes them.

1. **VPC** with a CIDR that contains `AWS_LAB_CIDR` (the default base needs
   the whole `172.20.0.0/16`):

   ```bash
   aws ec2 create-vpc --region <aws-region> --cidr-block 172.20.0.0/16 \
     --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=<site-id>-lab}]'
   ```

2. **Public router subnet**, inside the VPC but outside the slot range
   (`172.20.0.0`–`172.20.125.63` for the default base), in
   `<aws-availability-zone>`, with its own route table to an internet
   gateway. The private slot subnets are created later by the portal and use
   the VPC's **main** route table, which must never get a `0.0.0.0/0` route:

   ```bash
   aws ec2 create-subnet --region <aws-region> --vpc-id <aws-vpc-id> \
     --availability-zone <aws-availability-zone> --cidr-block 172.20.255.240/28
   aws ec2 modify-subnet-attribute --region <aws-region> \
     --subnet-id <router-subnet-id> --map-public-ip-on-launch
   aws ec2 create-internet-gateway --region <aws-region>
   aws ec2 attach-internet-gateway --region <aws-region> \
     --internet-gateway-id <igw-id> --vpc-id <aws-vpc-id>
   aws ec2 create-route-table --region <aws-region> --vpc-id <aws-vpc-id>
   aws ec2 create-route --region <aws-region> --route-table-id <router-route-table-id> \
     --destination-cidr-block 0.0.0.0/0 --gateway-id <igw-id>
   aws ec2 associate-route-table --region <aws-region> \
     --route-table-id <router-route-table-id> --subnet-id <router-subnet-id>
   ```

   **No NAT gateway.** It would cost about $33/month standing and give the
   labs an internet route they must not have.

3. **Router security group**: SSH (`tcp/22`) only from the staff addresses,
   and all traffic from `AWS_LAB_CIDR` (lab replies and reverse shells it
   forwards to the tailnet). Keep the default egress, which Tailscale needs
   to reach Headscale:

   ```bash
   aws ec2 create-security-group --region <aws-region> --vpc-id <aws-vpc-id> \
     --group-name <site-id>-router --description "lab subnet router"
   aws ec2 authorize-security-group-ingress --region <aws-region> --group-id <router-sg-id> \
     --protocol tcp --port 22 --cidr <staff-cidr>
   aws ec2 authorize-security-group-ingress --region <aws-region> --group-id <router-sg-id> \
     --protocol -1 --cidr 172.20.0.0/16
   ```

4. **Router instance**, a `t4g.nano` (arm64, so an arm64 image) in the router
   subnet with an ephemeral public IPv4, tagged for this site, with the
   source/destination check off so it may forward:

   ```bash
   aws ec2 run-instances --region <aws-region> --image-id <arm64-linux-ami-id> \
     --instance-type t4g.nano --subnet-id <router-subnet-id> \
     --security-group-ids <router-sg-id> --key-name <staff-key-pair> \
     --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=<site-id>-router},{Key=lab-playground-site,Value=<site-id>},{Key=lab-playground-kind,Value=router}]'
   aws ec2 modify-instance-attribute --region <aws-region> \
     --instance-id <router-instance-id> --no-source-dest-check
   ```

   Inside it, enable forwarding (`net.ipv4.ip_forward=1` in
   `/etc/sysctl.d/`) and join Headscale (see
   [Router Tailscale flags](#router-tailscale-flags)). The portal starts and
   stops this instance; never terminate it.

5. **Return route.** Lab machines see students' tailnet addresses (no SNAT),
   so the main route table sends `100.64.0.0/10` to the router's network
   interface:

   ```bash
   aws ec2 describe-route-tables --region <aws-region> \
     --filters Name=vpc-id,Values=<aws-vpc-id> Name=association.main,Values=true
   aws ec2 create-route --region <aws-region> --route-table-id <main-route-table-id> \
     --destination-cidr-block 100.64.0.0/10 --network-interface-id <router-eni-id>
   ```

6. **Portal IAM policy.** Give the portal an identity with exactly this
   policy. Replace every placeholder: `<aws-region>`, `<aws-account-id>`,
   `<aws-vpc-id>`, `<router-instance-id>` and `<site-id>` (your
   `LAB_SITE_ID`). Launch and tag permissions require the site tag on the
   request; terminate, delete and security-group rule changes require it on
   the resource; router start and stop name the one router. `Resource: "*"`
   appears only on the read-only `Describe*` actions, which EC2 does not
   scope to resources.

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ReadInventory",
         "Effect": "Allow",
         "Action": [
           "ec2:DescribeInstances",
           "ec2:DescribeImages",
           "ec2:DescribeSubnets",
           "ec2:DescribeSecurityGroups"
         ],
         "Resource": "*"
       },
       {
         "Sid": "LaunchSiteTaggedInstances",
         "Effect": "Allow",
         "Action": "ec2:RunInstances",
         "Resource": [
           "arn:aws:ec2:<aws-region>:<aws-account-id>:instance/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:volume/*"
         ],
         "Condition": {
           "StringEquals": {"aws:RequestTag/lab-playground-site": "<site-id>"}
         }
       },
       {
         "Sid": "LaunchIntoTheLabVpc",
         "Effect": "Allow",
         "Action": "ec2:RunInstances",
         "Resource": [
           "arn:aws:ec2:<aws-region>:<aws-account-id>:subnet/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:security-group/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:network-interface/*"
         ],
         "Condition": {
           "ArnEquals": {"ec2:Vpc": "arn:aws:ec2:<aws-region>:<aws-account-id>:vpc/<aws-vpc-id>"}
         }
       },
       {
         "Sid": "LaunchFromImages",
         "Effect": "Allow",
         "Action": "ec2:RunInstances",
         "Resource": "arn:aws:ec2:<aws-region>::image/*"
       },
       {
         "Sid": "CreateSiteTaggedNetworks",
         "Effect": "Allow",
         "Action": ["ec2:CreateSubnet", "ec2:CreateSecurityGroup"],
         "Resource": [
           "arn:aws:ec2:<aws-region>:<aws-account-id>:subnet/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:security-group/*"
         ],
         "Condition": {
           "StringEquals": {"aws:RequestTag/lab-playground-site": "<site-id>"}
         }
       },
       {
         "Sid": "CreateNetworksInTheLabVpc",
         "Effect": "Allow",
         "Action": ["ec2:CreateSubnet", "ec2:CreateSecurityGroup"],
         "Resource": "arn:aws:ec2:<aws-region>:<aws-account-id>:vpc/<aws-vpc-id>"
       },
       {
         "Sid": "TagOnlyAtCreation",
         "Effect": "Allow",
         "Action": "ec2:CreateTags",
         "Resource": [
           "arn:aws:ec2:<aws-region>:<aws-account-id>:instance/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:volume/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:subnet/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:security-group/*"
         ],
         "Condition": {
           "StringEquals": {
             "ec2:CreateAction": ["RunInstances", "CreateSubnet", "CreateSecurityGroup"],
             "aws:RequestTag/lab-playground-site": "<site-id>"
           },
           "ForAllValues:StringEquals": {
             "aws:TagKeys": [
               "Name", "lab-playground-site", "lab-playground-vmid",
               "lab-playground-deployment", "lab-playground-expires",
               "lab-playground-slot", "lab-playground-kind", "lab-playground-created"
             ]
           }
         }
       },
       {
         "Sid": "ExtendSiteInstances",
         "Effect": "Allow",
         "Action": "ec2:CreateTags",
         "Resource": "arn:aws:ec2:<aws-region>:<aws-account-id>:instance/*",
         "Condition": {
           "StringEquals": {"ec2:ResourceTag/lab-playground-site": "<site-id>"},
           "ForAllValues:StringEquals": {"aws:TagKeys": ["lab-playground-expires"]}
         }
       },
       {
         "Sid": "ManageSiteNetworks",
         "Effect": "Allow",
         "Action": [
           "ec2:ModifySubnetAttribute",
           "ec2:AuthorizeSecurityGroupIngress",
           "ec2:AuthorizeSecurityGroupEgress",
           "ec2:RevokeSecurityGroupIngress",
           "ec2:RevokeSecurityGroupEgress",
           "ec2:DeleteSecurityGroup",
           "ec2:DeleteSubnet"
         ],
         "Resource": [
           "arn:aws:ec2:<aws-region>:<aws-account-id>:subnet/*",
           "arn:aws:ec2:<aws-region>:<aws-account-id>:security-group/*"
         ],
         "Condition": {
           "StringEquals": {"ec2:ResourceTag/lab-playground-site": "<site-id>"}
         }
       },
       {
         "Sid": "TerminateSiteInstances",
         "Effect": "Allow",
         "Action": "ec2:TerminateInstances",
         "Resource": "arn:aws:ec2:<aws-region>:<aws-account-id>:instance/*",
         "Condition": {
           "StringEquals": {"ec2:ResourceTag/lab-playground-site": "<site-id>"}
         }
       },
       {
         "Sid": "StartStopTheRouter",
         "Effect": "Allow",
         "Action": ["ec2:StartInstances", "ec2:StopInstances"],
         "Resource": ["arn:aws:ec2:<aws-region>:<aws-account-id>:instance/<router-instance-id>"]
       }
     ]
   }
   ```

   The router carries the site tag too, so `TerminateSiteInstances` could
   terminate it; the portal never does (it only lists `kind=instance`). If
   you want IAM to forbid that as well, add a `Deny` on
   `ec2:TerminateInstances` for the router's ARN.

   Test the policy before the class with the
   [live contract run](#live-contract-runs) and with the portal in sweeper
   dry run (`ORPHAN_REAPER_DRY_RUN=true`): a missing permission surfaces as a
   `BackendError` naming the refused EC2 call in the portal log, and the
   record or resource is kept, never retired.

## Google Cloud one-time setup

Create these once in `<gcp-project>`. Name your own resources without the
`lp-<site-id>-` prefix: the portal owns names of the form
`lp-<site-id>-s-<slot>` and `lp-<site-id>-d-<16 hex>`.

1. **Custom-mode VPC** and a **public router subnetwork** outside the slot
   range (`172.21.0.0`–`172.21.125.63` for the default base). The portal
   creates the private slot subnetworks in `GCP_REGION`, with Private Google
   Access off:

   ```bash
   gcloud compute networks create <gcp-network> --project <gcp-project> --subnet-mode=custom
   gcloud compute networks subnets create <site-id>-router --project <gcp-project> \
     --network <gcp-network> --region <gcp-region> --range 172.21.255.240/28
   ```

2. **Router instance**: an `e2-micro` with `canIpForward`, an ephemeral
   external IP (the default), the network tag `<site-id>-router`, and this
   site's labels:

   ```bash
   gcloud compute instances create <gcp-router-name> --project <gcp-project> \
     --zone <gcp-zone> --machine-type e2-micro --subnet <site-id>-router \
     --can-ip-forward --tags <site-id>-router \
     --labels lab-playground-site=<site-id>,lab-playground-kind=router \
     --image-family <linux-image-family> --image-project <linux-image-project>
   ```

   Enable forwarding inside it (`net.ipv4.ip_forward=1`) and join Headscale
   (next section). The portal starts and stops it; never delete it.

3. **Firewall rules.** A custom-mode VPC denies all ingress by default.
   Add three operator rules; the portal adds one rule per live deployment
   and never touches these:

   ```bash
   # Staff-only SSH to the router
   gcloud compute firewall-rules create <site-id>-router-ssh --project <gcp-project> \
     --network <gcp-network> --direction INGRESS --allow tcp:22 \
     --source-ranges <staff-cidr> --target-tags <site-id>-router
   # Lab replies and reverse shells reach the router to be forwarded
   gcloud compute firewall-rules create <site-id>-router-from-labs --project <gcp-project> \
     --network <gcp-network> --direction INGRESS --allow all \
     --source-ranges 172.21.0.0/16 --target-tags <site-id>-router
   # The static tailnet rule: students' (policy-narrowed) traffic reaches lab VMs.
   # Every lab VM carries the network tag lp-<site-id>-lab.
   gcloud compute firewall-rules create <site-id>-lab-tailnet --project <gcp-project> \
     --network <gcp-network> --direction INGRESS --allow all \
     --source-ranges 100.64.0.0/10 --target-tags lp-<site-id>-lab
   ```

4. **Return route** for the tailnet range through the router, for lab VMs
   only:

   ```bash
   gcloud compute routes create <site-id>-tailnet-return --project <gcp-project> \
     --network <gcp-network> --destination-range 100.64.0.0/10 \
     --next-hop-instance <gcp-router-name> --next-hop-instance-zone <gcp-zone> \
     --tags lp-<site-id>-lab
   ```

   **No Cloud NAT.** Lab VMs have no external IP, so without NAT they have
   no internet route.

5. **Portal service account.** Grant it, on the project,
   `roles/compute.instanceAdmin.v1`, `roles/compute.networkAdmin` and
   `roles/compute.securityAdmin`, or one custom role with the same
   instance, subnetwork, firewall and operation permissions.
   `networkAdmin` alone is not enough: it can read firewall rules but not
   create or delete them, and the portal creates and deletes one per
   deployment. The portal attaches no service account to lab VMs, so
   `roles/iam.serviceAccountUser` is not needed; grant it only on one
   dedicated VM service account, and only if you change that. If your images
   live in another project, grant `roles/compute.imageUser` there. **Never
   grant Owner or Editor**: they reach every resource in the project,
   including the operator's base network and the router.

## Router Tailscale flags

Each cloud router joins the same Headscale as the jumpbox, with
`tag:jumpbox`, and advertises its whole cloud base; the per-student policy
narrows what each student may use:

```bash
# AWS router (AWS_LAB_CIDR)
sudo tailscale up --login-server=<headscale-url> --advertise-routes=172.20.0.0/16 \
  --advertise-tags=tag:jumpbox --snat-subnet-routes=false --stateful-filtering=false
# GCP router (GCP_LAB_CIDR)
sudo tailscale up --login-server=<headscale-url> --advertise-routes=172.21.0.0/16 \
  --advertise-tags=tag:jumpbox --snat-subnet-routes=false --stateful-filtering=false
```

Use a short-lived pre-auth key for a user that `tagOwners` lets own
`tag:jumpbox`, as for the jumpbox (`docs/JUMPBOX.md`, step 3). Then approve
the route on the Headscale host:

```bash
headscale nodes list-routes
headscale nodes approve-routes --identifier <router-node-id> --routes <cloud-cidr>
```

or add `"autoApprovers": {"routes": {"<cloud-cidr>": ["tag:jumpbox"]}}` to
the base policy.

`--snat-subnet-routes=false` lets lab machines see each student's tailnet
address, which reverse shells need. That only works because the reply to
`100.64.0.0/10` comes back through the router: the main route table entry
and the disabled source/destination check on AWS, the `canIpForward` flag
and the tagged route on GCP, and IP forwarding inside the router on both. If
any of them is missing, students can open a connection but never see a reply.

The router's public IP is ephemeral on both clouds and is released when the
portal stops it, so a stopped router costs only its disk. Tailscale only
dials out, so a new address on the next start needs no change.

## Capacity and quota checks

- **The caps are mandatory.** Set `AWS_MAX_VMS` / `GCP_MAX_VMS` to the number
  of lab machines the class may run at once on that cloud; a missing or
  non-positive cap refuses to start. A machine past the cap is refused at
  admission, before its router start, slot network or launch.
- **Subnets per VPC.** Each owner with labs running uses up to two slots
  (entry and internal), and each slot is one subnet while it is in use. AWS's
  default limit of 200 subnets per VPC is therefore roughly 100 students and
  groups with labs running at once. Request an increase for larger classes.
- **Request quotas before the first class.** New accounts get a few vCPUs.
  On AWS request the regional On-Demand vCPU quota for the instance family
  you use (a `t3.medium` is 2 vCPUs, so 90 machines need 180 plus the
  router), and check running instances, subnets per VPC and network
  interfaces. On GCP check, in `GCP_REGION`, the CPU quota (and the E2 CPU
  quota for `e2-*` types), instances, in-use IP addresses, and per project the
  subnetwork, route and firewall rule quotas: the portal holds one
  subnetwork per slot in use and one firewall rule per live deployment.
- **Rehearse with one student.** Deploy one group as a test student, reach
  the machine over the VPN, destroy it, and confirm the inventory is empty
  (see [Configure and validate the portal](#configure-and-validate-the-portal))
  before the class starts.

## Cost model

On-demand US list prices, verified 2026-11-04 for IPv4 and NAT; the instance
prices are list prices. **Re-check every price for your region and date
before relying on them.**

| Item | While a session runs | Between sessions |
|---|---|---|
| VPC, subnets, security groups, firewall rules, routes | free | free |
| Router, AWS `t4g.nano` plus public IPv4 | about $0.009/h | 8 GB gp3 disk, about $0.64/month |
| Router, GCP `e2-micro` plus external IPv4 | about $0.013/h (e2-micro is free-tier eligible in some US regions) | 10 GB disk, about $0.40/month |
| NAT gateway | **not used** (AWS would be $0.045/h plus $0.045/GB, about $33/month standing) | none |
| Lab machine, e.g. AWS `t3.medium` | about $0.042/h each | none (deleted) |
| Lab machine, e.g. GCP `e2-medium` | about $0.034/h each | none (deleted) |
| Lab disk, 30 GB | about $0.003/h each | none (deleted with the instance) |

Worked example: 90 students with one `t3.medium` machine each, for a 4-hour
session, costs about 90 × 4 × ($0.042 + $0.003) ≈ **$16** on AWS, or about
**$13** on GCP with `e2-medium`, plus a few cents for the router. Windows images
on AWS add the Windows license to the hourly rate.

The router stops after `<BACKEND>_ROUTER_IDLE_MINUTES` (default 30) with no
live record and no unexpired admission on that backend, so between sessions
only the router disks bill.

## Configure and validate the portal

1. **Migrate before the new code starts.** Back up the database and run
   every migration in "Redeploy an Existing Production Host"
   (`docs/PRODUCTION_DEPLOYMENT.md`), which includes
   `migrate_add_vm_backend.py`. It adds `backend`, `provider_id`,
   `deployment_id` and `network_slot` to `vm_records`; existing rows become
   `backend='proxmox'`.

2. **Fill the cloud section of `.env`** from `.env.example`:
   `ENABLED_BACKENDS` (for example `proxmox,aws`), `LAB_SITE_ID`, and the
   enabled cloud's `AWS_*` or `GCP_*` settings, including the cap.
   `docker-compose.prod.yml` passes each of these names into the container;
   an empty value means the code's default, exactly as if it were unset.
   Set `JUMPBOX_VLAN_BANDS` to the jumpbox's value if you changed it there.

3. **Credentials, never in a tracked file.** AWS uses boto3's standard chain
   and GCP uses Application Default Credentials. On a portal host that runs
   in the same cloud, an instance role (AWS; set the instance metadata hop
   limit to 2 so a container can reach it) or the attached service account
   (GCP metadata server) needs nothing else. Otherwise create an
   operator-owned override next to `docker-compose.prod.yml`, keep it out of
   git, and keep the key values in `.env`:

   ```yaml
   # docker-compose.cloud.yml (operator-owned, untracked)
   services:
     vm-automation:
       environment:
         - AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:?Set AWS_ACCESS_KEY_ID in .env}
         - AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:?Set AWS_SECRET_ACCESS_KEY in .env}
         - GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-portal.json
       volumes:
         - type: bind
           source: /etc/lab-playground/gcp-portal.json   # owner uid 1000, mode 0400
           target: /run/secrets/gcp-portal.json
           read_only: true
   ```

   Name the override in `.env`, the way "Running a released image"
   (`docs/PRODUCTION_DEPLOYMENT.md`) names its own, so a plain
   `docker compose` (no `-f`) uses it for every command:

   ```bash
   COMPOSE_FILE=docker-compose.prod.yml:docker-compose.cloud.yml
   # with a release image: docker-compose.prod.yml:docker-compose.release.yml:docker-compose.cloud.yml
   ```

   **Then never pass `-f docker-compose.prod.yml` alone to `up`**: an explicit
   `-f` replaces `COMPOSE_FILE`, and the app is recreated without
   credentials. Every cloud call then fails with `BackendError`; records are
   kept and cloud machines keep running (and billing) until the next `up`
   with the override. `exec` and `logs` never recreate a container and are
   safe.

4. **Add backend blocks to `templates.yaml`.** `templates.example.yaml`'s
   `multi-backend-example` group shows a machine with Proxmox, `aws:` and
   `gcp:` specs. Set `backend: aws` or `backend: gcp` on a group only after
   that backend is enabled; every machine of one group must resolve to the
   same backend, and `koth_mode` groups stay on Proxmox.

5. **Preflight, then start.**

   ```bash
   python3 scripts/preflight_env.py .env
   docker compose up -d --build                              # COMPOSE_FILE set (override)
   docker compose -f docker-compose.prod.yml up -d --build   # no override: instance role / metadata server
   ```

   Startup refuses missing settings of an enabled backend, a missing or
   invalid `LAB_SITE_ID`, a non-positive cap and an overlapping or undersized
   lab CIDR. The admin template validation reports groups whose backend is
   not enabled or whose cloud block is incomplete.

6. **Verify with one VM.** Turn on `ORPHAN_REAPER_DRY_RUN=true` for the first
   session. As a test student, deploy one cloud group: the first deployment
   of a session starts the router first (allow roughly a minute). Check that
   `/list` shows a private address inside the slot's `/26`, that you reach it
   over the VPN, and that the router is running. Destroy it, then check the
   inventory below is empty apart from the router, and that the router stops
   after the idle minutes. Read the reaper's `would delete` lines before you
   turn dry run off.

Two accepted limits:

- **A Proxmox node may not be named `aws` or `gcp`.** Cloud admission
  reservations use those names as their node key, so a Proxmox node of that
  name would share (and lose) capacity. Startup refuses such a `FIXED_NODE`,
  and the admin template validation reports such a
  `load_balancing.fixed_node` or `template_locations`/`storage_locations`
  key.
- **With Proxmox enabled, a Proxmox outage also blocks cloud deploys.** VMID
  allocation still reads the Proxmox inventory so that it never hands out a
  VMID someone occupied by hand (accepted trade-off, ruling M20). A
  cloud-only portal does not read Proxmox at all.

**Do not change a placement setting while the backend holds anything.** The
portal identifies its networks by placement as well as by tag, so changing
one strands what was created under the old value:

| Changed | Effect on what exists |
|---|---|
| `AWS_LAB_VPC_ID` | Old subnets and security groups are still listed by site tag, but every delete is refused (wrong VPC), and a slot whose old subnet exists cannot be used. |
| `GCP_NETWORK` | Old subnetworks and firewall rules are no longer listed or deleted; old instances are refused (wrong network), so their records cannot be destroyed. |
| `GCP_REGION` | Old subnetworks are no longer released by the lifecycle, and the same slot cannot be created in the new region while its old range exists; the sweeper removes unused old ones after the grace period. |
| `AWS_REGION`, `GCP_PROJECT`, `LAB_SITE_ID` | Everything created before becomes invisible to the portal. |
| `AWS_AVAILABILITY_ZONE`, `AWS_LAB_CIDR`, `GCP_LAB_CIDR` | A slot whose old subnet exists cannot be used until that subnet is released. |

Change them only after [teardown](#teardown-and-incident-response) has left
the backend with no records and an empty inventory. If you already changed
one, clean up by hand with the old value, as in that section: list by the
site tag (AWS) or the `lp-<site-id>-` name (GCP), check each resource's
VPC/network and region against the old value, and delete instances first,
then security groups or firewall rules, then subnets.

### Live contract runs

`tests/live/` drives the real backends against a real account
(`tests/live/README.md` has the full contract). Run it once per account and
after any provider change, from a checkout without a production `.env`:

```bash
export LIVE_AWS=1 ENABLED_BACKENDS=aws LAB_SITE_ID=live-contract AWS_MAX_VMS=1 \
       AWS_REGION=<aws-region> AWS_AVAILABILITY_ZONE=<aws-availability-zone> \
       AWS_LAB_VPC_ID=<aws-vpc-id> AWS_LAB_CIDR=172.20.0.0/16 \
       AWS_ROUTER_INSTANCE_ID=<live-router-instance-id> \
       LIVE_AWS_IMAGE_ID=<aws-ami-id> LIVE_AWS_INSTANCE_TYPE=<small-instance-type>
uv run python -m pytest tests/live/test_aws_contract.py -v -rs
# GCP: LIVE_GCP=1 ENABLED_BACKENDS=gcp GCP_MAX_VMS=1 plus the GCP_* settings,
#      LIVE_GCP_IMAGE and LIVE_GCP_MACHINE_TYPE; tests/live/test_gcp_contract.py
```

- **A dedicated live site.** `LAB_SITE_ID` must be `live-<name>` and used by
  no portal, with its own router tagged or labelled for that live site. The
  run refuses to start unless the site's inventory is empty, and caps itself
  at one machine with a 30-minute expiry.
- **What it verifies:** one full lifecycle per cloud (admission at a cap of
  one, router, slot network, launch, identity, status, private addresses
  inside the slot, expiry update, refusal at the cap, teardown and an empty
  inventory), plus the provider behaviours the unit fakes assume, such as
  `ClientToken` idempotency and `DependencyViolation` on AWS, and operation
  waits, aggregated-list warnings and `requestId` replay on GCP.
- **What a skip means.** Without its flag and every variable, a provider's
  tests are skipped before any client is built. A check whose race the run
  could not reproduce (for example the router already past `pending`) is
  reported SKIPPED with the reason; it is unproven, not passed.
- Teardown always runs and must leave the live site's inventory empty;
  otherwise it fails with the cleanup commands filled in.

## Apply the Headscale policy

Adding a cloud widens nothing by itself: the Headscale policy grants each
student only their own networks on every backend. Applying it to the live
Headscale host is an instructor step, not part of a deployment.

`scripts/apply_headscale_policy.sh` reads `scripts/site.env`
(`PORTAL_HOST`, `PORTAL_APP_CONTAINER`, `HEADSCALE_HOST`, `HEADSCALE_DIR`;
optional `HEADSCALE_BASE_POLICY`, default `config/headscale-acl.json`, and
`HEADSCALE_POLICY_FILE`, default `/etc/headscale/acl.json`):

```bash
scripts/apply_headscale_policy.sh            # dry run: prints the diff, writes nothing
scripts/apply_headscale_policy.sh --apply
```

Both modes export the roster, groups, VLAN pairs, non-ended KOTH games,
enabled backends, lab CIDRs and `JUMPBOX_VLAN_BANDS` from the running portal
(`docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py`),
render the policy locally (`scripts/headscale_policy.py`), and diff it
against `headscale policy get`. The export fails, and nothing is applied,
when a roster student or group lacks either VLAN tag or a group has a member
who is not on the roster. Review the diff. `--apply` then checks the policy
inside the Headscale container (`headscale policy check`), backs up the
current file as `<policy-file>.<YYYYmmddTHHMMSSZ>` (mode 0600), installs the
new one atomically, signals Headscale (`SIGHUP`) to reload it, and reads it
back; only if the read-back differs does it restart the headscale service
once, and it fails if it still differs.

What the policy grants:

- Each roster login `<login>@` reaches its own entry slot, its groups' entry
  and internal slots, and the entry and internal blocks of **every KOTH game
  whose status is not `ended`** (Proxmox only, since KOTH runs only there).
  Only slots inside `JUMPBOX_VLAN_BANDS` are granted, the same kinds on every
  backend. Those blocks may connect back to that login's own devices only.
- **The render keeps every section and every `acls` rule of the base file
  except the blanket rule whose `src` is exactly `["autogroup:member"]`.**
  Removing that rule removes everything it granted students:
  `autogroup:self`, `tag:jumpbox`, and any host or range it listed. Keep what
  must stay reachable as explicit rules in `config/headscale-acl.json`: the
  staff rule, and rules for infrastructure nodes, for example one letting
  the portal host's tailnet identity probe KOTH services.
- **Strict JSON only.** The base file and the live policy are parsed as JSON;
  a HuJSON policy (comments, trailing commas) makes the run fail closed
  before anything is installed.
- **The policy is a snapshot.** Re-run it after every roster import, group
  create, delete or membership change, VLAN reassignment, and KOTH game
  creation or ending, and at term changeover (`scripts/term_changeover.sh`
  runs it as its last stage). Until it is re-run, a new student or group
  member is refused, never let in. `term_changeover.sh`'s dry run renders
  the roster the portal holds before the reseed; its `--apply` renders again
  after it.

The export runs `create_app(start_background_tasks=False)`: no KOTH boot
recovery and no scorer start beside the live server. The other one-shot
tools that build the app (`scripts/init_db.py`, `cleanup_db.py`,
`import_students.py`, `migrate_add_vlan.py`) still run that startup recovery,
so running one beside the server can revert a KOTH game stuck in
`deploying` to `draft` and start a second scorer if a game is active.

**Roll back** on the Headscale host, from the backup the apply printed:

```bash
cd <headscale-dir>
cp -p <host-policy-file>.<stamp> <host-policy-file>.restore
docker compose exec -T headscale headscale policy check --file /dev/stdin < <host-policy-file>.restore
mv -f <host-policy-file>.restore <host-policy-file>
docker compose kill -s HUP headscale          # or: docker compose restart headscale
docker compose exec -T headscale headscale policy get
```

The last command must show the restored policy; a single-file bind mount can
need the restart before it does.

## Apply jumpbox VLAN isolation

The policy controls what a student's laptop reaches. Lab machines reaching
other students' labs through the Proxmox jumpbox is a separate path, closed
by `jumpbox_configure.sh --isolate-vlans` (`docs/JUMPBOX.md`, "VLAN
isolation"). It is an instructor step on the live jumpbox.

1. **Have console or recovery access** to the jumpbox first.
2. Copy the current `scripts/jumpbox_configure.sh`, `jumpbox_netgen.py`,
   `site_env.sh` and `site.env` to it, then dry-run and apply:

   ```bash
   sudo jumpbox/jumpbox_configure.sh --isolate-vlans                    # dry run
   sudo jumpbox/jumpbox_configure.sh --apply --no-try --isolate-vlans
   ```

   `--no-try` uses `netplan apply` for a netplan change instead of the
   interactive `netplan try`; on a converged jumpbox the run changes only the
   isolation table.
3. **Verify:** from a student device, the student's own lab still answers;
   from a lab VM, a lab VM on another VLAN does not, and
   `sudo nft list table inet jumpbox_vlan_isolation` shows the drop counter
   rising.

`--allow-vlans <list|range>` (for example `--allow-vlans 300-305`) exempts the
listed VLAN tags from the inter-VLAN drop in both directions: an exempt VLAN
may exchange traffic with every lab VLAN. Players reach KOTH boxes over the
tailnet, which isolation never touches, so set it to the KOTH VLAN range only
if KOTH boxes must reach each other across VLANs, or team VLANs must reach
the KOTH VLANs. Re-run with the new range when the KOTH tags change.

**Roll back:**

```bash
sudo systemctl disable --now jumpbox-vlan-isolation.service   # ExecStop deletes the table
sudo rm /etc/nftables.d/jumpbox-vlan-isolation.nft /etc/systemd/system/jumpbox-vlan-isolation.service
sudo systemctl daemon-reload
sudo nft list table inet jumpbox_vlan_isolation               # must fail: no such table
```

## Teardown and incident response

1. **Stop new creates.** Set `enabled: false` on the cloud groups in
   `templates.yaml` and restart. **Keep the backend in `ENABLED_BACKENDS`**
   until its inventory is empty: a record on a disabled backend is kept for
   retry but never destroyed, and the sweeper skips disabled backends, so its
   machines would run and bill unattended.
2. **Let expiry and the reaper run,** or destroy the machines from the admin
   page. Records are retired only on positive absence; deployment networks
   and slot subnets follow their last record.
3. **Query the site inventory** (instances by tag or label; GCP networks by
   the `lp-<site-id>-` name):

   ```bash
   # AWS
   aws ec2 describe-instances --region <aws-region> \
     --filters Name=tag:lab-playground-site,Values=<site-id> \
               Name=tag:lab-playground-kind,Values=instance \
               Name=instance-state-name,Values=pending,running,stopping,stopped
   aws ec2 describe-security-groups --region <aws-region> \
     --filters Name=tag:lab-playground-site,Values=<site-id>
   aws ec2 describe-subnets --region <aws-region> \
     --filters Name=tag:lab-playground-site,Values=<site-id>
   aws ec2 describe-volumes --region <aws-region> \
     --filters Name=tag:lab-playground-site,Values=<site-id>

   # GCP
   gcloud compute instances list --project <gcp-project> \
     --filter='labels.lab-playground-site=<site-id> AND labels.lab-playground-kind=instance'
   gcloud compute firewall-rules list --project <gcp-project> --filter='name ~ ^lp-<site-id>-d-'
   gcloud compute networks subnets list --project <gcp-project> --filter='name ~ ^lp-<site-id>-s-'
   ```

4. **Never delete a resource whose site does not match** `LAB_SITE_ID`, or
   that the portal refused to delete, without finding out why. A mismatched
   tag means another portal, a live run or a person owns it.
5. **Manual cleanup**, only for this site's leftovers and in this order:
   instances (`aws ec2 terminate-instances`, `gcloud compute instances
   delete`), then security groups or firewall rules (`aws ec2
   delete-security-group`, `gcloud compute firewall-rules delete`), then
   subnets (`aws ec2 delete-subnet`, `gcloud compute networks subnets
   delete`). EC2 frees a terminated instance's interface asynchronously, so
   a security group or subnet delete can fail with `DependencyViolation` for
   a few minutes; retry. Leave the router, the router subnet and the
   operator rules in place.
6. **Stop the router** if it is still running (`aws ec2 stop-instances
   --instance-ids <router-instance-id>`, `gcloud compute instances stop
   <gcp-router-name> --zone <gcp-zone>`); the portal stops it by itself
   after the idle minutes.
7. **Prove zero.** Re-run the queries in step 3: every list must be empty
   except the router, and `SELECT backend, COUNT(*) FROM vm_records WHERE
   destroyed = 0 GROUP BY backend` must show no cloud rows. Only then remove
   the backend from `ENABLED_BACKENDS`, change a placement setting, or roll
   the portal back to v1.0.0 (whose code ignores the new columns but cannot
   destroy cloud records).
