# Per-Student Internal VLANs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an individual-mode deployment of two machines (one entry point, one single-homed machine on the student's internal VLAN) work over the course VPN: the jumpbox serves DHCP on each student's internal VLAN, the Headscale policy grants each student their own internal VLAN, and no student's VLANs are renumbered.

**Architecture:** The per-student pools end at 199 (entry) and 299 (internal). That is exactly what the importers already assign to a roster of up to 100 students (`100+i`, `200+i`), so no stored tag changes and no database migration is needed. A row numbered by today's importers past the 100th student (a legacy `200/300` pair) is not renumbered; a new pre-rollout audit refuses it. The jumpbox gains a band `internal:200-299:100:1` (VLAN *N* → `10.<N-100>.1.0/24`) and the entry band ends at 199, so VLAN 200 is no longer in two bands. The Headscale renderer grants each login its own internal VLAN next to its entry VLAN, on Proxmox and on every enabled cloud, and still only where a band routes it. No NIC code changes: the portal already gives a plain machine one NIC on the internal VLAN, and already lets an entry point drop its second NIC; a regression test pins the two-machine shape. Two optional settings, `LAB_BRIDGE` and `VMID_RANGE`, let a portal run beside production on an isolated bridge in its own VMID range; unset, the portal behaves exactly as today. They carry a **permanent staging stack**: an isolated bridge, a staging jumpbox, a staging Headscale with two client containers, and a staging portal. The two-client acceptance test runs there, any time and repeatably, and gates the merge. Production never has a policy swapped or a portal frozen for testing.

**Tech Stack:** Python 3.12, Flask + SQLAlchemy, pytest through `uv`; bash and stdlib-only Python for the jumpbox and changeover scripts; netplan and dnsmasq on Ubuntu 24.04; Headscale v0.27 policy files.

**Spec:** `ENPM634-course` (the private course repo), branch `feature/ad-lab` at `d331778`: `coursework/docs/superpowers/specs/2026-11-16-active-directory-homework-and-classwork-design.md`, sections "Portal change this design needs", "Prerequisite: per-student isolation", "Reachability" and "Verification before handover".

### Where the code disagrees with the spec, and what this plan decides beyond it

