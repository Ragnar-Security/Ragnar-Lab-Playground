# Multi-backend lab VMs (Proxmox / AWS / GCP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each machine in `templates.yaml` run on Proxmox, AWS or GCP through one backend interface, with per-student cleanup and per-student VPN access on every backend, and with Proxmox behaviour unchanged.

**Architecture:** `app/backends/` defines a `VMBackend` protocol, a registry, and three implementations. `ProxmoxBackend` adapts the existing, proven Proxmox functions. AWS uses boto3; GCP uses the Compute Engine REST API through `google-auth`'s `AuthorizedSession`. `app/vm_provisioning.py` is the single create/destroy entry point; every lifecycle path (create, destroy, extend, expiry, reconcile, orphan sweep, survivors, listing) dispatches on `VMRecord.backend`. Cloud machines live in per-student "slot" subnets inside one VPC per cloud, behind a scale-to-zero subnet router; a generated Headscale policy limits each student to their own slots.

**Tech Stack:** Python 3.12, Flask 3.1, Flask-SQLAlchemy, MySQL 8 / SQLite tests, gunicorn gevent (single worker), proxmoxer 2.0.1, boto3, moto (dev), google-auth, uv-locked hash-pinned requirements, pytest.

**Spec:** `docs/superpowers/specs/2026-11-04-multi-backend-vms-design.md`

## Global Constraints

- Proxmox behaviour is unchanged: the production `templates.yaml` keeps working byte-for-byte; `ENABLED_BACKENDS` unset behaves as v1.0.0.
- Baseline suite at `bd75a88`: 1117 passed, 5 skipped. Existing tests may change patch points only, never what they assert.
- Backend resolution: machine `backend:` > group `backend:` > `proxmox`; all machines of one deployment must resolve to the same backend; KOTH groups must resolve to `proxmox`.
- Every later operation dispatches on the stored `VMRecord.backend`, never on current YAML.
- Never retire a `VMRecord` because a provider call failed; absence needs positive evidence (`confirm_absent`).
- Cloud lab machines: no public IP, boot disk deleted with the instance (`DeleteOnTermination` / `autoDelete`), no internet route, tagged at creation.
- Tags/labels: `lab-playground-site`, `lab-playground-vmid`, `lab-playground-deployment`, `lab-playground-expires` (epoch seconds), `lab-playground-slot`, `lab-playground-kind`. Provider names `lp-<site>-<vmid>`, `lp-<site>-d-<deployment_id[:16]>`, `lp-<site>-s-<slot>`.
- `LAB_SITE_ID`: 1–20 chars `[a-z0-9-]`; resources without this site's tag/prefix are never touched.
- Lab CIDRs: AWS default `172.20.0.0/16`, GCP default `172.21.0.0/16`; slot T → base + T×64 as `/26`; must not overlap `10.0.0.0/8`, `100.64.0.0/10`, `172.17.0.0/16`, or each other.
- `AWS_MAX_VMS` / `GCP_MAX_VMS` are required when the backend is enabled.
- Router start/stop under `vm_lifecycle_lock(router_lock_key(backend))`; slot ensure/release under `vm_lifecycle_lock(slot_lock_key(backend, slot))`.
- Single gunicorn gevent worker with `monkey.patch_all()`; no gRPC client libraries.
- New dependencies only via `pyproject.toml` + `uv lock` + `uv export --frozen --no-dev --no-emit-project -o requirements.txt`.
- Public repository: no real hosts, IPs, VMIDs, account IDs, node names or usernames in tracked files (repo CLAUDE.md rule 13).
- No task mutates AWS, GCP, Proxmox, Headscale, the jumpbox or production. Live contract tests are opt-in and never run in CI.
- Repo conventions bind: naive local datetimes via `datetime.now()`; idempotent migrations; comments say WHY; run tests with `uv run python -m pytest`.

## Review Focus

1. **An operator switches a lab's `backend:` while machines from the old setting are running.** Destroy, extend, listing and expiry must use each record's stored backend. Pinned in Task 8.
2. **A cloud launch fails after the provider created the instance but before the portal recorded it.** The instance must end up tracked (survivor record) or reaped by tag, and its admission must stay reserved until then. Pinned in Tasks 7, 8 and 15.
3. **Two workers retire the last two machines of one deployment at the same moment.** The deployment-network and slot cleanup must converge idempotently, with no error and no leftover. Pinned in Tasks 7 and 8.
4. **Provider inventory changes between `list_managed()` and `delete_resource()`.** Deletion must re-check site ownership and identity rather than trust the listed record. Pinned in Tasks 12, 14 and 15.
5. **A roster login contains uppercase letters, whitespace or a trailing `@`.** Policy export and render must normalize it exactly once, or reject it, before emitting a Headscale identity. Pinned in Task 17.

## Plan-level rulings

- **Ruling: Tasks 3 and 7 sequencing.** Task 3 ships the shared contract suite with the in-memory case only. Task 6 builds the Proxmox read and destroy half. Task 7 completes `ProxmoxBackend`, registers it, and adds the Proxmox contract case. Reason: a partially built backend cannot pass the full lifecycle contract without a stub or a skip, both of which are forbidden. Cost if wrong: the Proxmox contract coverage lands one task later.
- **Ruling: plan parts were drafted by GPT Daybreak** (the unpinned generic `task` agent), not by Opus. An Opus review of the whole plan against the spec runs before Task 1 (SDD preflight). Cost if wrong: none; this ruling only adds a review.

---

## Phase 1 — Foundation (no behaviour change)

### Task 1: Persist Backend Identity on VM Records

**Files:**
- Create: `scripts/migrate_add_vm_backend.py`
- Modify: `app/models.py` (`VMRecord` columns and `to_dict()`, approximately lines 27-119)
- Test: `tests/test_vm_backend_migration.py`
- Test: `tests/test_models.py` (`TestModels` VM record cases)

**Interfaces:**
- Consumes: the existing `VMRecord.vmid`, `node`, and `vm_type` identity and the repository rule that model datetimes remain naive local values.
- Produces: `VMRecord.backend: str`, `provider_id: Optional[str]`, `deployment_id: Optional[str]`, and `network_slot: Optional[int]`; `deployment_id` is indexed, and `VMRecord.to_dict()` returns all four fields. Existing rows and newly constructed records that omit `backend` read as `'proxmox'`.

**Context:** The migration must follow `scripts/migrate_vm_deletion_retry.py:16-51`: inspect `information_schema` before every DDL operation and make a second run a no-op. The exact schema is `backend VARCHAR(32) NOT NULL DEFAULT 'proxmox'`, `provider_id VARCHAR(255) NULL`, `deployment_id VARCHAR(64) NULL`, and `network_slot INT NULL`, with index `ix_vm_records_deployment_id`. Do not reinterpret `node` or `vm_type`: Proxmox keeps node plus `qemu`/`lxc`; clouds later store AZ/zone plus `aws`/`gcp`.

- [ ] **Step 1: Write the failing model and migration tests**

```python
# tests/test_models.py
# Catches a rollback that makes old records backend-less or omits cloud identity from APIs.
def test_vm_record_backend_defaults_and_serializes(self):
    vm = VMRecord(vmid=100, vm_name='student-test-vm', vm_type='qemu',
                  node='node-placeholder', template_name='lab/vm',
                  student_name='test', expires_at=datetime.now() + timedelta(hours=1),
                  provider_id='provider-placeholder', deployment_id='a' * 32,
                  network_slot=101)
    db.session.add(vm); db.session.commit()
    saved = VMRecord.query.one()
    self.assertEqual(saved.backend, 'proxmox')
    self.assertEqual(saved.to_dict()['backend'], 'proxmox')
    self.assertEqual(saved.to_dict()['provider_id'], 'provider-placeholder')
    self.assertEqual(saved.to_dict()['deployment_id'], 'a' * 32)
    self.assertEqual(saved.to_dict()['network_slot'], 101)

# tests/test_vm_backend_migration.py
# Catches a production restart that re-runs ALTER TABLE or CREATE INDEX.
def test_vm_backend_migration_is_idempotent():
    module = load_migration('migrate_add_vm_backend.py')
    engine = FakeInformationSchemaEngine()
    module.run_migration(engine=engine)
    module.run_migration(engine=engine)
    assert engine.connection.columns == {
        'backend', 'provider_id', 'deployment_id', 'network_slot'}
    assert engine.connection.indexes == {'ix_vm_records_deployment_id'}
    assert engine.connection.ddl_count == 5
```

The fake must answer both `information_schema.COLUMNS` and `information_schema.STATISTICS`, parse each `ALTER TABLE ... ADD COLUMN`, parse `CREATE INDEX`, and fail on duplicate DDL; also assert the backend DDL contains `NOT NULL DEFAULT 'proxmox'`.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_models.py tests/test_vm_backend_migration.py -q`
Expected: FAIL because the four attributes and migration file do not exist.

- [ ] **Step 3: Implement the additive model and idempotent migration**

Add the four SQLAlchemy columns exactly as contracted: `backend = db.Column(db.String(32), nullable=False, default='proxmox', server_default='proxmox')`, nullable provider/deployment/slot columns, and `index=True` on `deployment_id`. Add them to `to_dict()`. Implement `check_column_exists()`, `check_index_exists()`, `_add_column_if_missing()`, `_add_index_if_missing()`, and `run_migration(engine=None)` in the standalone migration; use `engine.begin()` so each execution is atomic.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_models.py tests/test_vm_backend_migration.py -q`
Expected: PASS.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS with the existing record constructors using the Proxmox default.

- [ ] **Step 5: Commit**

```bash
git add app/models.py scripts/migrate_add_vm_backend.py tests/test_models.py tests/test_vm_backend_migration.py
git commit -m "schema: persist VM backend identity" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 2: Add Conditional Multi-Backend Configuration

**Files:**
- Modify: `app/config.py` (backend constants and `validate_config()`, approximately lines 18-115 and 353-408)
- Modify: `scripts/preflight_env.py` (`problems()`)
- Test: `tests/test_config.py`
- Test: `tests/test_preflight_env.py`

**Interfaces:**
- Consumes: T1's persisted backend names only as configuration vocabulary; no database access.
- Produces: all configuration names fixed in the contract: `ENABLED_BACKENDS: tuple`, `LAB_SITE_ID`; AWS/GCP location, network, router, cap, idle, and timeout values; and `JUMPBOX_VLAN_BANDS`. `validate_config()` requires only the enabled backends' settings and rejects unknown backends, invalid caps/site IDs, overlapping/undersized CIDRs, and missing enabled-backend credentials.

**Context:** `ENABLED_BACKENDS` is a comma list, normalized to lower-case unique names in declaration order, defaulting to `('proxmox',)`. Proxmox host/auth/TLS checks at `app/config.py:360-396` apply only when Proxmox is enabled; a cloud-only process must start without Proxmox credentials. `LAB_SITE_ID` is required only when AWS or GCP is enabled and must be 1-20 characters of `[a-z0-9-]` with no leading/trailing hyphen. AWS requires `AWS_REGION`, `AWS_AVAILABILITY_ZONE`, `AWS_LAB_VPC_ID`, `AWS_ROUTER_INSTANCE_ID`, and positive `AWS_MAX_VMS`; GCP requires `GCP_PROJECT`, `GCP_REGION`, `GCP_ZONE`, `GCP_NETWORK`, `GCP_ROUTER_INSTANCE`, and positive `GCP_MAX_VMS`. Defaults are exactly those in the binding contract, including AWS/GCP lab CIDRs, 30 idle minutes, 600-second timeouts, and the documented VLAN-band string. Keep a private stdlib-`ipaddress` CIDR validator in this task; T5 moves that logic into `app.backends.network_slots.validate_lab_cidr` and updates `config.py` to call it.

- [ ] **Step 1: Write failing conditional-config and host-preflight tests**

```python
# tests/test_config.py
# Catches cloud-only startup still requiring an unrelated Proxmox cluster.
def test_cloud_only_config_does_not_require_proxmox(monkeypatch):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS='aws', LAB_SITE_ID='site-a',
        AWS_REGION='us-test-1', AWS_AVAILABILITY_ZONE='us-test-1a',
        AWS_LAB_VPC_ID='vpc-placeholder', AWS_ROUTER_INSTANCE_ID='i-placeholder',
        AWS_MAX_VMS='3', PROXMOX_HOST='', PROXMOX_PASSWORD='')
    cfg.validate_config()

# Catches accidentally enabling an unimplemented or misspelled provider.
@pytest.mark.parametrize('value', ['azure', 'proxmox,azure', ','])
def test_unknown_or_empty_enabled_backend_is_rejected(monkeypatch, value):
    cfg = reload_config(monkeypatch, ENABLED_BACKENDS=value,
                        FLASK_ENV='development')
    with pytest.raises(ValueError, match='ENABLED_BACKENDS'):
        cfg.validate_config()

# Catches an ownership tag that cloud naming APIs reject.
@pytest.mark.parametrize('site', ['', '-site', 'site-', 'UPPER', 'x' * 21])
def test_cloud_site_id_is_validated(monkeypatch, site):
    cfg = reload_valid_aws_config(monkeypatch, LAB_SITE_ID=site)
    with pytest.raises(ValueError, match='LAB_SITE_ID'):
        cfg.validate_config()

# Catches cross-backend routing collisions and slot 500 escaping the base range.
@pytest.mark.parametrize('aws_cidr,gcp_cidr,message', [
    ('10.20.0.0/16', '172.21.0.0/16', '10.0.0.0/8'),
    ('172.17.0.0/16', '172.21.0.0/16', '172.17.0.0/16'),
    ('172.20.0.0/24', '172.21.0.0/16', 'slot 500'),
    ('172.20.0.0/16', '172.20.128.0/17', 'overlap'),
])
def test_lab_cidrs_fail_closed(monkeypatch, aws_cidr, gcp_cidr, message):
    cfg = reload_valid_cloud_config(
        monkeypatch, AWS_LAB_CIDR=aws_cidr, GCP_LAB_CIDR=gcp_cidr)
    with pytest.raises(ValueError, match=message):
        cfg.validate_config()

# tests/test_preflight_env.py
# Catches a host preflight that passes although the enabled cloud cannot boot.
@pytest.mark.parametrize('key', [
    'LAB_SITE_ID', 'AWS_REGION', 'AWS_AVAILABILITY_ZONE', 'AWS_LAB_VPC_ID',
    'AWS_ROUTER_INSTANCE_ID', 'AWS_MAX_VMS'])
def test_aws_preflight_names_each_missing_required_key(tmp_path, key):
    values = valid_aws_env()
    values.pop(key)
    result = _run(str(_write_env(tmp_path / '.env', values)))
    assert result.returncode == 1
    problems = result.stderr.splitlines()
    assert len(problems) == 1
    assert problems[0].startswith(f'{key}:')

# Catches the host script and app accepting different CIDR safety boundaries.
def test_preflight_rejects_reserved_overlapping_and_undersized_cidrs(tmp_path):
    for cidr in ('10.20.0.0/16', '172.17.0.0/16', '172.20.0.0/24'):
        result = _run(str(_write_env(
            tmp_path / f'{cidr.replace("/", "_")}.env',
            {**valid_aws_env(), 'AWS_LAB_CIDR': cidr})))
        assert result.returncode == 1
        assert result.stderr.startswith('AWS_LAB_CIDR:')
```

For preflight fixtures, extend `GOOD` with `ENABLED_BACKENDS=proxmox`; cloud-specific tests build neutral placeholder environments and assert only key names, never values, appear in stderr.

Test helpers in `tests/test_config.py` are exact: `reload_config(monkeypatch, **overrides)` clears every backend-related environment key, applies `overrides`, and reloads `sys.modules['app.config']`; `reload_valid_aws_config()` adds the neutral AWS values shown in `test_cloud_only_config_does_not_require_proxmox`; `reload_valid_cloud_config()` adds both providers with GCP project/region/zone/network/router placeholders and positive caps. `tests/test_preflight_env.py:valid_aws_env()` returns existing `GOOD` plus those same AWS keys and `ENABLED_BACKENDS=aws`.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_config.py tests/test_preflight_env.py -q`
Expected: FAIL because backend selection and cloud validation do not exist.

- [ ] **Step 3: Implement exact config parsing and conditional validation**

Add the fixed module-level names and numeric defaults. Add `_parse_enabled_backends(raw: str) -> tuple[str, ...]` and `_validate_configured_lab_cidrs(enabled: tuple[str, ...]) -> None`; reject malformed networks, reserved-range overlap (`10.0.0.0/8`, `100.64.0.0/10`, `172.17.0.0/16`), AWS/GCP mutual overlap, and a base that cannot contain `/26` slot 500. Update `validate_config()` so Proxmox TLS warnings/checks are also conditional. Mirror the same stdlib-only environment checks in `preflight_env.problems()` because that script cannot import Flask under `python -I -S`.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_config.py tests/test_preflight_env.py -q`
Expected: PASS.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS with unset `ENABLED_BACKENDS` retaining Proxmox-only behavior.

- [ ] **Step 5: Commit**

```bash
git add app/config.py scripts/preflight_env.py tests/test_config.py tests/test_preflight_env.py
git commit -m "config: add conditional backend settings" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 3: Define the Backend Protocol, Registry, and Shared Contract Harness

**Files:**
- Create: `app/backends/base.py`
- Create: `app/backends/__init__.py`
- Create: `tests/backends/__init__.py`
- Create: `tests/backends/fake_backend.py`
- Create: `tests/backends/test_backend_contract.py`
- Create: `tests/backends/test_registry.py`
- Modify: `tests/conftest.py` (reset backend factories/instances around every test)

**Interfaces:**
- Consumes: T2's `app.config.ENABLED_BACKENDS` tuple and T1's four `VMRecord` fields.
- Produces: every constant, exception, dataclass, `VMBackend` method, and registry function exactly as declared in `local://multi-backend-contract.md:41-128`. It also produces test-only `BackendContractCase`, `make_memory_backend_case(app) -> BackendContractCase`, and a `backend_case` fixture whose callable-parameter seam T7/T11/T14 extend.

**Context:** Per the accepted contract change, `_DEFAULT_FACTORIES` starts empty and tests use `register_backend_factory`; T7 adds the first production factory after Proxmox implements the whole protocol. The registry holds `_backend_factories = dict(_DEFAULT_FACTORIES)` and `_backend_instances`, constructs at most one instance per backend per process, rejects unknown and disabled names with `BackendNotEnabled`, and treats `None`/`''` record backend as Proxmox. `reset_backends_for_tests()` clears instances and restores the mutable factory map from defaults. The autouse fixture prevents one test's stateful fake or patched Proxmox client from leaking into another.

`BackendContractCase` has exact fields `backend: VMBackend`, `spec: VMSpec`, `expected_ips: list[str]`, `expected_status: str`, and `cleanup: Callable[[], None]`; its `ref(launched: LaunchedVM, location: str) -> VMRef` copies the spec identity and launched provider/location/slot. The fixture shape is:

```python
@pytest.fixture(params=[make_memory_backend_case], ids=lambda factory: factory.__name__)
def backend_case(request, app):
    case = request.param(app)
    yield case
    case.cleanup()
```

T7 adds `make_proxmox_backend_case`, T11 adds `make_aws_backend_case`, and T14 adds `make_gcp_backend_case` by importing and appending one callable each.

- [ ] **Step 1: Write failing dataclass, registry, and contract tests**

```python
# tests/backends/test_registry.py
# Catches legacy rows becoming undispatchable after the schema migration.
def test_backend_for_legacy_record_defaults_to_proxmox(monkeypatch):
    fake = FakeBackend('proxmox')
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    register_backend_factory('proxmox', lambda: fake)
    assert backend_for(SimpleNamespace(backend=None)) is fake
    assert backend_for(SimpleNamespace(backend='')) is fake

# Catches constructing a new SDK/client wrapper on every route call.
def test_get_backend_is_process_singleton(monkeypatch):
    created = []
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    register_backend_factory('aws', lambda: created.append(FakeBackend('aws')) or created[-1])
    assert get_backend('aws') is get_backend('aws')
    assert len(created) == 1

# Catches disabled or misspelled backends being silently constructed.
@pytest.mark.parametrize('name', ['aws', 'unknown'])
def test_get_backend_refuses_disabled_or_unknown(monkeypatch, name):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('proxmox',))
    with pytest.raises(BackendNotEnabled):
        get_backend(name)

# tests/backends/test_backend_contract.py
# Catches a provider adapter that cannot complete the common VM lifecycle.
def test_backend_lifecycle_contract(backend_case):
    case = backend_case
    location = case.backend.admit(case.spec, reservation_id='reservation-a',
                                  load_balancing_config=None)
    heartbeats = []
    launched = case.backend.create(case.spec, location,
                                   heartbeat=lambda: heartbeats.append(True))
    ref = case.ref(launched, location)
    assert case.backend.renew_admission('reservation-a') is True
    assert case.backend.release_admission('reservation-a') is True
    assert heartbeats
    assert case.backend.inspect(ref) == GUEST_MATCH
    assert case.backend.ip_addresses(ref) == case.expected_ips
    assert case.backend.status(ref) == case.expected_status
    extended = datetime.now() + timedelta(hours=2)
    case.backend.set_expiry(ref, extended)
    resource = next(r for r in case.backend.list_managed()
                    if r.kind == 'instance' and r.vmid == ref.vmid)
    assert resource.expires_at_epoch == pytest.approx(extended.timestamp(), abs=1)
    case.backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    assert case.backend.inspect(ref) == GUEST_ABSENT
    assert case.backend.confirm_absent(ref)

# Catches a policy enum accepted by the protocol but ignored by an adapter.
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_backend_destroy_policies(backend_case, policy):
    case = backend_case
    location = case.backend.admit(
        case.spec, reservation_id=f'reservation-{policy.value}',
        load_balancing_config=None)
    launched = case.backend.create(case.spec, location, heartbeat=lambda: None)
    ref = case.ref(launched, location)
    case.backend.destroy(ref, policy)
    assert case.backend.inspect(ref) == GUEST_ABSENT
    assert case.backend.confirm_absent(ref)

# Catches a provider that deletes the instance but leaves billable deployment resources.
def test_backend_cleanup_invariant(backend_case):
    case = backend_case
    case.backend.ensure_router_running()
    case.backend.ensure_slot(case.spec.network_slot)
    location = case.backend.admit(case.spec, reservation_id='reservation-cleanup',
                                  load_balancing_config=None)
    launched = case.backend.create(case.spec, location, heartbeat=lambda: None)
    ref = case.ref(launched, location)
    assert case.backend.release_admission('reservation-cleanup') is True
    case.backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    case.backend.cleanup_deployment(case.spec.deployment_id)
    case.backend.release_slot(case.spec.network_slot)
    case.backend.stop_router()
    assert not [r for r in case.backend.list_managed()
                if r.deployment_id == case.spec.deployment_id]
```

