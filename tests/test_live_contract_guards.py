"""Safety guards of the opt-in live Proxmox contract run (tests/live).

The live run creates and destroys real guests. Every guard that keeps it
away from guests it does not own is verified here against an in-memory
Proxmox double, so CI proves the safety logic without a cluster.
"""

from datetime import datetime, timedelta
import itertools

import pytest
from proxmoxer.core import ResourceException

from app.orphan_reaper import _parse_expiration
from tests.live.harness import (
    NAME_PREFIX,
    ContractRun,
    Guest,
    LiveConfig,
    UnsafeLiveRun,
    live_skip_reason,
    parse_vmid_range,
)


FIXED_NOW = datetime(2026, 10, 27, 9, 0, 0)


def _missing(node, vmid):
    # Proxmox answers a read of a missing guest with HTTP 500 and this text.
    return ResourceException(
        500, 'Internal Server Error',
        f"Configuration file 'nodes/{node}/qemu-server/{vmid}.conf' does not exist")


class _Path:
    def __init__(self, api, parts):
        self._api = api
        self._parts = parts

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return _Path(self._api, self._parts + (name,))

    def __call__(self, *args):
        return _Path(self._api, self._parts + tuple(str(a) for a in args))

    def get(self, **kwargs):
        return self._api.handle('GET', self._parts, kwargs)

    def post(self, **kwargs):
        return self._api.handle('POST', self._parts, kwargs)

    def put(self, **kwargs):
        return self._api.handle('PUT', self._parts, kwargs)

    def delete(self, **kwargs):
        return self._api.handle('DELETE', self._parts, kwargs)


class FakeProxmox(_Path):
    """Just enough of the proxmoxer surface for one clone/start/stop/delete."""

    def __init__(self, guests=()):
        super().__init__(self, ())
        self.guests = {}
        for guest in guests:
            self.add_guest(**guest)
        self.tasks = {}
        self.calls = []
        self.fail_delete = set()
        self.reject_linked = False
        self.before_clone = None
        self.fail_config_put = False
        self._upids = itertools.count(1)

    def add_guest(self, vmid, name, node='node1', template=0, status='stopped',
                  description='', vm_type='qemu'):
        self.guests[int(vmid)] = {
            'vmid': int(vmid), 'name': name, 'node': node, 'type': vm_type,
            'status': status, 'template': template,
            'config': {'name': name, 'memory': '1024', 'cores': 1,
                       'template': template, 'description': description},
        }

    def _task(self, exitstatus='OK'):
        upid = f'UPID:node1:{next(self._upids):08X}:qmtask:'
        self.tasks[upid] = {'status': 'stopped', 'exitstatus': exitstatus}
        return upid

    def mutations(self, vmid):
        return [call for call in self.calls
                if call[0] in ('POST', 'PUT', 'DELETE')
                and len(call[1]) > 3 and call[1][3] == str(vmid)]

    def handle(self, method, parts, kwargs):
        self.calls.append((method, parts, kwargs))
        if parts == ('cluster', 'resources'):
            return [{k: g[k] for k in ('vmid', 'name', 'node', 'type',
                                       'status', 'template')}
                    for g in self.guests.values()]
        if parts[0] == 'nodes' and parts[2] == 'tasks':
            return dict(self.tasks[parts[3]])
        node, vmid, rest = parts[1], int(parts[3]), parts[4:]
        guest = self.guests.get(vmid)
        if method == 'POST' and rest == ('clone',):
            if guest is None:
                raise _missing(node, vmid)
            newid = int(kwargs['newid'])
            if self.reject_linked and not kwargs['full']:
                # Proxmox checks storage features before it forks the task.
                raise ResourceException(
                    500, 'Internal Server Error',
                    'Linked clone feature is not supported for drive scsi0')
            if self.before_clone:
                self.before_clone(newid)
            if newid in self.guests:
                raise ResourceException(500, 'Internal Server Error',
                                        f'VM {newid} already exists')
            self.add_guest(newid, kwargs['name'], node=node)
            return self._task()
        if guest is None or guest['node'] != node:
            raise _missing(node, vmid)
        if rest == ('config',):
            if method == 'GET':
                return dict(guest['config'])
            if self.fail_config_put:
                raise ResourceException(400, 'Parameter verification failed',
                                        'net0: invalid bridge')
            guest['config'].update(kwargs)
            return None
        if rest == ('status', 'start'):
            guest['status'] = 'running'
            return self._task()
        if rest == ('status', 'stop'):
            guest['status'] = 'stopped'
            return self._task()
        if rest == ('status', 'current'):
            return {'status': guest['status'], 'vmid': vmid}
        if method == 'DELETE' and rest == ():
            if vmid in self.fail_delete:
                raise ResourceException(500, 'Internal Server Error',
                                        'storage is locked')
            del self.guests[vmid]
            return self._task()
        raise AssertionError(f'unexpected call {method} {parts}')


