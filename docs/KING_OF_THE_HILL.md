# King of the Hill (KOTH) Mode

A competitive lab mode where multiple student teams attack a single shared
deployment of vulnerable boxes. Teams earn points for claiming boxes, keeping
captured services up, and submitting flags. The website renders a live
scoreboard, network map, and event log via Server-Sent Events.

**Audience:** course instructors (admins) and students competing in a KOTH event.

**Status:** v1 — single-active-game, admin-controlled deployment, web scoreboard,
in-process scoring loop.

---

## Concepts

| Concept | Meaning |
|---|---|
| **KOTH game** | A scheduled instance of a KOTH lab. Only **one** game can be active at a time. Lifecycle: `draft → deploying → active → ended`. |
| **Deployment group** | A `templates.yaml` group with `koth_mode: true`. The admin picks one when creating a game; the bundle's templates become the targets. |
| **Team** | An existing `StudentGroup` selected to play. Each team gets a color and a per-team **submission token**. |
| **Box** | One VM in the KOTH deployment, mirrored to a `KothBox` row. Has a name, IPs, services to probe, and an owner team. |
| **Claim** | A team takes ownership of a box by proving it controls the box with its submission token. Two transports, selected per game by `claim_mode`: a **beacon** (a TCP listener on the box serves the token; the scorer reads it with one extra local connect per box per tick, in the same bounded fan-out as the service probes, and **zero Proxmox API calls**) or a **claim file** (`/koth/claim` on Linux, `C:\koth\claim` on Windows, read through the QEMU guest agent — **one Proxmox API call per box per tick**). Both are server-pulled, never client-pushed. |
| **Flag** | An admin-defined UUID worth one-time bonus points. Teams submit flags from the player website (session-authed); one accepted submission per (flag, team), enforced at the database level. |
| **Scan tick** | A scheduler job (default every 12s) that captures claims, TCP-probes each box's `koth_services`, recomputes scores, and broadcasts one `scan` event carrying the complete state. Claim reads and port probes run concurrently (32 in flight), so tick cost does not scale linearly with box count. |
| **Score** | Accumulates per team on `KothTeam.score`: `box_ownership_score` per claimed box + `port_open_score` per open service, multiplied by an exponential growth factor (`1 + iteration/exp_val`). Accepted-flag bonuses are summed separately at read time so the tick and a flag submission can never clobber each other. `KothScoreSnapshot` is history only, written every `KOTH_SNAPSHOT_INTERVAL_SEC`. |

---

## Admin guide

### 1. Mark a deployment group as `koth_mode: true`

In `templates.yaml`, set `koth_mode: true` on the deployment group you want to
use as a KOTH arena. Optionally add `koth_services` to each template so the
scanner has services to probe (no `koth_services` = the team only earns the
ownership score, no service-uptime bonus).

```yaml
- display_name: "Example KOTH"
  description: "King of the Hill arena."
  enabled: true
  koth_mode: true
  uptime_minutes: 240
  extend_time_minutes: 60
  load_balancing: { enabled: true, cpu_threshold: 80, memory_threshold: 85 }
  templates:
    - name: "koth-mailserver"
      type: "qemu"
      entry_point: true
      memory: 2048
      cores: 2
      koth_services:
        - { name: "SSH",  port: 22 }
        - { name: "SMTP", port: 25 }
        - { name: "HTTP", port: 80 }
      template_locations:
        node1: 9501
        node2: 9601
```

Any deployment group can be configured as `koth_mode: true`; define its
services and template locations in the operator-owned `templates.yaml`.

`koth_mode: true` deployment groups are **hidden from the student `/templates`
list** so students cannot self-deploy them. Admins still see them everywhere.

### 2. Apply the schema migrations (one-time, before first use)

```bash
python scripts/migrate_add_koth.py
python scripts/migrate_koth_deploying_status.py
python scripts/migrate_koth_singleton_index.py
python scripts/migrate_koth_duration.py
python scripts/migrate_koth_claim_files.py
python scripts/migrate_koth_claim_mode.py
```