The in-memory fake must maintain reservations, instances, deployment networks, slot networks, router state, expiry, and identity in dictionaries/sets so assertions inspect terminal state rather than mock calls. Add direct tests for `VMRef.from_record()` and `VMSpec.network_slot` (entry slot for entry points, internal otherwise).

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_registry.py tests/backends/test_backend_contract.py -q`
Expected: FAIL because `app.backends` and the fake do not exist.

- [ ] **Step 3: Implement the fixed protocol and stateful fake**

Copy the fixed signatures and string values exactly; use frozen dataclasses. `VMRef.from_record()` maps `record.node` to `location`, defaults backend to Proxmox, and uses `getattr(record, field, None)` for each additive field so legacy test objects work. Implement the registry maps and reset fixture. Implement every protocol operation on `FakeBackend`; `destroy()` must fail closed on identity mismatch, `confirm_absent()` must inspect state, and cleanup methods must actually remove their corresponding fake resources.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_registry.py tests/backends/test_backend_contract.py -q`
Expected: PASS with one fixture case, `make_memory_backend_case`.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS; no production call site uses the registry yet.

- [ ] **Step 5: Commit**

```bash
git add app/backends/base.py app/backends/__init__.py tests/backends/__init__.py tests/backends/fake_backend.py tests/backends/test_backend_contract.py tests/backends/test_registry.py tests/conftest.py
git commit -m "backend: define lifecycle protocol and registry" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 4: Resolve and Validate Template Backends

**Files:**
- Create: `app/backends/template_spec.py`
- Modify: `app/template_manager.py` (`KNOWN_DEPLOYMENT_GROUP_KEYS`, `validate_template_config()`)
- Test: `tests/backends/test_template_spec.py`
- Test: `tests/test_template_manager.py`
- Test: `tests/test_templates_config.py`

**Interfaces:**
- Consumes: T2's enabled-backend tuple, T3's `KNOWN_BACKENDS`, and existing `TemplateConfigError`.
- Produces: `resolve_group_backend(group: Mapping) -> str`, `backend_block(template: Mapping, backend: str) -> Mapping`, and `validate_group_backends(group: Mapping, enabled: Sequence[str]) -> List[str]` exactly as fixed in the contract.

**Context:** Resolution is machine `backend` then group `backend` then `'proxmox'`, but the final group must be homogeneous. Existing top-level Proxmox keys remain byte-for-byte valid; a nested `proxmox:` mapping overlays those keys without mutating the input. AWS requires `image_id`, `instance_type`, and defaults `disk_gb=20`; GCP requires `image`, `machine_type`, and defaults `disk_gb=20`. Validation returns human-readable errors for mixed, unknown, disabled, missing cloud blocks/keys, and `koth_mode` on cloud. Add `backend` and `koth_mode` to known group keys so intentional schema additions do not become typo warnings.

- [ ] **Step 1: Write failing resolution and validation tests**

```python
# tests/backends/test_template_spec.py
# Catches a machine override silently creating a mixed-provider deployment.
def test_machine_override_must_not_mix_group():
    group = {'backend': 'aws', 'templates': [
        {'name': 'a', 'aws': {'image_id': 'ami-placeholder', 'instance_type': 'small'}},
        {'name': 'b', 'backend': 'gcp',
         'gcp': {'image': 'projects/example/global/images/test', 'machine_type': 'small'}},
    ]}
    with pytest.raises(TemplateConfigError, match='mixed'):
        resolve_group_backend(group)

# Catches a migration that breaks the existing flat Proxmox catalog.
def test_flat_proxmox_template_is_unchanged():
    template = {'type': 'qemu', 'memory': 2048,
                'template_locations': {'node-placeholder': 7001}}
    assert backend_block(template, 'proxmox') == template

# Catches nested Proxmox values failing to override legacy top-level defaults.
def test_nested_proxmox_block_overlays_top_level_without_mutation():
    template = {'type': 'qemu', 'memory': 2048,
                'proxmox': {'memory': 4096, 'cores': 4}}
    resolved = backend_block(template, 'proxmox')
    assert resolved['type'] == 'qemu'
    assert resolved['memory'] == 4096
    assert resolved['cores'] == 4
    assert template['memory'] == 2048

# Catches cloud deployment reaching an SDK with an incomplete image spec.
@pytest.mark.parametrize('backend,block,missing', [
    ('aws', {'instance_type': 'small'}, 'image_id'),
    ('gcp', {'image': 'projects/example/global/images/test'}, 'machine_type'),
])
def test_cloud_block_requires_pinned_keys(backend, block, missing):
    with pytest.raises(TemplateConfigError, match=missing):
        backend_block({'name': 'vm', backend: block}, backend)

# Catches KOTH reaching a backend with no guest-agent claim-file semantics.
def test_koth_cloud_group_is_rejected():
    errors = validate_group_backends(valid_aws_group(koth_mode=True), ('proxmox', 'aws'))
    assert any('koth_mode' in error and 'proxmox' in error for error in errors)
```

Also test machine override to the same backend, unknown backend, disabled backend, disk default 20, and `validate_template_config()` including these errors while the committed `templates.example.yaml` remains warning/error-free.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_template_spec.py tests/test_template_manager.py tests/test_templates_config.py -q`
Expected: FAIL because backend resolution and validation do not exist.

- [ ] **Step 3: Implement pure template resolution and wire validation**

Keep all helpers side-effect free and raise `TemplateConfigError` only from `resolve_group_backend()`/`backend_block()` when a caller requests an unusable selection. `validate_group_backends()` returns every error for admin display. Have `validate_template_config()` extend its existing error list for every group using `app.config.ENABLED_BACKENDS`; retain existing group-mode checks and unknown-key warnings.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_template_spec.py tests/test_template_manager.py tests/test_templates_config.py -q`
Expected: PASS.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS with every existing group resolving to Proxmox.

- [ ] **Step 5: Commit**

```bash
git add app/backends/template_spec.py app/template_manager.py tests/backends/test_template_spec.py tests/test_template_manager.py tests/test_templates_config.py
git commit -m "templates: resolve and validate VM backends" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 5: Define Network Slots and Cloud Resource Names

