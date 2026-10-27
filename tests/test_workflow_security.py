"""Supply-chain rules for the GitHub Actions workflows.

The release workflow publishes an image and a deploy bundle that operators
run in production, and it gates on ci.yml. Its trust therefore rests on:

- every remote action pinned to a commit, since a moved tag can change what
  runs (and ci.yml runs next to the Docker Hub token);
- write permissions and the OIDC token only in jobs that run for a pushed,
  ruleset-protected v* tag and wait for the maintainer-approved `release`
  environment, so no branch run or manual dispatch can publish;
- one serialized queue for publishing runs, so two releases cannot race to
  move the `latest` / X.Y aliases;
- verification commands bound to release.yml and the release tag, because
  `-R <repo>` alone accepts an attestation from any workflow on any ref.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / '.github' / 'workflows').glob('*.yml'))
RELEASE = REPO / '.github' / 'workflows' / 'release.yml'
SIGNER = ('--signer-workflow '
          'Ragnar-Security/Ragnar-Lab-Playground/.github/workflows/release.yml')
WRITE_SCOPES = {'packages', 'id-token', 'attestations', 'contents', 'actions',
                'deployments', 'pull-requests', 'issues', 'statuses',
                'artifact-metadata'}
TAG_PUSH_ONLY = ("github.event_name == 'push'", "github.ref_type == 'tag'")


def _load(path):
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as boolean True.
    data['on'] = data.pop(True, data.get('on'))
    return data


def _steps(workflow):
    for job_id, job in workflow['jobs'].items():
        for step in job.get('steps', []):
            yield job_id, step


def test_workflows_found():
    assert {path.name for path in WORKFLOWS} >= {'ci.yml', 'release.yml'}


@pytest.mark.parametrize('path', WORKFLOWS, ids=lambda path: path.name)
def test_remote_actions_are_pinned_to_a_commit_with_version_comment(path):
    uses = re.findall(r'^\s*(?:-\s+)?uses:\s*(\S+)(.*)$', path.read_text(),
                      re.MULTILINE)
    assert uses
    for ref, rest in uses:
        if ref.startswith('./'):
            continue
        assert re.fullmatch(r'[\w.-]+/[\w./-]+@[0-9a-f]{40}', ref), ref
        assert re.fullmatch(r'\s+#\s+v\d+(\.\d+)*\s*', rest), (ref, rest)


@pytest.mark.parametrize('path', WORKFLOWS, ids=lambda path: path.name)
def test_checkouts_do_not_persist_credentials(path):
    for job_id, step in _steps(_load(path)):
        if step.get('uses', '').startswith('actions/checkout@'):
            assert step.get('with', {}).get('persist-credentials') is False, job_id


def test_manual_dispatch_has_no_publish_switch():
    dispatch = _load(RELEASE)['on']['workflow_dispatch']
    assert not (dispatch or {}).get('inputs')


def test_tag_trigger_covers_stable_and_prerelease_tags():
    tags = _load(RELEASE)['on']['push']['tags']
    assert 'v[0-9]+.[0-9]+.[0-9]+' in tags
    assert 'v[0-9]+.[0-9]+.[0-9]+-*' in tags


def test_workflow_defaults_to_no_permissions():
    assert _load(RELEASE)['permissions'] == {}


def test_privileged_jobs_run_only_for_tag_pushes_in_release_environment():
    jobs = _load(RELEASE)['jobs']
    privileged = []
    for job_id, job in jobs.items():
        permissions = job.get('permissions', {})
        writes = {scope for scope, level in permissions.items()
                  if level == 'write' and scope in WRITE_SCOPES}
        if not writes:
            continue
        privileged.append(job_id)
        environment = job.get('environment')
        if isinstance(environment, dict):
            environment = environment.get('name')
        assert environment == 'release', job_id
        condition = job.get('if', '')
        for clause in TAG_PUSH_ONLY:
            assert clause in condition, (job_id, condition)
    assert {'publish-image', 'attest-bundle', 'release'} <= set(privileged)


def test_dry_run_jobs_hold_read_only_permissions():
    jobs = _load(RELEASE)['jobs']
    for job_id, job in jobs.items():
        if job.get('environment'):
            continue
        assert job.get('permissions') == {'contents': 'read'}, job_id


def test_publishing_runs_share_one_serialized_queue():
    concurrency = _load(RELEASE)['concurrency']
    assert "'release-publish'" in concurrency['group']
    assert "github.event_name == 'push'" in concurrency['group']
    assert concurrency['cancel-in-progress'] is False
    # The default queue keeps one pending run and cancels it when another
    # arrives; a cancelled release would silently never publish.
    assert concurrency['queue'] == 'max'


def test_aliases_come_from_the_protected_tags():
    text = RELEASE.read_text()
    assert 'scripts/release_version.py aliases' in text
    assert ':latest' not in text


def _commands(text):
    """Every `gh attestation verify` command, continuation lines joined."""
    joined = re.sub(r'\\\n\s*', ' ', text)
    return [line for line in joined.splitlines() if 'gh attestation verify' in line]


@pytest.mark.parametrize('name', [
    'README.md', 'docs/RELEASING.md', 'docs/DEPLOY-FROM-RELEASE.md',
    'docs/PRODUCTION_DEPLOYMENT.md',
])
def test_documented_verification_is_bound_to_release_workflow_and_tag(name):
    commands = _commands((REPO / name).read_text())
    assert commands, name
    for command in commands:
        assert SIGNER in command, command
        assert re.search(r'--source-ref refs/tags/v', command), command


def test_release_notes_verification_binds_workflow_tag_and_commit():
    commands = _commands(RELEASE.read_text())
    assert commands
    for command in commands:
        for flag in ('--signer-workflow', '--source-ref', '--source-digest'):
            assert flag in command, command