1. **VLAN 200 is served today, and its non-grant is deliberate.** The spec says the jumpbox serves only the entry band. In fact the default band `entry:100-200:0:0` (`scripts/jumpbox_netgen.py:35-39`, `app/config.py:209-211`) serves VLAN 200, student #0's internal VLAN, as `10.200.0.0/24`. The renderer never grants a user-internal VLAN because of ruling I12 (`docs/superpowers/plans/2026-11-04-multi-backend-vms.md:58`): granting by slot alone would over-grant tag 200. **This plan supersedes I12.** Internal becomes a granted kind. Tag 200 leaves the entry band and is granted only to the one login that owns it, because `claim()` (`scripts/headscale_policy.py:141-145`) already refuses a tag with two owners.
2. **No template-level NIC option is needed.** A plain machine (no `entry_point`) gets exactly one NIC, on the internal VLAN, and `net1` is deleted (`app/routes/vm_creation.py:240-246`; LXC `:177-180`). An entry point gets a second NIC unless it sets `internal_nic: false` (`vm_creation.py:228-239`, key read at `:151`). Both shapes are already pinned for single machines (`tests/backends/test_vm_provisioning.py:731-749`). So the domain controller is single-homed in both shapes:
   - **DC01 + SRV01:** SRV01 is `entry_point: true` with the default second NIC (the spec's "my call" stands), and DC01 is a plain machine.
   - **Classwork core:** DC01 is deployed alone as a plain machine, with one NIC on the internal VLAN. It then has the same VLAN and the same pinned `.10` address as in the two-machine deployment.

   The portal rewrites `net0` and deletes only `net1`, so the AD templates must be built with a single NIC. Task 5 documents this.
3. **Internal address plan:** `10.<N-100>.1.0/24`. A student's two networks share the second octet: VLANs 142 and 242 are `10.142.0.0/24` and `10.142.1.0/24`. Third octet 1 is unused by every existing band.
4. **A roster of more than 100 students is refused.** Today a 101st student gets entry VLAN 200, which is student #0's internal VLAN: two students on one layer-2 network. Every importer now refuses such a roster atomically (`init_db.py`, `import_students.py`, and `migrate_add_vlan.py`, whose assignment loops today commit a partial numbering), and so does `scripts/term_changeover.sh`, before anything changes. Rows that already exist are checked by a new stdlib audit (`scripts/audit_vlan_pairs.py`) before the rollout and after the changeover.
5. **The production jumpbox is hand-built.** Serving a new band means replacing its netplan and dnsmasq files. Task 8 adopts it with `jumpbox_configure.sh` at the changeover, using `docs/JUMPBOX.md:143-163`, which also clears the stale DHCP ranges recorded on that box. The operator's earlier rule "never `--apply` on that box" was made for a mid-term window. **The instructor must confirm** that adoption at the changeover is acceptable. The staging jumpbox (Task 7) is built fresh with the same two scripts. It rehearses every configure, Tailscale and isolation step, but not the move of hand-built files aside, which exists only on the production box.
6. **Permanent isolated staging stack (instructor ruling, 2026-11-17).** It replaces the earlier plan to test against the production jumpbox and Headscale in the changeover window. Its parts:
   - an isolated Linux bridge with no physical ports, on one Proxmox node;
   - a staging jumpbox, whose trunk is on that bridge and whose management NIC is on the management VLAN;
   - a dedicated staging VM running its own Headscale (its own tailnet) and two Tailscale client containers that act as two students;
   - a staging portal: a local test instance (`docs/LOCAL_TEST_INSTANCE.md`) with its own database, `FIXED_NODE` set to that node, `LAB_BRIDGE` set to the isolated bridge, and its own `VMID_RANGE`.

   Because the bridge has no uplink, staging can reuse VLAN tags that production uses without touching production's networks. The concrete node, VMIDs, management VLAN, VMID range and portal host are site identity (`CLAUDE.md` rule 13). This plan and `docs/STAGING.md` use the placeholders `<STAGING_NODE>`, `<STAGING_BRIDGE>`, `<STAGING_JUMPBOX_VMID>`, `<STAGING_VPN_VMID>`, `<MGMT_VLAN>`, `<STAGING_VMID_RANGE>` and `<STAGING_PORTAL_HOST>`. The instructor's values live in the operator's private notes. The staging VPN VM must not be a host already on the production tailnet.
7. **Found, out of scope:** `GROUP_ENTRY_VLAN_MAX` defaults to 400 (`app/config.py:262`). The allocator treats it as inclusive (`app/vlan_allocator.py:61-66`), and 400 is also the first group-internal tag: the same defect as VLAN 200, at the group edge. This plan does not change it.

## Global Constraints

- Ship at a term changeover, never mid-term (spec: "It ships at the term changeover with the isolation work below, never mid-term").
- Phase 1 is Proxmox only. AWS and GCP keep working unchanged: no backend code changes. The only cloud effect is that the policy also grants the internal slot's `/26`.
- No other portal change (spec: "No portal changes beyond the one this design needs"). The one exception is `LAB_BRIDGE` and `VMID_RANGE` (Task 6), which the instructor ruled on 2026-11-17 to carry the staging stack. Unset, they must leave production's behaviour unchanged, which Task 6's tests prove.
- Staging never touches production: no production VLAN on the staging bridge's uplink (it has none), no production Headscale, no production portal database. The staging portal's orphan reaper stays off (`ORPHAN_REAPER_ENABLED=false`, `docs/LOCAL_TEST_INSTANCE.md:13-19`).
- The AD labs do not ship until the per-student policy (P2) and the jumpbox inter-VLAN drop (J2) are live and the acceptance test shows one student's VPN client cannot reach another student's machines (spec "Prerequisite: per-student isolation").
- The band string, exactly: `entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4`.
- Tracked files carry no site identity (`CLAUDE.md` rule 13): no real host or node names, addresses, VMIDs or usernames. Scratch VMIDs and hosts come from the dispatch, never from this repo.
- Run tests only through `uv`: focused `uv run python -m pytest tests/<file>.py -q`, full `uv run python -m pytest tests/ --ignore=tests/integration -q` (`.github/workflows/ci.yml:66`). Run them on Linux: `tests/test_jumpbox_scripts.py` imports `pty`, and the ops tests run `bash`.
- Repo workflow (`CLAUDE.md:15-43`): change, test, Codex review, commit. Commit after each task on `feature/individual-internal-vlans`. Push CI runs only on `main` and `features/*` (`.github/workflows/ci.yml:3-10`), so this branch gets its CI from the pull request against `main` (`ci.yml:14-15`). The PR is squash-merged into one commit after a cross-family Codex review of the whole branch.

## Review Focus

1. **A single-machine individual lab whose entry point keeps the default `internal_nic: true`.** Once the internal band is served, its VM gets a second lease, a second default route and a second address in the UI. A reasonable operator expects their existing labs to look the same. Task 5 pins every committed single-machine example to `internal_nic: false`, and Task 8 audits the operator's `templates.yaml`.
2. **The portal's `.env` and the jumpbox's `site.env` disagree on the bands** because one was updated and the other was not. VLAN 200 would then be granted as one subnet and served as another. Task 3 pins the portal default to the jumpbox default, and Task 8 compares the two live values.
3. **A roster of more than 100 students, or a legacy `200/300` pair already in the database.** Expected: a new roster is refused before any change and never numbered into a shared VLAN; an existing out-of-rule pair or a tag held by two owners stops the rollout. Tasks 1 and 2.
4. **One tag held by two owners across kinds**: an internal tag equal to another student's entry or internal tag, or a KOTH tag typed into 200-299. Expected: the policy render refuses. Task 4.
5. **A cloud-enabled portal.** Expected: the internal slot's `/26` is granted on AWS and GCP exactly when a band routes it, and the reverse rule lets the student's own machines connect back (coercion and relay need this). Task 4.
6. **A staging `VMID_RANGE` above another portal's highest VMID, or a `LAB_BRIDGE` value carrying Proxmox options.** Expected: the range never pulls the production portal's VMIDs upward, and a bridge like `vmbr9,firewall=0` is refused at startup instead of being spliced into a `netN` string. Task 6 pins the range's high-water behaviour and the bridge validation; Task 7's preconditions check the range against the cluster.

---

### Task 1: Per-student pools end at 199 and 299, and the importers stay inside them

**Files:**
- Modify: `app/vlan_allocator.py:1-11` (docstring), `:21-22` (constants); add `user_pair_for_index` after `_first_free` (`:61-66`)
- Modify: `scripts/init_db.py:230-299` (`import_student_whitelist`)
- Modify: `scripts/import_students.py:106-109`
- Modify: `scripts/migrate_add_vlan.py:24-28` (constants), `:182-249` (both assignment functions)
- Delete: `app/routes/utils.py:23-27` (VLAN constants nothing imports)
- Modify: `app/config.py:542`, `:551`, `:555` (range text only)
- Modify: `app/models.py:160-161`, `:174-175`, `:239-240` (comments only)
- Test: `tests/test_vlan_allocator.py`; create `tests/test_student_vlan_numbering.py`

**Interfaces:**
- Produces: `app.vlan_allocator.USER_ENTRY_MIN, USER_ENTRY_MAX = 100, 199` and `USER_INTERNAL_MIN, USER_INTERNAL_MAX = 200, 299`.
- Produces: `app.vlan_allocator.user_pair_for_index(index: int) -> Tuple[int, int]`. It returns `(USER_ENTRY_MIN + index, USER_INTERNAL_MIN + index)`, raises `VlanPoolExhausted` past `USER_ENTRY_MAX - USER_ENTRY_MIN`, and raises `ValueError` for a negative index.
- Produces: `scripts.init_db.import_student_whitelist(csv_path: Optional[str] = None) -> None`. `None` keeps today's search of `data/` and `/app/data/`. It raises `ValueError` when the file holds more students than the pool, before adding any row.
- Produces: `scripts.migrate_add_vlan.assign_vlans_to_users(users) -> None` and `assign_vlans_to_whitelist(entries) -> None`. Both are owner-aware across tables:
  - A record whose login has a row in the *other* table (`User` ↔ `StudentWhitelist`) with both tags set takes that pair, as it is. One login holds one pair; `audit_vlan_pairs.py` reports the pair if it breaks the rule.
  - Any other record takes the lowest index *i* in 0-99 whose `100+i` and `200+i` are both absent from every tag in use anywhere: users, whitelist, groups and non-ended KOTH games (`app/vlan_allocator._used_tags`, `:43-58`), plus the pairs already chosen in this call.
  - Every record's pair is chosen first. If any record has no safe pair, the function raises `VlanPoolExhausted` before setting any attribute. Otherwise it sets them all and commits once.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vlan_allocator.py (module-level pytest functions beside the unittest classes)
def test_user_pools_are_disjoint_and_hold_one_hundred_students():
    assert (USER_ENTRY_MIN, USER_ENTRY_MAX) == (100, 199)
    assert (USER_INTERNAL_MIN, USER_INTERNAL_MAX) == (200, 299)

@pytest.mark.parametrize('index, pair', [(0, (100, 200)), (42, (142, 242)), (99, (199, 299))])
def test_user_pair_for_index_pairs_entry_with_internal(index, pair):
    assert user_pair_for_index(index) == pair

def test_user_pair_for_index_refuses_outside_the_pool():
    with pytest.raises(VlanPoolExhausted):
        user_pair_for_index(100)
    with pytest.raises(ValueError):
        user_pair_for_index(-1)

# In AutoAllocateTestCase:
def test_auto_allocate_user_is_exhausted_after_one_hundred(self):
    for i in range(100):
        db.session.add(StudentWhitelist(login_id=f's{i}', entry_vlan_tag=100 + i,
                                        internal_vlan_tag=200 + i))
    db.session.commit()
    with self.assertRaises(VlanPoolExhausted):
        allocate_pair(scope='user')
```

```python
# tests/test_student_vlan_numbering.py (uses the conftest `app` fixture). Imports: pytest; app.models
# db, StudentWhitelist, User; app.vlan_allocator VlanPoolExhausted; scripts.init_db
# import_student_whitelist; `from scripts import import_students, migrate_add_vlan`;
# app.models StudentGroup, KothGame for the cross-table tests.
def _roster(path, count, start=0):
    rows = ''.join(f'{9000 + n},s{n:03d},Student {n}\n' for n in range(start, start + count))
    path.write_text('SIS User ID,SIS Login ID,Student\n' + rows)
    return str(path)

def test_whitelist_seed_numbers_students_in_file_order(app, tmp_path):
    with app.app_context():
        import_student_whitelist(_roster(tmp_path / 'r.csv', 3))
        rows = StudentWhitelist.query.order_by(StudentWhitelist.entry_vlan_tag).all()
        assert [(r.login_id, r.entry_vlan_tag, r.internal_vlan_tag) for r in rows] == [
            ('s000', 100, 200), ('s001', 101, 201), ('s002', 102, 202)]

def test_whitelist_seed_refuses_a_roster_larger_than_the_pool(app, tmp_path):
    with app.app_context():
        with pytest.raises(ValueError, match='101'):
            import_student_whitelist(_roster(tmp_path / 'r.csv', 101))
        db.session.rollback()
        assert StudentWhitelist.query.count() == 0

def _seed_whitelist(app, count):
    with app.app_context():
        for i in range(count):
            db.session.add(StudentWhitelist(login_id=f'old{i}', entry_vlan_tag=100 + i,
                                            internal_vlan_tag=200 + i))
        db.session.commit()

def test_import_students_persists_the_last_pair(app, tmp_path, monkeypatch):
    monkeypatch.setattr(import_students, 'create_app', lambda: app)
    _seed_whitelist(app, 99)  # 100/200 .. 198/298
    import_students.import_students_from_csv(_roster(tmp_path / 'one.csv', 1, start=500))
    with app.app_context():
        last = StudentWhitelist.query.filter_by(login_id='s500').one()
        assert (last.entry_vlan_tag, last.internal_vlan_tag) == (199, 299)
        assert StudentWhitelist.query.count() == 100

def test_import_students_refuses_an_overflow_atomically(app, tmp_path, monkeypatch):
    monkeypatch.setattr(import_students, 'create_app', lambda: app)
    _seed_whitelist(app, 99)
    with pytest.raises(SystemExit):
        import_students.import_students_from_csv(_roster(tmp_path / 'two.csv', 2, start=600))
    with app.app_context():
        assert StudentWhitelist.query.count() == 99
        assert StudentWhitelist.query.filter_by(login_id='s600').first() is None

def _row(model, login, entry, internal):
    row = model(login_id=login, entry_vlan_tag=entry, internal_vlan_tag=internal)
    if model is User:
        row.set_password('x')
    return row

ASSIGNERS = [(StudentWhitelist, 'assign_vlans_to_whitelist'), (User, 'assign_vlans_to_users')]

@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_numbers_the_last_free_pair(app, model, assign):
    with app.app_context():
        rows = [_row(model, f'n{i}', 100 + i, 200 + i) for i in range(99)]
        late = _row(model, 'late', None, None)
        db.session.add_all(rows + [late])
        db.session.commit()
        getattr(migrate_add_vlan, assign)([late])
        db.session.expire_all()
        late = model.query.filter_by(login_id='late').one()
        assert (late.entry_vlan_tag, late.internal_vlan_tag) == (199, 299)

# Catches the migration committing a partial numbering and leaving the rest unnumbered.
@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_refuses_more_records_than_free_pairs_atomically(app, model, assign):
    with app.app_context():
        rows = [_row(model, f'n{i}', None, None) for i in range(101)]
        db.session.add_all(rows)
        db.session.commit()
        with pytest.raises(VlanPoolExhausted):
            getattr(migrate_add_vlan, assign)(rows)
        db.session.rollback()
        assert model.query.filter(model.entry_vlan_tag.isnot(None)).count() == 0

# Catches one login ending up with two different pairs across the two tables.
@pytest.mark.parametrize('model, other, assign', [
    (User, StudentWhitelist, 'assign_vlans_to_users'),
    (StudentWhitelist, User, 'assign_vlans_to_whitelist')])
def test_migrate_add_vlan_reuses_the_matching_logins_pair(app, model, other, assign):
    with app.app_context():
        db.session.add(_row(other, 'alice', 105, 205))
        target = _row(model, 'alice', None, None)
        db.session.add(target)
        db.session.commit()
        getattr(migrate_add_vlan, assign)([target])
        db.session.expire_all()
        row = model.query.filter_by(login_id='alice').one()
        assert (row.entry_vlan_tag, row.internal_vlan_tag) == (105, 205)

# Catches a pair that is free in its own table but held by another student,
# a group or a live KOTH game.
@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_skips_tags_held_in_other_tables(app, model, assign):
    other = User if model is StudentWhitelist else StudentWhitelist
    with app.app_context():
        db.session.add_all([
            _row(other, 'carol', 100, 200),
            StudentGroup(name='g', display_name='G', entry_vlan_tag=101, internal_vlan_tag=401),
            KothGame(name='k', deployment_group_name='arena', entry_vlan_tag=352,
                     internal_vlan_tag=202, status='draft'),
        ])
        target = _row(model, 'dave', None, None)
        db.session.add(target)
        db.session.commit()
        getattr(migrate_add_vlan, assign)([target])
        db.session.expire_all()
        row = model.query.filter_by(login_id='dave').one()
        assert (row.entry_vlan_tag, row.internal_vlan_tag) == (103, 203)

# Catches the migration taking a pair another table holds once the pool is full.
@pytest.mark.parametrize('model, assign', ASSIGNERS)
def test_migrate_add_vlan_refuses_when_no_globally_safe_pair_exists(app, model, assign):
    other = User if model is StudentWhitelist else StudentWhitelist
    with app.app_context():
        db.session.add_all([_row(other, f'w{i}', 100 + i, 200 + i) for i in range(99)])
        db.session.add(StudentGroup(name='g', display_name='G',
                                    entry_vlan_tag=199, internal_vlan_tag=401))
        target = _row(model, 'late', None, None)
        db.session.add(target)
        db.session.commit()
        with pytest.raises(VlanPoolExhausted):
            getattr(migrate_add_vlan, assign)([target])
        db.session.rollback()
        assert model.query.filter_by(login_id='late').one().entry_vlan_tag is None
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run python -m pytest tests/test_vlan_allocator.py tests/test_student_vlan_numbering.py -q`
Expected: FAIL. `user_pair_for_index` does not exist, `USER_ENTRY_MAX == 200`, `import_student_whitelist` takes no argument, `migrate_add_vlan` numbers all 101 records (the last as `200/300`, through its inclusive old maximum) instead of raising, and it looks only at its own table, so the cross-table tests get a pair another table holds (or, in the reuse test, a different pair).

- [ ] **Step 3: Implement**

- `app/vlan_allocator.py`: set the constants to 199/299 and add `user_pair_for_index` as specified. In the docstring, say that index *i* gets `100+i`/`200+i` and that the pool holds 100 students.
- `scripts/init_db.py`: use `csv_path` when given, instead of the search at `:242-253`. Read every new row first. Number the *k*-th new student with `user_pair_for_index(k)`. On `VlanPoolExhausted`, raise `ValueError(f"{csv_path} has {n} students; the per-student VLAN pool holds {USER_ENTRY_MAX - USER_ENTRY_MIN + 1}")` before calling `db.session.add`. Delete the offset arithmetic at `:259-261`: the function returns at `:238-240` whenever a row exists, so that offset is always 0. A `ValueError` is re-raised as a configuration error at `:55-59`, so the container stops with the message instead of retrying.
- `scripts/import_students.py:106-109`: assign `user_pair_for_index((max_entry or 99) - 99)`. On `VlanPoolExhausted`, call `db.session.rollback()` and `sys.exit("Error: ... nothing from this file was imported")`.
- `scripts/migrate_add_vlan.py`: import the four `USER_*` values from `app.vlan_allocator` under the script's existing names (`:24-28`). Rewrite `assign_vlans_to_users` and `assign_vlans_to_whitelist` (`:182-249`) to the Interfaces contract, sharing one helper. Today's loops look only at their own table's entry tags, then warn and `break` after committing earlier assignments.
- Delete `app/routes/utils.py:23-27`.
- Change the range text in `app/config.py` and `app/models.py` to 100-199 and 200-299. In the model comments, give the networks: entry `10.<N>.0.0/24`, internal `10.<N-100>.1.0/24`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run python -m pytest tests/test_vlan_allocator.py tests/test_student_vlan_numbering.py tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/vlan_allocator.py app/routes/utils.py app/config.py app/models.py scripts/init_db.py scripts/import_students.py scripts/migrate_add_vlan.py tests/test_vlan_allocator.py tests/test_student_vlan_numbering.py
git commit -m "Per-student VLAN pools end at 199/299; importers refuse a 101st student"
```

---

### Task 2: The changeover refuses rosters and existing VLAN pairs the bands cannot serve

**Files:**
- Modify: `scripts/build_students_merged.py:37-61` (add `ROSTER_MAX`; check it in `read_canvas`)
- Create: `scripts/audit_vlan_pairs.py` (stdlib only; runs on the operator's workstation)
- Modify: `docs/TERM_CHANGEOVER.md:9-19` (two "Before you start" items)
- Test: `tests/test_ops_scripts.py` (after `:740`), `tests/test_vlan_allocator.py`; create `tests/test_audit_vlan_pairs.py`

**Interfaces:**
- Consumes: `USER_ENTRY_MIN/MAX`, `USER_INTERNAL_MIN/MAX` (Task 1).
- Produces: `scripts.build_students_merged.ROSTER_MAX = 100`. When a Canvas export holds more than `ROSTER_MAX` real students, `read_canvas(path)` exits with `Error: <path> has <n> students; the portal numbers at most 100 (one VLAN pair each)`. Stage 1 of `scripts/term_changeover.sh` calls it through `HEADSCALE_SUMMARY_PY` (`:140-180`) before any backup, purge or restart. The script stays stdlib-only, so the value is a literal, pinned by a test.
- Produces: `scripts/audit_vlan_pairs.py` with `USER_ENTRY_FIRST, USER_ENTRY_LAST, USER_INTERNAL_OFFSET = 100, 199, 100` (literals pinned to `app/vlan_allocator.py` by a test) and `audit(slots: Mapping[str, Any]) -> List[str]`. `slots` is `scripts/export_network_slots.py` output. It returns sorted problem lines:
  - `<login>: pair <entry>/<internal> is outside entry 100-199 with internal = entry + 100` for each user breaking `100 <= entry <= 199 and internal == entry + 100`;
  - `VLAN <tag> is held by <owner> and <owner>` for every tag held more than once across users' entry and internal tags, groups' (`group <slug>`) and exported KOTH games' (`KOTH game <id>`) tags.

  The CLI `python3 scripts/audit_vlan_pairs.py SLOTS.json|-` prints each problem and exits 1, or prints `ok: <u> users, <g> groups, <k> KOTH games` and exits 0.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ops_scripts.py
# Catches a roster too large to number reaching the purge.
def test_term_changeover_refuses_a_roster_larger_than_the_vlan_pool(tmp_path):
    repo, env, config, roster = _term_rig(tmp_path)
    roster.write_text("SIS User ID,SIS Login ID\n"
                      + "".join(f"{1000 + i},student{i}\n" for i in range(101)))
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "term_changeover.sh"), "--apply", str(roster)],
        env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "101 students" in result.stderr and "at most 100" in result.stderr
    log = Path(env["EVENT_LOG"])
    events = log.read_text().splitlines() if log.exists() else []
    assert not {"DB BACKUP", "PURGE", "PORTAL RESTART"} & set(events)