**Files:**
- Create: `app/backends/network_slots.py`
- Create: `app/backends/cloud_common.py`
- Modify: `app/config.py` (replace T2's private CIDR implementation with `validate_lab_cidr()`)
- Test: `tests/backends/test_network_slots.py`
- Test: `tests/backends/test_cloud_common.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: T2's CIDR values and documented `JUMPBOX_VLAN_BANDS`, plus T3 backend constants.
- Produces: every constant, `VlanBand`, and function signature fixed at `local://multi-backend-contract.md:130-153`, including exact tag strings, `/26` slot arithmetic, naming, and negative lock-key namespaces.

**Context:** `parse_vlan_bands()` must match the grammar and safety checks in `scripts/jumpbox_netgen.py:63-88`: `name:first-last:offset:third`, VLANs 2-4094, address octets 0-255, no duplicate tags, and no duplicate resulting subnets. `proxmox_slot_cidr()` returns `None`, not an exception, for an unrouted slot. Cloud slot 0 is the base `/26`; slot 500 begins exactly 32,000 addresses after it. Names and tags contain no site-specific fixture values.

- [ ] **Step 1: Write failing pure-function tests**

```python
# tests/backends/test_network_slots.py
# Catches off-by-one subnet reuse between adjacent students.
def test_cloud_slots_advance_by_exactly_one_26():
    assert cloud_slot_cidr('172.20.0.0/16', 0) == '172.20.0.0/26'
    assert cloud_slot_cidr('172.20.0.0/16', 1) == '172.20.0.64/26'
    assert cloud_slot_cidr('172.20.0.0/16', 500) == '172.20.125.0/26'

# Catches accepting a network that cannot represent the highest allocated slot.
def test_cloud_slot_rejects_out_of_range_or_small_base():
    with pytest.raises(ValueError): cloud_slot_cidr('172.20.0.0/24', 500)
    with pytest.raises(ValueError): cloud_slot_cidr('172.20.0.0/16', 501)

# Catches Headscale policy disagreeing with the jumpbox's slot mapping.
def test_default_vlan_bands_map_documented_slots():
    bands = parse_vlan_bands(DEFAULT_BANDS)
    assert proxmox_slot_cidr(100, bands) == '10.100.0.0/24'
    assert proxmox_slot_cidr(300, bands) == '10.100.2.0/24'
    assert proxmox_slot_cidr(400, bands) == '10.100.4.0/24'
    assert proxmox_slot_cidr(250, bands) is None
    assert routed_slots([250, 400, 100], bands) == [100, 400]

# Catches cloud routes shadowing Proxmox, tailnet, Docker, or each other.
@pytest.mark.parametrize('cidr,others,conflict', [
    ('10.20.0.0/16', {}, '10.0.0.0/8'),
    ('100.64.0.0/16', {}, '100.64.0.0/10'),
    ('172.17.0.0/16', {}, '172.17.0.0/16'),
    ('172.20.0.0/16', {'gcp': '172.20.128.0/17'}, 'gcp'),
])
def test_validate_lab_cidr_rejects_reserved_and_peer_overlaps(
        cidr, others, conflict):
    with pytest.raises(ValueError, match=conflict):
        validate_lab_cidr('aws', cidr, others=others)

# tests/backends/test_cloud_common.py
# Catches provider names exceeding GCE's syntax/length boundary.
def test_names_and_site_validation_are_stable():
    validate_site_id('site-a')
    assert instance_name('site-a', 123) == 'lp-site-a-123'
    assert deployment_network_name('site-a', 'a' * 32) == 'lp-site-a-d-' + 'a' * 16
    assert slot_network_name('site-a', 101) == 'lp-site-a-s-101'
    assert site_prefix('site-a') == 'lp-site-a-'

# Catches lock collisions between VMIDs, routers, and backend slots.
def test_reserved_lock_keys_are_disjoint():
    assert router_lock_key('aws') == -1
    assert router_lock_key('gcp') == -2
    assert slot_lock_key('aws', 101) == -10101
    assert slot_lock_key('gcp', 101) == -20101
```

Also assert all fixed tag key strings, naive-local `expires_epoch(dt) == int(dt.timestamp())`, invalid site IDs, malformed/overlapping VLAN bands, and deterministic sorted unique `routed_slots()`.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_network_slots.py tests/backends/test_cloud_common.py tests/test_config.py -q`
Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement the pure helpers and centralize CIDR validation**

Use `ipaddress.ip_network(cidr, strict=True)` and integer network-address arithmetic; do not enumerate addresses. Validate the maximum slot by constructing slot 500 and checking its broadcast address remains inside the base. Move T2's app-side CIDR checks to `validate_lab_cidr()` and call it once per enabled cloud with the other enabled cloud in `others`; retain the stdlib-only mirror in `scripts/preflight_env.py` because its isolated interpreter cannot import the app.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_network_slots.py tests/backends/test_cloud_common.py tests/test_config.py -q`
Expected: PASS.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/backends/network_slots.py app/backends/cloud_common.py app/config.py tests/backends/test_network_slots.py tests/backends/test_cloud_common.py tests/test_config.py
git commit -m "backend: add network slot and naming primitives" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 6: Adapt Proxmox Read and Destroy Semantics

**Files:**
- Create: `app/backends/proxmox.py`
- Create: `tests/backends/proxmox_case.py`
- Modify: `tests/test_live_contract_guards.py` (`FakeProxmox` agent/status/description support needed by the shared case)
- Create: `tests/backends/test_proxmox_backend.py`

**Interfaces:**
- Consumes: T3's complete `VMBackend` contract and registry seam, T5 `expires_epoch`, existing `app.proxmox_service` identity/inventory/task-wait functions, and the unchanged Proxmox-only helpers in `app/routes/utils.py`.
- Produces: the `ProxmoxBackend(name='proxmox')` read/destroy half: `inspect`, `destroy`, `confirm_absent`, `ip_addresses`, `status`, `set_expiry`, `list_managed`, and `delete_resource`; no-op deployment/slot/router methods; and Proxmox admission renew/release delegation. The class is tested directly but not registered as a complete `VMBackend` until T7 adds `admit/create`.

**Context:** `ProxmoxBackend` is an adapter, not a rewrite: construct/access the client through `app.proxmox_service.get_proxmox_connection()` so `app.proxmox_service.ProxmoxAPI` remains the valid boundary for all existing tests. Call `inspect_vm_guest()`, `confirm_proxmox_absence()`, `wait_for_proxmox_task()`, and the route utility IP/config probes rather than duplicating their incident fixes. Convert the Proxmox IP sentinel `['N/A']` to protocol `[]`; leave `_read_guest_file()` and KOTH guest-agent claim polling unchanged.

Every current destructive path maps as follows, with the existing behavior that proves the mapping:

- **Expiry -> `DestroyPolicy.STRICT`:** `app/tasks.py:294-299` short-circuits `_delete_vm` when `_stop_vm` is false; `_stop_vm` returns false on a failed stop task or unclassified exception at `app/tasks.py:377-394`.
- **Admin bulk -> `DestroyPolicy.BEST_EFFORT`:** `app/routes/admin.py:460-480` logs a stop exception/non-OK result and still submits/awaits delete.
- **Failed-deploy, partial-preflight, KOTH cleanup, and group-cascade cleanup -> `DestroyPolicy.BEST_EFFORT`:** `app/routes/vm_creation.py:325-370` continues to delete after a stop error/non-OK result; `koth_admin.py:60-95` and `student_groups.py:207-224` feed their guests to that helper.
- **Student or group-card destroy -> `DestroyPolicy.FORCE_CORRUPT`:** `app/routes/vm_lifecycle.py:287-365` detects an unreadable-but-listed config, skips stop, and calls QEMU `delete(purge=1, skiplock=1)` or LXC `delete(purge=1, force=1)`; normal stop failure still proceeds to delete.
- **Existing orphan reaper -> strict stop-before-delete semantics:** `app/orphan_reaper.py:235-274` refuses delete when stop fails. T15, not this part, routes/generalizes that path; T6 merely supplies the adapter operations and inventory it will consume.

`list_managed()` is the T15 boundary: return one `ManagedResource(kind='instance')` for every `cluster/resources?type=vm` row, including non-app guests and templates so the reaper can retain its current ownership/template filters. Populate `backend='proxmox'`, `provider_id=str(vmid)`, `vmid`, `name`, `location=node`, `status`, `is_template`, and parsed `Expires:` as local `expires_at_epoch`; leave deployment/slot/created fields `None`. Read config only for non-template `qemu`/`lxc` rows and leave expiry `None` on read failure. `delete_resource()` re-reads fresh inventory to recover `vm_type` and calls BEST_EFFORT; it never trusts the earlier list alone.

No existing production call site moves in T6, so no existing test patch point changes yet. Focused adapter tests patch `app.proxmox_service.ProxmoxAPI` and `app.proxmox_service.wait_for_proxmox_task` at their definition sites. T8 updates route/task patch points when it replaces those call chains.

- [ ] **Step 1: Write failing Proxmox policy and inventory tests**

```python
# tests/backends/test_proxmox_backend.py
# Catches expiry deleting a VM after its stop failed.
def test_strict_refuses_delete_after_stop_failure(proxmox_case):
    case, endpoint = proxmox_case
    endpoint.status.stop.post.side_effect = RuntimeError('locked')
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, DestroyPolicy.STRICT)
    assert case.api.guests[case.ref.vmid]['name'] == case.ref.vm_name

# Catches failed-deploy/admin cleanup regressing to strict semantics.
def test_best_effort_deletes_after_stop_failure(proxmox_case):
    case, endpoint = proxmox_case
    endpoint.status.stop.post.side_effect = RuntimeError('locked')
    case.backend.destroy(case.ref, DestroyPolicy.BEST_EFFORT)
    assert case.ref.vmid not in case.api.guests

# Catches corrupt QEMU/LXC configs becoming undeletable or using the wrong force flags.
@pytest.mark.parametrize('vm_type,expected', [
    ('qemu', {'purge': 1, 'skiplock': 1}),
    ('lxc', {'purge': 1, 'force': 1}),
])
def test_force_corrupt_uses_existing_provider_flags(
        proxmox_case_factory, vm_type, expected):
    case, endpoint = proxmox_case_factory(vm_type=vm_type, corrupt_config=True)
    case.backend.destroy(case.ref, DestroyPolicy.FORCE_CORRUPT)
    assert case.api.delete_kwargs[case.ref.vmid] == expected
    assert case.ref.vmid not in case.api.guests

# Catches a stale request deleting a replacement at the same VMID.
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_every_policy_refuses_identity_mismatch(proxmox_case, policy):
    case, endpoint = proxmox_case
    case.api.guests[case.ref.vmid]['name'] = 'replacement'
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, policy)
    assert case.api.guests[case.ref.vmid]['name'] == 'replacement'

# Catches treating a transport error as positive absence.
def test_confirm_absent_requires_fresh_inventory(proxmox_case):
    case, _endpoint = proxmox_case
    case.api.cluster_error = RuntimeError('inventory unavailable')
    assert case.backend.confirm_absent(
        case.ref, RuntimeError('request failed')) is False

# Catches T15 losing templates, status, or expiration evidence.
def test_list_managed_projects_complete_cluster_inventory(proxmox_case):
    resources = case.backend.list_managed()
    instance = next(r for r in resources if r.vmid == case.ref.vmid)
    assert instance.kind == 'instance'
    assert instance.provider_id == str(case.ref.vmid)
    assert instance.location == case.ref.location
    assert instance.expires_at_epoch == pytest.approx(case.expires_at.timestamp(), abs=1)
    assert any(r.is_template for r in resources)

# Catches one unreadable config hiding a still-managed guest from T15.
def test_list_managed_keeps_guest_when_config_read_fails(proxmox_case):
    case, _endpoint = proxmox_case
    case.api.fail_config_read.add(case.ref.vmid)
    resource = next(r for r in case.backend.list_managed()
                    if r.vmid == case.ref.vmid)
    assert resource.name == case.ref.vm_name
    assert resource.expires_at_epoch is None
```

Also test `ip_addresses()` maps no answer to `[]` while retaining `10.*`, `status()` maps unreadable-listed config to `'config_error'`, `set_expiry()` writes exactly `Expires: <local isoformat>`, no-op cloud hooks do not mutate the fake, and `delete_resource()` re-verifies identity. `tests/backends/proxmox_case.py` supplies focused read/destroy fixtures now and the full `BackendContractCase` builder T7 will activate.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_proxmox_backend.py -q`
Expected: FAIL because the Proxmox adapter does not exist.

- [ ] **Step 3: Implement the adapter without changing low-level Proxmox semantics**

Give `ProxmoxBackend` an optional injected client for fakes and a lazy production `client` property. Centralize stop/delete task waits in private adapter helpers; `destroy()` performs a fresh `inspect_vm_guest()` itself even when a caller already inspected. STRICT raises `DestroyRefused` before delete on stop failure; BEST_EFFORT continues; FORCE_CORRUPT uses the existing `_verify_vm_exists()` classification and exact force flags. Reset the cluster index after successful delete or confirmed absence. Keep `ProxmoxBackend` unregistered until T7 completes its required create side.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_proxmox_backend.py -q`
Expected: PASS for the focused Proxmox read/destroy adapter regressions.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS; production routes still use their existing chains.

- [ ] **Step 5: Commit**

```bash
git add app/backends/proxmox.py tests/backends/proxmox_case.py tests/backends/test_proxmox_backend.py tests/test_live_contract_guards.py
git commit -m "proxmox: adapt read and destroy operations" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 7: Route Provisioning Through the Backend Contract

**Files:**
- Create: `app/vm_provisioning.py`
- Modify: `app/backends/proxmox.py` (`admit`, `create`)
- Modify: `app/backends/__init__.py` (register complete Proxmox factory)
- Modify: `tests/backends/test_backend_contract.py` (append `make_proxmox_backend_case`)
- Modify: `app/routes/vm_creation.py` (`create_single_vm_task`, `deploy_bundle`, `log_vm_to_database`; keep proven clone helpers)
- Test: `tests/backends/test_vm_provisioning.py`
- Test: `tests/test_admission_control.py`
- Test: `tests/test_create_flow_component.py`
- Test: `tests/test_vm_creation_parallel.py`
- Test: `tests/test_vmid_allocator.py`
- Test: `tests/test_untracked_survivors.py`

**Interfaces:**
- Consumes: T3 `VMSpec`/`LaunchedVM`/registry, T4 `backend_block`, T5 router/slot lock keys, T6 `ProxmoxBackend`, and the existing reservation/VMID/clone helpers.
- Produces: fixed `app.vm_provisioning.create_vm(spec, *, reservation_id, load_balancing_config, heartbeat) -> Tuple[str, LaunchedVM]`, `destroy_vm(ref, policy) -> None`, and `after_record_retired(ref) -> None`; backend-generic `create_single_vm_task` and `deploy_bundle`; VM records populated with backend/provider/deployment/slot identity; and the first complete production registry factory plus Proxmox shared-contract case.

**Context:** Preserve the current Proxmox order and leases in `app/routes/vm_creation.py:995-1127`: reserve one contiguous VMID block, admit each machine, honor the per-node clone semaphore, renew node and VMID leases while queued, create/start, commit all records atomically, then release reservations; failures retain reservations through cleanup/survivor tracking. Keep `create_lxc_container`, `create_qemu_vm`, `start_vm`, `extract_template_config`, and `get_node_clone_semaphores` as the proven low-level Proxmox helpers; `ProxmoxBackend` imports them lazily inside `create()` to avoid an import cycle and does not reimplement clone/NIC/storage behavior.

For cloud backends, `create_vm()` must hold `vm_lifecycle_lock(router_lock_key(spec.backend))` across `ensure_router_running()`, a nested `vm_lifecycle_lock(slot_lock_key(spec.backend, spec.network_slot))` around `ensure_slot()`, and `backend.create()`. This preserves the fixed admit -> router -> slot -> create order and prevents T16 from stopping the router between start and launch. No backend method takes these locks itself; no path acquires router while already holding slot. Proxmox skips both negative-key locks because its hooks are no-ops. The caller, not `create_vm()`, releases admission: a create exception can still leave a provider resource that failed-deploy cleanup must resolve first.

`create_single_vm_task` may add keyword parameters `backend_name='proxmox'` and `deployment_id=None` while retaining the current positional prefix so existing direct tests stay meaningful. It constructs `VMSpec`, uses the bundle reservation ID as `deployment_id`, and passes a heartbeat that requires `backend.renew_admission(reservation_id)` while treating VMID-renewal after provider creation as best effort exactly as today. `deploy_bundle` accepts `backend_name='proxmox'`; T9 makes the route resolve/pass it. KOTH calls it with explicit Proxmox in T9.

Moved patch points in this task:

- `tests/test_api_routes.py` and `tests/test_vm_creation_parallel.py`: `app.routes.vm_creation.get_proxmox_connection` -> unchanged preferred boundary `app.proxmox_service.ProxmoxAPI`; `app.routes.vm_creation.select_best_node` -> `app.proxmox_service.select_best_node` because admission now lives in the adapter.
- `tests/test_admission_control.py`, `tests/test_untracked_survivors.py`, and `tests/test_vm_creation_parallel.py`: patches of `renew_node_reservation`/`release_node_reservation` at `app.routes.vm_creation` -> stateful `register_backend_factory()` fake state, or `app.proxmox_service.renew_node_reservation`/`release_node_reservation` for Proxmox-specific cases. Patches of `create_single_vm_task`, `_cleanup_with_sweep`, `ThreadPoolExecutor`, and `log_vm_to_database` remain at their current route-module points.
- `tests/test_vmid_allocator.py`, `tests/test_create_flow_component.py`, and all other `app.proxmox_service.ProxmoxAPI` patches remain unchanged.

- [ ] **Step 1: Write failing generic-provisioning and persisted-identity tests**

```python
# tests/backends/test_vm_provisioning.py
# Catches a router stop racing between first-backend startup and instance launch.
def test_cloud_create_holds_router_lock_through_create(app, memory_backend_case):
    case = memory_backend_case(app)
    location, launched = create_vm(
        case.spec, reservation_id='reservation-a', load_balancing_config=None,
        heartbeat=lambda: None)
    assert location == launched.location
    assert case.backend.router_running
    assert launched.vmid in case.backend.instances
    assert case.backend.slot_networks == {case.spec.network_slot}

# Catches generic provisioning releasing admission before failure cleanup owns the guest.
def test_create_failure_keeps_admission_reserved_for_caller(
        app, memory_backend_case):
    case = memory_backend_case(app)
    case.backend.fail_create_after_launch = True
    with pytest.raises(RuntimeError, match='injected create failure'):
        create_vm(case.spec, reservation_id='reservation-failed',
                  load_balancing_config=None, heartbeat=lambda: None)
    assert 'reservation-failed' in case.backend.reservations
    assert case.spec.vmid in case.backend.instances

# Catches a cloud VM being committed without enough identity to destroy after restart.
def test_deploy_bundle_persists_backend_identity(app, memory_backend_case):
    case = memory_backend_case(app)
    records = deploy_bundle(
        deployment_group_name='Cloud Lab',
        templates=[case.spec.template],
        owning_identifier=case.spec.owner,
        entry_vlan=case.spec.entry_slot,
        internal_vlan=case.spec.internal_slot,
        uptime_minutes=60,
        user_id=None,
        backend_name='aws')
    record = records[0]
    assert record.backend == 'aws'
    assert record.provider_id == case.backend.instances[record.vmid].provider_id
    assert record.deployment_id
    assert record.network_slot == case.spec.network_slot
    assert record.vm_type == 'aws'
    assert record.node == 'memory-zone'
```

Add a lock-observation fake that asserts router lock held during create and slot lock held during ensure without using mock call lists. Add Proxmox regression assertions that the same fake boundary still sees contiguous VMIDs, exact clone semaphore limit, lease renewal, clone collision retry, QEMU/LXC NIC configuration, start-before-commit, atomic DB rollback, and reservation release only after commit/cleanup. Every result dict carries `_backend`, `_reservation_id`, `provider_id`, `deployment_id`, and `network_slot` for finalization.

`memory_backend_case` is a T7 fixture that calls `make_memory_backend_case(app)`, registers its exact backend instance, and invokes `cleanup()` after the test. Its fake exposes `fail_create_after_launch`: it inserts the instance and then raises `RuntimeError('injected create failure')`. For the persistence test, the fixture's template is a valid AWS backend block and the fake returns `provider_id='provider-100'`, `vm_type='aws'`, and `location='memory-zone'`.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_vm_provisioning.py tests/backends/test_backend_contract.py tests/test_admission_control.py tests/test_create_flow_component.py tests/test_vm_creation_parallel.py tests/test_vmid_allocator.py tests/test_untracked_survivors.py -q`
Expected: FAIL because generic provisioning and persisted launched metadata do not exist.

- [ ] **Step 3: Implement the generic wrapper and migrate nominal bundle creation**

Implement the three fixed functions in `app/vm_provisioning.py`; `after_record_retired()` uses live `VMRecord` and unexpired `VMIDReservation` queries, calls deployment cleanup only after the last record/reservation, releases a cloud slot only after the last same-backend/same-slot record, takes `slot_lock_key`, and catches/logs every exception. Implement Proxmox `admit()` with existing `select_best_node`, renew/release with existing reservation helpers, and `create()` with the unchanged clone helpers plus heartbeat. Add `BACKEND_PROXMOX: ProxmoxBackend` to `_DEFAULT_FACTORIES`, import `make_proxmox_backend_case` into the shared fixture params, and require all shared lifecycle/cleanup tests to pass. Refactor task/bundle orchestration to consume `VMSpec`/`LaunchedVM` and write all T1 fields, but leave failed-deployment cleanup conversion to T8 before any cloud production backend is registered.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_vm_provisioning.py tests/backends/test_backend_contract.py tests/test_admission_control.py tests/test_create_flow_component.py tests/test_vm_creation_parallel.py tests/test_vmid_allocator.py tests/test_untracked_survivors.py -q`
Expected: PASS, including all existing Proxmox ordering and lease regressions.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/vm_provisioning.py app/backends/proxmox.py app/backends/__init__.py app/routes/vm_creation.py tests/backends/test_backend_contract.py tests/backends/test_vm_provisioning.py tests/test_admission_control.py tests/test_create_flow_component.py tests/test_vm_creation_parallel.py tests/test_vmid_allocator.py tests/test_untracked_survivors.py
git commit -m "provisioning: dispatch VM creation by backend" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 8: Dispatch Every Existing VM Lifecycle Path

**Files:**
- Modify: `app/routes/vm_lifecycle.py` (`extend_vm`, `destroy_vm`)
- Modify: `app/routes/admin.py` (`bulk_destroy`)
- Modify: `app/tasks.py` (expiry, `_stop_vm`/`_delete_vm` removal, reconciliation)
- Modify: `app/routes/vm_creation.py` (`cleanup_vms`, `_cleanup_with_sweep`, `_finalize_failed_deployment`, survivor metadata, preflight inventory)
- Modify: `app/routes/vm_listing.py` (`list_vms`, `api_vm_detail`)
- Modify: `app/routes/student_groups.py` (`delete_group` inventory/cleanup)
- Modify: `app/routes/koth_admin.py` (KOTH destroy inventory only; claim reads remain untouched)
- Modify: `app/untracked_survivors.py` (`backend`, `provider_id`, `deployment_id`, `network_slot` round-trip; legacy defaults)
- Modify: `app/routes/utils.py` (retain Proxmox-only `_verify_vm_exists`, IP cache, and `_read_guest_file`; remove no call sites needed only by generic routes)
- Test: `tests/backends/test_lifecycle_dispatch.py`
- Test: `tests/test_admin_dashboard.py`
- Test: `tests/test_api_routes.py`
- Test: `tests/test_tasks.py`
- Test: `tests/test_vm_destroy_safety.py`
- Test: `tests/test_vm_orphan_cleanup.py`
- Test: `tests/test_vm_cleanup_sweep.py`
- Test: `tests/test_untracked_survivors.py`
- Test: `tests/test_student_groups_routes.py`
- Test: `tests/test_koth_routes.py`
- Test: `tests/test_koth_scorer.py`
- Test: `tests/test_koth_timebox.py`
- Test: `tests/test_deploy_efficiency.py`
- Test: `tests/test_windows_fallback_template.py`

**Interfaces:**
- Consumes: T3 `backend_for`/guest states/`VMRef`, T6 exact policy semantics and inventory, and all T7 `app.vm_provisioning` functions.
- Produces: every route/task operation dispatches from the stored `VMRecord.backend`; retirement calls `after_record_retired(ref)` after a durable DB commit; untracked survivors preserve enough identity to resume cleanup after restart. The existing Proxmox orphan reaper remains untouched until T15, invoked only when Proxmox is enabled and passed `ProxmoxBackend.client`.

**Context:** Never retire on provider failure. Under the existing per-VM lifecycle lock, refresh the row and identity, call `backend.inspect(ref)`, retire immediately only on `GUEST_ABSENT`, refuse mismatch/unknown, then call `destroy_vm(ref, policy)`. Preserve KOTH history through `tasks._retire_vm_record()`. Commit retirement before best-effort `after_record_retired(ref)`. Extend commits the database's naive-local `expires_at` as canonical and then calls `set_expiry()` best effort; preserve current `extended_count` semantics by counting provider updates that succeeded.

Listing maps backend values without applying Proxmox rules to clouds: `ip_addresses()` already returns only usable provider addresses; apply the `10.*` filter and cache fallback only inside `ProxmoxBackend`; the UI maps empty entry IPs to `N/A` and empty internal IPs to `-`. `status()` supplies the displayed status. Proxmox `'config_error'` retains the current `CONFIG ERROR` display/webhook; unknown/mismatch keeps the row. `api_vm_detail` authorizes against the DB first, then builds identity/memory/cores from the record and status/IP from its backend, so it no longer searches unrelated providers.

Reconciliation retains two sightings and positive evidence: group live rows by backend, call `inspect(ref)`, clear `note_vm_state` on any non-absent answer, and on a confirmed window re-read under `vm_lifecycle_lock`, require `confirm_absent(ref)`, add the existing alert, retire/commit, then run `after_record_retired`. Expiry uses STRICT; admin and failed deployment use BEST_EFFORT; student/group card destroy uses FORCE_CORRUPT, as proven in T6. Failed-deployment cleanup calls `cleanup_deployment(deployment_id)` only when every launched instance is confirmed absent; otherwise durable survivor rows/T15 own the leftovers.

KOTH remains explicitly Proxmox-only: `_read_guest_file`, `_poll_claims`, and `_refresh_box_ips` keep their current Proxmox client/guest-agent code. Only KOTH destroy inventory and mutation go through `get_backend('proxmox')`/`cleanup_vms`. The existing Proxmox orphan reaper is not generalized here; T15 owns that work.

Every existing moved patch point (tests patching `app.proxmox_service.ProxmoxAPI` remain valid and unchanged):

- `tests/test_api_routes.py`, `tests/test_vm_orphan_cleanup.py`, `tests/test_deploy_efficiency.py`, `tests/test_windows_fallback_template.py`: `app.routes.vm_listing.get_proxmox_connection`, `_verify_vm_exists`, and `_get_vm_ip_addresses` -> one `app.routes.vm_listing.backend_for` patch returning a stateful fake whose `inspect`/`status`/`ip_addresses` values replace those three patches; assertions do not change.
- `tests/test_api_routes.py`: `app.routes.vm_lifecycle.get_proxmox_connection` -> `app.proxmox_service.ProxmoxAPI`; `app.routes.vm_lifecycle.wait_for_proxmox_task` -> `app.proxmox_service.wait_for_proxmox_task`.
- `tests/test_vm_destroy_safety.py`: route-level creation/lifecycle wait patches -> `app.proxmox_service.wait_for_proxmox_task`; admin sleep patches are removed; lifecycle-lock patches remain at the route/task import sites.
- `tests/test_tasks.py`: direct `_stop_vm`/`_delete_vm` wait patches -> `app.backends.proxmox.ProxmoxBackend.destroy` policy tests or `app.proxmox_service.wait_for_proxmox_task`; `app.tasks.get_proxmox_connection` outage -> `app.tasks.backend_for`; reaper/time/alert patches stay.
- `tests/test_admin_dashboard.py`: `app.proxmox_service.get_proxmox_connection` and `wait_for_proxmox_task` stay valid because the adapter calls those definition-site functions.
- `tests/test_vm_cleanup_sweep.py`, `tests/test_api_routes.py`, and `tests/test_vm_creation_parallel.py`: `app.routes.vm_creation.find_vms_by_name_prefix` -> `app.routes.vm_creation.get_backend` returning a fake with `list_managed()`; `cleanup_vms`, `_preflight_deployment_group`, and `deploy_bundle` route patch points stay.
- `tests/test_student_groups_routes.py`: `app.routes.student_groups.find_vms_by_name_prefix` -> `app.routes.student_groups.get_backend`; `cleanup_vms` stays.
- `tests/test_koth_routes.py`, `tests/test_koth_scorer.py`, and `tests/test_koth_timebox.py`: destroy-path `app.proxmox_service.find_vms_by_name_prefix` -> `app.routes.koth_admin.get_backend`; KOTH claim/IP `get_proxmox_connection` and `_get_vm_ip_addresses` patches stay unchanged.
- `tests/test_untracked_survivors.py`: `create_single_vm_task`, `_cleanup_with_sweep`, and `ProxmoxAPI` patches stay; assertions gain persisted backend identity.

- [ ] **Step 1: Write failing dispatch, retirement, listing, and durable-survivor tests**

```python
# tests/backends/test_lifecycle_dispatch.py
# Catches expiry using Proxmox regardless of the record's persisted backend.
def test_expiry_dispatches_strict_to_record_backend(app, memory_backend_case):
    record = cloud_record(backend='aws', expired=True)
    run_one_expiry_pass(app)
    assert memory_backend_case.backend.destroyed[record.vmid] == DestroyPolicy.STRICT
    assert db.session.get(VMRecord, record.id) is None

# Catches student destroy losing its corrupt-config force behavior.
def test_student_destroy_dispatches_force_corrupt(
        app, client, memory_backend_case):
    case = memory_backend_case(app)
    record = persist_case_record(case)
    login_owner(client, record.created_by_user_id)
    response = client.post('/destroy', data={'deployment_group': 'Cloud Lab'})
    assert response.get_json()['success'] is True
    assert case.backend.destroyed[record.vmid] == DestroyPolicy.FORCE_CORRUPT

# Catches admin bulk/failed deploy accidentally becoming strict.
def test_admin_bulk_dispatches_best_effort(app, admin_client, memory_backend_case):
    case = memory_backend_case(app)
    record = persist_case_record(case)
    body = admin_client.post(
        '/admin/bulk-destroy', json={'vm_ids': [record.id]}).get_json()
    assert body['destroyed'] == 1
    assert case.backend.destroyed[record.vmid] == DestroyPolicy.BEST_EFFORT

def test_failed_deploy_dispatches_best_effort_and_cleans_network(
        app, memory_backend_case):
    case = memory_backend_case(app)
    case.backend.fail_next_create = True
    with pytest.raises(DeployFailed):
        deploy_case_bundle(case)
    assert set(case.backend.destroyed.values()) == {DestroyPolicy.BEST_EFFORT}
    assert not [r for r in case.backend.list_managed()
                if r.deployment_id == case.spec.deployment_id]

# Catches deleting a DB row after mismatch, unknown, timeout, or provider error.
@pytest.mark.parametrize('identity', [GUEST_MISMATCH, GUEST_UNKNOWN])
def test_no_lifecycle_path_retires_unverified_guest(
        app, client, memory_backend_case, identity):
    case = memory_backend_case(app)
    record = persist_case_record(case)
    case.backend.inspect_result = identity
    login_owner(client, record.created_by_user_id)
    response = client.post('/destroy', data={'deployment_group': 'Cloud Lab'})
    assert response.get_json()['success'] is False
    assert db.session.get(VMRecord, record.id) is not None
    assert record.vmid in case.backend.instances

# Catches premature SG/firewall/subnet deletion while a sibling or reservation exists.
def test_after_retirement_cleans_only_last_deployment_and_slot(
        app, memory_backend_case):
    case = memory_backend_case(app)
    first, second = persist_two_case_records(case)
    retire_and_finalize(first)
    assert case.spec.deployment_id in case.backend.deployment_networks
    assert case.spec.network_slot in case.backend.slot_networks
    retire_and_finalize(second)
    assert case.spec.deployment_id not in case.backend.deployment_networks
    assert case.spec.network_slot not in case.backend.slot_networks

# Catches a manually deleted cloud VM being retired on one observation.
def test_reconcile_requires_two_positive_absent_sightings(
        app, memory_backend_case, monkeypatch):
    case = memory_backend_case(app)
    record = persist_case_record(case)
    case.backend.instances.pop(record.vmid)
    monkeypatch.setattr(route_utils, 'ORPHAN_CONFIRM_SECONDS', 300.0)
    assert reconcile_missing_vm_records() == []
    assert db.session.get(VMRecord, record.id) is not None
    monkeypatch.setitem(route_utils._missing_since, (record.node, record.vmid),
                        time.monotonic() - 301)
    assert reconcile_missing_vm_records() == [record.vmid]
    assert db.session.get(VMRecord, record.id) is None

# Catches cloud listing applying Proxmox's 10/8 guest-agent filter.
def test_list_uses_backend_private_ip_and_status(app, client, memory_backend_case):
    case = memory_backend_case(app)
    persist_case_record(case)
    login_case_owner(client)
    response = client.get('/list').get_json()
    vm = response['students'][0]['deployment_groups'][0]['vms'][0]
    assert vm['ip_address'] == '192.0.2.10'
    assert vm['status'] == 'running'

# Catches concurrent retirement making idempotent cleanup fail or leave a network.
def test_concurrent_last_retirements_converge_without_error(
        app, memory_backend_case):
    case = memory_backend_case(app)
    first, second = persist_two_case_records(case)
    errors = retire_rows_together_then_finalize(first, second)
    assert errors == []
    assert case.spec.deployment_id not in case.backend.deployment_networks
    assert case.spec.network_slot not in case.backend.slot_networks

# Catches an operator YAML edit redirecting a running VM to another provider.
def test_destroy_uses_record_backend_not_current_template(
        app, client, memory_backend_case):
    case = memory_backend_case(app)
    record = persist_case_record(case)
    login_owner(client, record.created_by_user_id)
    with patch('app.template_manager.load_template_config',
               return_value=valid_gcp_catalog()):
        response = client.post('/destroy',
                               data={'deployment_group': 'Cloud Lab'})
    assert response.get_json()['success'] is True
    assert case.backend.destroyed[record.vmid] == DestroyPolicy.FORCE_CORRUPT

# Catches an empty cloud-only expiry pass trying to open Proxmox before alerts.
def test_cloud_only_empty_pass_delivers_alert_without_proxmox(
        app, memory_backend_case, queued_alert):
    attempted = {'proxmox': False}
    register_backend_factory(
        'proxmox',
        lambda: attempted.__setitem__('proxmox', True))
    run_one_expiry_pass(app, enabled=('aws',))
    assert queued_alert.delivered is True
    assert attempted['proxmox'] is False
```

Extend `tests/test_untracked_survivors.py` with one cloud line asserting backend/provider/deployment/slot round-trip and one legacy line asserting `backend == 'proxmox'` with the other three `None`. Add state-based group/KOTH cleanup tests, and retain every existing asynchronous task, stale VMID, KOTH FK, missing-sighting, cache-efficiency, authorization, and exact-name cleanup assertion after patch-point updates.

Define the test helpers in `tests/backends/test_lifecycle_dispatch.py` rather than hiding behavior in mocks: `persist_case_record(case)` inserts one live `VMRecord` from `case.spec`; `persist_two_case_records(case)` inserts two live rows sharing deployment/slot; `retire_and_finalize(record)` captures `VMRef`, retires and commits the row, then calls `after_record_retired`; `retire_rows_together_then_finalize(first, second)` uses two app contexts synchronized by a barrier, calls `after_record_retired` after each committed retirement, and returns collected exceptions; `run_one_expiry_pass(app, enabled=None)` stops the infinite loop after one pass using the existing 60-second sleep technique and optionally overrides enabled backends; `deploy_case_bundle(case)` uses two templates and configures the fake to launch the first and fail the second. `login_owner` and `login_case_owner` authenticate the persisted owner through the existing test client; `valid_gcp_catalog()` resolves the same group to GCP; `queued_alert` exposes durable delivered state after dispatch.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_lifecycle_dispatch.py tests/test_admin_dashboard.py tests/test_api_routes.py tests/test_tasks.py tests/test_vm_destroy_safety.py tests/test_vm_orphan_cleanup.py tests/test_vm_cleanup_sweep.py tests/test_untracked_survivors.py tests/test_student_groups_routes.py tests/test_koth_routes.py tests/test_koth_scorer.py tests/test_koth_timebox.py tests/test_deploy_efficiency.py tests/test_windows_fallback_template.py -q`
Expected: FAIL because lifecycle paths still call Proxmox directly and survivors do not store backend identity.

- [ ] **Step 3: Replace each private Proxmox chain with backend dispatch**

Use one `VMRef` captured before retirement. Remove `_stop_vm`/`_delete_vm` after expiry uses `destroy_vm`. Rewrite cleanup payloads to accept legacy dicts (missing backend means Proxmox) but always call `backend.inspect/destroy/confirm_absent`; prefix sweeps consume `list_managed()` and exact expected names. Update survivor parse/write/track functions with the new fields. Convert list/detail and reconcile as described. Update group/KOTH destroy inventory but do not touch claim-file reads or generalize `app/orphan_reaper.py`.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_lifecycle_dispatch.py tests/test_admin_dashboard.py tests/test_api_routes.py tests/test_tasks.py tests/test_vm_destroy_safety.py tests/test_vm_orphan_cleanup.py tests/test_vm_cleanup_sweep.py tests/test_untracked_survivors.py tests/test_student_groups_routes.py tests/test_koth_routes.py tests/test_koth_scorer.py tests/test_koth_timebox.py tests/test_deploy_efficiency.py tests/test_windows_fallback_template.py -q`
Expected: PASS with each fake's terminal state and DB rows matching the old behavior.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/vm_lifecycle.py app/routes/admin.py app/tasks.py app/routes/vm_creation.py app/routes/vm_listing.py app/routes/student_groups.py app/routes/koth_admin.py app/untracked_survivors.py app/routes/utils.py tests/backends/test_lifecycle_dispatch.py tests/test_admin_dashboard.py tests/test_api_routes.py tests/test_tasks.py tests/test_vm_destroy_safety.py tests/test_vm_orphan_cleanup.py tests/test_vm_cleanup_sweep.py tests/test_untracked_survivors.py tests/test_student_groups_routes.py tests/test_koth_routes.py tests/test_koth_scorer.py tests/test_koth_timebox.py tests/test_deploy_efficiency.py tests/test_windows_fallback_template.py
git commit -m "lifecycle: dispatch records through VM backends" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 9: Allocate VMIDs Without Proxmox and Resolve Backend Before VLAN Checks

**Files:**
- Modify: `app/proxmox_service.py` (`get_next_available_vmid()` accepts no Proxmox client)
- Modify: `app/routes/vm_creation.py` (`/create` resolution/validation order, backend-aware preflight and `deploy_bundle` call)
- Modify: `app/routes/koth_admin.py` (`_resolve_koth_group`, deploy/redeploy explicit Proxmox)
- Test: `tests/test_vmid_allocator.py`
- Test: `tests/test_api_routes.py`
- Test: `tests/test_vm_creation_group_mode.py`
- Test: `tests/test_koth_routes.py`

**Interfaces:**
- Consumes: T2 enabled backends, T3 registry, T4 resolution/validation, T6 Proxmox inventory, and T7 backend-aware `deploy_bundle`.
- Produces: `get_next_available_vmid(proxmox: Optional[ProxmoxAPI] = None, *, count: int = 1, reservation_id: str) -> int`; `/create` resolves/refuses the group backend before any Proxmox-only VLAN check and passes `backend_name`; KOTH deploy/redeploy explicitly require/pass Proxmox.

**Context:** Keep the allocator in `app.proxmox_service` in this phase to avoid needless movement of the proven coordination ledger and its test seam. The only Proxmox-dependent operation is the initial inventory maximum at `app/proxmox_service.py:615-620`; skip it when the optional client is `None`, while still taking the max of the persistent allocator mark, all VMRecord rows (including destroyed/cloud rows), and live VMID reservations at lines 622-655. When Proxmox is enabled, `deploy_bundle` passes `get_backend('proxmox').client`; when it is disabled, it passes `None`. Thus a cloud-only portal never constructs ProxmoxAPI, but a mixed portal still avoids manually occupied Proxmox VMIDs. All existing allocator patch points remain unchanged.

In `/create`, load the group and templates, call `validate_group_backends()` and `resolve_group_backend()`, then run `validate_user_vlans()` only for Proxmox. Cloud still uses the assigned numeric entry/internal tags as network-slot identities when present; it does not perform the Proxmox VLAN gate. Existing flat YAML resolves to Proxmox and follows the old order after resolution. `_resolve_koth_group()` returns HTTP 400 for any non-Proxmox resolution before status changes or `deploy_bundle`; deploy/redeploy pass `backend_name=BACKEND_PROXMOX`. KOTH `_read_guest_file` and claim polling remain unchanged.

Moved patch points:

- `tests/test_api_routes.py` and `tests/test_vm_creation_parallel.py`: `app.routes.vm_creation.get_next_available_vmid` remains valid because the route imports the same function; tests may instead patch `app.proxmox_service.get_next_available_vmid` only if implementation switches to module-qualified access.
- `tests/test_vmid_allocator.py`: direct calls add the explicit `proxmox=None` case; all `app.proxmox_service.ProxmoxAPI` create-flow patches remain valid.
- `tests/test_koth_routes.py`: `deploy_bundle` stays at `app.routes.koth_admin.deploy_bundle`; new cloud-refusal tests patch it there and assert it is not called.

- [ ] **Step 1: Write failing cloud-only allocation, route-order, and KOTH tests**

```python
# tests/test_vmid_allocator.py
# Catches a cloud-only portal trying to contact a disabled Proxmox cluster.
def test_allocator_uses_database_and_reservations_without_proxmox(app):
    db.session.add(cloud_vm(vmid=9000, backend='aws'))
    db.session.commit()
    first = proxmox_service.get_next_available_vmid(
        None, count=2, reservation_id='cloud-bundle')
    second = proxmox_service.get_next_available_vmid(
        None, count=1, reservation_id='cloud-bundle-2')
    assert (first, second) == (9001, 9003)

# Catches a mixed portal colliding with a manually allocated Proxmox guest.
def test_allocator_still_observes_proxmox_inventory_when_available(app):
    proxmox = FakeProxmox()
    proxmox.add_guest(9500, 'manual-guest')
    base = proxmox_service.get_next_available_vmid(
        proxmox, count=1, reservation_id='mixed-bundle')
    assert base == 9501

# tests/test_api_routes.py
# Catches the old early VLAN gate rejecting a valid cloud deployment.
def test_create_resolves_cloud_before_proxmox_vlan_check(self):
    self.test_user.entry_vlan_tag = None
    self.test_user.internal_vlan_tag = None
    db.session.commit()
    with registered_memory_backend('aws'), patch(
            'app.routes.vm_creation.load_template_config',
            return_value=valid_aws_catalog()), patch(
            'app.routes.vm_creation.deploy_bundle', return_value=[]):
        response = self.client.post('/create', data={'deployment_group': 'Cloud Lab'})
    self.assertTrue(response.get_json()['success'])

# Catches an unknown/disabled/mixed group reaching allocation or provider calls.
@pytest.mark.parametrize('catalog', [
    unknown_backend_catalog(), disabled_backend_catalog(), mixed_backend_catalog()])
def test_create_refuses_invalid_backend_before_allocation(client, catalog):
    attempted = []
    def record_attempt(**kwargs):
        attempted.append(kwargs)
        return []
    with patch('app.routes.vm_creation.load_template_config',
               return_value=catalog), patch(
            'app.routes.vm_creation.deploy_bundle', side_effect=record_attempt):
        response = client.post('/create', data={'deployment_group': 'Invalid Lab'})
    assert response.get_json()['success'] is False
    assert attempted == []

# tests/test_koth_routes.py
# Catches cloud KOTH reaching deploy and guest-agent claim reads.
def test_koth_cloud_group_is_refused_before_deploy_bundle(
        admin_client, cloud_koth_game):
    attempted = []
    def record_attempt(**kwargs):
        attempted.append(kwargs)
        return []
    with patch('app.routes.koth_admin.load_template_config',
               return_value=cloud_koth_catalog()), patch(
            'app.routes.koth_admin.deploy_bundle', side_effect=record_attempt):
        response = admin_client.post(
            f'/koth/api/games/{cloud_koth_game.id}/deploy')
    assert response.status_code == 400
    assert attempted == []
```

Retain the existing Proxmox missing-VLAN test and assert it still returns the same error. Add a successful flat legacy catalog test asserting `backend_name='proxmox'` reaches `deploy_bundle`.

Test catalog helpers return complete mappings, not partial stubs: `valid_aws_catalog()` has one enabled `Cloud Lab` group and one AWS machine with `image_id`, `instance_type`, and `disk_gb`; unknown/disabled/mixed variants change only the backend selection under test. `cloud_koth_catalog()` has one enabled `koth_mode` group resolving to AWS. Each route test creates and authenticates its user or admin before the request.

- [ ] **Step 2: Run to verify they fail**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_vmid_allocator.py tests/test_api_routes.py tests/test_vm_creation_group_mode.py tests/test_koth_routes.py -q`
Expected: FAIL because allocation requires a client and `/create` validates VLANs before reading the group.

- [ ] **Step 3: Make inventory optional and reorder route validation**

Guard only the inventory loop in `get_next_available_vmid`; do not weaken the database lock, high-water update, reservation TTL, or unique-row handling. Select the optional Proxmox client from `enabled_backends()` in `deploy_bundle`. Reorder `/create` exactly as described, pass `backend_name` through preflight/deploy, and make KOTH resolution explicitly Proxmox-only.

- [ ] **Step 4: Run to verify they pass**

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_vmid_allocator.py tests/test_api_routes.py tests/test_vm_creation_group_mode.py tests/test_koth_routes.py -q`
Expected: PASS for cloud-only allocation, legacy Proxmox VLAN behavior, and KOTH refusal.

Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS; Phase 1 ends with no behavior change for the default Proxmox-only deployment.

- [ ] **Step 5: Commit**

```bash
git add app/proxmox_service.py app/routes/vm_creation.py app/routes/koth_admin.py tests/test_vmid_allocator.py tests/test_api_routes.py tests/test_vm_creation_group_mode.py tests/test_koth_routes.py
git commit -m "creation: resolve backend before Proxmox checks" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```


## Phase 2 — AWS

### Task 10: Lock the cloud SDK and AWS test dependencies

**Files:**
- Modify: `pyproject.toml` (`[project].dependencies`; add `[dependency-groups].dev`)
- Modify: `uv.lock` (generated)
- Modify: `requirements.txt` (generated runtime export)
- Modify: `tests/test_dependency_lock.py` (runtime/dev membership and gevent import smoke)
- Modify: `.github/workflows/ci.yml` (`unit-tests` dependency install and test command)

**Interfaces:**
- Consumes: the existing lock contract `uv.lock` → `uv export --frozen --no-dev --no-emit-project` → hash-pinned `requirements.txt`.
- Produces: importable runtime packages `boto3` and `google-auth`; dev-only `moto[ec2]`; a CI unit environment containing the dev group; no `moto` package in the production `requirements.txt` export.

**Context:** `uv pip compile - --offline --python-version 3.12` was attempted with `boto3`, `google-auth`, and `moto[ec2]`, but this workstation's uv cache contains none of them, so no version can be honestly pinned from offline PyPI metadata. Add the three bare constraints below and let `uv lock` select the latest mutually compatible Python-3.12 releases at implementation time; the committed lock and hashes, not an invented version number, are the reproducibility boundary. Do not hand-edit `uv.lock` or `requirements.txt`. The production app is a single gevent worker; boto3 reaches HTTP through botocore's urllib3 transport, whose sockets are gevent-patched when `monkey.patch_all()` runs first. The subprocess smoke below proves the selected dependency set imports cleanly in that exact order; it does not claim that every external credential provider is reachable.

- [ ] **Step 1: Write the failing dependency and gevent tests** — extend `tests/test_dependency_lock.py` with these assertions (the comments identify the production regressions they catch):

```python
import os
import sys
import tomllib


def _requirement_name(value):
    return re.split(r'[<>=!~;\[]', value, maxsplit=1)[0].strip().lower()


def test_cloud_sdks_are_runtime_and_moto_ec2_is_dev_only():
    # Catches shipping a cloud backend whose SDK is absent from the image, or shipping moto in production.
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    runtime = {_requirement_name(v) for v in project['project']['dependencies']}
    dev = project['dependency-groups']['dev']
    assert {'boto3', 'google-auth'} <= runtime
    assert any(_requirement_name(v) == 'moto' and '[ec2]' in v.lower()
               for v in dev)
    assert 'moto' not in runtime

    exported = subprocess.run(EXPORT, cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.lower()
    assert re.search(r'^boto3==', exported, re.MULTILINE)
    assert re.search(r'^google-auth==', exported, re.MULTILINE)
    assert not re.search(r'^moto==', exported, re.MULTILINE)


def test_boto3_imports_after_gevent_monkey_patch():
    # Catches an SDK/urllib3 update that is incompatible with the process's mandatory patch order.
    program = '''
from gevent import monkey
monkey.patch_all()
assert monkey.is_module_patched("socket")
import boto3
import botocore.httpsession
assert botocore.httpsession.URLLib3Session.__module__ == "botocore.httpsession"
boto3.client(
    "ec2",
    region_name="us-east-1",
    aws_access_key_id="testing",
    aws_secret_access_key="testing",
    endpoint_url="http://127.0.0.1:9",
)
'''
    env = dict(os.environ, AWS_EC2_METADATA_DISABLED='true')
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'MonkeyPatchWarning' not in result.stderr
```

- [ ] **Step 2: Run to verify they fail** — from the worktree root run:

```bash
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run --frozen python -m pytest tests/test_dependency_lock.py -q
```

Expected: FAIL because the three dependency declarations and imports do not exist yet.

- [ ] **Step 3: Add dependencies and regenerate both lock artifacts** — make these exact declaration changes in `pyproject.toml`:

```toml
# inside [project].dependencies
    "boto3",
    "google-auth",

[dependency-groups]
dev = [
    "moto[ec2]",
]
```

Then run the repository's lock workflow, in this order:

```bash
uv lock --resolution highest
uv export --frozen --no-dev --no-emit-project -o requirements.txt
uv lock --check
uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.check
```

Confirm `diff <(grep -v '^#' requirements.txt) <(grep -v '^#' /tmp/requirements.check)` is empty. `uv.lock` must contain exact selected versions for boto3, botocore, google-auth, moto and their transitives; `requirements.txt` must contain exact hash-bearing runtime entries for boto3/google-auth and must not contain moto.

Change only the `unit-tests` job in `.github/workflows/ci.yml`: replace its `pip install --require-hashes -r requirements.txt` install with `uv sync --frozen --dev`, and run the suite with `uv run --frozen python -m pytest tests/ --ignore=tests/integration -q --tb=short`. Keep the preceding `uv lock --check` and runtime export diff. Leave the integration image/install path on `requirements.txt`; moto is not a production or integration-runtime dependency.

- [ ] **Step 4: Run to verify they pass** — run:

```bash
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run --frozen python -m pytest tests/test_dependency_lock.py -q
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run --frozen python -m pytest tests/ --ignore=tests/integration -q
```

Expected: the dependency-lock tests pass, the boto3/gevent subprocess exits within 15 seconds without a patch warning, and the full unit suite passes.

- [ ] **Step 5: Commit** —

```bash
git add pyproject.toml uv.lock requirements.txt tests/test_dependency_lock.py .github/workflows/ci.yml
git commit -m "deps: lock cloud backend SDKs and moto tests" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 11: Implement AWS instance lifecycle and capped admission

**Files:**
- Create: `app/backends/cloud_admission.py`
- Create: `app/backends/aws.py`
- Modify: `app/backends/__init__.py` (register the AWS factory without constructing it when AWS is disabled)
- Create: `tests/backends/test_aws_backend.py`

**Interfaces:**
- Consumes: `VMBackend`, `VMRef`, `VMSpec`, `LaunchedVM`, `ManagedResource`, `DestroyPolicy`, `AdmissionRefused`, `BackendError`, `GUEST_MATCH`, `GUEST_ABSENT`, `GUEST_MISMATCH`, `GUEST_UNKNOWN`, and `BACKEND_AWS` from `app/backends/base.py`; `TAG_SITE`, `TAG_VMID`, `TAG_DEPLOYMENT`, `TAG_EXPIRES`, `TAG_SLOT`, `TAG_KIND`, and `expires_epoch()` from `app/backends/cloud_common.py`; T2's `LAB_SITE_ID`, `AWS_REGION`, `AWS_AVAILABILITY_ZONE`, `AWS_MAX_VMS`, `AWS_LAUNCH_TIMEOUT_SECONDS`, and `AWS_TERMINATE_TIMEOUT_SECONDS`; `coordination.run_locked()` / `coordination.NODE_ADMISSION`; `NodeMemoryReservation`, `VMRecord`, and `db`; existing `renew_node_reservation()` / `release_node_reservation()`.
- Produces: `reserve_cloud_capacity(backend: str, location: str, max_vms: int, reservation_id: str) -> str` in `app/backends/cloud_admission.py`; `AWSBackend(ec2_client=None, network_resolver=None)` implementing instance-side `VMBackend` operations; deterministic `_client_token(deployment_id: str, vmid: int) -> str`; bounded `_waiter_config(timeout_seconds: int) -> dict`; `get_backend('aws')` lazily returns one registered `AWSBackend` per process when enabled. T13 may reuse `reserve_cloud_capacity()` for GCP.

**Context:** moto is not installed in the pre-task checkout, so its source could not be inspected offline. Use `mock_aws` with real boto3 clients and assert EC2 state, never boto call lists. The selected moto release is allowed to be less complete than EC2: (1) exercise boto waiters against moto's immediate state transitions, but keep the waiter-budget helper independently tested; (2) do not treat moto's `ClientToken` behavior as authority—test the exact token derivation here and leave provider idempotency to T19's live contract; (3) use EC2 tag filters and defensively re-check returned tags so the foreign-site test stays valid if moto over-returns; (4) T12 separately probes attached-security-group deletion and falls back to post-termination cleanup if moto does not model `DependencyViolation`. The T11 moto fixture creates a VPC, a private slot subnet, and a tagged deployment security group directly; T12 replaces that test setup with the backend's network methods.

Admission is one machine per reservation: inside one `coordination.run_locked(current_engine(), NODE_ADMISSION, ...)` transaction, purge expired `node_memory_reservations`, reject duplicate `reservation_id`, count live `VMRecord` rows with `backend == backend` plus unexpired reservations with `node == backend`, raise `AdmissionRefused` when that total is already `>= max_vms`, then insert `{reservation_id, node=backend, memory_bytes=1, expires_at=lease_clock()+RESERVATION_TTL_SECONDS}` and remember the lease engine. Return the configured availability zone, not the reservation node name. `AWSBackend.renew_admission()` and `.release_admission()` delegate to the existing node-reservation functions.

For EC2, `_client_token()` is exactly `sha256(f'{deployment_id}:{vmid}'.encode('utf-8')).hexdigest()` (64 lowercase hex characters). Resolve the AMI's `RootDeviceName` with `describe_images`, launch exactly one instance, set `AssociatePublicIpAddress=False`, attach the pre-existing slot subnet and deployment SG, and set the root EBS mapping's requested `disk_gb` with `DeleteOnTermination=True`. Use `TagSpecifications` at launch with every fixed instance tag plus `Name=spec.vm_name`, so a crash before DB commit remains sweepable. Wait with `instance_running` using `_waiter_config(AWS_LAUNCH_TIMEOUT_SECONDS)`; terminate with `instance_terminated` and `_waiter_config(AWS_TERMINATE_TIMEOUT_SECONDS)`. `_waiter_config` uses `Delay=5` and `MaxAttempts=max(1, ceil(timeout_seconds / 5))`; reject a non-positive timeout with `BackendError`. Call `heartbeat()` before `run_instances` and immediately after the running waiter.

`inspect()` performs a fresh `describe_instances(InstanceIds=[provider_id])`: `InvalidInstanceID.NotFound`, no returned instance, or state `terminated` is `GUEST_ABSENT`; a live instance is `GUEST_MATCH` only when both `TAG_SITE == LAB_SITE_ID` and `TAG_VMID == str(ref.vmid)`; either tag differing/missing is `GUEST_MISMATCH`; malformed IDs, throttling, timeout, and every other provider failure are `GUEST_UNKNOWN`. `confirm_absent()` returns true only for an supplied `ClientError` whose code is exactly `InvalidInstanceID.NotFound`, or for a fresh describe that proves no instance/`terminated`; it returns false on every unknown error. `set_expiry()` writes `TAG_EXPIRES=str(expires_epoch(expires_at))` but catches/logs provider failures because the already-committed DB expiry is canonical. `list_managed()` uses a server-side `tag:TAG_SITE` filter, locally re-checks that tag, excludes `terminated`, and emits `ManagedResource(kind='instance')` from tags, placement, launch time, state, and private identity. `ip_addresses()` returns the private IP as a one-element list or `[]`; `status()` returns the EC2 state string or `'unknown'`.

- [ ] **Step 1: Write the failing moto tests** — create one `@mock_aws` fixture `aws_instance_backend(app, monkeypatch)` that uses region `us-east-1`, AZ `us-east-1a`, `LAB_SITE_ID='test-site'`, `AWS_MAX_VMS=2`, and launch/terminate timeout 10. It creates a `172.20.0.0/16` VPC, private slot subnet, and tagged deployment SG. Build a real moto AMI by creating an 8-GiB volume in `us-east-1a`, snapshotting it, then calling `register_image(Name='test-image', Architecture='x86_64', RootDeviceName='/dev/sda1', VirtualizationType='hvm', BlockDeviceMappings=[{'DeviceName': '/dev/sda1', 'Ebs': {'SnapshotId': snapshot_id, 'VolumeSize': 8, 'DeleteOnTermination': True, 'VolumeType': 'gp3'}}])`. Return the real boto3 client, backend, resource IDs, and `VMSpec(vmid=700, vm_name='test-aws-700', display_name='Test AWS VM', backend='aws', template={'image_id': image_id, 'instance_type': 't3.micro', 'disk_gb': 24}, memory_mb=1024, cores=1, storage_gb=24, is_entry_point=True, entry_slot=1, internal_slot=2, expires_at=datetime.now() + timedelta(hours=1), deployment_id='d' * 32, owner='test-owner')`; its `network_slot` property is therefore `1`. Add these exact behavioral tests:

```python
def test_admit_counts_live_records_and_unexpired_reservations_atomically(app, aws_instance_backend):
    # Catches concurrent creates exceeding the mandatory AWS_MAX_VMS cap.
    backend, spec, ec2, resources = aws_instance_backend
    assert backend.admit(spec, reservation_id='r1', load_balancing_config=None) == 'us-east-1a'
    assert NodeMemoryReservation.query.filter_by(
        reservation_id='r1', node='aws', memory_bytes=1).one()
    assert backend.admit(spec, reservation_id='r2', load_balancing_config=None) == 'us-east-1a'
    with pytest.raises(AdmissionRefused):
        backend.admit(spec, reservation_id='r3', load_balancing_config=None)
    assert backend.renew_admission('r1') is True
    assert backend.release_admission('r1') is True
    assert backend.release_admission('r2') is True


def test_admit_counts_live_aws_records_but_not_expired_or_other_backend_leases(
        app, aws_instance_backend):
    # Catches stale/cross-cloud reservations consuming AWS cap, or live AWS VMs being omitted.
    backend, spec, ec2, resources = aws_instance_backend
    now = coordination.lease_clock()
    db.session.add(VMRecord(
        vmid=699, vm_name='test-aws-699', vm_type='aws', node='us-east-1a',
        template_name='test/aws', student_name='test-owner',
        memory=1024, cores=1, storage=24, is_entry_point=True,
        expires_at=spec.expires_at, destroyed=False, backend='aws',
        provider_id='i-live-record', deployment_id='c' * 32, network_slot=1))
    db.session.add_all([
        NodeMemoryReservation(reservation_id='expired-aws', node='aws',
                              memory_bytes=1, expires_at=now - 1),
        NodeMemoryReservation(reservation_id='live-gcp', node='gcp',
                              memory_bytes=1, expires_at=now + 600),
    ])
    db.session.commit()
    assert backend.admit(spec, reservation_id='live-aws-reservation',
                         load_balancing_config=None) == 'us-east-1a'
    with pytest.raises(AdmissionRefused):
        backend.admit(spec, reservation_id='over-cap',
                      load_balancing_config=None)
    assert db.session.get(NodeMemoryReservation, 'expired-aws') is None
    assert db.session.get(NodeMemoryReservation, 'live-gcp') is not None
    assert backend.release_admission('live-aws-reservation') is True


def test_create_launches_one_private_tagged_instance_with_disposable_root_disk(aws_instance_backend):
    # Catches duplicate/public instances and EBS volumes that keep billing after termination.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    tags = {tag['Key']: tag['Value'] for tag in instance['Tags']}
    assert instance['State']['Name'] == 'running'
    assert instance['Placement']['AvailabilityZone'] == 'us-east-1a'
    assert instance['SubnetId'] == resources['subnet_id']
    assert resources['security_group_id'] in [g['GroupId'] for g in instance['SecurityGroups']]
    assert 'PublicIpAddress' not in instance
    assert 'Association' not in instance['NetworkInterfaces'][0]
    assert instance['BlockDeviceMappings'][0]['Ebs']['DeleteOnTermination'] is True
    assert tags == {
        'Name': spec.vm_name,
        TAG_SITE: 'test-site', TAG_VMID: '700',
        TAG_DEPLOYMENT: 'd' * 32, TAG_EXPIRES: str(expires_epoch(spec.expires_at)),
        TAG_SLOT: '1', TAG_KIND: 'instance',
    }
    assert launched == LaunchedVM(vmid=700, vm_type='aws', location='us-east-1a',
                                  network_slot=1, provider_id=instance['InstanceId'])


def test_client_token_and_waiter_budget_are_stable_and_bounded():
    # Catches retry tokens changing across processes and provider waits becoming unbounded.
    expected = hashlib.sha256(f"{'d' * 32}:700".encode('utf-8')).hexdigest()
    assert _client_token('d' * 32, 700) == expected
    assert len(expected) == 64
    assert _waiter_config(1) == {'Delay': 5, 'MaxAttempts': 1}
    assert _waiter_config(6) == {'Delay': 5, 'MaxAttempts': 2}
    assert _waiter_config(600) == {'Delay': 5, 'MaxAttempts': 120}
    with pytest.raises(BackendError):
        _waiter_config(0)


def test_inspect_requires_site_and_vmid_identity(aws_instance_backend):
    # Catches destroying an instance reused by another portal or record.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    assert backend.inspect(ref) == GUEST_MATCH
    ec2.create_tags(Resources=[launched.provider_id],
                    Tags=[{'Key': TAG_VMID, 'Value': '701'}])
    assert backend.inspect(ref) == GUEST_MISMATCH


def test_destroy_and_confirm_absent_require_positive_fresh_evidence(aws_instance_backend):
    # Catches retiring a record merely because describe failed transiently.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    backend.destroy(ref, DestroyPolicy.STRICT)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert instance['State']['Name'] == 'terminated'
    assert backend.confirm_absent(ref) is True
    not_found = ClientError({'Error': {'Code': 'InvalidInstanceID.NotFound',
                                       'Message': 'not found'}}, 'DescribeInstances')
    assert backend.confirm_absent(ref, not_found) is True
    assert backend.confirm_absent(replace(ref, provider_id='malformed')) is False


def test_ips_status_expiry_and_inventory_come_from_moto_state(aws_instance_backend):
    # Catches UI and sweeper results coming from stale DB fields instead of EC2.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert backend.ip_addresses(ref) == [instance['PrivateIpAddress']]
    assert backend.status(ref) == 'running'
    extended = spec.expires_at + timedelta(hours=1)
    backend.set_expiry(ref, extended)
    [managed] = [r for r in backend.list_managed() if r.provider_id == launched.provider_id]
    assert managed.kind == 'instance'
    assert managed.vmid == 700
    assert managed.deployment_id == 'd' * 32
    assert managed.network_slot == 1
    assert managed.expires_at_epoch == expires_epoch(extended)
    assert resources['foreign_instance_id'] not in {
        resource.provider_id for resource in backend.list_managed()
    }


def test_set_expiry_is_best_effort_when_ec2_cannot_find_instance(aws_instance_backend):
    # Catches a provider-tag outage rolling back an already-committed extension.
    backend, spec, ec2, resources = aws_instance_backend
    missing = replace(resources['ref_without_provider'], provider_id='i-00000000000000000')
    assert backend.set_expiry(missing, spec.expires_at + timedelta(hours=1)) is None


def test_aws_backend_imports_after_gevent_patch_in_subprocess():
    # Catches AWSBackend adding an import that violates run_prod.py's gevent order.
    program = '''
from gevent import monkey
monkey.patch_all()
from app.backends.aws import AWSBackend
import boto3
client = boto3.client("ec2", region_name="us-east-1", aws_access_key_id="testing",
                      aws_secret_access_key="testing", endpoint_url="http://127.0.0.1:9")
AWSBackend(ec2_client=client)
assert monkey.is_module_patched("socket")
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                            env=dict(os.environ, AWS_EC2_METADATA_DISABLED='true'),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
```

The fixture must also create a foreign-site instance and the inventory test must assert its ID is absent from `list_managed()`. Add an expired reservation row and a live GCP reservation row before the cap test and assert neither counts toward AWS; add one live AWS `VMRecord` before a separate cap assertion to prove DB records do count.

- [ ] **Step 2: Run to verify they fail** — run:

```bash
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_aws_backend.py -q
```

Expected: FAIL because `app.backends.aws`, `cloud_admission.reserve_cloud_capacity`, and the AWS registry factory do not exist.

- [ ] **Step 3: Implement AWS admission and instance operations** — implement the signatures and exact state/evidence rules in the Interfaces and Context sections. `network_resolver`, when supplied, is `Callable[[VMSpec], tuple[str, str]]` returning `(subnet_id, security_group_id)` for moto instance tests. Its production default looks up the already-created slot subnet by `TAG_SITE` + `TAG_SLOT` + `TAG_KIND='slot-network'` and the deployment SG by `TAG_SITE` + `TAG_DEPLOYMENT` + `TAG_KIND='deployment-network'`, locally verifies all identity tags, and raises `BackendError` unless exactly one of each exists. T12 changes the SG half from lookup-only to create-if-absent. Do not put credentials in this class; when `ec2_client` is absent construct `boto3.client('ec2', region_name=AWS_REGION)` so boto3's standard credential chain remains intact. In `app/backends/__init__.py`, import `BACKEND_AWS` and `AWSBackend`, then add `BACKEND_AWS: AWSBackend` to the existing `_DEFAULT_FACTORIES` (which T7 already populated with Proxmox); do not construct it at import time. The existing `_backend_factories = dict(_DEFAULT_FACTORIES)`, cache eviction in `register_backend_factory()`, and reset-to-default behavior remain unchanged.

`destroy()` treats all three `DestroyPolicy` values identically, because EC2 terminate has no separate stop/delete decision, but it must propagate terminate or waiter failures. `delete_resource()` supports `kind='instance'` by terminating the exact provider ID and waiting; reject other kinds with `BackendError` until T12 adds them. Do not define no-op network/router methods in T11; T12 completes those protocol operations before AWS is added to the shared contract fixture.

- [ ] **Step 4: Run to verify they pass** — run:

```bash
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_aws_backend.py -q
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q
```

Expected: all AWS instance tests and the full unit suite pass; every provider assertion is against moto's EC2 state.

- [ ] **Step 5: Commit** —

```bash
git add app/backends/cloud_admission.py app/backends/aws.py app/backends/__init__.py tests/backends/test_aws_backend.py
git commit -m "backends: add AWS instance lifecycle" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### Task 12: Implement AWS slot networks, deployment isolation, router lifecycle, and shared contract coverage

**Files:**
- Modify: `app/backends/aws.py` (`ensure_slot`, `release_slot`, deployment SG creation/cleanup, router methods, resource inventory/deletion)
- Modify: `tests/backends/test_aws_backend.py` (network, SG, router, moto capability tests)
- Modify: `tests/backends/fake_backend.py` (`make_aws_backend_case(app) -> BackendContractCase`)
- Modify: `tests/backends/test_backend_contract.py` (`backend_case` params/imports)

**Interfaces:**
- Consumes: T11's `AWSBackend`; T5's `cloud_slot_cidr(base_cidr: str, slot: int) -> str`, `deployment_network_name()`, `slot_network_name()`, `router_lock_key()`, and `slot_lock_key()`; T2's `AWS_LAB_VPC_ID`, `AWS_LAB_CIDR`, `AWS_AVAILABILITY_ZONE`, `AWS_ROUTER_INSTANCE_ID`, `AWS_LAUNCH_TIMEOUT_SECONDS`, and `AWS_TERMINATE_TIMEOUT_SECONDS`; `vm_lifecycle_lock()`; T3's `BackendContractCase(backend, spec, expected_ips, expected_status, cleanup)`, its `.ref(launched, location)`, and `backend_case` builder signature `(app) -> BackendContractCase`.
- Produces: the remaining AWS `VMBackend` methods `cleanup_deployment(deployment_id)`, `ensure_slot(slot)`, `release_slot(slot)`, `ensure_router_running()`, and `stop_router()`; `list_managed()` now includes `kind='deployment-network'` SGs and `kind='slot-network'` subnets; `delete_resource()` dispatches all three AWS kinds; `make_aws_backend_case(app) -> BackendContractCase`; AWS is the third parameter of the shared lifecycle/policy/cleanup suite.

**Context:** A slot subnet is deterministic: name `slot_network_name(LAB_SITE_ID, slot)`, CIDR `cloud_slot_cidr(AWS_LAB_CIDR, slot)`, VPC `AWS_LAB_VPC_ID`, and AZ `AWS_AVAILABILITY_ZONE`. It must have `MapPublicIpOnLaunch=False`. A deployment SG is deterministic by `deployment_network_name(LAB_SITE_ID, deployment_id)` and permits exactly protocol `-1` from/to itself and `100.64.0.0/10`; revoke the default `0.0.0.0/0` egress before authorizing those two egress sources. There is no internet ingress/egress rule. Both operations are idempotent by identity tags and locally verify every returned resource before reuse. On an `InvalidGroup.Duplicate` or subnet conflict from a concurrent creator, re-describe and accept only the correctly tagged resource with the exact expected VPC/AZ/CIDR; otherwise raise `BackendError`.

The orchestration owns lock scope: T7 holds `vm_lifecycle_lock(router_lock_key('aws'))` across `ensure_router_running()` through instance create so idle stop cannot interleave, T8 holds `slot_lock_key('aws', slot)` around release, and T16 holds the router key across its idle DB check and `stop_router()`. Therefore these backend methods must not nest/reacquire the locks. Direct T12 tests call them while holding those exact locks. Start a stopped router and wait for `running` with the launch timeout; stop a running router and wait for `stopped` with the terminate timeout; running/stopped are idempotent; a missing, shutting-down, or terminated configured router raises `BackendError`.

Moto support is determined behaviorally, not assumed. Boto3 waiters and tag filters are exercised by the normal tests. Add an isolated capability test that attaches a sacrificial SG to a live instance and directly calls `delete_security_group`: if moto raises `DependencyViolation`, assert the SG still exists; if moto allows deletion, `pytest.skip('selected moto does not model attached-ENI SG dependency')`. The backend's required test remains provider-real and portable: terminate/wait first, then call `cleanup_deployment`, and assert the SG is gone. Likewise, ClientToken provider deduplication remains a T19 live assertion; this task does not weaken the deterministic T11 token test to fit moto.

- [ ] **Step 1: Write the failing network, isolation, router, and shared-contract tests** — add these tests using the T11 real-client moto fixture (each comment names the production regression):

```python
def test_ensure_slot_creates_one_private_subnet_with_exact_cidr_az_and_tags(aws_instance_backend):
    # Catches slot drift, duplicate subnets, and automatic public-address assignment.
    backend, spec, ec2, resources = aws_instance_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 3)):
        backend.ensure_slot(3)
        backend.ensure_slot(3)
    subnets = ec2.describe_subnets(Filters=[
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']},
        {'Name': f'tag:{TAG_SLOT}', 'Values': ['3']},
    ])['Subnets']
    assert len(subnets) == 1
    subnet = subnets[0]
    assert subnet['VpcId'] == resources['vpc_id']
    assert subnet['CidrBlock'] == cloud_slot_cidr('172.20.0.0/16', 3)
    assert subnet['AvailabilityZone'] == 'us-east-1a'
    assert subnet['MapPublicIpOnLaunch'] is False
    assert {t['Key']: t['Value'] for t in subnet['Tags']} == {
        'Name': slot_network_name('test-site', 3),
        TAG_SITE: 'test-site', TAG_SLOT: '3', TAG_KIND: 'slot-network',
    }