All idempotent — safe to re-run in order:

1. `migrate_add_koth.py` — creates the 7 `koth_*` tables and adds `vm_records.koth_game_id`.
2. `migrate_koth_deploying_status.py` — relaxes the `koth_games.status` CHECK constraint to allow `'deploying'`.
3. `migrate_koth_singleton_index.py` — repairs the single-active-game UNIQUE so ended games don't block new drafts (MySQL doesn't support partial indexes; we use an app-managed `active_singleton` sentinel column instead).
4. `migrate_koth_duration.py` — adds `duration_minutes` and `ends_at` columns to `koth_games` for time-boxed games.
5. `migrate_koth_claim_files.py` — adds `koth_boxes.claim_file_path` for the file claim transport. Backfills existing rows with `/koth/claim`.
6. `migrate_koth_claim_mode.py` — adds `koth_games.claim_mode` / `claim_port` (the beacon transport), `koth_teams.score` (the live scoreboard value, backfilled from the latest snapshot so an in-flight game keeps its scores), and the per-team history index.

### 3. Create a game

Open `/admin/koth` (or click "King of the Hill" in the admin sidebar) and fill
in the **CREATE NEW GAME** form:

| Field | Notes |
|---|---|
| **Name** | Free-form label (e.g., "Final Showdown"). |
| **Deployment group** | Auto-populated from `koth_mode: true` groups in `templates.yaml`. |
| **Entry VLAN** | Integer 2–4094. Validated against all User/StudentGroup/active-KothGame VLANs. |
| **Internal VLAN** | Same validation. |
| **Participating teams** | Checkboxes over your existing `StudentGroup`s. Each gets an auto-assigned color from the palette (`red`, `blue`, `green`, `orange`, `purple`, `yellow`, `cyan`, `magenta`). |

Submitting creates a **draft** game. Each team's submission token is
displayed once on creation — copy them and share with each team's captain (or
let students grab their own from `/koth` after deploy).

Only one game at a time can be `active`. Attempting to create a second active
game returns a 409 from the API.

### 4. Deploy

Click **DEPLOY** on the current-game card. The system will:

1. Set `KothGame.status='deploying'` and commit immediately, so the admin
   page (and any other open tab) flips to the cyan **DEPLOYMENT IN
   PROGRESS** panel.
2. Call `deploy_bundle()` in `app/routes/vm_creation.py` — the same
   parallel-clone orchestration the regular `/create` endpoint uses. This
   gives KOTH per-template load-balanced node selection (templates with
   `template_locations: {node1, node2}` are distributed across both nodes),
   per-node ZFS clone semaphores, and partial-failure cleanup for free.
3. VM names follow the pattern `koth-<game_id>-<template_name>`.
4. Populate `KothBox` rows with the cloned VM IDs, IPs, and service
   definitions (post-processed from `deploy_bundle`'s returned
   `VMRecord` list via `_materialize_koth_boxes`).
5. Compute a deterministic circular layout for the network map (positions
   persist across page reloads and across redeploys).
6. Set the game `status='active'`, `is_active=True`, and start the
   APScheduler-backed scoring loop.

If any clone fails, `deploy_bundle` cleans up the partials in Proxmox,
the status reverts to `draft`, and the admin sees the error. The deploy
is gated by an in-process per-game lock to reject double-clicks; the DB
status check (`status != 'draft'` rejects with 409) is the second line of
defense across multiple Flask workers.

### 5. Add flags (optional)

Once the game is active, the **FLAGS** card appears. Add per-game flags:

- **Name** — admin-readable label (e.g., "Root flag on DC").
- **Value** — the exact string students must submit. Stored plaintext so you
  can edit/inspect after creation.
- **Points** — float; awarded once per team per flag.

