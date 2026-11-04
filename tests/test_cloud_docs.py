"""The cloud runbook, the operator examples and the production compose file
agree with the code: every cloud setting is documented and reaches the
container, the example catalog validates on a Proxmox-only portal, the IAM
example is scoped to the site, and no real site identity is committed."""

import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.backends.template_spec import backend_block, validate_group_backends
from app.template_manager import validate_template_config

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "CLOUD_BACKENDS.md"
SPEC = ROOT / "docs" / "superpowers" / "specs" / "2026-11-04-multi-backend-vms-design.md"

CLOUD_ENV = {
    "ENABLED_BACKENDS", "LAB_SITE_ID", "JUMPBOX_VLAN_BANDS",
    "AWS_REGION", "AWS_AVAILABILITY_ZONE", "AWS_LAB_VPC_ID", "AWS_LAB_CIDR",
    "AWS_ROUTER_INSTANCE_ID", "AWS_MAX_VMS", "AWS_ROUTER_IDLE_MINUTES",
    "AWS_LAUNCH_TIMEOUT_SECONDS", "AWS_TERMINATE_TIMEOUT_SECONDS",
    "GCP_PROJECT", "GCP_REGION", "GCP_ZONE", "GCP_NETWORK", "GCP_LAB_CIDR",
    "GCP_ROUTER_INSTANCE", "GCP_MAX_VMS", "GCP_ROUTER_IDLE_MINUTES",
    "GCP_OPERATION_TIMEOUT_SECONDS",
}
# Settings an enabled cloud cannot run without (app/config.py _CLOUD_REQUIRED
# plus the caps and the site).
CLOUD_REQUIRED_VALUES = {
    "LAB_SITE_ID": "site-a",
    "AWS_REGION": "us-test-1", "AWS_AVAILABILITY_ZONE": "us-test-1a",
    "AWS_LAB_VPC_ID": "vpc-placeholder", "AWS_ROUTER_INSTANCE_ID": "i-placeholder",
    "AWS_MAX_VMS": "3",
    "GCP_PROJECT": "project-placeholder", "GCP_REGION": "region-test1",
    "GCP_ZONE": "region-test1-a", "GCP_NETWORK": "network-placeholder",
    "GCP_ROUTER_INSTANCE": "router-placeholder", "GCP_MAX_VMS": "2",
}
# Credentials reach the container only through the operator-owned override.
CREDENTIAL_ENV = {
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
}

REQUIRED_HEADINGS = {
    "## Safety model and ownership",
    "## Shared address plan",
    "## AWS one-time setup",
    "## Google Cloud one-time setup",
    "## Router Tailscale flags",
    "## Capacity and quota checks",
    "## Cost model",
    "## Configure and validate the portal",
    "## Apply the Headscale policy",
    "## Apply jumpbox VLAN isolation",
    "## Teardown and incident response",
}

# Shapes of real provider identifiers: EC2 resource ids, 12-digit AWS account
# numbers, named AWS/GCP regions and zones. Placeholders such as <aws-vpc-id>
# never match.
REAL_ID_PATTERNS = re.compile(
    r"\b(?:vpc|subnet|sg|i|ami|eni|rtb|igw|vol|eipalloc)-[0-9a-f]{8,17}\b"
    r"|\b\d{12}\b"
    r"|\b(?:us|eu|ap|sa|ca|me|af|il)-(?:gov-)?(?:east|west|north|south|central"
    r"|northeast|northwest|southeast|southwest)-\d[a-z]?\b"
    r"|\b(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-"
    r"(?:central|east|west|north|south|northeast|southeast|southwest)\d(?:-[a-f])?\b"
)


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight_env = _load_script("preflight_env")


def parse_env_keys(path):
    return set(preflight_env.parse_env(path.read_text(encoding="utf-8")))


def markdown_headings(path):
    """Every ATX heading outside fenced code blocks, as written."""
    headings, fenced = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,6} ", line):
            headings.add(line.rstrip())
    return headings


def _section(text, heading):
    """The lines under `heading` up to the next heading of the same or a
    higher level, outside fenced code."""
    level = len(heading) - len(heading.lstrip("#"))
    lines, inside, fenced = [], False, False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and re.match(r"#{1,6} ", line):
            depth = len(line) - len(line.lstrip("#"))
            if inside and depth <= level:
                break
            if line.rstrip() == heading:
                inside = True
                continue
        if inside:
            lines.append(line)
    assert inside, f"no section {heading!r}"
    return lines


def _fenced_blocks(text, language):
    # Fences may be indented under a list item.
    return re.findall(rf"^[ ]*```{language}\n(.*?)^[ ]*```", text, re.MULTILINE | re.DOTALL)


def _table_rows(lines):
    return [line.strip() for line in lines if line.strip().startswith("|")]


# ---- .env.example ----------------------------------------------------------

def test_env_example_covers_every_cloud_config_name():
    keys = parse_env_keys(ROOT / ".env.example")
    assert CLOUD_ENV <= keys