def test_create_uses_exact_deployment_security_group_rules(aws_instance_backend):
    # Catches lateral access between deployments and the default allow-all egress surviving.
    backend, spec, ec2, resources = aws_instance_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    networked_spec = replace(spec, vmid=701, deployment_id='e' * 32)
    launched = backend.create(networked_spec, 'us-east-1a', heartbeat=lambda: None)
    [group] = ec2.describe_security_groups(Filters=[
        {'Name': f'tag:{TAG_DEPLOYMENT}', 'Values': ['e' * 32]},
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']},
    ])['SecurityGroups']
    ingress = _flatten_group_rules(group['IpPermissions'])
    egress = _flatten_group_rules(group['IpPermissionsEgress'])
    expected = {('-1', 'group', group['GroupId']),
                ('-1', 'cidr', '100.64.0.0/10')}
    assert ingress == expected
    assert egress == expected
    assert ('-1', 'cidr', '0.0.0.0/0') not in egress
    [instance] = ec2.describe_instances(InstanceIds=[launched.provider_id])['Reservations'][0]['Instances']
    assert [g['GroupId'] for g in instance['SecurityGroups']] == [group['GroupId']]
    assert 'PublicIpAddress' not in instance


def test_cleanup_deployment_and_release_slot_remove_resources_after_instance(aws_instance_backend):
    # Catches a free-but-isolating SG or subnet leaking after the last billable VM is gone.
    backend, spec, ec2, resources = aws_instance_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    ref = resources['ref'](launched)
    backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    backend.cleanup_deployment(spec.deployment_id)
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.release_slot(1)
    assert ec2.describe_security_groups(Filters=[
        {'Name': f'tag:{TAG_DEPLOYMENT}', 'Values': [spec.deployment_id]}])['SecurityGroups'] == []
    assert ec2.describe_subnets(Filters=[
        {'Name': f'tag:{TAG_SLOT}', 'Values': ['1']},
        {'Name': f'tag:{TAG_SITE}', 'Values': ['test-site']}])['Subnets'] == []
    assert all(r.deployment_id != spec.deployment_id for r in backend.list_managed())