TEMPLATE = {'vmid': 9000, 'name': 'contract-template', 'template': 1}


def _run(api, vmids='99000-99009'):
    return ContractRun(api, parse_vmid_range(vmids), now=lambda: FIXED_NOW)


# --- VMID range -------------------------------------------------------------

@pytest.mark.parametrize('text', [
    '', 'abc', '99000', '99099-99000', '0-10', '99-150', '99000-1000000000',
    '99000 - x',
])
def test_malformed_or_out_of_bounds_range_is_rejected(text):
    with pytest.raises(ValueError):
        parse_vmid_range(text)


def test_range_includes_both_bounds():
    vmids = parse_vmid_range('99000-99099')
    assert vmids[0] == 99000 and vmids[-1] == 99099 and len(vmids) == 100


# --- preflight ---------------------------------------------------------------

def test_preflight_refuses_when_a_foreign_guest_occupies_the_range():
    api = FakeProxmox([TEMPLATE, {'vmid': 99004, 'name': 'student-alice-web'}])
    run = _run(api)

    with pytest.raises(UnsafeLiveRun, match='99004'):
        run.preflight()

    assert api.mutations(99004) == []


def test_preflight_refuses_a_nameless_guest_in_range():
    api = FakeProxmox([TEMPLATE, {'vmid': 99001, 'name': None}])

    with pytest.raises(UnsafeLiveRun, match='99001'):
        _run(api).preflight()


def test_preflight_accepts_leftover_contract_guests_and_guests_outside_range():
    api = FakeProxmox([
        TEMPLATE,
        {'vmid': 99000, 'name': f'{NAME_PREFIX}deadbeef'},
        {'vmid': 98999, 'name': 'student-bob'},
        {'vmid': 99010, 'name': 'student-carol'},
    ])

    _run(api).preflight()


# --- allocation ----------------------------------------------------------------

def test_allocation_skips_occupied_vmids_and_stays_inside_the_range():
    api = FakeProxmox([
        TEMPLATE,
        {'vmid': 99000, 'name': f'{NAME_PREFIX}leftover'},
        {'vmid': 99001, 'name': f'{NAME_PREFIX}leftover2'},
    ])
    run = _run(api, '99000-99002')

    guest = run.clone_template('node1', 9000)

    assert guest.vmid == 99002
    with pytest.raises(UnsafeLiveRun, match='free VMID'):
        run.clone_template('node1', 9000)
    assert set(api.guests) == {9000, 99000, 99001, 99002}


def test_allocation_refuses_when_a_foreign_guest_appears_after_preflight():
    api = FakeProxmox([TEMPLATE])
    run = _run(api)
    run.preflight()
    api.add_guest(99007, 'student-dave')

    with pytest.raises(UnsafeLiveRun, match='99007'):
        run.clone_template('node1', 9000)

    assert [vmid for vmid in api.guests if vmid != 9000] == [99007]


# --- naming and expiry ------------------------------------------------------------

def test_each_guest_gets_a_unique_prefixed_name_and_a_reaper_readable_expiry():
    api = FakeProxmox([TEMPLATE])
    run = _run(api)

    first = run.clone_template('node1', 9000)
    second = run.clone_template('node1', 9000)

    assert first.name != second.name
    for guest in (first, second):
        assert guest.name.startswith(NAME_PREFIX)
        assert len(guest.name) > len(NAME_PREFIX) + 4
        config = api.guests[guest.vmid]['config']
        assert config['name'] == guest.name
        assert _parse_expiration(config['description']) == (
            FIXED_NOW + timedelta(minutes=30))


def test_linked_clone_is_used_when_the_source_is_a_template():
    api = FakeProxmox([TEMPLATE])

    _run(api).clone_template('node1', 9000)

    clone = [c for c in api.calls if c[1][-1] == 'clone']
    assert clone[0][2]['full'] == 0
    assert 'storage' not in clone[0][2]


def test_configured_storage_forces_a_full_clone_onto_it():
    api = FakeProxmox([TEMPLATE])

    _run(api).clone_template('node1', 9000, storage='fast-zfs')

    clone = [c for c in api.calls if c[1][-1] == 'clone']
    assert clone[0][2]['full'] == 1
    assert clone[0][2]['storage'] == 'fast-zfs'