# Catches an example that switches a copied .env to a cloud, or that ships a
# value the preflight would refuse for a cloud setting.
def test_env_example_keeps_proxmox_default_and_inert_cloud_settings():
    env = preflight_env.parse_env((ROOT / ".env.example").read_text(encoding="utf-8"))
    assert env["ENABLED_BACKENDS"] == "proxmox"
    reported = {problem.split(":", 1)[0] for problem in preflight_env.problems(env)}
    assert not reported & (CLOUD_ENV | {"FIXED_NODE"})
    for key in CLOUD_REQUIRED_VALUES:
        assert env[key] == "", f"{key} must stay empty in the example"


# ---- templates.example.yaml ------------------------------------------------

def test_template_example_has_valid_neutral_blocks_for_all_backends(monkeypatch):
    # Catches an example that only validates when a cloud is enabled (I10).
    monkeypatch.setattr(importlib.import_module("app.config"),
                        "ENABLED_BACKENDS", ("proxmox",))
    catalog = yaml.safe_load((ROOT / "templates.example.yaml").read_text())
    group = next(g for g in catalog["deployment_groups"]
                 if g["display_name"] == "multi-backend-example")
    machine = group["templates"][0]
    assert "backend" not in group and "backend" not in machine
    assert group["enabled"] is False
    assert machine["aws"] == {"image_id": "<aws-ami-id>", "instance_type": "t3.medium", "disk_gb": 30}
    assert machine["gcp"] == {"image": "projects/<gcp-project>/global/images/<image-name>",
                              "machine_type": "e2-medium", "disk_gb": 30}
    # The flat legacy form is the Proxmox spec unchanged (ruling: the nested
    # proxmox: overlay is shown in a comment so test_templates_config holds).
    assert machine["template_locations"]
    assert set(machine["template_locations"]) == set(machine["storage_locations"])
    assert backend_block(machine, "proxmox") == machine
    assert validate_template_config(catalog) == ([], [])


# Catches cloud blocks that look right but would be refused the moment the
# operator sets backend: aws|gcp after enabling that backend.
@pytest.mark.parametrize("backend", ["aws", "gcp"])
def test_template_example_cloud_blocks_validate_once_that_backend_is_selected(backend):
    catalog = yaml.safe_load((ROOT / "templates.example.yaml").read_text())
    group = next(g for g in catalog["deployment_groups"]
                 if g["display_name"] == "multi-backend-example")
    assert validate_group_backends({**group, "backend": backend}, (backend,)) == []


# ---- docs/CLOUD_BACKENDS.md ------------------------------------------------

def test_cloud_runbook_contains_required_operator_sections_and_no_site_identity():
    headings = markdown_headings(RUNBOOK)
    assert REQUIRED_HEADINGS <= headings
    assert not REAL_ID_PATTERNS.search(RUNBOOK.read_text())


@pytest.mark.parametrize("path", [".env.example", "templates.example.yaml"])
def test_examples_carry_no_site_identity(path):
    assert not REAL_ID_PATTERNS.search((ROOT / path).read_text())


# Catches a hand-edited price drifting from the design's cost model.
def test_runbook_cost_table_reproduces_the_spec_table_exactly():
    runbook = _table_rows(_section(RUNBOOK.read_text(), "## Cost model"))
    spec = _table_rows(_section(SPEC.read_text(), "## Cost model"))
    assert len(spec) == 9
    assert runbook == spec


def _iam_policy():
    policies = [json.loads(block) for block in _fenced_blocks(RUNBOOK.read_text(), "json")
                if '"Statement"' in block]
    assert len(policies) == 1, "exactly one IAM policy example"
    return policies[0]


def _actions(statement):
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else actions


# Catches an IAM example that lets the portal terminate or delete what another
# site (or the operator) owns, or that grants writes on Resource "*".
def test_iam_example_scopes_every_write_to_the_site():
    statements = _iam_policy()["Statement"]
    site_tag = "ec2:ResourceTag/lab-playground-site"
    for statement in statements:
        assert statement["Effect"] == "Allow"
        resources = statement["Resource"]
        if "*" in ([resources] if isinstance(resources, str) else resources):
            assert all(a.startswith("ec2:Describe") for a in _actions(statement)), statement
    by_action = {}
    for statement in statements:
        for action in _actions(statement):
            by_action.setdefault(action, []).append(statement)
    for action in ("ec2:TerminateInstances", "ec2:DeleteSecurityGroup", "ec2:DeleteSubnet",
                   "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress"):
        assert by_action[action], action
        for statement in by_action[action]:
            assert statement["Condition"]["StringEquals"][site_tag] == "<site-id>", action
    launches = [s for s in by_action["ec2:RunInstances"]
                if any(r.endswith(":instance/*") for r in s["Resource"])]
    assert launches and all(
        s["Condition"]["StringEquals"]["aws:RequestTag/lab-playground-site"] == "<site-id>"
        for s in launches)
    for action in ("ec2:StartInstances", "ec2:StopInstances"):
        for statement in by_action[action]:
            assert statement["Resource"] == [
                "arn:aws:ec2:<aws-region>:<aws-account-id>:instance/<router-instance-id>"]
    for statement in by_action["ec2:CreateTags"]:
        assert set(statement["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"]) <= {
            "Name", "lab-playground-site", "lab-playground-vmid", "lab-playground-deployment",
            "lab-playground-expires", "lab-playground-slot", "lab-playground-kind",
            "lab-playground-created"}