Flags are constant-time-compared on submission. A team can submit any flag
exactly once (one accepted `KothFlagSubmission` per (flag, team)).

### 6. Live controls

The current-game card has three checkboxes (live-toggled, no reload needed):

- **Pause other deployments** — non-admin `/create` calls return 409 ("Deployments paused: KOTH game in progress."). Admins still bypass.
- **Show scoreboard** — when off, students see "HIDDEN BY INSTRUCTOR" instead of scores. Server-side redaction (no client-side trick to bypass). Admins always see scores.
- **Show network map** — same redaction for the box ownership map.

And action buttons:

- **REDEPLOY** — destroys the current KOTH VMs in Proxmox (via `cleanup_vms`, also marks `VMRecord.destroyed=True`), then re-clones them through `deploy_bundle()`. **Scores, claim history, and team assignments persist.** Box ownership clears (new IPs). Box positions and `KothBox.id` persist when template names match (so `KothClaimEvent` foreign keys survive). The game flips to `deploying` during the re-clone, then back to `active`.
- **END GAME** — destroys all KOTH VMs in Proxmox (idempotent — calling it again on an already-ended game still cleans up any stragglers), sets `status='ended'`, freezes the scoreboard, and stops the scheduler.

### 7. Token rotation

If a team's submission token leaks, click **ROTATE TOKEN** in the team table.
The old token is no longer matched by the claim poller — any box still
holding the old token in its claim file becomes a no-op until rewritten
with the new token.

### 8. Manual claim override (rare)

There's an API endpoint `POST /koth/api/games/<id>/boxes/<id>/manual-claim`
that lets an admin force a box's ownership. Used when a team can prove they
owned a box but the callback didn't fire. Logged as an event with
`meta.manual = true`.

---

## Student guide

### 1. Open the scoreboard

Visit `/koth` (or click the **KOTH** link in the navbar). You'll see:

- **SCOREBOARD** — ranked teams with live score, color swatch, and an amber flash when scores update.
- **NETWORK MAP** — Cytoscape graph of all boxes, colored by current owner. Pulse-flashes when ownership flips.
- **EVENT LOG** — recent claim/flag events.
- **MY TEAM** — only shown if you're a member of a participating `StudentGroup`. Displays your team color, your submission token, the claim commands for whichever `claim_mode` the game is running, and a SUBMIT FLAG form.

The page connects to a Server-Sent Events stream and updates without manual reload. The status indicator in the header reads **LIVE** (green pulse) when connected. Each `scan` event carries the complete scoreboard and map state, so the page renders straight from the push — it does not poll.

### 2. Claim a box (after rooting it)

Box ownership is **server-pulled, not client-pushed.** On every scoring tick
(default 12 s, admin-configurable) the KOTH server asks each box who owns it,
and assigns ownership to whichever team's submission token comes back.

Which question it asks depends on the game's `claim_mode`:

#### `beacon` — serve your token on a TCP port (recommended)

Run a listener on the game's `claim_port` (default 31337) that writes your
token to anything that connects:

```bash
# Linux box
while :; do echo 'YOUR-TEAM-TOKEN' | nc -lN -p 31337; done &
```

```powershell
# Windows box
powershell -c "$l=[Net.Sockets.TcpListener]31337;$l.Start();while($true){$c=$l.AcceptTcpClient();$w=New-Object IO.StreamWriter($c.GetStream());$w.WriteLine('YOUR-TEAM-TOKEN');$w.Flush();$c.Close()}"
```

**You have to keep the listener alive.** If it dies — reboot, someone kills
your process, you lose your foothold — the next tick sees nothing and the box
is up for grabs. That is the point: holding the hill means holding it.

The server reads the first line, up to 256 bytes, with a 1.5 s budget for the
whole exchange. That is one extra TCP connect per box per tick — cheap and
local, and crucially not a Proxmox API call.

#### `file` — write your token to the claim file