# --- destroy ownership --------------------------------------------------------------

def test_destroy_refuses_a_vmid_this_run_did_not_create():
    api = FakeProxmox([TEMPLATE, {'vmid': 99003, 'name': f'{NAME_PREFIX}other-run'}])
    run = _run(api)

    with pytest.raises(UnsafeLiveRun, match='did not create'):
        run.destroy(99003)

    assert 99003 in api.guests
    assert api.mutations(99003) == []


def test_destroy_refuses_when_the_vmid_now_names_a_different_guest():
    api = FakeProxmox([TEMPLATE])
    run = _run(api)
    guest = run.clone_template('node1', 9000)
    # Someone removed our guest and reused the VMID before cleanup ran.
    del api.guests[guest.vmid]
    api.add_guest(guest.vmid, 'student-erin')
    api.calls.clear()

    with pytest.raises(UnsafeLiveRun, match='student-erin'):
        run.destroy(guest.vmid)

    assert api.guests[guest.vmid]['name'] == 'student-erin'
    assert api.mutations(guest.vmid) == []


def test_rejected_linked_clone_falls_back_to_a_full_clone():
    api = FakeProxmox([TEMPLATE])
    api.reject_linked = True

    guest = _run(api).clone_template('node1', 9000)

    fulls = [c[2]['full'] for c in api.calls if c[1][-1] == 'clone']
    assert fulls == [0, 1]
    assert api.guests[guest.vmid]['name'] == guest.name


def test_a_clone_that_lost_its_vmid_to_another_creator_is_not_claimed():
    api = FakeProxmox([TEMPLATE])
    other = f'{NAME_PREFIX}other-run'
    api.before_clone = lambda newid: api.add_guest(newid, other)
    run = _run(api)

    with pytest.raises(ResourceException, match='already exists'):
        run.clone_template('node1', 9000)
    api.calls.clear()

    assert run.owned_vmids() == []
    assert run.cleanup() == []
    assert api.guests[99000]['name'] == other
    assert api.mutations(99000) == []


def test_a_failure_after_the_linked_clone_landed_is_reported_not_retried():
    api = FakeProxmox([TEMPLATE])
    api.fail_config_put = True
    run = _run(api)

    with pytest.raises(ResourceException, match='invalid bridge'):
        run.clone_template('node1', 9000)

    fulls = [c[2]['full'] for c in api.calls if c[1][-1] == 'clone']
    assert fulls == [0]
    # The half-configured guest is ours and stays owned for cleanup.
    assert run.owned_vmids() == [99000]
    assert run.cleanup() == []
    assert 99000 not in api.guests


def test_power_actions_refuse_a_guest_this_run_did_not_create():
    api = FakeProxmox([TEMPLATE, {'vmid': 99003, 'name': f'{NAME_PREFIX}other-run',
                                  'status': 'running'}])
    run = _run(api)
    foreign = Guest(99003, 'node1', f'{NAME_PREFIX}other-run')

    with pytest.raises(UnsafeLiveRun, match='did not create'):
        run.stop(foreign)
    with pytest.raises(UnsafeLiveRun, match='did not create'):
        run.start(foreign)

    assert api.mutations(99003) == []


def test_power_actions_refuse_when_the_vmid_now_names_a_different_guest():
    api = FakeProxmox([TEMPLATE])
    run = _run(api)
    guest = run.clone_template('node1', 9000)
    del api.guests[guest.vmid]
    api.add_guest(guest.vmid, 'student-gina', status='running')
    api.calls.clear()

    with pytest.raises(UnsafeLiveRun, match='student-gina'):
        run.stop(guest)

    assert api.guests[guest.vmid]['status'] == 'running'
    assert api.mutations(guest.vmid) == []


def test_power_actions_return_the_task_result_and_its_upid():
    api = FakeProxmox([TEMPLATE])
    run = _run(api)
    guest = run.clone_template('node1', 9000)

    started = run.start(guest)

    assert started['success'] is True
    assert started['upid'] in api.tasks
    assert api.guests[guest.vmid]['status'] == 'running'


def test_destroy_stops_deletes_and_confirms_absence():
    api = FakeProxmox([TEMPLATE])
    run = _run(api)
    guest = run.clone_template('node1', 9000)
    run.start(guest)

    run.destroy(guest.vmid)

    assert guest.vmid not in api.guests
    assert run.owned_vmids() == []