def test_list_managed_and_delete_resource_cover_all_aws_resource_kinds(aws_instance_backend):
    # Catches the orphan sweeper being unable to see or dispatch AWS network leftovers.
    backend, spec, ec2, resources = aws_instance_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    managed = backend.list_managed()
    assert {'instance', 'deployment-network', 'slot-network'} <= {r.kind for r in managed}
    instance_resource = next(r for r in managed if r.provider_id == launched.provider_id)
    backend.delete_resource(instance_resource)
    assert backend.confirm_absent(resources['ref'](launched)) is True
    for resource in [r for r in backend.list_managed()
                     if r.kind in {'deployment-network', 'slot-network'}]:
        backend.delete_resource(resource)
    assert not [r for r in backend.list_managed()
                if r.deployment_id == spec.deployment_id or r.network_slot == 1]


def test_delete_resource_rechecks_site_identity_before_mutation(aws_instance_backend):
    # Catches a stale inventory entry deleting a resource that another portal now owns.
    backend, spec, ec2, resources = aws_instance_backend
    with vm_lifecycle_lock(slot_lock_key(BACKEND_AWS, 1)):
        backend.ensure_slot(1)
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    resource = next(r for r in backend.list_managed()
                    if r.provider_id == launched.provider_id)
    ec2.create_tags(Resources=[launched.provider_id],
                    Tags=[{'Key': TAG_SITE, 'Value': 'other-site'}])
    with pytest.raises(BackendError):
        backend.delete_resource(resource)
    assert _instance_state(ec2, launched.provider_id) == 'running'


def test_router_start_stop_is_idempotent_under_reserved_lock(aws_instance_backend):
    # Catches the shared router stopping between admission and launch or never scaling down.
    backend, spec, ec2, resources = aws_instance_backend
    router_id = resources['router_instance_id']
    ec2.stop_instances(InstanceIds=[router_id])
    ec2.get_waiter('instance_stopped').wait(
        InstanceIds=[router_id], WaiterConfig={'Delay': 1, 'MaxAttempts': 10})
    with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
        backend.ensure_router_running()
        backend.ensure_router_running()
    assert _instance_state(ec2, router_id) == 'running'
    with vm_lifecycle_lock(router_lock_key(BACKEND_AWS)):
        backend.stop_router()
        backend.stop_router()
    assert _instance_state(ec2, router_id) == 'stopped'


def test_moto_attached_security_group_dependency_capability(aws_instance_backend):
    # Documents whether this moto release can prove EC2's attached-ENI deletion refusal.
    backend, spec, ec2, resources = aws_instance_backend
    launched = backend.create(spec, 'us-east-1a', heartbeat=lambda: None)
    group_id = ec2.describe_instances(InstanceIds=[launched.provider_id])[
        'Reservations'][0]['Instances'][0]['SecurityGroups'][0]['GroupId']
    try:
        ec2.delete_security_group(GroupId=group_id)
    except ClientError as error:
        assert error.response['Error']['Code'] == 'DependencyViolation'
        assert ec2.describe_security_groups(GroupIds=[group_id])['SecurityGroups']
    else:
        pytest.skip('selected moto does not model attached-ENI SG dependency')
```

In `tests/backends/fake_backend.py`, add `make_aws_backend_case(app) -> BackendContractCase`. It must manually start a `mock_aws()` context (a decorator would stop moto before the parametrized test uses the returned backend), patch the T2 AWS config values for the case lifetime, and create the moto VPC, registered AMI (using the same volume/snapshot recipe as the focused fixture), and router. Use the same complete `VMSpec` values as the focused fixture. Define a test-only `RecordingAWSBackend(AWSBackend)` whose `create()` calls `super().create()`, fresh-describes the returned instance, and replaces the contents of one captured `expected_ips: list[str]` with its `PrivateIpAddress`; it must not alter any provider operation. Return `BackendContractCase(backend=RecordingAWSBackend(real_moto_client), spec=spec, expected_ips=expected_ips, expected_status='running', cleanup=cleanup)`, where `cleanup()` stops the config patches and moto context. This mutable list is populated before the unchanged shared lifecycle test compares it, avoiding any hard-coded moto address.

Import `make_aws_backend_case` in `tests/backends/test_backend_contract.py` and change only the fixture parameter list to:

```python
@pytest.fixture(params=[make_memory_backend_case,
                        make_proxmox_backend_case,
                        make_aws_backend_case],
                ids=lambda factory: factory.__name__)
def backend_case(request, app):
    case = request.param(app)
    yield case
    case.cleanup()
```

The existing `test_backend_lifecycle_contract`, `test_backend_destroy_policies`, and `test_backend_cleanup_invariant` bodies remain unchanged; AWS must pass them by changing production behavior, not by branching in the shared tests.

- [ ] **Step 2: Run to verify they fail** — run:

```bash
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_aws_backend.py tests/backends/test_backend_contract.py -q
```

Expected: FAIL because AWS has no subnet/SG/router methods, network resource inventory, or contract-case builder.

- [ ] **Step 3: Implement network, isolation, router, and cleanup operations** — implement the exact resource identity, rules, timeout, idempotency, and caller-owned lock semantics in Context. Change T11's production network resolution so `create()` requires the slot subnet from `ensure_slot()` and creates-or-reuses the deployment SG before `run_instances`; keep the injectable `network_resolver` only for focused instance tests.

`cleanup_deployment()` describes groups by site + deployment + kind, locally verifies those tags, and deletes every match; absent is success and `DependencyViolation` propagates so normal cleanup/sweeper can retry. `release_slot()` behaves the same for the single tagged slot subnet. `list_managed()` adds SG/subnet resources with their provider IDs, deterministic names, deployment/slot identity, and `created_at_epoch=None` (EC2 describe responses do not expose creation time for these resource types).

`delete_resource()` is a safety boundary, not a blind dispatcher. Before mutation, fresh-describe `resource.provider_id` and require `TAG_SITE == LAB_SITE_ID` and `TAG_KIND == resource.kind`; also require the described `TAG_DEPLOYMENT`/`TAG_SLOT` to equal each non-`None` identity on the `ManagedResource`. A provider-confirmed absent resource is success; a tag mismatch raises `BackendError` and makes no mutation; an unknown describe error propagates. Only after that check dispatch `instance` to T11 termination, `deployment-network` to `delete_security_group(GroupId=...)`, or `slot-network` to `delete_subnet(SubnetId=...)`; raise `BackendError` for every unknown kind.

- [ ] **Step 4: Run to verify they pass** — run:

```bash
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_aws_backend.py tests/backends/test_backend_contract.py -q
PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q
```

Expected: AWS passes the unchanged shared lifecycle, all-policy, and zero-deployment-leftover invariants; all focused moto state assertions pass; the full unit suite passes. The attached-SG capability test may report one documented skip only when the locked moto release does not model that EC2 dependency.

- [ ] **Step 5: Commit** —

```bash
git add app/backends/aws.py tests/backends/test_aws_backend.py tests/backends/fake_backend.py tests/backends/test_backend_contract.py
git commit -m "backends: add AWS network and router lifecycle" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```


## Phase 3 — GCP

### Task 13: Stateful fake Compute Engine session and GCP instance lifecycle

**Files:**
- Create: `tests/backends/fake_gce.py`
- Create: `tests/backends/test_gcp_backend.py`
- Create: `tests/backends/test_gcp_import.py`
- Create: `app/backends/gcp.py`
- Modify: `app/backends/__init__.py` (`_DEFAULT_FACTORIES`; import `GCPBackend`, add `BACKEND_GCP: GCPBackend`, and let T3's reset copy restore it)

**Interfaces:**
- Consumes: `BACKEND_GCP`, `GUEST_MATCH`, `GUEST_ABSENT`, `GUEST_MISMATCH`, `GUEST_UNKNOWN`, `AdmissionRefused`, `BackendError`, `DestroyRefused`, `DestroyPolicy`, `VMRef`, `VMSpec`, `LaunchedVM`, and `ManagedResource` from `app.backends.base`; `instance_name(site, vmid)`, `deployment_network_name(site, deployment_id)`, `slot_network_name(site, slot)`, `site_prefix(site)`, `expires_epoch(expires_at)`, and label constants `TAG_SITE`, `TAG_VMID`, `TAG_DEPLOYMENT`, `TAG_EXPIRES`, `TAG_SLOT`, `TAG_KIND` from `app.backends.cloud_common`; T11's `reserve_cloud_capacity(backend: str, location: str, max_vms: int, reservation_id: str) -> str` from `app.backends.cloud_admission`; `renew_node_reservation(reservation_id)` and `release_node_reservation(reservation_id)` from `app.proxmox_service`; config values `LAB_SITE_ID`, `GCP_PROJECT`, `GCP_REGION`, `GCP_ZONE`, `GCP_NETWORK`, `GCP_MAX_VMS`, and `GCP_OPERATION_TIMEOUT_SECONDS`.
- Produces: `class GCPBackend` with `name = BACKEND_GCP` and `GCPBackend(session: Optional[requests.Session] = None)`; T13's instance-side methods `admit`, `renew_admission`, `release_admission`, `create`, `inspect`, `destroy`, `confirm_absent`, `ip_addresses`, `status`, `set_expiry`, instance-only `list_managed`, and instance-only `delete_resource`, all with the exact T3 `VMBackend` signatures; `FakeGCESession`. T14 completes the network/router protocol methods and produces `make_gcp_backend_case(app) -> BackendContractCase`.

**Context:** Compute Engine is called only through the v1 JSON REST API. The production default session is `google.auth.transport.requests.AuthorizedSession(credentials)` built from `google.auth.default(scopes=['https://www.googleapis.com/auth/compute'])`; importing this module must not discover credentials or perform I/O. Tests inject `FakeGCESession`, which presents the `requests.Session.request()` surface. Do not import a GCP gRPC client. `AuthorizedSession` is requests-based and must still import after `gevent.monkey.patch_all()`.

The base URL is `https://compute.googleapis.com/compute/v1/projects/{GCP_PROJECT}`. URL-quote resource-name path segments. T13 uses these endpoints and fields:

- `POST /zones/{zone}/instances?requestId={uuid}`. The request body is exactly `name`, `machineType="zones/{zone}/machineTypes/{spec.template['machine_type']}"`, one boot disk `{'boot': True, 'autoDelete': True, 'initializeParams': {'sourceImage': spec.template['image'], 'diskSizeGb': str(spec.template.get('disk_gb', 20))}}`, one `networkInterfaces` entry whose `subnetwork` is `projects/{project}/regions/{region}/subnetworks/{slot_network_name(site, spec.network_slot)}`, `labels`, and `tags`. Omit `accessConfigs` entirely so the lab VM gets no external IP. Labels are all six contract keys: site, decimal VMID, full deployment id, expiry epoch as a decimal string, slot as a decimal string, and kind `instance`. Tags are ordered `[deployment_network_name(site, spec.deployment_id), f'{site_prefix(site)}lab']`: the deployment tag is for same-deployment traffic and the site lab tag is the target of the operator-owned static tailnet rule documented by T20. The returned operation supplies `name`, `status`, optional `zone`/`region`, and optional `error.errors`; after it completes, `GET` the instance and return `LaunchedVM(spec.vmid, 'gcp', zone, spec.network_slot, provider_id=provider_name)`.
- `GET /zones/{zone}/instances/{name}` reads `name`, `labels`, `status`, `networkInterfaces[*].networkIP`, `creationTimestamp`, `zone`, and `tags.items`. A 404 is positive absence. `inspect` returns match only when name plus site/VMID/deployment/slot/kind labels agree with the `VMRef`; a present but differently labelled instance is mismatch; transport/5xx/invalid JSON is unknown. `confirm_absent(ref, error)` returns true immediately only for a supplied `requests.HTTPError` whose response is 404; otherwise it performs this GET and returns true only on 404, false on every ambiguous failure. `status` maps `RUNNING -> running`, `TERMINATED -> stopped`, `PROVISIONING|STAGING -> starting`, and lower-cases other provider states. `ip_addresses` returns every non-empty private `networkIP` and never inspects `accessConfigs`.
- `DELETE /zones/{zone}/instances/{name}?requestId={uuid}` returns a zonal operation. Before deletion, `destroy` performs the same identity check; absence is an idempotent success, mismatch raises `DestroyRefused`, and unknown raises `BackendError`. GCP has no separate stop in destroy, so all three `DestroyPolicy` values have identical successful behavior. Boot disk removal is guaranteed by `autoDelete: true` in the insert body.
- `GET /zones/{zone}/instances?filter=(labels.lab-playground-site = "{site}")` (sent as a `params` value, not string-concatenated) reads `items` and `nextPageToken`; paginate until absent. Filter again in Python for the exact site and `TAG_KIND == 'instance'` before producing `ManagedResource`, so a loose provider filter cannot confer ownership. Parse VMID, slot and expiry labels only when valid decimal integers; malformed owned resources remain listed with the corresponding optional field `None` rather than disappearing. Parse RFC3339 `creationTimestamp` to epoch; set `provider_id=name`, location from the final segment of `zone`, deployment id from the full label, and mapped status.
- `POST /zones/{zone}/instances/{name}/setLabels` first verifies the same name/site/VMID/deployment/slot/kind identity used by `inspect`, then reads the current `labels` and `labelFingerprint` and sends the complete labels mapping with only `TAG_EXPIRES` changed plus that fingerprint. Mismatch raises `BackendError` without mutation. On HTTP 412, refetch, revalidate identity, and retry at most three total attempts; preserve every unrelated label. A persistent conflict raises `BackendError` and never reports success.
- Mutations wait on the operation scope returned by Compute: zonal `POST /zones/{zone}/operations/{operation}/wait`, regional `POST /regions/{region}/operations/{operation}/wait`, or global `POST /global/operations/{operation}/wait`, with an empty request body as required by the REST method. Repeat while `status != 'DONE'`, using a monotonic deadline bounded by `GCP_OPERATION_TIMEOUT_SECONDS`; call the create heartbeat between waits. A terminal `error.errors` (read each `code`, `message`, and optional `location`) raises `BackendError`. A timeout raises `BackendError`; it is never converted to absence or success. The fake completes operations immediately. T19 must live-confirm the list-label filter expression/escaping and the `/wait` behavior when Google's server-side wait returns an unchanged non-`DONE` operation; unit tests must not pretend those provider behaviors were observed.

`requestId` must be a non-zero RFC-4122 UUID. Derive stable UUIDv5 values from operation kind plus immutable identity (for instance insert, `spec.deployment_id` and VMID), not from Python's randomized `hash()`. A retried insert therefore returns the original operation and cannot create a second instance.

The fake is stateful, not a call spy. `FakeGCESession` owns dictionaries keyed by provider identity for instances `(zone, name)`, subnetworks `(region, name)`, firewalls `name`, operations `(scope, location, name)`, and request-id results `(method, path, requestId)`. It exposes no ordered `calls` list. Its response object implements `status_code`, `json()`, and `raise_for_status()` with `requests.HTTPError(response=...)`. It implements all endpoint families needed by both T13 and T14: instances insert/get/delete/list/setLabels/start/stop, zonal/regional/global operation wait, subnetworks insert/delete/list, and firewalls insert/delete/list. Mutations create terminal operations; absent resources return 404; stale `labelFingerprint` returns 412; a repeated insert with the same endpoint and `requestId` returns the original operation without changing state. A `label_conflicts_remaining` counter lets a test create a real setLabels race; pagination is configurable; an injected next-operation error yields `DONE` plus `error.errors` without applying the mutation. Tests assert dictionary state and resource fields, never request order.

Cloud admission must not fork AWS's cap algorithm: `admit` delegates to T11's `reserve_cloud_capacity(BACKEND_GCP, GCP_ZONE, GCP_MAX_VMS, reservation_id)`. That shared helper atomically counts live GCP records plus unexpired GCP node reservations and writes the one-unit lease under `NODE_ADMISSION`. `renew_admission` and `release_admission` delegate to the existing node-reservation helpers.