```bash
# Linux box
echo 'YOUR-TEAM-TOKEN' > /koth/claim

# Windows box
echo YOUR-TEAM-TOKEN > C:\koth\claim
```

The exact path per box is shown in the admin's box list as `claim_file_path`
(set per template via `koth_claim_file:` in `templates.yaml`). This survives
losing your shell — the file stays until someone overwrites it — but it needs
a working QEMU guest agent, and it costs the server one Proxmox API call per
box per tick.

#### `both`

Both work. **The beacon wins a conflict**, because a live listener is stronger
evidence that you still hold the box than a file somebody wrote once. Use this
when some boxes are Windows (file) and some are Linux (beacon), or to give
teams a way back in when their listener dies.

---

Within one tick the scoreboard reflects your claim and the network map node
recolors. The 30-second per-box claim cooldown still applies (configurable per
game), so a script that rewrites the claim every tick can't ping-pong ownership.

What happens with what the server reads:
- nothing there / empty / whitespace → no-op
- token doesn't match any team in this game → debug log, no-op
- token is the current owner's → no-op
- token belongs to a different team and cooldown is clear → ownership flips, `KothClaimEvent(event_type='claim', meta={source: 'beacon'|'file_poll'})` recorded, SSE `claim` event published.

A box whose guest agent stops answering goes into exponential backoff (15 s
doubling to a 5-minute cap) instead of being retried — and timing out — on
every tick. **The beacon has no backoff.** A refused connect (nothing
listening) costs nothing, but a box that is powered off, rebooting, or
running a DROP-by-default firewall absorbs the SYN and burns the full 1.5 s
beacon budget, every tick, for as long as it stays that way. That is the
input class most likely to push a tick over its interval — watch
`tick.overruns` in `/koth/api/_diag`.

The legacy `POST /koth/claim` curl callback was removed: in
deployments where the boxes can't reach the KOTH host (NAT, asymmetric
routing, firewalled VLANs) the push-from-box model never worked. Both current
transports are pull-from-server.

### 3. Submit a flag

Flags are admin-defined UUIDs that grant **one-time bonus points per team**. They are submitted from the player website, **not** by curling from the box (one accept per `(flag_id, team_id)` is enforced at the database level).

On the player page's MY TEAM panel, paste your flag value into the SUBMIT FLAG form and click submit. Inline status:

- `✓ +N (flag_name)` — accepted, points credited.
- `✗ Not a valid flag` — value didn't match any flag in the active game.
- `⚠ Already submitted by your team` — that flag's bonus is already burned.

The server endpoint is `POST /koth/api/flags/submit` (session-authenticated; team identity comes from your `StudentGroupMember` row, not from a token).

Admins can add flags via the admin UI either one-at-a-time (form) or in bulk via the **UPLOAD FLAGS YAML** button. Both paths work in **draft** state too, so the flag set can be prepped before deploying. YAML format:

```yaml
- name: "Root flag — DC"
  value: "<UUID>"     # the string players submit
  points: 10
  box_name: "final-dc"   # optional; resolves to box_id by name lookup. Omit for global.
- name: "Web pivot flag"
  value: "<UUID>"
  points: 5
```

Atomic — any per-row validation failure (unknown `box_name`, missing required field) aborts the whole upload and inserts zero rows.

### 4. Service uptime scoring

Every scan tick (default 12s), the website TCP-probes each `koth_service` you
own. **Open services earn `port_open_score` per tick.** Closed services earn
zero. Keep services up to maximize your score; if you defend by killing a
service, you lose the bonus while still keeping ownership.

The exponential growth factor (`1 + iteration / exp_val`) means later ticks
are worth more — late-game claims are extra valuable.

---

## Configuration reference

Defaults live in `app/config.py`; per-game overrides are editable in the admin UI.