# --- cleanup ---------------------------------------------------------------------------

def test_failure_inside_the_run_still_destroys_every_guest_it_created():
    api = FakeProxmox([TEMPLATE, {'vmid': 99009, 'name': f'{NAME_PREFIX}keep'}])

    with pytest.raises(RuntimeError, match='injected'):
        with _run(api) as run:
            run.clone_template('node1', 9000)
            run.start(run.clone_template('node1', 9000))
            raise RuntimeError('injected failure')

    assert set(api.guests) == {9000, 99009}
    deleted = [c[1][3] for c in api.calls if c[0] == 'DELETE']
    assert sorted(deleted) == ['99000', '99001']


def test_cleanup_reports_a_copy_paste_command_for_a_guest_it_could_not_delete(capsys):
    api = FakeProxmox([TEMPLATE])
    run = _run(api)
    kept = run.clone_template('node1', 9000)
    gone = run.clone_template('node1', 9000)
    api.fail_delete.add(kept.vmid)

    failures = run.cleanup()

    assert gone.vmid not in api.guests
    assert kept.vmid in api.guests
    assert len(failures) == 1
    report = failures[0]
    assert f'qm destroy {kept.vmid} --purge' in report
    assert f'pvesh delete /nodes/node1/qemu/{kept.vmid}' in report
    assert kept.name in report
    assert f'qm destroy {kept.vmid} --purge' in capsys.readouterr().err


def test_cleanup_never_touches_a_guest_that_failed_the_identity_check():
    api = FakeProxmox([TEMPLATE])
    run = _run(api)
    guest = run.clone_template('node1', 9000)
    del api.guests[guest.vmid]
    api.add_guest(guest.vmid, 'student-frank')
    api.calls.clear()

    failures = run.cleanup()

    assert api.guests[guest.vmid]['name'] == 'student-frank'
    assert api.mutations(guest.vmid) == []
    assert len(failures) == 1 and 'student-frank' in failures[0]
    # Never hand an operator a destroy command for a guest this run does not own.
    assert 'qm destroy' not in failures[0]
    assert 'pvesh delete' not in failures[0]


# --- opt-in gate -------------------------------------------------------------------------

LIVE_ENV = {
    'LIVE_PROXMOX': '1',
    'PROXMOX_HOST': 'lab.example.edu',
    'PROXMOX_TOKEN_ID': 'contract',
    'PROXMOX_TOKEN_SECRET': 'secret',
    'LIVE_TEMPLATE_VMID': '9000',
    'LIVE_NODE': 'node1',
    'LIVE_VMID_RANGE': '99000-99099',
}


def test_live_run_is_skipped_unless_explicitly_enabled():
    env = dict(LIVE_ENV, LIVE_PROXMOX='')
    assert 'LIVE_PROXMOX=1' in live_skip_reason(env)
    assert 'LIVE_PROXMOX=1' in live_skip_reason({})


@pytest.mark.parametrize('name', [
    'PROXMOX_HOST', 'LIVE_TEMPLATE_VMID', 'LIVE_NODE', 'LIVE_VMID_RANGE'])
def test_live_run_is_skipped_when_required_env_is_missing(name):
    env = dict(LIVE_ENV)
    del env[name]
    assert name in live_skip_reason(env)


def test_live_run_is_skipped_without_credentials():
    env = {k: v for k, v in LIVE_ENV.items() if not k.startswith('PROXMOX_TOKEN')}
    assert 'credentials' in live_skip_reason(env)
    assert live_skip_reason(dict(env, PROXMOX_PASSWORD='pw')) is None
    assert live_skip_reason(LIVE_ENV) is None


def test_config_maps_templates_and_storage_per_node():
    config = LiveConfig.from_env(dict(
        LIVE_ENV, LIVE_NODE='node1, node2',
        LIVE_TEMPLATE_VMID='node1:9000,node2:9001',
        LIVE_CLONE_STORAGE='node2:fast-zfs'))

    assert config.nodes == ['node1', 'node2']
    assert config.template_for('node1') == 9000
    assert config.template_for('node2') == 9001
    assert config.storage_for('node1') is None
    assert config.storage_for('node2') == 'fast-zfs'
    assert list(config.vmid_range) == list(range(99000, 99100))


def test_config_rejects_a_node_without_a_template():
    with pytest.raises(ValueError, match='node2'):
        LiveConfig.from_env(dict(LIVE_ENV, LIVE_NODE='node1,node2',
                                 LIVE_TEMPLATE_VMID='node1:9000'))