```

```python
# tests/test_vlan_allocator.py
def test_changeover_roster_cap_equals_the_user_vlan_pool():
    from scripts.build_students_merged import ROSTER_MAX
    assert ROSTER_MAX == USER_ENTRY_MAX - USER_ENTRY_MIN + 1 \
        == USER_INTERNAL_MAX - USER_INTERNAL_MIN + 1 == 100
```

```python
# tests/test_audit_vlan_pairs.py. Imports: json, subprocess, sys, pytest, pathlib.Path
# (ROOT = repo root); from scripts.audit_vlan_pairs import USER_ENTRY_FIRST,
# USER_ENTRY_LAST, USER_INTERNAL_OFFSET, audit.
def _slots(users=(), groups=(), koth=()):
    return {"config": {}, "users": list(users), "groups": list(groups), "koth": list(koth)}

def _user(login, entry, internal):
    return {"login": login, "entry_vlan": entry, "internal_vlan": internal}

def test_a_clean_export_has_no_problems():
    assert audit(_slots([_user("alice", 100, 200), _user("bob", 199, 299)],
                        [{"slug": "red", "members": ["alice"], "entry_vlan": 300, "internal_vlan": 400}],
                        [{"game_id": 1, "entry_vlan": 350, "internal_vlan": 450}])) == []

# Catches a pair the old importers numbered past the 100th student.
def test_a_legacy_200_300_pair_is_reported():
    problems = audit(_slots([_user("alice", 100, 200), _user("zed", 200, 300)]))
    assert any("zed" in p and "200/300" in p for p in problems)
    assert any("VLAN 200" in p and "alice" in p and "zed" in p for p in problems)

@pytest.mark.parametrize("entry, internal", [(99, 199), (150, 251), (150, 150)])
def test_a_pair_outside_the_rule_is_reported(entry, internal):
    assert audit(_slots([_user("eve", entry, internal)]))

# Catches one tag held by owners of different kinds.
@pytest.mark.parametrize("groups, koth", [
    ([{"slug": "red", "members": [], "entry_vlan": 200, "internal_vlan": 400}], []),
    ([], [{"game_id": 7, "entry_vlan": 100, "internal_vlan": 450}]),
])
def test_cross_kind_collisions_are_reported(groups, koth):
    assert any(p.startswith("VLAN ") for p in audit(_slots([_user("alice", 100, 200)], groups, koth)))

def test_cli_exits_nonzero_and_names_each_problem(tmp_path):
    path = tmp_path / "slots.json"
    path.write_text(json.dumps(_slots([_user("zed", 200, 300)])))
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_vlan_pairs.py"), str(path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "zed" in result.stdout

def test_audit_pool_matches_the_allocator():
    from app.vlan_allocator import USER_ENTRY_MAX, USER_ENTRY_MIN, USER_INTERNAL_MIN
    assert (USER_ENTRY_FIRST, USER_ENTRY_LAST, USER_INTERNAL_OFFSET) == (
        USER_ENTRY_MIN, USER_ENTRY_MAX, USER_INTERNAL_MIN - USER_ENTRY_MIN)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run python -m pytest tests/test_audit_vlan_pairs.py "tests/test_ops_scripts.py::test_term_changeover_refuses_a_roster_larger_than_the_vlan_pool" "tests/test_vlan_allocator.py::test_changeover_roster_cap_equals_the_user_vlan_pool" -q`
Expected: FAIL. `ROSTER_MAX` and `scripts/audit_vlan_pairs.py` are missing, and the 101-row roster gets past stage 1. (Node IDs, not two `-k` options: pytest keeps only the last `-k`.)

- [ ] **Step 3: Implement**

Add `ROSTER_MAX = 100` beside `FIELDNAMES`, with a comment naming `app/vlan_allocator.py`. In `read_canvas`, after the loop, `sys.exit(...)` with the message above when `len(students) > ROSTER_MAX`. Write `scripts/audit_vlan_pairs.py` to the Interfaces contract; it reads only its argument (a file, or `-` for stdin). Add to `docs/TERM_CHANGEOVER.md` "Before you start":
- The roster holds at most 100 students, because each student gets one VLAN pair; the script refuses a larger one before changing anything.
- After a seed, `docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py | python3 scripts/audit_vlan_pairs.py -` must print `ok`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run python -m pytest tests/test_audit_vlan_pairs.py tests/test_ops_scripts.py tests/test_vlan_allocator.py -q`
Expected: PASS, including every existing changeover test.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_students_merged.py scripts/audit_vlan_pairs.py docs/TERM_CHANGEOVER.md tests/test_ops_scripts.py tests/test_vlan_allocator.py tests/test_audit_vlan_pairs.py
git commit -m "Changeover refuses an unnumberable roster; add the VLAN pair audit"
```

---

### Task 3: The jumpbox serves the per-student internal band

**Files:**
- Modify: `scripts/jumpbox_netgen.py:13-23` (docstring), `:35-39` (`DEFAULT_BANDS`)
- Modify: `app/config.py:207-211` (`JUMPBOX_VLAN_BANDS` default)
- Modify: `.env.example:123-127`, `scripts/site.env.example:52-54`
- Modify: `docs/JUMPBOX.md:13-22` (diagram: add `vlan200 10.100.1.1/24 ─ student internal network`), `:37-58` (address plan)
- Test: `tests/test_jumpbox_scripts.py:93-131`, `:144-180`; `tests/test_config.py:332-333`; `tests/backends/test_network_slots.py:9`, `:35-41`
- Update band fixtures to the new string: `tests/test_headscale_policy.py:25`, `tests/test_export_network_slots.py:12`, `tests/test_ops_scripts.py:305`. In `tests/test_headscale_policy.py:126` and `:264`, move carol's tags to 600/601: 250/260 are routed now, and both tests need a login with nothing routed.

**Interfaces:**
- Consumes: `vlan_allocator._pool_for("user")` returning `((100, 199), (200, 299))` (Task 1).
- Produces: `jumpbox_netgen.DEFAULT_BANDS` and the default `app.config.JUMPBOX_VLAN_BANDS`, both equal to the Global Constraints string. The new band is named `internal`; `JUMPBOX_DHCP_RESERVATIONS` refers to bands by name.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_jumpbox_scripts.py: replace the address-plan cases (:93-114)
@pytest.mark.parametrize("tag, gateway, first, last", [
    (100, "10.100.0.1/24", "10.100.0.50", "10.100.0.254"),
    (199, "10.199.0.1/24", "10.199.0.50", "10.199.0.254"),
    (200, "10.100.1.1/24", "10.100.1.50", "10.100.1.254"),
    (299, "10.199.1.1/24", "10.199.1.50", "10.199.1.254"),
    (300, "10.100.2.1/24", "10.100.2.50", "10.100.2.254"),
    (399, "10.199.2.1/24", "10.199.2.50", "10.199.2.254"),
    (400, "10.100.4.1/24", "10.100.4.50", "10.100.4.254"),
    (500, "10.200.4.1/24", "10.200.4.50", "10.200.4.254"),
])
# ... and the outside-the-bands cases become [99, 501, 600].

# Replaces test_jumpbox_routes_every_vlan_the_portal_hands_out_for_entry_and_group_networks:
# the user internal pool joins the set that must be served.
def test_jumpbox_routes_every_vlan_the_portal_hands_out(): ...

# Catches VLAN 200 (or any tag) belonging to the entry and internal bands at once.
def test_each_student_pool_sits_in_its_own_band():
    from app import vlan_allocator
    ng = _netgen()
    bands = ng.parse_bands(ng.DEFAULT_BANDS)
    (e_lo, e_hi), (i_lo, i_hi) = vlan_allocator._pool_for("user")
    assert {ng._band_for(t, bands).name for t in range(e_lo, e_hi + 1)} == {"entry"}
    assert {ng._band_for(t, bands).name for t in range(i_lo, i_hi + 1)} == {"internal"}
    assert (ng.gateway(142, bands), ng.gateway(242, bands)) == ("10.142.0.1/24", "10.142.1.1/24")

# In the netplan test (:144-158):
    assert vlans["vlan200"] == {"id": 200, "link": "ens19", "addresses": ["10.100.1.1/24"]}
# In the dnsmasq test (:161-180):
    assert "dhcp-range=10.100.1.50,10.100.1.254,255.255.255.0,12h" in ranges
    assert not any(r.startswith("dhcp-range=10.200.0.") for r in ranges)
```