| Key | Default | Meaning |
|---|---|---|
| `KOTH_DEFAULT_SCAN_INTERVAL_SEC` | `12` | Seconds between scan ticks. |
| `KOTH_DEFAULT_CLAIM_COOLDOWN_SEC` | `30` | Per-box cooldown after a claim. |
| `KOTH_DEFAULT_CLAIM_MODE` | `both` | Claim transport for NEW games: `file`, `beacon`, or `both`. Games created before this column existed stay on `file`. |
| `KOTH_DEFAULT_CLAIM_PORT` | `31337` | Port the claim beacon listens on. Per-game editable, 1024–65535. |
| `KOTH_SNAPSHOT_INTERVAL_SEC` | `60` | Wall-clock seconds between `KothScoreSnapshot` history rows. The live scoreboard reads `KothTeam.score` and never touches this table. |
| `KOTH_DEFAULT_BOX_OWNERSHIP_SCORE` | `1` | Points per owned box per tick (pre-exp). |
| `KOTH_DEFAULT_PORT_OPEN_SCORE` | `3` | Points per open service per tick (pre-exp). |
| `KOTH_DEFAULT_EXP_SCORING` | `true` | Enable exponential late-game multiplier. |
| `KOTH_DEFAULT_EXP_VAL` | `60` | Divisor in `1 + iteration / exp_val`. Lower = faster growth. |
| `KOTH_TEAM_COLORS` | 24 distinct names | Auto-assigned in order of selection; caps teams per game. |

Override any default by setting the matching environment variable (e.g.
`KOTH_DEFAULT_SCAN_INTERVAL_SEC=20`).

---

## Operational notes

### Network reachability

The KOTH VMs sit on the admin-supplied entry/internal VLANs. For students to
attack them, your firewall/router must route from each team's normal VLAN
into the KOTH internal VLAN.

The boxes never need outbound access to the Flask host — claims are
server-pulled either way. What each transport does need:

| Transport | Requires |
|---|---|
| `beacon` | Flask host → box TCP on `claim_port`. Same direction and same network path the service probe already uses, so if your services probe `open`, the beacon works. |
| `file` | Proxmox host → QEMU guest agent on each VM, over the Proxmox cluster network. Same channel as IP discovery. |

Flag submissions go through the player's already-authenticated browser
session, unaffected by box egress either way.

### Cost per tick at classroom scale

For a game with `B` boxes and `S` services per box, one tick costs:

| | Proxmox API calls | TCP connects | SQL queries |
|---|---|---|---|
| `beacon` | 0 | `B` (beacon) + `B×S` (services) | ~6 |
| `file` | `B` (minus boxes in backoff) | `B×S` | ~6 |
| `both` | `B` (minus backoff, minus boxes that answered their beacon) | `B` + `B×S` | ~6 |

All network work runs 32 in flight, so a 22-box game is one round of beacons
plus two of probes even when every box is unreachable — about 5.5 s against
the 12 s default interval. SQL is flat in team count: the scoreboard reads
`KothTeam.score` plus one grouped flag-bonus query.

SSE is one `json.dumps` per tick regardless of how many tabs are open — the
frame is built once and handed to every subscriber — and each `scan` carries
the complete state so no client ever polls `/koth/api/state` during a game.

If a tick ever runs longer than its own interval, APScheduler silently skips
runs. The scorer now logs a warning when that happens and counts it in
`/koth/api/_diag` under `tick.overruns`. If you see overruns, raise
`scan_interval_sec` or switch to `beacon` mode.

### Scheduler lifecycle

- The scoring loop runs as an **APScheduler `BackgroundScheduler`** inside the
  Flask process. It's idempotent — `start_scheduler(app)` is a no-op if
  already running.
- On Flask boot, `app/__init__.py` checks for `KothGame.is_active=True` and
  resumes the scheduler automatically. Tests skip this via `TESTING=True`.
- Production must run exactly one gevent worker. APScheduler and the SSE
  event bus are both in-process; additional workers run competing schedulers
  and split events across separate subscriber sets.