def test_jumpbox_doc_documents_vlan_isolation():
    headings = markdown_headings(ROOT / "docs" / "JUMPBOX.md")
    assert "### VLAN isolation" in headings
    section = "\n".join(_section((ROOT / "docs" / "JUMPBOX.md").read_text(), "### VLAN isolation"))
    for needle in ("--isolate-vlans", "--allow-vlans", "systemctl disable --now "
                   "jumpbox-vlan-isolation.service", "nft list table inet jumpbox_vlan_isolation"):
        assert needle in section


# ---- migrations and changelog ----------------------------------------------

def _migration_loops(text):
    return re.findall(r"^[ ]*for migration in (.*?); do$", text, re.MULTILINE | re.DOTALL)


def test_both_production_migration_loops_run_the_backend_migration():
    text = (ROOT / "docs" / "PRODUCTION_DEPLOYMENT.md").read_text()
    loops = _migration_loops(text)
    assert len(loops) == 2
    for loop in loops:
        assert "migrate_add_vm_backend.py" in loop.split()
    assert "`migrate_add_vm_backend.py`" in text
    assert "CLOUD_BACKENDS.md" in text


def test_changelog_upgrade_notes_name_the_migration_and_new_settings():
    lines = _section((ROOT / "CHANGELOG.md").read_text(), "## [Unreleased]")
    notes = "\n".join(_section("\n".join(lines), "### Upgrade notes"))
    loops = _migration_loops(notes)
    assert len(loops) == 1 and "migrate_add_vm_backend.py" in loops[0].split()
    for name in ("ENABLED_BACKENDS", "LAB_SITE_ID", "docs/CLOUD_BACKENDS.md"):
        assert name in notes


# ---- docker-compose.prod.yml -----------------------------------------------

def _compose_environment():
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    entries = compose["services"]["vm-automation"]["environment"]
    return dict(entry.split("=", 1) for entry in entries)


# Catches a cloud setting that an operator writes to .env but that never
# reaches the container (the prod stack has no env_file), or a credential
# committed to the tracked compose file.
def test_prod_compose_passes_every_cloud_setting_and_no_credential():
    environment = _compose_environment()
    for name in CLOUD_ENV:
        assert environment.get(name) == f"${{{name}:-}}", name
    assert not CREDENTIAL_ENV & set(environment)


_SNAPSHOT = r"""
import json, sys
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False  # only this process's env
import importlib
# Not `import app.config as config`: the package re-exports a mapping under that name.
config = importlib.import_module('app.config')
names = sys.argv[1].split(',')
try:
    config.validate_config()
    verdict = 'ok'
except ValueError as error:
    verdict = str(error)
print(json.dumps({'values': {n: getattr(config, n) for n in names},
                  'verdict': verdict}))
"""


def _snapshot(overrides, blank):
    """app.config's cloud values and validate_config() verdict in a fresh
    process: every CLOUD_ENV name not in `overrides` is '' when `blank`,
    absent otherwise."""
    env = {key: value for key, value in os.environ.items() if key not in CLOUD_ENV}
    env.update(FLASK_ENV="development", PROXMOX_HOST="fake-host",
               PROXMOX_PASSWORD="fake-password")
    if blank:
        env.update({name: "" for name in CLOUD_ENV})
    env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT, ",".join(sorted(CLOUD_ENV))],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


# Compose passes `${NAME:-}`, i.e. '' for anything the operator left out of
# .env. Catches a parser for which '' means something other than unset (an
# empty backend name, a zero cap, an empty CIDR or band string).
@pytest.mark.parametrize("overrides", [
    {},
    {"ENABLED_BACKENDS": "proxmox,aws,gcp", **CLOUD_REQUIRED_VALUES},
    {"ENABLED_BACKENDS": "aws,gcp"},
], ids=["proxmox-only", "clouds-configured", "clouds-missing-required"])
def test_empty_compose_passthrough_behaves_exactly_like_unset(overrides):
    unset = _snapshot(overrides, blank=False)
    blank = _snapshot(overrides, blank=True)
    assert blank == unset
    if overrides.get("LAB_SITE_ID"):
        assert unset["verdict"] == "ok"
    elif overrides:
        assert "LAB_SITE_ID is required" in unset["verdict"]


# Catches the example's defaulted values drifting from the code's defaults.
def test_env_example_defaults_match_the_code_defaults():
    env = preflight_env.parse_env((ROOT / ".env.example").read_text(encoding="utf-8"))
    defaults = _snapshot({}, blank=False)["values"]
    for name in CLOUD_ENV - set(CLOUD_REQUIRED_VALUES) - {"ENABLED_BACKENDS"}:
        assert env[name] == str(defaults[name]), name