```python
# tests/test_config.py:332-333: load scripts/jumpbox_netgen.py by path, as
# tests/test_jumpbox_scripts.py:36-40 does
    assert cfg.JUMPBOX_VLAN_BANDS.split() == _netgen().DEFAULT_BANDS.split() == [
        "entry:100-199:0:0", "internal:200-299:100:1",
        "group-entry:300-399:200:2", "group-internal:400-500:300:4"]

# tests/backends/test_network_slots.py:35-41
    assert proxmox_slot_cidr(199, bands) == '10.199.0.0/24'
    assert proxmox_slot_cidr(200, bands) == '10.100.1.0/24'
    assert proxmox_slot_cidr(250, bands) == '10.150.1.0/24'
    assert proxmox_slot_cidr(600, bands) is None
    assert routed_slots([600, 400, 250, 100], bands) == [100, 250, 400]
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run python -m pytest tests/test_jumpbox_scripts.py tests/test_config.py tests/backends/test_network_slots.py -q`
Expected: FAIL. `gateway(200)` is `10.200.0.1/24`, and there is no `internal` band.

- [ ] **Step 3: Implement**

Set the band string in `jumpbox_netgen.DEFAULT_BANDS`, in the `app/config.py` default, and in both example files. In the netgen docstring, replace "Per-student internal VLANs (200-300) are not served" with the `internal` row. `docs/JUMPBOX.md` address plan:
- Table rows: `entry` 100–199 `10.<N>.0.0/24`; `internal` 200–299 `10.<N-100>.1.0/24` (per-student internal VLAN, `app/vlan_allocator.py`); the group rows unchanged.
- Delete the sentence saying per-student internal VLANs are not served.
- Add one sentence: a student's two networks share the second octet.

`scripts/jumpbox_configure.sh` needs no change: it passes `JUMPBOX_VLAN_BANDS` through (`:219-229`), and an empty value takes the netgen default.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run python -m pytest tests/test_jumpbox_scripts.py tests/test_config.py tests/backends/test_network_slots.py tests/test_headscale_policy.py tests/test_export_network_slots.py tests/test_ops_scripts.py -q`
Expected: PASS. The policy tests still pass, because Task 4 has not changed what is granted yet.

- [ ] **Step 5: Commit**

```bash
git add scripts/jumpbox_netgen.py app/config.py .env.example scripts/site.env.example docs/JUMPBOX.md tests/test_jumpbox_scripts.py tests/test_config.py tests/backends/test_network_slots.py tests/test_headscale_policy.py tests/test_export_network_slots.py tests/test_ops_scripts.py
git commit -m "Jumpbox: serve per-student internal VLANs 200-299; entry band ends at 199"
```

---

### Task 4: The Headscale policy grants each student their own internal VLAN

**Files:**
- Modify: `scripts/headscale_policy.py:16-20` (docstring), `:147-155` (grant seed)
- Modify: `README.md:420-429`, `docs/JUMPBOX.md:221-224`, `docs/CLOUD_BACKENDS.md:1055-1061` ("its own entry slot" becomes "its own entry and internal slots")
- Test: `tests/test_headscale_policy.py:44-76`, `:250-259`, `:276-294`, plus one new test

**Interfaces:**
- Consumes: the Task 3 band string (fixture `DEFAULT_BANDS` in `tests/test_headscale_policy.py`).
- Produces: `render(base_policy, slots, bands, cloud_cidrs) -> Dict[str, Any]` with an unchanged signature. Each login's grant set starts as `{own entry, own internal}` instead of `{own entry}`. Everything after it is unchanged: group pairs, KOTH (forward only), the `routed_slots` gate on every backend, `claim()` uniqueness, and the cut-down `autogroup:member` rule.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_headscale_policy.py:44-49
# Own entry 100 and internal 200 plus the group's 300/400.
EXPECTED_ALICE_CIDRS = [
    "10.100.0.0/24", "10.100.1.0/24", "10.100.2.0/24", "10.100.4.0/24",
    "172.20.25.0/26", "172.20.50.0/26", "172.20.75.0/26", "172.20.100.0/26",
    "172.21.25.0/26", "172.21.50.0/26", "172.21.75.0/26", "172.21.100.0/26",
]
# :75-76 become (the reverse-rule assertion at :70-72 now also proves her own
# machines may connect back to her devices, which coercion and relay need):
    assert not {"10.101.0.0/24:*", "10.101.1.0/24:*"} & set(alice_forward["dst"])
    assert "10.200.0.0/24" not in json.dumps(policy)

# :256-259
    assert _forward(policy, "alice@")["dst"] == [
        "10.100.0.0/24:*", "10.100.1.0/24:*", "10.100.2.0/24:*", "10.100.4.0/24:*"]
    assert _forward(policy, "bob@")["dst"] == [
        "10.101.0.0/24:*", "10.101.1.0/24:*", "10.101.2.0/24:*", "10.101.4.0/24:*"]

# Two new rows in test_invalid_or_overlapping_inputs_are_refused (:276-294):
    (_with(users=[{"login": "alice", "entry_vlan": 100, "internal_vlan": 200},
                  {"login": "bob", "entry_vlan": 101, "internal_vlan": 200}], groups=[]), {}),
    (_with(users=[{"login": "alice", "entry_vlan": 100, "internal_vlan": 200},
                  {"login": "bob", "entry_vlan": 200, "internal_vlan": 201}], groups=[]), {}),

# Catches an internal VLAN granted on any backend where no band routes it.
def test_internal_vlan_outside_every_band_is_not_granted():
    bands = PolicyBands(tuple(parse_vlan_bands(
        "entry:100-199:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4")),
        proxmox_enabled=True)
    dst = set(_forward(render(BASE, SLOTS, bands, CLOUDS), "alice@")["dst"])
    assert "10.100.0.0/24:*" in dst
    assert not {"10.100.1.0/24:*", "172.20.50.0/26:*", "172.21.50.0/26:*"} & dst
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run python -m pytest tests/test_headscale_policy.py -q`
Expected: FAIL. `10.100.1.0/24` and `172.20.50.0/26` are missing from Alice's rules. The two new invalid rows and the out-of-band test already pass (`claim()`, `routed_slots`); keep them as guards.

- [ ] **Step 3: Implement**

At `scripts/headscale_policy.py:147-155`, keep the internal tag that `claim()` returns and seed `granted[identity]` with both tags. In the docstring (`:16-20`), "one rule to its own entry network" becomes "its own entry and internal networks". Make the same wording change in the three docs passages listed in Files.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run python -m pytest tests/test_headscale_policy.py tests/test_export_network_slots.py tests/test_ops_scripts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/headscale_policy.py README.md docs/JUMPBOX.md docs/CLOUD_BACKENDS.md tests/test_headscale_policy.py
git commit -m "Headscale policy: grant each student their own internal VLAN (supersedes I12)"
```

---

### Task 5: Single-homed machines in two-machine individual deployments

No production code changes (decision 2 above). This task pins the behaviour and documents the template rules.

**Files:**
- Test: `tests/backends/test_vm_provisioning.py` (after `:749`), `tests/test_templates_config.py`
- Modify: `templates.example.yaml` (append a disabled group `two-machine-individual-example`: two Proxmox machines with placeholder VMIDs `7103/7203` and `7104/7204`, the first `entry_point: true` with no `internal_nic` key, the second plain)
- Modify: `README.md:348-376`. Add `internal_nic: false` to the single-machine example and state the NIC rules: entry point = entry VLAN plus an internal NIC unless `internal_nic: false`; plain machine = one NIC on the internal VLAN; the portal rewrites `net0` and deletes `net1`, so build templates with one NIC. Change `:38` and `:386` to 100-199 and 200-299.
- Modify: `VLAN_MIGRATION_GUIDE.md:5-23`, `:28-29`, `:40-43`, `:75`. Correct the ranges and both layouts to match `vm_creation.py:177-180` and `:225-246`: entry point `net0` = entry and `net1` = internal; a plain machine has `net0` = internal only. Correct the IP display to match `app/routes/vm_listing.py:264-277`: non-entry machines show their addresses too.

**Interfaces:**
- Consumes: `_deploy`, `_qemu` and the `proxmox_portal` fixture (`tests/backends/test_vm_provisioning.py:534-572`).
- Produces: the example group name `two-machine-individual-example`.

- [ ] **Step 1: Write the tests**

```python
# tests/backends/test_vm_provisioning.py
# Catches the plain machine of a two-machine individual lab (an AD DC) gaining
# a second NIC, whatever order the templates are listed in.
@pytest.mark.parametrize('order', [('srv01', 'dc01'), ('dc01', 'srv01')])
def test_proxmox_two_machine_bundle_keeps_the_plain_machine_single_homed(
        app, proxmox_portal, order):
    machines = {'srv01': _qemu('srv01', entry_point=True), 'dc01': _qemu('dc01')}
    records = _deploy(app, [machines[name] for name in order])
    nics = {r.is_entry_point: proxmox_portal.guests[r.vmid]['config'] for r in records}
    assert (nics[True].get('net0'), nics[True].get('net1')) == (
        'virtio,bridge=vmbr0,tag=101', 'virtio,bridge=vmbr0,tag=201')
    assert (nics[False].get('net0'), nics[False].get('net1'), nics[False].get('delete')) == (
        'virtio,bridge=vmbr0,tag=201', None, 'net1')
```

```python
# tests/test_templates_config.py
# Catches a documented single-machine lab keeping a second NIC, which the served
# internal VLANs turn into a second lease and a second address.
@pytest.mark.parametrize("catalog_path", CATALOG_PATHS, ids=["test-fixture", "example"])
def test_single_machine_individual_labs_are_single_homed(catalog_path):
    for group in yaml.safe_load(catalog_path.read_text())["deployment_groups"]:
        templates = group.get("templates") or []
        if not group.get("group_mode") and len(templates) == 1 and templates[0].get("entry_point"):
            assert templates[0].get("internal_nic") is False, group["display_name"]

def test_example_two_machine_lab_has_one_entry_point_and_one_plain_machine():
    catalog = yaml.safe_load((REPO_ROOT / "templates.example.yaml").read_text())
    group = next(g for g in catalog["deployment_groups"]
                 if g["display_name"] == "two-machine-individual-example")
    assert group["enabled"] is False and not group.get("group_mode", False)
    entry = [t for t in group["templates"] if t.get("entry_point")]
    plain = [t for t in group["templates"] if not t.get("entry_point")]
    assert len(entry) == 1 and "internal_nic" not in entry[0]
    assert len(plain) == 1 and "internal_nic" not in plain[0]
```

- [ ] **Step 2: Run the tests**

Run: `uv run python -m pytest "tests/backends/test_vm_provisioning.py::test_proxmox_two_machine_bundle_keeps_the_plain_machine_single_homed" tests/test_templates_config.py -q`
Expected:
- The provisioning test PASSES, because it pins existing behaviour.
- `test_example_two_machine_lab_...` FAILS, because the group does not exist yet.

- [ ] **Step 3: Prove the provisioning test can go red**

Temporarily delete only the line `proxmox.nodes(target_node).qemu(vmid).config.put(delete="net1")` at `app/routes/vm_creation.py:241`, leaving the `net0` statements at `:243-246` in place. Run the provisioning test. Expected: FAIL on the plain machine's tuple assertion, which gets `None` where it expects `'net1'` for `delete`. Restore the line, then confirm `git diff app/` is empty.

- [ ] **Step 4: Add the example group and the documentation listed in Files**

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run python -m pytest tests/backends/test_vm_provisioning.py tests/test_templates_config.py tests/test_cloud_docs.py -q`
Expected: PASS. `validate_template_config` still returns `([], [])` for the example (`tests/test_templates_config.py:21-29`).