- [ ] **Step 1: Write the failing tests** — in `tests/backends/test_gcp_backend.py`, define fixed neutral values (`PROJECT='example-project'`, `REGION='us-central1'`, `ZONE='us-central1-a'`, `SITE='test-site'`, deployment id `0123456789abcdef0123456789abcdef`, VMID `101`, slot `100`, and image `projects/example-project/global/images/lab-image`) and a `VMSpec` fixture. Patch the module config values, inject `FakeGCESession`, pre-create the slot subnetwork in fake state, and add these tests (each retains the one-line bug comment shown):

  ```python
  def test_create_has_no_external_ip_and_owns_disk_labels_and_tags(backend, gce, spec):
      # Catches a public-IP or detached-boot-disk billing leak in the insert body.
      launched = backend.create(spec, ZONE, heartbeat=lambda: None)
      instance = gce.instances[(ZONE, 'lp-test-site-101')]
      assert launched == LaunchedVM(101, 'gcp', ZONE, 100, 'lp-test-site-101')
      assert instance['disks'][0]['boot'] is True
      assert instance['disks'][0]['autoDelete'] is True
      assert instance['disks'][0]['initializeParams'] == {
          'sourceImage': 'projects/example-project/global/images/lab-image',
          'diskSizeGb': '30',
      }
      nic = instance['networkInterfaces'][0]
      assert nic['subnetwork'].endswith('/regions/us-central1/subnetworks/lp-test-site-s-100')
      assert 'accessConfigs' not in nic
      assert instance['labels'] == {
          TAG_SITE: SITE, TAG_VMID: '101', TAG_DEPLOYMENT: spec.deployment_id,
          TAG_EXPIRES: str(expires_epoch(spec.expires_at)), TAG_SLOT: '100',
          TAG_KIND: 'instance',
      }
      assert instance['tags']['items'] == [
          'lp-test-site-d-0123456789abcdef', 'lp-test-site-lab']
  ```

  Add `test_create_retry_uses_request_id_once` (invoke `create` twice with the same spec and assert `list(gce.instances) == [(ZONE, 'lp-test-site-101')]` and both launched refs have the same provider id), `test_get_operations_map_identity_status_and_private_ips` (matching and deliberately relabelled resources produce `GUEST_MATCH`/`GUEST_MISMATCH`; status is `running`; IPs contain only the fake's `networkIP`), `test_absent_is_positive_but_transport_failure_is_unknown` (404 gives `GUEST_ABSENT` and `confirm_absent=True`, injected 500 gives `GUEST_UNKNOWN` and `confirm_absent=False`), `test_destroy_all_policies_delete_the_instance_and_auto_deleted_disk` (parametrize all three policies from fresh state), `test_set_expiry_retries_one_412_and_preserves_labels` (set `gce.label_conflicts_remaining = 1`, assert the new decimal expiry and every other label remain), `test_set_expiry_stops_after_three_conflicts`, `test_set_expiry_refuses_a_relabelled_instance_without_mutation`, `test_list_managed_pages_and_filters_foreign_labels` (page size 1; one owned and one foreign instance; assert only owned `ManagedResource` and exact parsed fields), `test_operation_error_and_timeout_never_report_success`, `test_admit_counts_live_records_and_pending_reservations_at_the_cap`, and `test_release_and_renew_admission_use_the_shared_lease`.

  In `tests/backends/test_gcp_import.py`, run this in a clean subprocess and assert exit zero:

  ```python
  from gevent import monkey
  monkey.patch_all()
  from app.backends.gcp import GCPBackend
  from google.auth.transport.requests import AuthorizedSession
  import requests, sys
  assert issubclass(AuthorizedSession, requests.Session)
  assert 'grpc' not in sys.modules
  ```

  Directly test the fake's 404, stale-fingerprint 412, and repeated-`requestId` behavior by asserting response status and final dictionaries; do not assert a sequence of requests.

- [ ] **Step 2: Run to verify they fail** — from the worktree root run `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_gcp_backend.py tests/backends/test_gcp_import.py -q`; expect collection/import failure because `app.backends.gcp` and `tests.backends.fake_gce` do not exist.

- [ ] **Step 3: Implement** — implement the fake and the instance/admission half of `GCPBackend` exactly above. Keep REST serialization and operation parsing in small private helpers shared with T14. Construct ADC/`AuthorizedSession` only when `session is None`. Catch `requests.RequestException` and JSON/type errors at provider boundaries and wrap them in `BackendError` except where `inspect`/`confirm_absent` deliberately return a guest state. Do not catch programmer errors or turn 403/429/5xx into absence. Do not add no-op or `NotImplementedError` network/router protocol methods; T14 adds those methods in the next task. Import `GCPBackend` in `app/backends/__init__.py` and add `BACKEND_GCP: GCPBackend` to `_DEFAULT_FACTORIES` so `reset_backends_for_tests()` restores the built-in factory. The T13 fake supports all T14 REST endpoints.

- [ ] **Step 4: Run to verify they pass** — run `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_gcp_backend.py tests/backends/test_gcp_import.py -q`, then the gevent smoke directly with `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -c "from gevent import monkey; monkey.patch_all(); from app.backends.gcp import GCPBackend; from google.auth.transport.requests import AuthorizedSession; import requests, sys; assert issubclass(AuthorizedSession, requests.Session); assert 'grpc' not in sys.modules"`, then the full unit suite `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`.

- [ ] **Step 5: Commit** — `git add app/backends/gcp.py app/backends/__init__.py tests/backends/fake_gce.py tests/backends/test_gcp_backend.py tests/backends/test_gcp_import.py && git commit -m "gcp: add fake REST session and instance backend" -m "Co-Authored-By: Claude <noreply@anthropic.com>"`

### Task 14: GCP slot networks, deployment firewalls, router lifecycle, and shared contract

**Files:**
- Modify: `app/backends/gcp.py` (`create`, `list_managed`, `delete_resource`, `cleanup_deployment`, `ensure_slot`, `release_slot`, `ensure_router_running`, `stop_router` and regional/global REST helpers)
- Modify: `tests/backends/fake_gce.py` (`make_gcp_backend_case`; endpoint behavior was established in T13)
- Modify: `tests/backends/test_gcp_backend.py` (network, firewall, inventory, deletion and router cases)
- Modify: `tests/backends/test_backend_contract.py` (`backend_case` fixture parameters/imports only; add the GCP builder from `tests.backends.fake_gce`)

**Interfaces:**
- Consumes: all T13 interfaces; `cloud_slot_cidr(base_cidr, slot)` from `app.backends.network_slots`; `router_lock_key(BACKEND_GCP)` and `slot_lock_key(BACKEND_GCP, slot)` from `app.backends.cloud_common`; `vm_lifecycle_lock(key)` from `app.proxmox_service`; config values `GCP_LAB_CIDR` and `GCP_ROUTER_INSTANCE`; T3's `BackendContractCase(backend: VMBackend, spec: VMSpec, expected_ips: list[str], expected_status: str, cleanup: Callable[[], None])`, its `ref(launched, location) -> VMRef`, and `backend_case = pytest.fixture(params=[...], ids=lambda factory: factory.__name__)` builder contract `(app) -> BackendContractCase`.
- Produces: a complete `GCPBackend` implementation of `cleanup_deployment(deployment_id) -> None`, `ensure_slot(slot) -> None`, `release_slot(slot) -> None`, `ensure_router_running() -> None`, `stop_router() -> None`, network-aware `create/list_managed/delete_resource`; `make_gcp_backend_case(app) -> BackendContractCase`; GCP included as one parameter in the shared contract suite.

**Context:** The operator provisions the VPC, cloud router instance, routes, IAM, and one static ingress rule from `100.64.0.0/10` to target tag `lp-<site>-lab` (T20). The portal must never create or delete that static tailnet rule. It creates only zero-cost slot subnetworks and one firewall rule per deployment. A deployment firewall is named `deployment_network_name(site, deployment_id)` and allows instances carrying that same network tag to reach one another. GCP firewall resources have no labels: portal ownership is a syntactically valid `lp-<site>-d-<16 lowercase hex>` name plus a parseable `creationTimestamp`; do not infer ownership from list position or from an operator rule that merely shares the broader site prefix.

Use these REST endpoints and fields:

- Slot creation: `POST /regions/{GCP_REGION}/subnetworks?requestId={uuid}` with `{'name': slot_network_name(site, slot), 'network': 'projects/{project}/global/networks/{GCP_NETWORK}', 'ipCidrRange': cloud_slot_cidr(GCP_LAB_CIDR, slot), 'privateIpGoogleAccess': False}`. Do not send the output-only `region` field in the insert body. It returns a regional operation. If insert reports an existing name, find it through the paginated list and accept it only when exact `name`, `network`, response `region`, and `ipCidrRange` agree; a collision raises `BackendError`. `GET /regions/{region}/subnetworks` reads paginated `items`, `nextPageToken`, `name`, `selfLink`, `network`, `region`, `ipCidrRange`, and `creationTimestamp`.
- Slot release: `DELETE /regions/{region}/subnetworks/{name}?requestId={uuid}` and wait for the regional operation. A direct 404 is idempotent success. Any in-use/409 error remains a failure so the caller leaves evidence for T15's retry.
- Deployment firewall creation, called idempotently by `create` before inserting its instance: `POST /global/firewalls?requestId={uuid}` with exactly `{'name': deployment_name, 'network': 'projects/{project}/global/networks/{GCP_NETWORK}', 'direction': 'INGRESS', 'priority': 1000, 'sourceTags': [deployment_name], 'targetTags': [deployment_name], 'allowed': [{'IPProtocol': 'all'}]}`. Do not add `sourceRanges`, `destinationRanges`, or an egress rule. If insert reports an existing name, find it through the paginated list and accept it only when all of network/direction/priority/sourceTags/targetTags/allowed agree; otherwise raise `BackendError` rather than attach a lab to a foreign rule. `GET /global/firewalls` reads paginated `items`, `nextPageToken`, `name`, `selfLink`, `network`, `direction`, `priority`, `sourceTags`, `targetTags`, `allowed`, and `creationTimestamp`.
- Deployment cleanup: use the paginated firewall list to find the exact `deployment_network_name(site, deployment_id)`. Absence is already clean; an exact-name object without a parseable `creationTimestamp` fails ownership validation with `BackendError`. Only an owned match may be removed with `DELETE /global/firewalls/{deployment_name}?requestId={uuid}` followed by its global operation. Never delete every resource sharing a prefix.
- Router lifecycle: `GET /zones/{GCP_ZONE}/instances/{GCP_ROUTER_INSTANCE}`, `POST /zones/{zone}/instances/{name}/start?requestId={uuid}`, and `POST /zones/{zone}/instances/{name}/stop?requestId={uuid}`, each mutation followed by its zonal operation and a bounded instance-state read. `ensure_router_running` is idempotent when already `RUNNING`; it starts `TERMINATED` and verifies `RUNNING`. `stop_router` is idempotent when `TERMINATED`; it stops `RUNNING` and verifies `TERMINATED`. Missing/mismatched router configuration and persistent transitional states raise `BackendError`. These methods do not create or label the operator-owned router and `list_managed` must not return it.

The backend must not reacquire caller-owned lifecycle locks. T7's cloud create order is fixed: acquire `vm_lifecycle_lock(router_lock_key(BACKEND_GCP))`; call `ensure_router_running`; while still holding the router lock, briefly acquire `vm_lifecycle_lock(slot_lock_key(BACKEND_GCP, slot))` for `ensure_slot`; release the slot lock; call `create`; then release the router lock. No path acquires the router lock while holding a slot lock. T16 separately holds the router lock across its authoritative idle re-check and `stop_router`; retirement holds only the slot lock for `release_slot`. Backend methods perform only provider operations. Direct T14 tests put router methods inside the router lock and slot methods inside the slot lock. Reacquiring the same key inside `GCPBackend` would raise because repository lifecycle locks are intentionally non-reentrant.

`list_managed()` returns all three kinds in one list: T13 label-owned instances; slot subnetworks whose names match exactly `lp-<site>-s-<decimal slot>`; and deployment firewalls whose names match exactly `lp-<site>-d-<16 lowercase hex>` and have a parseable `creationTimestamp`. For subnetworks set `kind='slot-network'`, `provider_id=selfLink or name`, parsed slot, region location, and creation epoch. For firewalls set `kind='deployment-network'`, `provider_id=selfLink or name`, `deployment_id` to the 16-character encoded deployment prefix, and creation epoch. Name parsing is anchored; `lp-test-site-lab`, another site's prefix, malformed suffixes, and resources without a creation timestamp are not managed. `delete_resource` dispatches only these three known kinds and re-reads the provider object before mutation: an instance must still have exact site and kind labels, a subnetwork must still match the anchored current-site slot name, and a firewall must still match the anchored current-site deployment name and have a parseable `creationTimestamp`. A missing object is idempotent success; failed ownership revalidation raises `BackendError` without DELETE. After revalidation, delete the instance by recorded zone/name, slot network by region/name, or deployment network by global firewall name and wait for its operation. Unknown kinds raise `BackendError`.

T19 must live-confirm that firewall `allowed: [{'IPProtocol': 'all'}]` plus only `sourceTags` is accepted unchanged by the selected project's Compute API. The fake models the public REST schema but is not evidence of project IAM, organization policy, quota, server-side normalization, or the exact label-filter parser deployed by the live API.

- [ ] **Step 1: Write the failing tests** — extend `tests/backends/test_gcp_backend.py` with state assertions (no ordered-call assertions):

  ```python
  def test_ensure_and_release_slot_are_idempotent_and_use_the_derived_cidr(backend, gce):
      # Catches two slot owners being placed on an overlapping/default network.
      with vm_lifecycle_lock(slot_lock_key(BACKEND_GCP, 100)):
          backend.ensure_slot(100)
          backend.ensure_slot(100)
      subnet = gce.subnetworks[(REGION, 'lp-test-site-s-100')]
      assert subnet['ipCidrRange'] == '172.21.25.0/26'
      assert subnet['network'].endswith('/global/networks/lab-vpc')
      assert len(gce.subnetworks) == 1
      with vm_lifecycle_lock(slot_lock_key(BACKEND_GCP, 100)):
          backend.release_slot(100)
          backend.release_slot(100)
      assert gce.subnetworks == {}

  def test_create_uses_only_the_per_deployment_firewall(backend, gce, spec):
      # Catches broad lateral ingress or accidental ownership of the static tailnet rule.
      backend.ensure_slot(100)
      backend.create(spec, ZONE, heartbeat=lambda: None)
      name = 'lp-test-site-d-0123456789abcdef'
      assert list(gce.firewalls) == [name]
      assert gce.firewalls[name] == {
          **gce.firewall_server_fields(name),
          'name': name,
          'network': 'projects/example-project/global/networks/lab-vpc',
          'direction': 'INGRESS',
          'priority': 1000,
          'sourceTags': [name],
          'targetTags': [name],
          'allowed': [{'IPProtocol': 'all'}],
      }
      assert gce.instances[(ZONE, 'lp-test-site-101')]['tags']['items'] == [
          name, 'lp-test-site-lab']
      assert 'lp-test-site-lab' not in gce.firewalls
  ```

  If `firewall_server_fields()` would make the assertion opaque, assert each named field plus exact key absence for `sourceRanges`/`destinationRanges`; keep assertions against fake resource state, not requests. Add `test_existing_firewall_must_match_every_security_field`, `test_cleanup_deployment_deletes_only_its_exact_firewall_and_is_idempotent`, `test_list_managed_parses_only_owned_subnets_and_firewalls_with_timestamps`, `test_delete_resource_removes_each_known_kind_and_rejects_unknown_kind`, `test_delete_resource_rereads_and_refuses_forged_or_relabelled_ownership`, and `test_network_operation_failure_keeps_the_resource_visible_for_retry`.

  Add the router state test:

  ```python
  def test_router_start_and_stop_use_the_reserved_lock_boundary(backend, gce):
      # Catches a launch racing an idle-stop and losing its only VPN route.
      gce.add_router(ZONE, 'lab-router', status='TERMINATED')
      with vm_lifecycle_lock(router_lock_key(BACKEND_GCP)):
          backend.ensure_router_running()
      assert gce.instances[(ZONE, 'lab-router')]['status'] == 'RUNNING'
      with vm_lifecycle_lock(router_lock_key(BACKEND_GCP)):
          backend.stop_router()
      assert gce.instances[(ZONE, 'lab-router')]['status'] == 'TERMINATED'
      assert all(r.name != 'lab-router' for r in backend.list_managed())
  ```

  Add separate cases for already-running/already-stopped idempotency, absent router failure, and an operation that completes without reaching the required instance state.

  In `tests/backends/fake_gce.py`, implement `make_gcp_backend_case(app)`: start scoped `unittest.mock.patch.multiple` patches for the neutral GCP config, seed a stopped operator router, create a `FakeGCESession`, construct `GCPBackend(session=fake)`, and return T3's `BackendContractCase` with a GCP `VMSpec`, exact expected private IP, `expected_status='running'`, and cleanup that stops patches and clears fake state. The builder must not mutate process configuration without restoring it.

  In `tests/backends/test_backend_contract.py`, import `make_gcp_backend_case` and append exactly that callable to T3/T12's existing `backend_case` fixture parameter list:

  ```python
  @pytest.fixture(
      params=[make_memory_backend_case, make_proxmox_backend_case,
              make_aws_backend_case, make_gcp_backend_case],
      ids=lambda factory: factory.__name__,
  )
  def backend_case(request, app):
      case = request.param(app)
      try:
          yield case
      finally:
          case.cleanup()
  ```

  Do not duplicate or weaken `test_backend_lifecycle_contract`, `test_backend_destroy_policies`, or `test_backend_cleanup_invariant`; the new parameter must run their unchanged assertions, including that no managed resource carrying the deployment id remains after instance/deployment/slot cleanup.

- [ ] **Step 2: Run to verify they fail** — run `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_gcp_backend.py tests/backends/test_backend_contract.py -q`; expect network/router tests to fail on T13's incomplete methods and the shared contract to fail for the new GCP parameter.

- [ ] **Step 3: Implement** — implement the regional/global operations and inventory exactly above, reusing T13's request, pagination, idempotency and operation-wait helpers. Make `create` verify/create the exact deployment firewall before instance insert; do not create the static tailnet rule. Keep caller-owned router/slot locks outside backend methods to avoid non-reentrant nested acquisition. Complete `make_gcp_backend_case` and add its single shared-suite parameter.

- [ ] **Step 4: Run to verify they pass** — run `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/backends/test_gcp_backend.py tests/backends/test_backend_contract.py -q`, then the full unit suite `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`.

- [ ] **Step 5: Commit** — `git add app/backends/gcp.py tests/backends/fake_gce.py tests/backends/test_gcp_backend.py tests/backends/test_backend_contract.py && git commit -m "gcp: add networks router and backend contract" -m "Co-Authored-By: Claude <noreply@anthropic.com>"`


## Phase 4 — Cross-cutting

### Task 15: Reap site-owned cloud resources without changing the Proxmox sweep

**Files:**
- Modify: `app/orphan_reaper.py` (`maybe_reap_orphaned_vms`, `reap_orphaned_vms`, new cloud-resource eligibility helpers)
- Modify: `tests/test_orphan_reaper.py` (stateful cloud sweep cases; leave every existing Proxmox assertion intact)
- Test: `tests/test_vm_orphan_cleanup.py` (unchanged positive-evidence regression suite)

**Interfaces:**
- Consumes: `enabled_backends() -> Tuple[str, ...]`, `get_backend(name: str) -> VMBackend`; `ManagedResource`; `VMBackend.list_managed() -> List[ManagedResource]` and `delete_resource(resource: ManagedResource) -> None`; `BACKEND_AWS`, `BACKEND_GCP`; `VMRecord.backend`, `.deployment_id`, `.network_slot`, `.destroyed`; `VMIDReservation.vmid`, `.reservation_id`, `.expires_at`; `site_prefix(LAB_SITE_ID)`; the T12/T14 guarantee that `list_managed()` returns only this site and `delete_resource()` re-reads provider state and refuses a resource that no longer has this site's exact tag/name-prefix ownership evidence.
- Produces: `reap_orphaned_cloud_resources(*, now_epoch: Optional[float] = None, grace_minutes: Optional[int] = None, dry_run: Optional[bool] = None, max_seconds: Optional[float] = None, max_candidates: Optional[int] = None, clock: Optional[Callable[[], float]] = None) -> List[dict]`; `reap_orphaned_vms(proxmox=None, ...) -> List[dict]` remains the combined entry point; `maybe_reap_orphaned_vms(proxmox=None, *, monotonic_now=None) -> List[dict]` keeps its kill switch and ten-minute throttle.

**Context:** The current Proxmox path protects any retained `VMRecord` (including a destroyed row), KOTH references, reservations, templates, and non-`student-`/`group-` names; it rechecks under `vm_lifecycle_lock(vmid)` and requires fresh inventory before treating a provider error as absence. Preserve that code and all existing tests byte-for-behaviour. Add cloud inventory after the Proxmox pass, using the same pass start time and remaining item budget. Cloud eligibility is deliberately different and exact: an instance needs no live (`destroyed=False`) record for `(backend, vmid)`, no unexpired VMID reservation for its VMID, a parseable expiry tag, and `expiry + grace < now`; a deployment network needs no live record, unexpired reservation, or managed instance with its deployment id and `created_at + grace < now`; a slot network needs no live record on that backend/slot, no managed instance in that slot, and `created_at + grace < now`. Missing timestamps, malformed identities, templates, backend mismatches, provider/list failures, and ownership refusals are unknown—not permission to delete. `delete_resource()` returning is the positive evidence of absence; an exception yields `delete_failed` and leaves the resource for a later pass.

- [ ] **Step 1: Write the failing cloud sweep tests without weakening the Proxmox tests**

  Add a small stateful `_CloudSweepBackend` in `tests/test_orphan_reaper.py`: `list_managed()` returns its current resources and `delete_resource()` removes the exact object only after checking `resource.backend == self.name`; an optional failure set raises `BackendError`. Register it through `register_backend_factory`, patch `enabled_backends`, and reset the registry after each test. Pin these production bugs with comments and assertions:

  ```python
  def test_cloud_instance_needs_no_record_no_vmid_lease_and_expiry_past_grace(app):
      # Catches a stale provider expiry deleting a still-tracked or in-flight VM.
      resources = [
          managed_instance("aws", "i-delete", 801, expires=NOW_EPOCH - 3601),
          managed_instance("aws", "i-record", 802, expires=NOW_EPOCH - 3601),
          managed_instance("aws", "i-reserved", 803, expires=NOW_EPOCH - 3601),
          managed_instance("aws", "i-young", 804, expires=NOW_EPOCH - 3599),
          managed_instance("aws", "i-untimed", 805, expires=None),
      ]
      add_live_record(backend="aws", vmid=802)
      add_live_vmid_reservation(vmid=803, expires_at=NOW_EPOCH + 60)
      reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60)
      assert [(r["provider_id"], r["status"]) for r in reports] == [("i-delete", "reaped")]
      assert {r.provider_id for r in backend.resources} == {"i-record", "i-reserved", "i-young", "i-untimed"}

  def test_deployment_network_requires_no_record_reservation_or_instance_and_old_age(app):
      # Catches removal of a firewall/SG while its deployment is live or creating.
      reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60)
      assert deleted_ids(backend) == {"net-orphan"}
      assert deleted_ids(backend).isdisjoint({"net-record", "net-reservation", "net-instance", "net-young", "net-untimed"})
      assert reports == [report("aws", "deployment-network", "net-orphan", "reaped")]

  def test_slot_network_requires_no_same_backend_record_or_instance_and_old_age(app):
      # Catches deleting a shared slot subnet still used by a VM.
      reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60)
      assert deleted_ids(backend) == {"slot-orphan", "slot-other-backend-record"}
      assert deleted_ids(backend).isdisjoint(
          {"slot-record", "slot-instance", "slot-young", "slot-untimed"})
  ```

  In the slot case, seed `slot-other-backend-record` with only a GCP record for the same numeric slot; it remains eligible on AWS and proves the query is scoped by `(backend, network_slot)`. Also add:

  ```python
  def test_cloud_dry_run_reports_but_mutates_nothing(app):
      before = list(backend.resources)
      reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, dry_run=True)
      assert all(r["status"] == "dry_run" for r in reports)
      assert backend.resources == before

  def test_cloud_item_and_time_budgets_stop_starting_new_deletes(app):
      assert len(reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, max_candidates=2)) == 2
      assert len(backend.deleted) == 2
      backend.reset_with_old_orphans(5, advance_clock_per_delete=61)
      assert len(reap_orphaned_cloud_resources(
          now_epoch=NOW_EPOCH, max_seconds=120, clock=backend.clock)) == 2
      assert len(backend.deleted) == 2

  def test_cloud_delete_refusal_is_reported_and_resource_remains(app):
      reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH)
      assert reports[0]["status"] == "delete_failed"
      assert backend.resources == [refused]
  ```

- [ ] **Step 2: Run the focused tests and verify the new tests fail**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_orphan_reaper.py tests/test_vm_orphan_cleanup.py -q`

  Expected: FAIL because `reap_orphaned_cloud_resources` and the cloud inventory pass do not exist; all pre-existing Proxmox and positive-evidence tests still pass.

- [ ] **Step 3: Implement the cloud pass behind the existing schedule and safety guards**

  In `app/orphan_reaper.py`, keep the current Proxmox candidate loop and `_recheck_and_reap` semantics unchanged. Build one inventory snapshot per enabled cloud backend, reject any row whose `.backend` differs, and index managed instances by deployment and slot. Query live records and unexpired VMID reservations once per sweep, not once per resource. Use epoch seconds for provider tags/reservations and the existing configured grace in minutes. Check the elapsed/item budget immediately before each `delete_resource`; count dry-runs, successful deletes, and failed delete attempts as started items. A provider inventory exception aborts that backend with no deletes. Combine Proxmox and cloud reports while sharing `ORPHAN_REAPER_MAX_PASS_SECONDS` and `ORPHAN_REAPER_MAX_PASS_GUESTS`; do not rename the config keys in this task. Never call a raw SDK from the reaper.

- [ ] **Step 4: Run focused and full unit suites**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_orphan_reaper.py tests/test_vm_orphan_cleanup.py -q`

  Expected: PASS, including every existing Proxmox budget, lock, KOTH, dry-run, and fresh-inventory assertion.

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`

  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add app/orphan_reaper.py tests/test_orphan_reaper.py
  git commit -m "cleanup: reap orphaned cloud resources" \
    -m "Co-Authored-By: Claude <noreply@anthropic.com>"
  ```

### Task 16: Stop idle cloud routers under the lifecycle lock

**Files:**
- Modify: `app/tasks.py` (`check_and_destroy_expired_vms`, new `_stop_idle_cloud_routers` and process-local idle state)
- Modify: `tests/test_tasks.py` (router-idle state, lock race, and expiry-pass ordering)

**Interfaces:**
- Consumes: `enabled_backends()`, `get_backend(name)`, `VMBackend.stop_router()`, `router_lock_key(backend: str) -> int`, `vm_lifecycle_lock(key)`, `NodeMemoryReservation` rows where `.node == backend` and `.expires_at` is epoch seconds, `VMRecord.backend`/`.destroyed`, `AWS_ROUTER_IDLE_MINUTES`, `GCP_ROUTER_IDLE_MINUTES`.
- Produces: `_stop_idle_cloud_routers(*, now_epoch: Optional[float] = None) -> List[str]` returning backend names successfully stopped; `_reset_router_idle_state_for_tests() -> None`.

**Context:** There is no durable last-activity column: normal VM rows are hard-deleted and admission reservations are released. Maintain conservative process-local `idle_since[backend]` state. Under the reserved router lifecycle lock, roll back the session, then query for any live record or unexpired node reservation on that backend. Any workload clears the idle timestamp. The first continuously-idle observation stores `now_epoch`; stop only after the configured whole interval has elapsed. A worker restart therefore waits a fresh interval (small extra router cost, never a premature stop). Hold `vm_lifecycle_lock(router_lock_key(backend))` across the fresh idle check and `stop_router()`, matching the launch-side lock. Run this step after ordinary expiry, missing-record reconcile, alert delivery, and the time-boxed orphan sweep, so shared cleanup cannot delay destruction of an expired VM.

- [ ] **Step 1: Write failing fake-backend tests for idle timing, reservations, locking, and order**

  Use a stateful fake backend whose `stop_router()` changes `router_running` to `False`. Register it rather than asserting mock call lists.

  ```python
  def test_router_stops_only_after_one_continuous_idle_window(app):
      # Catches stopping immediately when the last row disappears.
      add_live_record(backend="aws")
      assert _stop_idle_cloud_routers(now_epoch=1_000) == []
      retire_record()
      assert _stop_idle_cloud_routers(now_epoch=1_001) == []
      assert _stop_idle_cloud_routers(now_epoch=2_800) == []
      assert _stop_idle_cloud_routers(now_epoch=2_801) == ["aws"]
      assert backend.router_running is False

  def test_live_record_or_unexpired_admission_reservation_resets_idle_window(app):
      # Catches a stop racing a new deployment that has reserved capacity but no VMRecord yet.
      observe_idle_at(1_000)
      add_node_reservation(node="aws", expires_at=2_000)
      assert _stop_idle_cloud_routers(now_epoch=1_100) == []
      remove_reservation()
      assert _stop_idle_cloud_routers(now_epoch=1_200) == []  # first idle observation
      assert _stop_idle_cloud_routers(now_epoch=2_999) == []
      assert backend.router_running is True
      assert _stop_idle_cloud_routers(now_epoch=3_000) == ["aws"]

  def test_router_lock_rechecks_workload_before_stop(app):
      # Catches launch admission landing while the idle worker waits for the router lock.
      arrange_lock_owner_to_insert_reservation_before_yield(backend="aws")
      assert _stop_idle_cloud_routers(now_epoch=10_000) == []
      assert backend.router_running is True

  def test_router_idle_step_runs_after_reconcile_alerts_and_orphan_sweep(app):
      # Catches shared-cost cleanup moving ahead of the safety-critical expiry phases.
      _run_one_expiry_pass(app)
      assert events == ["destroy", "reconcile", "alerts", "orphans", "routers"]
  ```

  Add a separate assertion that a `stop_router()` exception keeps `router_running=True`, returns no backend, and retains the elapsed idle timestamp so the next pass retries rather than claiming success.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_tasks.py -q`

  Expected: FAIL because the router idle helper and post-reaper step do not exist.

- [ ] **Step 3: Implement conservative idle tracking and wire it last in the expiry pass**

  Add one module-level lock-protected dictionary keyed by `aws`/`gcp`; expose only the reset helper for deterministic tests. `_stop_idle_cloud_routers` must ignore Proxmox, use the backend-specific configured minutes, perform the final DB queries inside `vm_lifecycle_lock(router_lock_key(name))`, and never hold a SQL transaction open across the provider waiter beyond the read already completed. Retain the timestamp on failure; after a successful stop, remove it so an already-stopped router is not called every minute. Log and isolate one backend's failure so the other backend and next expiry pass continue. Invoke it immediately after `maybe_reap_orphaned_vms(...)` in `check_and_destroy_expired_vms`.

- [ ] **Step 4: Run focused and full unit suites**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_tasks.py -q`

  Expected: PASS.

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`

  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add app/tasks.py tests/test_tasks.py
  git commit -m "cleanup: stop cloud routers after idle grace" \
    -m "Co-Authored-By: Claude <noreply@anthropic.com>"
  ```

### Task 17: Generate and apply per-student Headscale policy

**Files:**
- Create: `scripts/export_network_slots.py`
- Create: `scripts/headscale_policy.py`
- Create: `scripts/apply_headscale_policy.sh`
- Create: `tests/test_export_network_slots.py`
- Create: `tests/test_headscale_policy.py`
- Modify: `scripts/term_changeover.sh` (fourth, final policy stage after database replacement)
- Modify: `scripts/site.env.example` (policy file, VLAN bands, enabled backend/CIDR inputs)
- Modify: `tests/test_ops_scripts.py` (PATH-stubbed SSH/Docker orchestration and term-changeover integration)

**Interfaces:**
- Consumes: `StudentWhitelist`, `StudentGroup`, `StudentGroupMember`, `create_app()`; `parse_vlan_bands`, `proxmox_slot_cidr`, `cloud_slot_cidr`; `PORTAL_HOST`, `PORTAL_DIR`, `PORTAL_COMPOSE`, `PORTAL_APP_CONTAINER`, `HEADSCALE_HOST`, `HEADSCALE_DIR`, `HEADSCALE_BASE_POLICY` (default `config/headscale-acl.json`), `HEADSCALE_POLICY_FILE` (default `/etc/headscale/acl.json` in the container), `JUMPBOX_VLAN_BANDS`, `ENABLED_BACKENDS`, `AWS_LAB_CIDR`, `GCP_LAB_CIDR` loaded by `scripts/site_env.sh`; Headscale v0.27 `headscale policy get` and `headscale policy check --file FILE`.
- Produces: `collect_network_slots() -> Dict[str, List[dict]]`; stdout JSON exactly `{"users":[{"login", "entry_vlan", "internal_vlan"}], "groups":[{"slug", "members", "entry_vlan", "internal_vlan"}]}` with both arrays and each membership sorted; `PolicyBands(values: Tuple[VlanBand, ...], proxmox_enabled: bool)`; `render(base_policy: Mapping[str, Any], slots: Mapping[str, Any], bands: PolicyBands, cloud_cidrs: Mapping[str, str]) -> Dict[str, Any]`; `policy_diff(current: Mapping[str, Any], desired: Mapping[str, Any]) -> str`; CLI subcommands `render` and `diff`; dry-run-default `scripts/apply_headscale_policy.sh [--apply]`.

**Context:** The export runs **inside the portal application container**, so it may import the Flask app and query the DB. `StudentWhitelist` is the complete term roster even before first login; use it for `users`. Group membership comes from `StudentGroup.members -> User.login_id`. Refuse incomplete VLAN pairs and group members absent from the exported roster instead of silently granting a partial policy. The renderer is pure—no Flask, DB, SSH, subprocess, environment, or filesystem access in `render`. `PolicyBands.values` remains the single source of which slot kinds are routed on every backend, while `proxmox_enabled` prevents a cloud-only portal from granting access to stale Proxmox ranges. Headscale v0.27 identities are `login@`; network prefixes are valid in both `src` and `dst`. Rebuild `acls` rather than retaining the base blanket member rule, while copying the base `groups`, `tagOwners`, and `hosts` unchanged.

- [ ] **Step 1: Write failing tests for export, pure rendering, orchestration, and term integration**

  In `tests/test_export_network_slots.py`, seed whitelist rows in reverse order plus a group and memberships, then assert the exact sorted object. Add failing tests for a user/group missing either VLAN and a group member not in the whitelist.

  ```python
  assert collect_network_slots() == {
      "users": [
          {"login": "alice", "entry_vlan": 100, "internal_vlan": 200},
          {"login": "bob", "entry_vlan": 101, "internal_vlan": 201},
      ],
      "groups": [{
          "slug": "red-team", "members": ["alice", "bob"],
          "entry_vlan": 300, "internal_vlan": 400,
      }],
  }
  ```

  In `tests/test_headscale_policy.py`, use the documented bands and AWS/GCP defaults. Pin the exact reachable blocks for Alice (own entry plus her group entry/internal, never user-internal slot 200): Proxmox `10.100.0.0/24`, `10.100.2.0/24`, `10.100.4.0/24`; AWS `172.20.25.0/26`, `172.20.75.0/26`, `172.20.100.0/26`; GCP `172.21.25.0/26`, `172.21.75.0/26`, `172.21.100.0/26`.

  ```python
  policy = render(
      base, slots,
      PolicyBands(tuple(parse_vlan_bands(DEFAULT_BANDS)), proxmox_enabled=True),
      {"aws": "172.20.0.0/16", "gcp": "172.21.0.0/16"},
  )
  assert policy["groups"] == base["groups"]
  assert policy["tagOwners"] == base["tagOwners"]
  assert policy["hosts"] == base["hosts"]
  assert policy["acls"][0] == {"action": "accept", "src": ["group:staff"], "dst": ["*:*"]}
  alice_forward = next(r for r in policy["acls"] if r["src"] == ["alice@"])
  assert alice_forward["dst"] == [f"{cidr}:*" for cidr in EXPECTED_ALICE_CIDRS]
  alice_reverse = next(r for r in policy["acls"] if r["dst"] == ["alice@:*"])
  assert alice_reverse["src"] == EXPECTED_ALICE_CIDRS
  assert "autogroup:member" not in json.dumps(policy)
  assert all("200" not in dst for dst in alice_forward["dst"])
  ```

  Also assert deterministic user/CIDR ordering, invalid/overlapping inputs raise `ValueError`, a disabled cloud is absent, no group membership grants another group's blocks, and `diff` emits a unified removal of the blanket rule plus additions of `login@` rules.

  In `tests/test_ops_scripts.py`, use executable `ssh` and `docker` stubs on `PATH` backed by temporary JSON/files (not mock call assertions):

  * dry run exports slots from the portal, reads `headscale policy get`, prints a diff, and leaves the policy file/backups/container restart count unchanged;
  * `--apply` runs `policy check --file` before installation, creates one timestamped owner-readable backup, atomically installs the desired JSON, and a matching read-back causes zero restarts;
  * a deliberately stale first read-back causes exactly one Headscale service restart and a second read-back must equal the desired canonical JSON; a still-different second read-back exits nonzero;
  * failed `policy check` leaves policy and backups unchanged;
  * `term_changeover.sh` invokes policy dry-run after its database dry-run, and invokes `apply_headscale_policy.sh --apply` only after the apply path has purged/seeded the new roster DB.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_export_network_slots.py tests/test_headscale_policy.py tests/test_ops_scripts.py -q`

  Expected: FAIL because the three scripts, pure functions, and fourth changeover stage do not exist.

- [ ] **Step 3: Implement export, rendering, safe apply, and changeover integration**

  `export_network_slots.py` must call `create_app()`, enter its app context, serialize compact JSON to stdout only, and send errors to stderr with nonzero exit. `headscale_policy.py render` reads base/slots JSON paths, `--bands`, `--proxmox-enabled`, and repeatable `--cloud-cidr backend=CIDR`; it emits canonical `indent=2, sort_keys=True` JSON. For each login, emit one forward ACL from `login@` to every routed own/group block on each enabled backend, and one reverse ACL from those IP prefixes to `login@:*`; prepend the staff-all rule. Do not preserve any base `acls`, especially `autogroup:member`.

  `apply_headscale_policy.sh` must follow `term_changeover.sh` quoting and `SSH_OPTIONS`: validate required site variables before connecting; export slots with `ssh PORTAL_HOST 'cd PORTAL_DIR && docker compose -f PORTAL_COMPOSE exec -T PORTAL_APP_CONTAINER python scripts/export_network_slots.py'`; render locally; fetch the effective policy with `ssh HEADSCALE_HOST 'cd HEADSCALE_DIR && docker compose exec -T headscale headscale policy get'`; and print canonical diff. Dry-run performs no remote writes. Apply uploads to a remote temporary file, validates inside the Headscale container with `headscale policy check --file`, backs up `/etc/headscale/acl.json` with a UTC `YYYYmmddTHHMMSSZ` suffix, atomically installs, and reads back. Restart only the `headscale` service when canonical read-back differs; read back again after that single restart and fail closed if still different. Never print policy secrets or put untrusted file contents into shell code.

  Add the needed neutral values to `scripts/site.env.example`. The apply script passes `--proxmox-enabled` only when `proxmox` appears in `ENABLED_BACKENDS`, and emits `--cloud-cidr` only for each enabled cloud; thus a cloud-only site gets no stale Proxmox grants. In `term_changeover.sh`, relabel the existing output as `[1/4]` through `[3/4]`, make the policy the final `[4/4]` stage after the production database stage, forward the same `SITE_ENV_FILE`, and preserve the existing stages' ordering and dry-run guarantees.

- [ ] **Step 4: Run focused and full unit suites**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_export_network_slots.py tests/test_headscale_policy.py tests/test_ops_scripts.py -q`

  Expected: PASS.

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`

  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/export_network_slots.py scripts/headscale_policy.py \
    scripts/apply_headscale_policy.sh scripts/term_changeover.sh \
    scripts/site.env.example tests/test_export_network_slots.py \
    tests/test_headscale_policy.py tests/test_ops_scripts.py
  git commit -m "networking: apply per-student Headscale policy" \
    -m "Co-Authored-By: Claude <noreply@anthropic.com>"
  ```

### Task 18: Isolate routed lab VLANs on the jumpbox

**Files:**
- Modify: `scripts/jumpbox_netgen.py` (`render_vlan_isolation`, unit renderer, CLI choices)
- Modify: `scripts/jumpbox_configure.sh` (`--isolate-vlans` planning, validation, install, activation)
- Modify: `tests/test_jumpbox_scripts.py` (ruleset semantics, dry-run, validation, persistence, idempotence, reload safety)

**Interfaces:**
- Consumes: existing `stage`, `install_file`, `run`, and dry-run/apply conventions in `jumpbox_configure.sh`.
- Produces: `render_vlan_isolation() -> str`; `render_vlan_isolation_unit(nft_file: str = "/etc/nftables.d/jumpbox-vlan-isolation.nft") -> str`; `jumpbox_netgen.py isolate-vlans` and `isolate-vlans-unit`; `jumpbox_configure.sh --isolate-vlans`.

**Context:** The Headscale policy governs tailnet-to-VLAN traffic, but lab VMs can currently route laterally through the jumpbox. The nft table must have a forward hook with policy accept and only drop packets whose input **and** output interfaces match `vlan*`; therefore `tailscale0 -> vlan*`, `vlan* -> tailscale0`, management, and same-VLAN traffic remain unaffected. Same-VLAN traffic does not traverse the forward hook. Applying this option to the live jumpbox remains an instructor action after merge.

- [ ] **Step 1: Write failing render and configure tests**

  ```python
  def test_vlan_isolation_drops_only_vlan_to_vlan_forwarding():
      # Catches a broad forward drop breaking VPN access to every lab.
      text = _netgen().render_vlan_isolation()
      assert "type filter hook forward priority -10; policy accept;" in text
      assert 'iifname "vlan*" oifname "vlan*" counter drop' in text
      assert "tailscale0" not in text
      assert "hook input" not in text

  def test_vlan_isolation_unit_is_persistent_and_reloadable():
      unit = _netgen().render_vlan_isolation_unit()
      assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
      assert "ExecStart=/usr/sbin/nft -f /etc/nftables.d/jumpbox-vlan-isolation.nft" in unit
      assert "ExecReload=/usr/sbin/nft -f /etc/nftables.d/jumpbox-vlan-isolation.nft" in unit
      assert "ExecStop=-/usr/sbin/nft delete table inet jumpbox_vlan_isolation" in unit
  ```

  Add CLI output tests and configure tests mirroring the existing staff-only harness:

  ```python
  def test_isolate_vlans_dry_run_writes_and_mutates_nothing(tmp_path):
      before = _tree(sysroot)
      result = _run(CONFIGURE, ["--isolate-vlans"], env)
      assert result.returncode == 0
      assert _tree(sysroot) == before and _mutations(_calls(tmp_path)) == []
      assert "would run: systemctl enable jumpbox-vlan-isolation.service" in result.stdout

  def test_isolate_vlans_apply_validates_installs_enables_and_converges(tmp_path):
      first = _run(CONFIGURE, ["--apply", "--no-try", "--isolate-vlans"], env)
      assert first.returncode == 0
      assert (sysroot / "etc/nftables.d/jumpbox-vlan-isolation.nft").exists()
      assert calls.index(nft_check_call) < calls.index("systemctl enable jumpbox-vlan-isolation.service")
      assert "systemctl start jumpbox-vlan-isolation.service" in calls
      assert second_apply_has_no_mutations()
  ```

  Also pin: `nft -c -f` failure leaves the tree untouched; missing `nft` blocks apply but only warns in dry-run; an active unit uses `systemctl reload-or-restart jumpbox-vlan-isolation.service` and never explicit `start`/`restart`; the generated rule and unit modes are `0644`.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_jumpbox_scripts.py -q`

  Expected: FAIL because the renderers, CLI choices, and configure option do not exist.

- [ ] **Step 3: Implement the isolated nft table and configure lifecycle**

  Add constants for table `jumpbox_vlan_isolation`, nft path `/etc/nftables.d/jumpbox-vlan-isolation.nft`, and unit `jumpbox-vlan-isolation.service`. Render declare/delete/recreate lines so repeated loads are idempotent. The systemd oneshot must load before networking, support atomic `ExecReload`, and delete only its own table on stop. In `jumpbox_configure.sh`, mirror `--ssh-staff-only`: render to the temp directory, require/validate with `nft -c -f` before any apply mutation, stage both files, install atomically, daemon-reload and enable, start only if inactive, and `reload-or-restart` if active. Keep dry-run as the default and do not combine this table with the SSH input table.

- [ ] **Step 4: Run focused and full unit suites**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_jumpbox_scripts.py -q`

  Expected: PASS.

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`

  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/jumpbox_netgen.py scripts/jumpbox_configure.sh \
    tests/test_jumpbox_scripts.py
  git commit -m "networking: isolate routed lab VLANs" \
    -m "Co-Authored-By: Claude <noreply@anthropic.com>"
  ```

### Task 19: Add opt-in live AWS and GCP contract runs

**Files:**
- Create: `tests/live/cloud_harness.py`
- Create: `tests/live/test_aws_contract.py`
- Create: `tests/live/test_gcp_contract.py`
- Create: `tests/test_live_cloud_contract_guards.py`
- Modify: `tests/live/conftest.py` (provider-specific markers, lazy backend fixtures)
- Modify: `tests/live/README.md` (cloud guard variables, invocation, manual cleanup)

**Interfaces:**
- Consumes: registry `get_backend(name)`; `VMBackend` lifecycle/network/router methods; `VMSpec`, `VMRef`, `DestroyPolicy.BEST_EFFORT`, `GUEST_MATCH`; `ManagedResource`; cloud configuration and template keys from T2/T4.
- Produces: `cloud_live_skip_reason(backend: str, environ: Mapping[str, str]) -> Optional[str]`; `CloudLiveConfig.from_env(backend: str, environ: Mapping[str, str]) -> CloudLiveConfig`; `CloudContractRun(backend: VMBackend, config: CloudLiveConfig)` context manager whose exit always tears down and then proves `backend.list_managed() == []` for the dedicated live site.

**Context:** These tests are destructive and must remain inert in CI. AWS requires `LIVE_AWS=1`, `ENABLED_BACKENDS` containing `aws`, `LAB_SITE_ID=live-<dedicated-name>`, `AWS_MAX_VMS=1`, the normal `AWS_REGION`, `AWS_AVAILABILITY_ZONE`, `AWS_LAB_VPC_ID`, `AWS_LAB_CIDR`, and `AWS_ROUTER_INSTANCE_ID`, plus `LIVE_AWS_IMAGE_ID` and `LIVE_AWS_INSTANCE_TYPE`. GCP requires `LIVE_GCP=1`, `ENABLED_BACKENDS` containing `gcp`, the dedicated `LAB_SITE_ID`, `GCP_MAX_VMS=1`, the normal `GCP_PROJECT`, `GCP_REGION`, `GCP_ZONE`, `GCP_NETWORK`, `GCP_LAB_CIDR`, and `GCP_ROUTER_INSTANCE`, plus `LIVE_GCP_IMAGE` and `LIVE_GCP_MACHINE_TYPE`. Credentials stay in boto3's standard chain or Google ADC and are never echoed. Every launched instance receives `expires_at = datetime.now() + timedelta(minutes=30)`. Preflight refuses to run when `list_managed()` is non-empty, so teardown may only touch the dedicated live site's resources. Teardown runs on body failure, releases admission, deletes the instance/deployment/slot resources, stops the router, and fails loudly unless a fresh post-teardown inventory is empty.

- [ ] **Step 1: Write failing CI guard tests and live contract modules**

  In `tests/test_live_cloud_contract_guards.py`, use the in-memory backend (no SDK/network):

  ```python
  @pytest.mark.parametrize(("backend", "flag"), [("aws", "LIVE_AWS"), ("gcp", "LIVE_GCP")])
  def test_cloud_live_run_is_skipped_without_explicit_opt_in(backend, flag):
      # Catches CI constructing a real cloud client during collection.
      assert f"{flag}=1" in cloud_live_skip_reason(backend, {})

  def test_cloud_live_guard_requires_dedicated_site_cap_one_and_thirty_minute_expiry():
      with pytest.raises(ValueError, match="live-"):
          CloudLiveConfig.from_env("aws", aws_env(LAB_SITE_ID="production"))
      with pytest.raises(ValueError, match="AWS_MAX_VMS=1"):
          CloudLiveConfig.from_env("aws", aws_env(AWS_MAX_VMS="2"))
      assert CloudLiveConfig.from_env("aws", aws_env()).expiry == timedelta(minutes=30)

  def test_cloud_live_preflight_refuses_existing_site_resources_without_deleting_them():
      run = CloudContractRun(fake_with_one_managed_resource(), config)
      with pytest.raises(UnsafeLiveRun):
          run.__enter__()
      assert fake.list_managed() == [existing]

  def test_cloud_live_cleanup_runs_after_injected_failure_and_proves_empty():
      with pytest.raises(InjectedFailure):
          with CloudContractRun(fake, config) as run:
              run.launch_one()
              raise InjectedFailure()
      assert fake.list_managed() == []
      assert fake.router_running is False
  ```

  Parametrize required-env omissions for each provider and assert no backend fixture imports/constructs a client when skipped. In `tests/live/test_aws_contract.py` and `test_gcp_contract.py`, mark with `pytest.mark.live` plus provider-specific marker and perform one backend lifecycle: admit cap 1; ensure router/slot; create from the configured image with 30-minute expiry; assert `inspect == GUEST_MATCH`, status is provider-running, IPs contain only private addresses, expiry update appears in managed inventory, and a second admission is refused at cap 1. Exit through `CloudContractRun`; after the context, assert a fresh `list_managed()` is empty.

- [ ] **Step 2: Run guard tests and skipped live modules to verify failure**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_live_cloud_contract_guards.py tests/live/test_aws_contract.py tests/live/test_gcp_contract.py -q -rs`

  Expected: FAIL because the cloud harness/markers do not exist; after implementation, with `LIVE_AWS`/`LIVE_GCP` unset, guard tests pass and every live cloud case reports SKIPPED without constructing SDK clients.

- [ ] **Step 3: Implement provider-specific collection guards and cleanup-first harness**

  Extend `pytest_collection_modifyitems` so `live_aws` uses only the AWS guard, `live_gcp` only the GCP guard, and existing unqualified `live` Proxmox tests keep their current `LIVE_PROXMOX` behavior. Register marker descriptions. Import `get_backend` only inside a fixture after its guard succeeds. `CloudContractRun` must claim each created `VMRef` before create returns, use backend methods only, and preserve a cleanup failure as teardown failure with provider IDs and manual inventory commands—never silently swallow it. Cleanup order is instance destroy/positive absence, deployment cleanup, slot release, admission release, router stop, final `list_managed`; if normal cleanup leaves site-owned rows, attempt `delete_resource` only on that fresh site-filtered inventory and then require a second empty inventory. Document exact opt-in variables and separate AWS/GCP invocation commands; no real IDs or projects enter tracked files.

- [ ] **Step 4: Run skipped-live guards and the full unit suite**

  Run: `env -u LIVE_AWS -u LIVE_GCP PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_live_cloud_contract_guards.py tests/live/test_aws_contract.py tests/live/test_gcp_contract.py -q -rs`

  Expected: guard tests PASS; all cloud live cases SKIPPED with explicit opt-in reasons; zero cloud connections.

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/ --ignore=tests/integration -q`

  Expected: PASS. Do not run either live provider without an operator intentionally supplying its dedicated environment.

- [ ] **Step 5: Commit**

  ```bash
  git add tests/live/cloud_harness.py tests/live/test_aws_contract.py \
    tests/live/test_gcp_contract.py tests/test_live_cloud_contract_guards.py \
    tests/live/conftest.py tests/live/README.md
  git commit -m "test: add guarded cloud live contracts" \
    -m "Co-Authored-By: Claude <noreply@anthropic.com>"
  ```

### Task 20: Document cloud operations, examples, migration, and rollback

**Files:**
- Create: `docs/CLOUD_BACKENDS.md`
- Create: `tests/test_cloud_docs.py`
- Modify: `.env.example` (all T2 AWS/GCP/backend variables with neutral values)
- Modify: `templates.example.yaml` (one additive AWS/GCP-capable deployment group; existing Proxmox example remains valid)
- Modify: `CHANGELOG.md` (`[Unreleased]` Added/Changed/Upgrade notes)
- Modify: `CLAUDE.md` (multi-backend architecture and cleanup/operator invariants)
- Modify: `docs/PRODUCTION_DEPLOYMENT.md` (migration list and cloud pre/post-deploy links)

**Interfaces:**
- Consumes: the exact config names/defaults from T2; `scripts/migrate_add_vm_backend.py`; `scripts/apply_headscale_policy.sh`; `scripts/jumpbox_configure.sh --isolate-vlans`.
- Produces: an operator runbook and neutral, copyable configuration examples; no runtime interface.

**Context:** This public repository must contain no real account/project/VPC/image/router identifiers. Use `<aws-region>`, `<aws-vpc-id>`, `<gcp-project>`, and documentation IP ranges/placeholders. Cloud base infrastructure is operator-created (not Terraform); lab instances have no public IP or internet route; per-deployment resources and instances must scale to zero. Applying Headscale policy and VLAN isolation to the live hosts is an instructor step after merge, not an automated deployment side effect.

- [ ] **Step 1: Write failing documentation/example consistency tests**

  In `tests/test_cloud_docs.py`, parse rather than substring-smoke-test where possible:

  ```python
  CLOUD_ENV = {
      "ENABLED_BACKENDS", "LAB_SITE_ID",
      "AWS_REGION", "AWS_AVAILABILITY_ZONE", "AWS_LAB_VPC_ID", "AWS_LAB_CIDR",
      "AWS_ROUTER_INSTANCE_ID", "AWS_MAX_VMS", "AWS_ROUTER_IDLE_MINUTES",
      "AWS_LAUNCH_TIMEOUT_SECONDS", "AWS_TERMINATE_TIMEOUT_SECONDS",
      "GCP_PROJECT", "GCP_REGION", "GCP_ZONE", "GCP_NETWORK", "GCP_LAB_CIDR",
      "GCP_ROUTER_INSTANCE", "GCP_MAX_VMS", "GCP_ROUTER_IDLE_MINUTES",
      "GCP_OPERATION_TIMEOUT_SECONDS",
  }

  def test_env_example_covers_every_cloud_config_name():
      keys = parse_env_keys(ROOT / ".env.example")
      assert CLOUD_ENV <= keys

  def test_template_example_has_valid_neutral_blocks_for_all_backends():
      catalog = yaml.safe_load((ROOT / "templates.example.yaml").read_text())
      machine = find_example_machine(catalog, "multi-backend-example")
      assert machine["aws"] == {"image_id": "<aws-ami-id>", "instance_type": "t3.medium", "disk_gb": 30}
      assert machine["gcp"] == {"image": "projects/<gcp-project>/global/images/<image-name>", "machine_type": "e2-medium", "disk_gb": 30}
      assert machine["proxmox"]["template_locations"]

  def test_cloud_runbook_contains_required_operator_sections_and_no_site_identity():
      headings = markdown_headings(ROOT / "docs/CLOUD_BACKENDS.md")
      assert REQUIRED_HEADINGS <= headings
      assert not REAL_ID_PATTERNS.search((ROOT / "docs/CLOUD_BACKENDS.md").read_text())
  ```

  Make `REQUIRED_HEADINGS` the exact headings listed in Step 3. Also assert both migration loops in `docs/PRODUCTION_DEPLOYMENT.md` include `migrate_add_vm_backend.py`, and the changelog Upgrade notes name the migration plus `ENABLED_BACKENDS` and `LAB_SITE_ID`.

- [ ] **Step 2: Run the docs tests and verify they fail**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_cloud_docs.py -q`

  Expected: FAIL because the cloud runbook and examples do not yet exist.

- [ ] **Step 3: Write the runbook and update every operator-facing example**

  Create `docs/CLOUD_BACKENDS.md` with these exact sections and facts:

  * `## Safety model and ownership`: `LAB_SITE_ID` isolation; no public lab IP/no NAT/no internet; delete-on-termination/autoDelete disks; caps and expiry; dry-run cleanup; never share a live-test site id.
  * `## Shared address plan`: slot-to-`/26` calculation, default AWS/GCP CIDRs, forbidden overlaps, VPC cost-free/shared model, per-deployment SG/firewall and first-use slot subnet lifecycle.
  * `## AWS one-time setup`: VPC plus public router subnet and private slot ranges; router `t4g.nano`; route `100.64.0.0/10` to the router ENI; disable source/destination check; no NAT gateway; router SSH only from staff addresses. Include a complete least-privilege IAM policy JSON: read-only `Describe*` actions; launch/tag actions constrained with `aws:RequestTag/lab-playground-site=<site-id>`/`aws:TagKeys`; `TerminateInstances`, `DeleteSecurityGroup`, and `DeleteSubnet` constrained by `ec2:ResourceTag/lab-playground-site=<site-id>`; start/stop limited to the placeholder router ARN; `Resource: "*"` only where EC2 does not support narrower resources. Explain replacing every placeholder and testing with the portal dry run.
  * `## Google Cloud one-time setup`: custom-mode VPC; public router subnet/private slot ranges; `e2-micro` router with `canIpForward`; `100.64.0.0/10` route through it; no Cloud NAT; staff-only SSH firewall. Grant the portal service account `roles/compute.instanceAdmin.v1` and `roles/compute.networkAdmin`, plus `roles/iam.serviceAccountUser` only on a VM service account if images require one; explicitly reject Owner/Editor.
  * `## Router Tailscale flags`: for each cloud CIDR show `tailscale up --login-server=<headscale-url> --advertise-routes=<cloud-cidr> --advertise-tags=tag:jumpbox --snat-subnet-routes=false --stateful-filtering=false`; approve routes; explain return route/source check/canIpForward requirements and ephemeral public IP release on stop.
  * `## Capacity and quota checks`: mandatory max-VM caps; AWS 200-subnet default is roughly 100 simultaneous student/group slot pairs; request regional vCPU/instance/subnet quotas before class; equivalent GCP CPU/instance/address/route/firewall quotas; use a one-student rehearsal.
  * `## Cost model`: reproduce the spec's table exactly—free VPC/network objects; AWS router about `$0.009/h` and 8-GB gp3 about `$0.64/month` stopped; GCP router about `$0.013/h` and 10-GB disk about `$0.40/month` stopped; no `$0.045/h + $0.045/GB` AWS NAT; AWS `t3.medium` about `$0.042/h`; GCP `e2-medium` about `$0.034/h`; 30-GB lab disk about `$0.003/h`; worked 90-student/four-hour totals about `$16` AWS and `$13` GCP; date/region re-check warning.
  * `## Configure and validate the portal`: migrate, fill `.env`, add backend blocks, run preflight, start, and verify router/one VM/cleanup inventory; credentials use boto3 chain and GCP ADC, never tracked secrets.
  * `## Apply the Headscale policy`: run `scripts/apply_headscale_policy.sh` dry-run, review diff, use `--apply`, explain policy check/backup/read-back/conditional restart; rollback by restoring the timestamped ACL backup, `headscale policy check --file`, restart/reload, and verify `policy get`.
  * `## Apply jumpbox VLAN isolation`: instructor console/recovery prerequisite; dry-run then `sudo scripts/jumpbox_configure.sh --apply --no-try --isolate-vlans`; verify tailnet-to-own lab works and cross-VLAN fails. Roll back with `systemctl disable --now jumpbox-vlan-isolation.service`, remove only its nft/unit files, `systemctl daemon-reload`, and confirm its table is absent.
  * `## Teardown and incident response`: stop new creates, let expiry/reaper run, query site-tag/prefix inventory, never manually delete a mismatched-site resource, provider-specific manual cleanup and router stop, and post-cleanup zero-resource proof.

  In `.env.example`, group and explain every cloud variable while keeping `ENABLED_BACKENDS=proxmox` as the compatibility default. In `templates.example.yaml`, retain the current legacy top-level Proxmox example and add a disabled neutral multi-backend example showing group default, machine override semantics, and all three backend blocks without real IDs. In `CHANGELOG.md`, add Unreleased Added/Changed/Upgrade notes including migration-before-restart, new variables, Proxmox-default compatibility, and rollback allowed only when no cloud records exist. In `CLAUDE.md`, update the opening/architecture, orphan-reaper rule (site tags/prefixes, positive evidence, budgets), cloud SDK/gevent constraint, and `docs/CLOUD_BACKENDS.md` pointer. Add `migrate_add_vm_backend.py` to **both** migration loops and the explanatory migration list in `docs/PRODUCTION_DEPLOYMENT.md`, plus a post-deploy link to the cloud runbook.

- [ ] **Step 4: Run documentation checks**

  Run: `PROXMOX_HOST=fake-host PROXMOX_PASSWORD=fake-password uv run python -m pytest tests/test_cloud_docs.py tests/test_preflight_env.py tests/test_release_bundle.py -q`

  Expected: PASS; all examples parse, every cloud config name is documented, both migration paths include the new script, and the existing dynamic release manifest includes the new migration.

- [ ] **Step 5: Commit**

  ```bash
  git add docs/CLOUD_BACKENDS.md .env.example templates.example.yaml \
    CHANGELOG.md CLAUDE.md docs/PRODUCTION_DEPLOYMENT.md tests/test_cloud_docs.py
  git commit -m "docs: add cloud backend operations runbook" \
    -m "Co-Authored-By: Claude <noreply@anthropic.com>"
  ```