### Database state preservation

Across redeploys, **scores and team assignments persist**. Box `owner_team_id`
clears (because the boxes have new IPs). `KothScoreSnapshot` and
`KothClaimEvent` rows are append-only and never touched by redeploy. The
only way to wipe state is **End game → Create new game**.

### Anonymous students

Logged-out users hitting `/koth` get redirected to login (it's behind
`@login_required`). Logged-in students who aren't on a participating team see
the scoreboard and map but no "MY TEAM" panel and no submission token.

---

## Security considerations

- **Box ownership comes from "who has shell on the box."** The team that can run the beacon listener, or write the claim file, owns the box on the next tick. That is the entire identity check — no source-IP, no client-supplied box name, no callback to hijack. The threat model is "rooting the box = owning it," which is exactly the intent for a competitive lab.
- **Your token is visible to whoever else is on the box.** Anyone who can read `/koth/claim`, or simply connect to your beacon port, sees your submission token. That lets them claim boxes *as you* — which helps you, not them — but it does muddy attribution, and a team that wants to can keep a box pinned to a rival to deny them a reclaim. Rotate via the admin UI if it matters. There is no transport where the token stays secret from someone standing on the same box.
- **Beacon mode has no guest-agent dependency.** A box with a broken or missing `qemu-guest-agent` can still be claimed. The reverse is also true: beacon mode needs the Flask host to reach the box's `claim_port`, which the service probe already requires.
- **Guest agent must be available for `file` mode.** If `qemu-guest-agent` isn't running on a template, the read fails, the box enters backoff, and ownership stays at whatever it was before. Confirm with `systemctl status qemu-guest-agent` (Linux) or `Get-Service QEMU-GA` (Windows). Proxmox VM config must also have `agent: 1` set.
- **A hostile box can't flood or stall the scorer.** The beacon read is capped at 256 bytes with a 1.5 s budget for the *whole* exchange, and the guest-agent read at 4096 bytes — so a root-owned box can neither make the tick buffer nor hold its slot in the fan-out by drip-feeding bytes. (It could before: the beacon's timeout used to apply per-`recv()` inside a 256-iteration loop, which one box could stretch to minutes, freezing scoring for the whole class.) Watch `tick.overruns` in `/koth/api/_diag` during a game regardless.
- **Flag submission** is session-authed via the player's already-logged-in browser. Team identity comes from the user's `StudentGroupMember` row, not from a token. A user not on a participating team gets HTTP 403 with no points credited.
- **One accept per (flag, team)** is enforced by a partial unique index on `koth_flag_submissions` (`uq_koth_flag_sub_accepted`), not just by application logic — even a race condition can't double-credit.
- **Plaintext flag storage:** `KothFlag.value` is stored as plain text so admins can edit/copy after creation. Anyone with admin DB access can read the answer key — same posture as the rest of the app's secrets.

---

## API summary