- [ ] **Step 6: Commit**

```bash
git add tests/backends/test_vm_provisioning.py tests/test_templates_config.py templates.example.yaml README.md VLAN_MIGRATION_GUIDE.md
git commit -m "Pin single-homed plain machines in two-machine individual labs; document NIC rules"
```

---

### Task 6: `LAB_BRIDGE` and `VMID_RANGE`: a portal beside production on an isolated bridge

Unset, both settings must leave behaviour unchanged. Every existing NIC and allocator test keeps passing unedited, and that is the proof.

**Files:**
- Modify: `app/config.py:140-146` (add `LAB_BRIDGE`, `parse_vmid_range`, `VMID_RANGE` and `VMID_RANGE_ERROR` directly after `_env_optional_int`, because `_env_text` at `:135` must already be defined), `:483-498` (`_backend_errors`)
- Modify: `app/routes/vm_creation.py:162-246` (`create_lxc_container`, `create_qemu_vm`: the five `bridge=vmbr0` literals at `:177-180`, `:229-244`)
- Modify: `app/proxmox_service.py:639-711` (`get_next_available_vmid`); add `VMIDRangeExhausted`
- Modify: `docker-compose.prod.yml:32` and `docker-compose.yml:25` (pass `LAB_BRIDGE=${LAB_BRIDGE:-}` and `VMID_RANGE=${VMID_RANGE:-}` after `FIXED_NODE`)
- Modify: `.env.example:98-102` (both settings, empty, with one comment each); `README.md:305` (two table rows); `docs/LOCAL_TEST_INSTANCE.md:35-37` (the VMIDs bullet names `VMID_RANGE` and its rule below)
- Test: `tests/test_config.py` (add both names to `BACKEND_ENV_KEYS`, `:257-265`), `tests/backends/test_vm_provisioning.py`, `tests/test_vmid_allocator.py`

**Interfaces:**
- Produces: `app.config.LAB_BRIDGE: str`, which is `_env_text('LAB_BRIDGE', 'vmbr0')`.
- Produces: `app.config.parse_vmid_range(text: str) -> Optional[Tuple[int, int]]`. Blank text gives `None`. Otherwise the stripped text must match `^(\d+)-(\d+)$` with `100 <= min <= max <= 999999999`, or it raises `ValueError`.
- Produces: `app.config.VMID_RANGE: Optional[Tuple[int, int]]` and `app.config.VMID_RANGE_ERROR: Optional[str]`, both parsed at import. An invalid value leaves `VMID_RANGE = None` and keeps the message in `VMID_RANGE_ERROR`.
- `_backend_errors` reports two new problems:
  - `VMID_RANGE_ERROR`, always;
  - with Proxmox enabled, a `LAB_BRIDGE` not matching `^[A-Za-z][A-Za-z0-9_.-]{0,14}$`. The value is spliced into Proxmox `netN` strings, where a comma would add options.
- `create_lxc_container` and `create_qemu_vm` keep their signatures. Each reads `importlib.import_module('app.config').LAB_BRIDGE` per call (`CLAUDE.md` rule 14), so tests monkeypatch `app.config`.
- Produces: `app.proxmox_service.VMIDRangeExhausted(RuntimeError)`. `get_next_available_vmid` keeps its signature and reads `VMID_RANGE` and `VMID_RANGE_ERROR` per call.
  - It raises `ValueError` if `VMID_RANGE_ERROR` is set.
  - With `VMID_RANGE = (lo, hi)`, every source of the high-water value (cluster inventory, stored mark, `VMRecord` maximum, reservation maximum) counts only values inside `[lo, hi]`, and `base = max(lo - 1, those) + 1`. It raises `VMIDRangeExhausted` before reserving anything when `base + count - 1 > hi`.
  - The stored mark becomes `max(previous mark, base + count - 1)` in both modes. Unset, that equals today's write, because `base` is already above the mark.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
# Catches a staging-only setting changing what production does when unset.
def test_lab_bridge_and_vmid_range_defaults_are_todays_behaviour(monkeypatch):
    cfg = reload_config(monkeypatch)
    assert (cfg.LAB_BRIDGE, cfg.VMID_RANGE, cfg.VMID_RANGE_ERROR) == ('vmbr0', None, None)

@pytest.mark.parametrize('text, parsed', [('', None), ('7000-7999', (7000, 7999)), (' 100-100 ', (100, 100))])
def test_parse_vmid_range_accepts_min_dash_max(monkeypatch, text, parsed):
    assert reload_config(monkeypatch).parse_vmid_range(text) == parsed

@pytest.mark.parametrize('text', ['7000', '7999-7000', '99-200', '7000-1000000000', 'a-b', '7000-7999,8000-8999'])
def test_parse_vmid_range_rejects_anything_else(monkeypatch, text):
    with pytest.raises(ValueError):
        reload_config(monkeypatch).parse_vmid_range(text)

def test_invalid_vmid_range_is_refused_at_startup(monkeypatch):
    cfg = reload_config(monkeypatch, VMID_RANGE='7999-7000')
    assert cfg.VMID_RANGE is None and 'VMID_RANGE' in cfg.VMID_RANGE_ERROR
    with pytest.raises(ValueError, match='VMID_RANGE'):
        cfg.validate_config()

# Catches a bridge value that injects options into a Proxmox net string.
@pytest.mark.parametrize('bridge', ['vmbr9,firewall=0', 'vmbr 9', '9vmbr', 'a' * 16])
def test_invalid_lab_bridge_is_refused_at_startup(monkeypatch, bridge):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS='proxmox', LAB_BRIDGE=bridge)
    with pytest.raises(ValueError, match='LAB_BRIDGE'):
        cfg.validate_config()
```

```python
# tests/backends/test_vm_provisioning.py
# Catches a NIC left on vmbr0 when the portal runs on an isolated bridge.
def test_lab_bridge_replaces_vmbr0_on_every_nic(app, proxmox_portal, monkeypatch):
    monkeypatch.setattr(config, 'LAB_BRIDGE', 'vmbr9')
    records = _deploy(app, [_qemu('srv01', entry_point=True), _qemu('dc01'), _lxc('ct')])
    nets = [value for r in records
            for key, value in proxmox_portal.guests[r.vmid]['config'].items() if key.startswith('net')]
    assert len(nets) == 4
    assert all('bridge=vmbr9' in v for v in nets) and not any('vmbr0' in v for v in nets)
```

```python
# tests/test_vmid_allocator.py (module-level, beside :445-466). Add `import importlib`, `import pytest`
# and `config = importlib.import_module('app.config')` to the module imports.
def _get(proxmox, count, rid):
    return proxmox_service.get_next_available_vmid(proxmox, count=count, reservation_id=rid)

# Catches a staging portal following production's VMIDs upward.
def test_range_ignores_cluster_guests_above_it(app, monkeypatch):
    proxmox = ContractFakeProxmox()
    proxmox.add_guest(9500, 'production-guest')
    proxmox.add_guest(7004, 'in-range-guest')
    monkeypatch.setattr(config, 'VMID_RANGE', (7000, 7999))
    with app.app_context():
        assert (_get(proxmox, 2, 'r1'), _get(proxmox, 1, 'r2')) == (7005, 7007)

def test_range_starts_at_its_minimum_whatever_the_database_holds(app, monkeypatch):
    monkeypatch.setattr(config, 'VMID_RANGE', (7000, 7999))
    with app.app_context():
        db.session.add(VMRecord(vmid=9000, vm_name='student-x-vm', vm_type='qemu', node='n',
                                template_name='Lab/vm', student_name='x',
                                expires_at=datetime.now() + timedelta(hours=1)))
        db.session.commit()
        assert _get(ContractFakeProxmox(), 1, 'r') == 7000

# Catches a range silently spilling past its maximum.
def test_range_refuses_when_exhausted_and_reserves_nothing(app, monkeypatch):
    proxmox = ContractFakeProxmox()
    proxmox.add_guest(7001, 'in-range-guest')
    monkeypatch.setattr(config, 'VMID_RANGE', (7000, 7002))
    with app.app_context():
        with pytest.raises(proxmox_service.VMIDRangeExhausted):
            _get(proxmox, 2, 'r')
        assert VMIDReservation.query.count() == 0

# Catches range mode lowering the high-water mark an unranged portal relies on.
def test_range_never_lowers_the_stored_high_water_mark(app, monkeypatch):
    proxmox = ContractFakeProxmox()
    proxmox.add_guest(9600, 'guest')
    with app.app_context():
        assert _get(proxmox, 1, 'a') == 9601
        proxmox_service.release_vmid_reservation('a')
        monkeypatch.setattr(config, 'VMID_RANGE', (7000, 7999))
        assert _get(ContractFakeProxmox(), 1, 'b') == 7000
        proxmox_service.release_vmid_reservation('b')
        monkeypatch.setattr(config, 'VMID_RANGE', None)
        assert _get(ContractFakeProxmox(), 1, 'c') == 9602

def test_an_invalid_range_refuses_to_allocate(app, monkeypatch):
    monkeypatch.setattr(config, 'VMID_RANGE_ERROR', 'VMID_RANGE must be <min>-<max>')
    with app.app_context(), pytest.raises(ValueError):
        _get(ContractFakeProxmox(), 1, 'r')
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run python -m pytest tests/test_config.py tests/backends/test_vm_provisioning.py tests/test_vmid_allocator.py -q`
Expected: FAIL. `LAB_BRIDGE`, `VMID_RANGE`, `parse_vmid_range` and `VMIDRangeExhausted` do not exist. Every pre-existing test in these files still passes.

- [ ] **Step 3: Implement to the Interfaces block**

- In `get_next_available_vmid`, clip inside `allocate(conn)` (`:675-706`), so the range check and the insert share the locked transaction.
- In `README.md`, the `VMID_RANGE` row and the `.env.example` comment both carry this rule: *choose a range below the cluster's highest VMID; a guest above another portal's high-water mark raises that portal's next VMIDs for good* (`proxmox_service.py:664-692`).
- The `LAB_BRIDGE` row says: "Proxmox bridge for every lab NIC; default `vmbr0`; only a staging portal on an isolated bridge changes it (`docs/STAGING.md`)".

- [ ] **Step 4: Run the tests and confirm they pass, then the full suite**

Run: `uv run python -m pytest tests/test_config.py tests/backends/test_vm_provisioning.py tests/test_vmid_allocator.py -q && uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS, with no existing test edited apart from the `BACKEND_ENV_KEYS` addition.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/routes/vm_creation.py app/proxmox_service.py docker-compose.prod.yml docker-compose.yml .env.example README.md docs/LOCAL_TEST_INSTANCE.md tests/test_config.py tests/backends/test_vm_provisioning.py tests/test_vmid_allocator.py
git commit -m "LAB_BRIDGE and VMID_RANGE: run a portal on an isolated bridge in its own VMID range"
```

---

### Task 7: The permanent staging stack and the two-client acceptance test

Steps 1-5 are done by the implementer. Step 6 builds the stack once. Step 7 is the acceptance test, which can be re-run at any time; **a pass at the PR head gates the merge (Task 8 Step 4)**. None of these steps touches production. The stack is permanent: Step 3's "Stop, start, reset, remove" section is how it is retained.

**Files:**
- Create: `docs/STAGING.md` (the runbook; placeholders only, `CLAUDE.md` rule 13)
- Modify: `docs/LOCAL_TEST_INSTANCE.md:1-10` (one sentence pointing at `docs/STAGING.md` for the permanent staging portal), `docs/JUMPBOX.md` (one line under "1. Create the VM": a staging jumpbox puts `JUMPBOX_TRUNK_BRIDGE` on an isolated bridge, see `docs/STAGING.md`)
- Test: `tests/test_cloud_docs.py`

**Interfaces:**
- Consumes: `LAB_BRIDGE` and `VMID_RANGE` (Task 6); the band string (Task 3); the internal grant (Task 4); `jumpbox_provision.sh` (`JUMPBOX_BRIDGE`, `JUMPBOX_MGMT_VLAN`, `JUMPBOX_TRUNK_BRIDGE`, `:31-45`); `apply_headscale_policy.sh` through `SITE_ENV_FILE` (`:10`).
- Produces: `docs/STAGING.md` with the sections 1-7 listed in Step 3, and the placeholder names from decision 6.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cloud_docs.py
STAGING = ROOT / "docs" / "STAGING.md"

# Catches the staging runbook losing a setting that keeps staging off production.
def test_staging_runbook_keeps_its_safety_settings_and_placeholders():
    text = STAGING.read_text()
    for required in ("ORPHAN_REAPER_ENABLED=false", "FIXED_NODE=<STAGING_NODE>",
                     "LAB_BRIDGE=<STAGING_BRIDGE>", "VMID_RANGE=<STAGING_VMID_RANGE>",
                     "bridge-ports none", "--accept-routes", "SITE_ENV_FILE="):
        assert required in text, required
    assert not REAL_ID_PATTERNS.search(text)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run python -m pytest "tests/test_cloud_docs.py::test_staging_runbook_keeps_its_safety_settings_and_placeholders" -q`
Expected: FAIL, `docs/STAGING.md` does not exist.

- [ ] **Step 3: Write `docs/STAGING.md`**

It holds these sections. Commands are given exactly; prose is the implementer's.

1. **Values.** A table of the decision 6 placeholders, plus `<STAGING_VPN_ADDR>` (the VPN VM's DHCP address), `<STORAGE>` and `<CLOUD_IMAGE>`. It notes that the real values live in the operator's notes, never in the repo.
2. **The isolated bridge**, on `<STAGING_NODE>` as root:
   ```bash
   cp -a /etc/network/interfaces /root/interfaces.before-staging-bridge
   ip -br link | grep -E '^vmbr0' > /root/vmbr0-link.before; ip -br addr | grep -E '^vmbr0' > /root/vmbr0-addr.before
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
   ifreload -a
   diff <(ip -br link | grep -E '^vmbr0') /root/vmbr0-link.before \
     && diff <(ip -br addr | grep -E '^vmbr0') /root/vmbr0-addr.before && echo vmbr0-unchanged
   ip -br link show <STAGING_BRIDGE>; bridge link show | grep -c 'master <STAGING_BRIDGE>'
   ```
   Expected: `vmbr0-unchanged`, the bridge listed, and `0` member ports before any staging VM starts. Any other `vmbr0*` difference means: restore the backup, `ifreload -a`, and stop. To remove the bridge (only with no VM attached): restore the backup, `ifreload -a`, then `ip link del <STAGING_BRIDGE> 2>/dev/null`.
3. **The staging VPN VM**, on `<STAGING_NODE>`. It has one NIC on the management VLAN and is never on the lab trunk. The `qm` lines mirror `scripts/jumpbox_provision.sh:113-123`:
   ```bash
   qm create <STAGING_VPN_VMID> --name staging-vpn --ostype l26 --cores 2 --memory 2048 --agent enabled=1 --onboot 1 \
     --scsihw virtio-scsi-single --serial0 socket --vga serial0 --net0 virtio,bridge=vmbr0,tag=<MGMT_VLAN>
   qm set <STAGING_VPN_VMID> --scsi0 <STORAGE>:0,import-from=<CLOUD_IMAGE>,discard=on --boot order=scsi0
   qm resize <STAGING_VPN_VMID> scsi0 32G
   qm set <STAGING_VPN_VMID> --ide2 <STORAGE>:cloudinit --ciuser ubuntu --sshkeys <PUBKEY_FILE> --ipconfig0 ip=dhcp
   qm start <STAGING_VPN_VMID>
   ```
   - **Headscale.** A Docker Compose project in `/opt/headscale` whose service is named `headscale` (the name `scripts/apply_headscale_policy.sh:114` uses), pinned to the **same Headscale version as production**. Config:
     - `server_url: http://<STAGING_VPN_ADDR>:8080` and `listen_addr: 0.0.0.0:8080`;
     - policy `mode: file`, `path: /etc/headscale/acl.json`, with `/etc/headscale` a **directory** bind mount;
     - the default DERP map, or the embedded DERP server if the management VLAN has no internet.

     Create the users `staging-infra`, `zz-a` and `zz-b`. The staging base policy is a copy of `config/headscale-acl.example.json` with `tag:jumpbox` owned by `staging-infra@` and no staff members. It is kept in the operator's notes, never in the repo. [Verify on first build that Tailscale clients accept a plain-HTTP login server; if they do not, front Headscale with TLS and trust its CA in the clients.]
   - **Two students**, as two containers, each with its own network namespace and its own `tailscaled`. The auth key is written by the shell builtin `printf`, so it never appears in a process's argv:
     ```bash
     for s in zz-a zz-b; do
       read -rs -p "pre-auth key for $s: " KEY; echo
       install -m 600 /dev/null /root/$s.env && printf 'TS_AUTHKEY=%s\n' "$KEY" > /root/$s.env; unset KEY
       docker run -d --name $s --hostname $s --restart unless-stopped --cap-add NET_ADMIN --device /dev/net/tun \
         -v $s-state:/var/lib/tailscale --env-file /root/$s.env -e TS_STATE_DIR=/var/lib/tailscale -e TS_USERSPACE=false \
         -e TS_EXTRA_ARGS="--login-server=http://<STAGING_VPN_ADDR>:8080 --accept-routes" tailscale/tailscale:<PINNED>
     done
     ```
     Once `headscale nodes list` shows both, run `shred -u /root/zz-a.env /root/zz-b.env`. The state volumes keep the nodes.
4. **The staging jumpbox: the production adoption's rehearsal.** Use an operator-owned staging `site.env` kept outside the checkout (for example `~/staging/site.env`, passed as `SITE_ENV_FILE=`). It sets:
   - `JUMPBOX_NODE=<STAGING_NODE>`, `JUMPBOX_VMID=<STAGING_JUMPBOX_VMID>`, `JUMPBOX_NAME=staging-jumpbox`;
   - `JUMPBOX_BRIDGE=vmbr0`, `JUMPBOX_MGMT_VLAN=<MGMT_VLAN>`, `JUMPBOX_TRUNK_BRIDGE=<STAGING_BRIDGE>`;
   - `HEADSCALE_URL=http://<STAGING_VPN_ADDR>:8080`, `JUMPBOX_VLAN_BANDS="<band string>"`;
   - the policy keys of section 6.

   Then follow `docs/JUMPBOX.md` §1-3 exactly: `jumpbox_provision.sh` dry run then `--apply`; `jumpbox_configure.sh` dry run then `--apply`; `--apply --tailscale --authkey-stdin` with a tagged key from the staging Headscale (`headscale preauthkeys create --user <staging-infra-id> --expiration 1h --tags tag:jumpbox`); `headscale nodes approve-routes --identifier <id> --routes 10.0.0.0/8`; then `--apply --no-try --isolate-vlans`. The checks after `--apply` are Task 8 Step 7's: 401 VLAN interfaces, 401 `dhcp-range` lines, `vlan200` = `10.100.1.1/24`, dnsmasq and tailscaled active. Task 8 Step 8's J2 check also applies. **Record every difference from the docs.** A generic error is fixed in this repo; a site-specific one goes in the operator's notes.
5. **The staging portal.** Follow `docs/LOCAL_TEST_INSTANCE.md` (development stack, local override, dedicated short-lived token, reaper proven off), but check out the **branch or release under test**. Its `.env` adds:
   `FIXED_NODE=<STAGING_NODE>`, `ENABLE_LOAD_BALANCING=false`, `LAB_BRIDGE=<STAGING_BRIDGE>`, `VMID_RANGE=<STAGING_VMID_RANGE>`, `JUMPBOX_VLAN_BANDS="<band string>"`, `ENABLED_BACKENDS=proxmox`, `VPN_LOGIN_SERVER=http://<STAGING_VPN_ADDR>:8080`, `ORPHAN_REAPER_ENABLED=false`.

   That guide's step 2 (clearing a VLAN pair against production) and step 8 (setting the pair by SQL) do not apply: the isolated bridge carries no production VLAN. `data/students_merged.csv` holds `zz-a` and `zz-b`, which are seeded as 100/200 and 101/201. `templates.yaml` has one enabled individual group, `staging-two-machine`, with two distinct entries:
   - `srv01` (`entry_point: true`) and `dc01` (plain);
   - both point at a small Linux template on `<STAGING_NODE>` that has `sshd` and the QEMU guest agent;
   - `template_locations` names `<STAGING_NODE>` only.

   **Precondition, on a node:** both of these hold, otherwise choose another range with the instructor:
   - `pvesh get /cluster/resources --type vm --output-format json | jq --argjson lo <min> --argjson hi <max> '[.[] | select(.vmid >= $lo and .vmid <= $hi)] | length'` prints `0`;
   - `jq 'map(.vmid) | max'` on the same output is above `<max>`.

   A staging guest that became the cluster's highest VMID would raise production's next VMIDs for good. The same reasoning applies to the two staging VMIDs.
6. **The staging policy.** The staging `site.env` also sets:
   - `PORTAL_HOST=<STAGING_PORTAL_HOST>`, `PORTAL_APP_CONTAINER=proxmox-vm-automation` (`docker-compose.yml:4`);
   - `HEADSCALE_HOST=<staging VPN VM ssh>`, `HEADSCALE_DIR=/opt/headscale`;
   - `HEADSCALE_BASE_POLICY=<staging base policy path>`, `HEADSCALE_POLICY_FILE=/etc/headscale/acl.json`.

   Run `SITE_ENV_FILE=~/staging/site.env scripts/apply_headscale_policy.sh`, then the same with `--apply`. Expected: `verified: the policy file Headscale serves matches the rendered one.` Re-run after every staging roster or VLAN change.
7. **The acceptance test** (repeatable). Then **Stop, start, reset, remove**:
   - **Stop:** destroy staging deployments through the portal; `docker compose -f docker-compose.yml -f docker-compose.local.yml stop` on the portal host; `qm shutdown <STAGING_JUMPBOX_VMID>` and `qm shutdown <STAGING_VPN_VMID>`. The bridge stays.
   - **Start:** `qm start` both VMs; `docker compose ... start`; section 6's dry run reports nothing to do.
   - **Reset:**
     1. Destroy the staging deployments, and check the section 5 range query lists no `student-zz-*` guest.
     2. Run `docker compose ... down -v`, which wipes the staging database and with it the in-range high-water mark. Destroyed records keep their VMIDs, so a range of *n* VMIDs refuses with `VMIDRangeExhausted` after *n* deployments' worth of VMs; reset then.
     3. Run `docker compose ... up -d` (`init_db.py` reseeds `zz-a`/`zz-b`), then section 6 again.
     4. On the jumpbox, `jumpbox_configure.sh --apply` must report every file unchanged.
   - **Remove** (instructor decision only): `qm destroy` both VMs, then remove the bridge as in section 2.

   The acceptance test itself:
   1. Record `git -C <checkout> rev-parse HEAD` on the portal host. Section 6's dry run reports nothing to do.
   2. `zz-a` and `zz-b` each register (first use) and deploy `staging-two-machine`.
   3. `pvesh get /cluster/resources --type vm --output-format json | jq -r '.[] | select(.name // "" | test("^student-zz-(a|b)-")) | "\(.vmid) \(.name) \(.node)"'` lists exactly four guests, each inside `<STAGING_VMID_RANGE>` and on `<STAGING_NODE>`. `qm config <vmid> | grep ^net` shows `bridge=<STAGING_BRIDGE>` on every NIC:
      - `srv01`: `net0` on the entry tag and `net1` on the internal tag;
      - `dc01`: `net0` on the internal tag and no `net1`.
   4. In `/var/lib/misc/dnsmasq.leases` on the staging jumpbox, each `srv01` holds two leases and each `dc01` one (match the MACs from step 3). A's addresses are in `10.100.0.0/24` and `10.100.1.0/24`; B's in `10.101.0.0/24` and `10.101.1.0/24`.
   5. For each of A's three addresses, `docker exec zz-a ping -c2 -W2 <addr>` and `docker exec zz-a nc -w3 <addr> 22 </dev/null` both succeed. Both fail for each of B's three. Then the same with the roles reversed.
   6. Read the J2 counter (`sudo nft list table inet jumpbox_vlan_isolation` on the staging jumpbox). From A's `dc01` (`qm guest exec <vmid> -- ping -c2 -W2 <B srv01 internal addr>`), there is no reply, and the counter rises.
   7. Record the date, commit, four VMIDs and each result in the operator's staging log. Then destroy both deployments; step 3's query prints nothing.

- [ ] **Step 4: Run the test and confirm it passes**

Run: `uv run python -m pytest tests/test_cloud_docs.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/STAGING.md docs/LOCAL_TEST_INSTANCE.md docs/JUMPBOX.md tests/test_cloud_docs.py
git commit -m "Staging runbook: isolated bridge, staging jumpbox, Headscale and two-client acceptance"
```

- [ ] **Step 6 (operator, once): Build the stack**

Build `docs/STAGING.md` sections 2-6 in order, with that section's expected outputs at each one. Run section 5's VMID precondition **before** section 3, because it covers the two staging VMIDs too. Record the rehearsal differences (section 4). This needs no production window. Nothing here touches production's VLANs, jumpbox, Headscale or database.

- [ ] **Step 7 (operator, repeatable): Run the acceptance test**

Run `docs/STAGING.md` section 7's acceptance test with the staging portal at the PR head. Expected: every line passes and is recorded. On any failure, fix it on the branch, redeploy the staging portal, and run again. The pass recorded at the final PR head is Task 8 Step 4's merge gate.

---

### Task 8: Release notes, merge, and the changeover rollout

Steps 1-4 are done by the implementer. Steps 5-12 are **operator steps at the term changeover**, after the merge and never mid-term. The implementing agent does not run them.

**Files:**
- Modify: `CHANGELOG.md:13-70` (`[Unreleased]`: `### Added`, `### Changed` and `### Upgrade notes`)

- [ ] **Step 1: Write the CHANGELOG entries**

Under `### Added`:
- `LAB_BRIDGE` (default `vmbr0`) and `VMID_RANGE` (unset: unchanged allocation), which let a staging portal run on an isolated bridge in its own VMID range.
- `docs/STAGING.md`, the permanent staging stack and its two-client acceptance test.
- `scripts/audit_vlan_pairs.py`, which checks the exported VLAN pairs.

Under `### Changed`:
- The jumpbox serves each student's internal VLAN (200-299, `10.<N-100>.1.0/24`), and the entry band ends at 199.
- The Headscale policy grants each student their own internal VLAN on every enabled backend.
- The per-student pools are 100-199 and 200-299.
- The importers, `migrate_add_vlan.py` and `term_changeover.sh` refuse a roster of more than 100 students before changing anything.

Under `### Upgrade notes`, **replace** the current first bullet (`CHANGELOG.md:65-67`, "No action. `VPN_MODE` defaults to …"). Its facts stay, but without "No action", because this release does need action: "`VPN_MODE` defaults to `headscale`, … existing security groups already match the default rule set." Keep the compose bullet (`:68-70`). Then add:
- **Apply this release only at a term changeover.** Before deploying, run `docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py | python3 scripts/audit_vlan_pairs.py -`. It must print `ok`.
- Set `JUMPBOX_VLAN_BANDS` in `.env` and in `scripts/site.env` to the new string, or delete the line to take the default. Both files must agree.
- Re-run `jumpbox_configure.sh`; a hand-built jumpbox is adopted first (`docs/JUMPBOX.md`).
- Set `internal_nic: false` on every single-machine individual entry point.
- Re-run `scripts/apply_headscale_policy.sh`.
- Leave `LAB_BRIDGE` and `VMID_RANGE` unset on production; `docker-compose.prod.yml` passes them through empty.
- No database migration: stored tags do not change.

- [ ] **Step 2: Check the CHANGELOG and run the full suite on Linux**

Run (one chain: the full suite runs only if the CHANGELOG gate passes):
```bash
awk '/^## \[Unreleased\]/{f=1; next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/unreleased.md \
  && ! grep -q 'No action' /tmp/unreleased.md \
  && grep -q 'JUMPBOX_VLAN_BANDS' /tmp/unreleased.md && echo unreleased-ok \
  && uv run python -m pytest tests/test_release_notes.py -q \
  && uv run python -m pytest tests/ --ignore=tests/integration -q
```
Expected: `unreleased-ok`, then PASS. This check is a one-off: a permanent test that `[Unreleased]` never says "No action" would be wrong for a later release that really needs none.

- [ ] **Step 3: Cross-family review**

Run a Codex review of the branch diff. Point it at:
- `scripts/headscale_policy.py:139-205`, `scripts/jumpbox_netgen.py:35-115`;
- `app/vlan_allocator.py`, `scripts/init_db.py:230-299`, `scripts/migrate_add_vlan.py:182-249`, `scripts/build_students_merged.py:40-61`, `scripts/audit_vlan_pairs.py`;
- `app/proxmox_service.py:639-711`, `app/routes/vm_creation.py:162-246`, the `LAB_BRIDGE`/`VMID_RANGE` code in `app/config.py`, and `docs/STAGING.md`.

Ask for Critical/Important/Minor findings with file:line. Fix every Critical and Important finding, then repeat Step 2.

- [ ] **Step 4: Commit, push, open the PR, and wait for green CI and the staging pass**

```bash
git add CHANGELOG.md
git commit -m "Changelog: per-student internal VLANs, staging settings"
git push -u origin feature/individual-internal-vlans
```
Open the PR against `main`. CI runs from the PR event (push CI does not cover `feature/*`). Expected: the "Unit Tests" and "Integration Tests" checks pass, **and** Task 7 Step 7 has a recorded pass at the PR head. Squash-merge after the instructor approves.

#### Operator rules for Steps 5-12

- **Access check after every change.** The operator's student-access check (`check_student_access.py`, kept with the changeover isolation ticket) prints `student nodes: N; reach own /24: K; LOST own /24: L; also reach a foreign /24: F; filter entries on jumpbox: E`.
  - It exits 1 when `L > 0`.
  - It counts only Headscale nodes of roster logins: users without nodes, staff and `zz-*` are skipped.
  - It tests only each student's **entry** /24. Internal VLANs are proven by the staging test (Task 7) and the smoke test (Step 11).

  The expected results:
  - **Baseline (Step 5):** exit 0 and `L = 0`. Record `N` and `K`. A red baseline stops the rollout before anything changes.
  - **After Steps 6, 7 and 8:** exit 0, and `N`, `K`, `L` exactly equal the baseline.
  - **After Step 10** (new roster, so `N` changes): exit 0 and `L = 0`. Record `N` and `K` as the post-changeover baseline.
  - **After Step 11's policy applies (11c and 11g):** exit 0 with the post-changeover counts.

  **At the first result that differs, stop and run Step 12 for every step done so far.**
- **Procedure R: restore a Headscale policy backup (rollback only).** Only Step 12 uses it, when the instructor wants the exact pre-changeover policy back rather than a re-render. Run it on the Headscale host, in `HEADSCALE_DIR`, after setting `HEADSCALE_HOST` in that shell to exactly the value the apply script used. The apply script prints `backup: <HEADSCALE_HOST>:<path>` (`scripts/apply_headscale_policy.sh:159`), so strip that exact prefix (IPv6-safe). The live file is the backup path without its `.<stamp>` suffix (`:138-140`). `BACKUP_REF` is the `backup:` value that Step 10's policy stage printed.
  ```bash
  BACKUP_REF='<everything after "backup: ">'; BACKUP="${BACKUP_REF#"$HEADSCALE_HOST:"}"; HOST_FILE="${BACKUP%.*}"
  STAMP=$(date -u +%Y%m%dT%H%M%SZ); printf 'ref=%s\nbackup=%s\nlive=%s\n' "$BACKUP_REF" "$BACKUP" "$HOST_FILE"
  [[ "${BACKUP##*.}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] && test -f "$BACKUP" && test -f "$HOST_FILE" && echo files-ok
  ```
  Expected: `files-ok`. Otherwise stop: nothing has been modified. Then:
  ```bash
  docker compose exec -T headscale headscale policy check --file /dev/stdin < "$BACKUP"   # must exit 0
  cp -p "$HOST_FILE" "$HOST_FILE.$STAMP.replaced" && chmod 600 "$HOST_FILE.$STAMP.replaced"
  cp -p "$HOST_FILE" "$HOST_FILE.$STAMP.tmp" && cat "$BACKUP" > "$HOST_FILE.$STAMP.tmp" \
      && mv -f "$HOST_FILE.$STAMP.tmp" "$HOST_FILE"
  docker compose kill -s HUP headscale
  docker compose exec -T headscale headscale policy get > "$HOST_FILE.$STAMP.readback"
  python3 -c 'import json,sys; a,b=(json.load(open(p)) for p in sys.argv[1:]); sys.exit(a != b)' \
      "$HOST_FILE.$STAMP.readback" "$BACKUP" && echo restored
  ```
  Expected: `restored`. If the read-back differs (a single-file bind mount keeps showing the old file), run `docker compose restart headscale` once, wait until `headscale policy get` answers, and repeat the read-back. If it still differs, stop and escalate. A matching file proves only what Headscale serves, not what it has loaded (`docs/CLOUD_BACKENDS.md:1042-1051`). The access check that follows is the proof of what is enforced.

- [ ] **Step 5 (operator): Preconditions and pre-change records**

All of these must hold before continuing:
- The instructor has approved the window and decision 5 at the top of this plan.
- Task 7 Step 7 has a recorded pass at the merged commit.
- The term is over. The admin dashboard shows no live VM record, and the cluster holds no production portal guest: on a Proxmox node, `pvesh get /cluster/resources --type vm --output-format json | jq -r '.[] | select(.name // "" | test("^(student|group)-")) | select(.name | test("^student-zz-") | not) | "\(.vmid) \(.name)"'` prints nothing. Staging's own `student-zz-*` guests are excluded; destroy them anyway before the changeover.
- **J2 gate.** All three must hold:
  - J1 has been read after at least 7 days (counters and `journalctl -k --grep jb-xvlan`), and every cross-VLAN flow maps to staff or test traffic, with zero legitimate student or group flows.
  - The instructor has accepted that reading in the changeover isolation ticket.
  - The `--allow-vlans` list for KOTH or staff VLANs is decided, possibly empty.

  Otherwise stop the rollout here. Nothing has changed yet. Without J2 the AD labs do not ship (Global Constraints).
- **VLAN pair audit:** `docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py > slots.json && python3 scripts/audit_vlan_pairs.py slots.json` prints `ok`. On any problem, stop. Either remove or renumber the offending rows, or run the term purge first: Step 10, then this audit again, then Steps 6-9 before the new term's students are told the VPN is open. Only after that may the jumpbox or the policy change.
- **Lab subnets clear of the management side.** On the jumpbox, with `jumpbox_netgen.py` copied to `jumpbox/`:
  ```bash
  ip -4 -j route show table all | python3 -c '
  import ipaddress, json, sys; sys.path.insert(0, "jumpbox"); import jumpbox_netgen as ng
  bands = ng.parse_bands("entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4")
  lab = [ipaddress.ip_network(ng.gateway(t, bands), strict=False) for t in ng.vlan_tags(bands)]
  bad = sorted({(r["dst"], r.get("dev", "")) for r in json.load(sys.stdin)
                if r["dst"] != "default" and not r.get("dev", "").startswith(("vlan", "tailscale"))
                and any(ipaddress.ip_network(r["dst"], strict=False).overlaps(n) for n in lab)})
  print(bad or "no-overlap"); sys.exit(1 if bad else 0)'
  ```
  Expected: `no-overlap`. Any route on the management NIC, `docker0` or another non-lab device that overlaps a generated lab subnet stops the rollout.
- **Jumpbox records, made before the backup so they are inside it:**
  - `sudo sh -c 'nft list table inet jumpbox_vlan_isolation > /root/j1-before-j2.nft'` if the J1 table exists. Otherwise record "no J1".
  - `ip -br addr | grep -c '^vlan' | sudo tee /root/vlan-count-before`.
  - Then take a Proxmox backup of the jumpbox VM.
- Run the student-access check and record the result as the baseline.

- [ ] **Step 6 (operator): Deploy the portal**

Deploy the merged release, with `JUMPBOX_VLAN_BANDS` set to the new string in `.env` (or the line removed), and `LAB_BRIDGE`/`VMID_RANGE` absent. Recreate `vm-automation`.
Verify: `docker exec <PORTAL_APP_CONTAINER> python scripts/export_network_slots.py | jq -r .config.jumpbox_vlan_bands` prints the Global Constraints string. Then run the access check.

- [ ] **Step 7 (operator): Adopt the jumpbox**

Follow `docs/JUMPBOX.md` "Adopting a jumpbox that was built by hand" from an interactive console session, with `scripts/site.env` carrying the same band string. Never start `nftables.service`. Read the rehearsal differences recorded in Task 7 Step 6 first.
- Before moving the hand-built files aside, compare them with the rendered netplan (`python3 jumpbox/jumpbox_netgen.py netplan --bands "<band string>" --mgmt-if <mgmt> --trunk-if <trunk>`). The only VLAN differences must be `vlan200` moving from `10.200.0.1/24` to `10.100.1.1/24`, the new `vlan201`-`vlan299`, and `vlan500`.
- After `--apply`:
  - `ip -br addr show vlan200` lists only `10.100.1.1/24`. If `10.200.0.1/24` is still there, run `sudo ip addr del 10.200.0.1/24 dev vlan200`, then `ip -br addr show vlan200` again; it must list only `10.100.1.1/24`.
  - `ip -br addr show vlan299` lists `10.199.1.1/24`.
  - `ip -br addr | grep -c '^vlan'` and `grep -c '^dhcp-range=' /etc/dnsmasq.d/jumpbox.conf` both print `401`.
  - `systemctl is-active dnsmasq tailscaled` prints `active` twice.
- Run the access check.

- [ ] **Step 8 (operator): Inter-VLAN drop (J2)**

Install J2 as the isolation ticket says, with the `--allow-vlans` list decided at the Step 5 J2 gate. On the now-converged jumpbox, the dry run of `sudo jumpbox/jumpbox_configure.sh --isolate-vlans [--allow-vlans <list>]` must report netplan and dnsmasq unchanged before `--apply --no-try --isolate-vlans [--allow-vlans <list>]`. If it does not, install the table by hand as the ticket describes.
Verify:
- `sudo nft list table inet jumpbox_vlan_isolation` shows the drop rule.
- `systemctl is-enabled jumpbox-vlan-isolation.service` prints `enabled`.

Then run the access check. **If J2 is not applied, the AD labs do not ship** (Global Constraints).

- [ ] **Step 9 (operator): Audit the live `templates.yaml`**

Every enabled individual group with a single machine sets `internal_nic: false` on its entry point.

- [ ] **Step 10 (operator): Run the changeover**

Run `scripts/term_changeover.sh --apply <canvas-export>.csv`. Its stage 4 applies the production policy (P2) for the new roster. **Record the `backup:` value that stage prints** (Procedure R's `BACKUP_REF`). If it scrolled away, use the newest `<policy file>.<YYYYmmddTHHMMSSZ>` on the Headscale host, prefixed with `<HEADSCALE_HOST>:`.
Verify:
- The dry run of `scripts/apply_headscale_policy.sh` reports nothing to do.
- The Step 5 audit, re-run, prints `ok`.
- The access check exits 0 with `LOST own /24: 0`.

- [ ] **Step 11 (operator): Production smoke test with one `zz-` user**

Run this after Step 10, before students are told the VPN is open. It uses the normal policy apply; nothing is swapped.

**a. A two-machine group.** Use an enabled individual group with an entry point and a plain machine, if production has one. Otherwise add a temporary enabled group `zz-internal-vlan-smoke`, shaped like `templates.example.yaml`'s `two-machine-individual-example`, with a small Linux template present on the production nodes. Then run `cd <PORTAL_DIR> && docker compose -f <PORTAL_COMPOSE> restart vm-automation` and wait for `healthy` (the probe at `scripts/term_changeover.sh:413-422`).

**b. The test login.**
1. Write `data/zz-smoke.csv` (`SIS User ID,SIS Login ID` / `0,zz-smoke`) into the portal's `data/` directory.
2. Run `docker exec <PORTAL_APP_CONTAINER> python scripts/import_students.py /app/data/zz-smoke.csv`. Note the pair it prints, `<E>/<E+100>`.
3. Re-run the Step 5 audit. It must print `ok`.

**c. Policy.** `scripts/apply_headscale_policy.sh` (dry run) shows only the `zz-smoke@` forward and reverse rules added. Then run `--apply`, followed by the access check.

**d. VPN client.** On production Headscale, create the user `zz-smoke` and a one-hour pre-auth key. Join one client with `--accept-routes`, from a machine that is not on the staging tailnet.

**e. Deploy.** Register `zz-smoke` at `/register` and deploy the group.

**f. Pass criteria.** Pass only if all of these hold:
- The production jumpbox's lease file holds the entry machine's two leases and the plain machine's one.
- The client reaches (ping and tcp/22) the entry machine on `10.<E>.0.0/24` and both machines on `10.<E>.1.0/24`, and both gateways `10.<E>.0.1` and `10.<E>.1.1` answer ping.
- For another roster student with entry `<X>`, neither `10.<X>.0.1` nor `10.<X>.1.1` answers.

**g. Cleanup, on a pass and on a failure alike.** Skip any item whose step never ran.
1. Destroy the deployment through the portal.
2. Delete the `zz-smoke` node, then the user, in Headscale.
3. Run:
   ```bash
   docker exec <MYSQL_CONTAINER> sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u"$MYSQL_USER" "$MYSQL_DATABASE" -e "
     DELETE FROM student_whitelist WHERE login_id=\"zz-smoke\";
     UPDATE users SET entry_vlan_tag=NULL, internal_vlan_tag=NULL, disabled=1 WHERE login_id=\"zz-smoke\";"'
   ```
   The user row is kept, disabled and untagged: its destroyed VM records reference it, and a tagged leftover would collide with the next student given that pair.
4. Run `scripts/apply_headscale_policy.sh --apply`. Its diff removes only the `zz-smoke@` rules. Then run the access check.
5. Re-run the Step 5 audit. It must print `ok`.
6. If 11a added the temporary group, remove it and restart `vm-automation` as in 11a.
7. `scripts/term_changeover.sh <canvas-export>.csv` (dry run) reports every stage complete.

If 11f failed, continue with Step 12.

- [ ] **Step 12 (operator): Rollback, if any check fails**

Undo the steps already done, newest first, in this order. Skip any item whose step never ran.

1. **Smoke test.** Run 11g in full.
2. **Headscale policy.** If Step 10 has run, then after item 5 below, run `scripts/apply_headscale_policy.sh --apply` from the previous release's checkout. It re-renders the new roster with entry-only grants. If the instructor wants the exact pre-changeover policy file back instead, use Procedure R with Step 10's `BACKUP_REF`.
3. **J2.**
   ```bash
   sudo systemctl disable --now jumpbox-vlan-isolation.service   # ExecStop deletes the table
   sudo rm -f /etc/nftables.d/jumpbox-vlan-isolation.nft /etc/systemd/system/jumpbox-vlan-isolation.service
   sudo systemctl daemon-reload
   sudo nft list table inet jumpbox_vlan_isolation   # must fail: no such table
   ```
   If J1 existed in Step 5, restore it with `sudo nft -f /root/j1-before-j2.nft`. Then `sudo nft list table inet jumpbox_vlan_isolation` shows J1's log-only rules and no drop rule. J1 is transient, as it was before.
4. **Jumpbox network files.** From the console:
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
   Verify:
   - `ip -br addr | grep -c '^vlan'` equals `/root/vlan-count-before`.
   - `ip -br addr show vlan200` lists only `10.200.0.1/24`.
   - `systemctl is-active dnsmasq tailscaled` prints `active` twice.
   - `tailscale status` shows the subnet route.

   **Restore the Step 5 Proxmox backup instead** if any of these happens:
   - management access cannot be regained from the console;
   - any check above fails after one retry;
   - `nft list tables` shows a table other than Tailscale's, `jumpbox_ssh` and `jumpbox_vlan_isolation`;
   - the cause of the failure cannot be attributed to the files above.

   A VM restore reboots the jumpbox, which clears J1 (reload it from `/root/j1-before-j2.nft`, which the backup contains) and the current DHCP leases.
5. **Portal.** Redeploy the previous release with the old `JUMPBOX_VLAN_BANDS`, recreate `vm-automation`, and check that the export prints the old band string.
6. Run the access check. It must match the Step 5 baseline, or the post-changeover baseline if Step 10 ran.

No database rollback is needed: stored tags never changed. A half-rolled-back state, where the portal and the jumpbox use different bands, only leaves internal VLANs unreachable; no student can reach another student's networks.