Admin (session + admin):

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/koth` | KOTH admin page |
| POST | `/koth/api/games` | Create draft game + teams |
| PATCH | `/koth/api/games/<id>` | Update settings (toggles, scoring tunables) |
| POST | `/koth/api/games/<id>/deploy` | Clone VMs, activate game, start scheduler |
| POST | `/koth/api/games/<id>/redeploy` | Re-clone VMs, preserve scores |
| POST | `/koth/api/games/<id>/end` | End game, stop scheduler |
| DELETE | `/koth/api/games/<id>` | Delete ended game (only if VMs destroyed) |
| POST | `/koth/api/games/<id>/flags` | Add a single flag (allowed in draft/deploying/active; rejected on ended) |
| POST | `/koth/api/games/<id>/flags/upload` | Bulk-add flags from YAML (`Content-Type: application/x-yaml` with the YAML body, or JSON `{yaml: "..."}`). Atomic. |
| DELETE | `/koth/api/games/<id>/flags/<flag_id>` | Delete (force=1 if accepted submissions exist) |
| POST | `/koth/api/games/<id>/teams/<id>/rotate-token` | New token for a team |
| POST | `/koth/api/games/<id>/boxes/<id>/manual-claim` | Override box ownership |
| GET | `/koth/api/_diag` | Live resource numbers: SSE subscriber count, dropped frames, tick duration + overrun count, boxes in guest-agent backoff, DB pool checkouts. Read this *during* a game. |

Players (session):

| Method | Path | Purpose |
|---|---|---|
| GET | `/koth` | Player scoreboard + map page |
| GET | `/koth/api/state` | Bootstrap state (game, teams, scores, boxes, hide flags) |
| GET | `/koth/api/my-team` | Calling user's team + submission token (if member) |
| POST | `/koth/api/flags/submit` | `{value: "<UUID>"}` — submit a flag (session-authed; team derived from StudentGroupMember) |
| GET | `/koth/api/stream` | Server-Sent Events: `scan` (complete state snapshot — the page renders from it and does not poll), `claim`, `flag`, `game_ended` |

There are no token-authed callback endpoints. Box claims are server-pulled, over a TCP beacon read or the Proxmox guest agent (see "Claim a box" above), never pushed by the box.

---

## Files

| File | Role |
|---|---|
| `app/models.py` | KothGame / KothTeam / KothBox / KothFlag / KothFlagSubmission / KothClaimEvent / KothScoreSnapshot |
| `app/config.py` | KOTH defaults, color palette, `get_koth_defaults()` |
| `app/template_manager.py` | `get_koth_groups()`, `get_koth_services()`, `get_koth_claim_file()` |
| `app/vlan_allocator.py` | KOTH VLAN-collision check (active games reserve their tags) |
| `app/koth_layout.py` | Deterministic circular network-map layout |
| `app/koth_scoring.py` | Pure scoring math (`compute_team_deltas`) |
| `app/koth_scanner.py` | TCP service probe (`probe_port`, `probe_many`) and claim-beacon read (`probe_beacon`, `probe_beacons`) |
| `app/koth_events.py` | SSE pub/sub: frames serialized once per publish, bounded per-subscriber buffer, `scan` snapshots coalesce |
| `app/concurrency.py` | `map_concurrent()` — bounded fan-out that works under gevent and plain threads. Workers must not touch the DB session. |
| `app/koth_scorer.py` | Scoring tick (`run_tick`), claim capture (`_poll_claims`, `_apply_claim`), shared state payload (`build_state_payload`, `latest_scores`), APScheduler lifecycle |
| `app/routes/utils.py` | `_read_guest_file()` helper that calls Proxmox guest agent `file-read` |
| `app/routes/koth.py` | Blueprint root |
| `app/routes/koth_admin.py` | Admin endpoints (CRUD, deploy/redeploy, flag add + bulk YAML upload, tokens). `deploy()` and `redeploy()` are thin wrappers around `deploy_bundle()` plus KothBox materialization. |
| `app/routes/koth_player.py` | Player endpoints (state, my-team, **flag submit**, SSE stream) |
| `app/routes/vm_creation.py` | Hosts `deploy_bundle()` — the shared parallel-clone orchestration KOTH and `/create` both use. |
| `templates/koth.html` | Player page (brutalist tactical readout) |
| `templates/koth_admin.html` | Admin page |
| `templates/base.html` | Shared Jinja layout |
| `static/css/koth.css` | Player + admin theme (JetBrains Mono, charcoal + amber, scanline overlay) |
| `static/js/koth-player.js` | Scoreboard, map, SSE handler |
| `static/js/koth-admin.js` | Game lifecycle UI |
| `scripts/migrate_add_koth.py` | MySQL migration |
| `tests/test_koth_*.py` | Unit tests (~35 tests) |

---

## See also

- Original Node.js KOTH (the inspiration for the scoring algorithm): `koth/app.js` in the repo root.
