"""The AWS backend (scripts/image_build/backends/aws.sh) for build_image.sh.

Most tests run the real core and backend against a fake `aws` that keeps a
small EC2 world in a JSON file, answers from it, and logs every call's argv.
The fake fails loudly on any subcommand, query or option shape it does not
know, so a test cannot pass by accident.

The tests at the end run the same backend against the real AWS CLI v2 (when
one is installed), pointed at an in-process EC2 stub that speaks the EC2 query
API. They prove the flags, the shorthand/JSON arguments and the JMESPath
queries are accepted and evaluated by the real CLI, not only by the fake."""

import base64
import fnmatch
import json
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

import pytest

from tests.test_image_build import (
    IMAGE_BUILD,
    KEY_MATERIAL,
    _build,
    _build_env,
    _build_log,
    _calls,
)

AWS_BACKENDS = IMAGE_BUILD / "backends"
REGION = "eu-north-1"
SUBNET = "subnet-0123456789abcdef0"
SG = "sg-0123456789abcdef0"
BASE_AMI = "ami-0123456789abcdef0"
NEW_AMI = "ami-0fedcba9876543210"
INSTANCE = "i-0123456789abcdef0"
PUBKEY = "ssh-ed25519 AAAAC3FAKEPUBLIC image-build"  # what the core's fake ssh-keygen writes
RUN_ID_RE = re.compile(r"^ib-\d{12}-[0-9a-f]{4}$")

# --- the fake aws ------------------------------------------------------------------

FAKE_AWS = r'''
import fnmatch, json, os, sys

argv = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as f:
    f.write("aws " + " ".join(argv) + "\n")


def fail(msg, rc=2):
    sys.stderr.write("fake aws: %s\n  argv: %r\n" % (msg, argv))
    sys.exit(rc)


# ec2 <op> [<waiter>] then --options, each followed by its values (if any).
words, opts, i = [], {}, 0
while i < len(argv) and not argv[i].startswith("--"):
    words.append(argv[i])
    i += 1
while i < len(argv):
    name, vals = argv[i], []
    i += 1
    while i < len(argv) and not argv[i].startswith("--"):
        vals.append(argv[i])
        i += 1
    if name in opts:
        fail("option %s given twice" % name)
    opts[name] = vals

record = {"words": words, "opts": opts, "user_data": None}
ud = opts.get("--user-data")
if ud:
    if len(ud) != 1 or not ud[0].startswith("file://"):
        fail("--user-data must be one file:// argument")
    with open(ud[0][len("file://"):]) as f:
        record["user_data"] = f.read()
with open(os.environ["CALL_LOG"] + ".aws.jsonl", "a") as f:
    f.write(json.dumps(record) + "\n")

if opts.get("--region") != [os.environ["FAKE_AWS_REGION"]]:
    fail("every call must name --region %s" % os.environ["FAKE_AWS_REGION"])
if len(opts.get("--output", [])) != 1:
    fail("every call must name --output")
if "--query" in opts and opts["--output"] != ["text"]:
    fail("a --query result is parsed as --output text")

state_path = os.environ["FAKE_AWS_STATE"]
try:
    with open(state_path) as f:
        world = json.load(f)
except FileNotFoundError:
    world = {}
world.setdefault("instances", {})
world.setdefault("images", {})


def save():
    with open(state_path, "w") as f:
        json.dump(world, f)


def one(name):
    v = opts.get(name)
    if not v or len(v) != 1:
        fail("expected exactly one value for %s" % name)
    return v[0]


def instance(iid):
    if iid not in world["instances"]:
        fail("An error occurred (InvalidInstanceID.NotFound): %s" % iid, 254)
    return world["instances"][iid]


def text(v):
    return "None" if v is None else str(v)


query = opts.get("--query", [""])[0]
op = words[1:] if words[:1] == ["ec2"] else None

if op == ["describe-images"] and "--owners" in opts:
    if query != "sort_by(Images, &CreationDate)[-1].ImageId":
        fail("unknown describe-images query %r" % query)
    print(os.environ.get("FAKE_AWS_BASE", "@BASE_AMI@"))
elif op == ["describe-images"] and "--image-ids" in opts:
    ami = one("--image-ids")
    if query == "Images[0].RootDeviceName":
        print(os.environ.get("FAKE_AWS_ROOT_DEVICE", "/dev/sda1"))
    elif query == "Images[0].State":
        print(world["images"].get(ami, {}).get("state", "None"))
    else:
        fail("unknown describe-images query %r" % query)
elif op == ["run-instances"]:
    specs = json.loads(one("--tag-specifications"))
    tags = {t["Key"]: t["Value"] for s in specs if s["ResourceType"] == "instance" for t in s["Tags"]}
    iid = os.environ.get("FAKE_AWS_INSTANCE", "@INSTANCE@")
    world["instances"][iid] = {"state": "running", "tags": tags, "ip": "192.0.2.10"}
    save()
    if query != "Instances[0].InstanceId":
        fail("unknown run-instances query %r" % query)
    if os.environ.get("FAKE_AWS_RUN_FAIL"):
        # The instance exists, but the client never learns its ID.
        fail("Read timeout on endpoint URL", 255)
    print(iid)
elif op == ["describe-instances"] and "--instance-ids" in opts:
    inst = instance(one("--instance-ids"))
    if query == "Reservations[0].Instances[0].PublicIpAddress":
        print(text(inst.get("ip")))
    elif query == ("Reservations[0].Instances[0].[State.Name, "
                   "Tags[?Key=='image-build'].Value | [0], "
                   "Tags[?Key=='image-build-expires'].Value | [0]]"):
        tag = os.environ.get("FAKE_AWS_RECHECK_TAG", inst["tags"].get("image-build"))
        exp = os.environ.get("FAKE_AWS_RECHECK_EXPIRES", inst["tags"].get("image-build-expires"))
        print("\t".join(text(v) for v in (inst["state"], tag, exp)))
    else:
        fail("unknown describe-instances query %r" % query)
elif op == ["describe-instances"] and "--filters" in opts:
    leftovers = ("Reservations[].Instances[].[InstanceId, "
                 "Tags[?Key=='image-build-expires'].Value | [0]]")
    if query not in (leftovers, "Reservations[].Instances[].[InstanceId]"):
        fail("unknown describe-instances query %r" % query)
    rows = list(world["instances"].items())
    for flt in opts["--filters"]:
        name, _, values = flt.partition(",Values=")
        values = values.split(",")
        if name == "Name=tag-key":
            rows = [r for r in rows if any(k in r[1]["tags"] for k in values)]
        elif name == "Name=tag:image-build":
            rows = [r for r in rows if r[1]["tags"].get("image-build") in values]
        elif name == "Name=instance-state-name":
            rows = [r for r in rows if r[1]["state"] in values]
        else:
            fail("unknown filter %r" % flt)
    for iid, inst in rows:
        if query == leftovers:
            print("%s\t%s" % (iid, text(inst["tags"].get("image-build-expires"))))
        else:
            print(iid)
elif op == ["create-image"]:
    instance(one("--instance-id"))
    if query != "ImageId":
        fail("unknown create-image query %r" % query)
    ami = os.environ.get("FAKE_AWS_NEW_AMI", "@NEW_AMI@")
    world["images"][ami] = {"state": os.environ.get("FAKE_AWS_IMAGE_STATE", "available"),
                            "name": one("--name")}
    save()
    print(ami)
elif op == ["terminate-instances"]:
    inst = instance(one("--instance-ids"))
    inst["state"] = "terminated"
    save()
    if query != "TerminatingInstances[0].CurrentState.Name":
        fail("unknown terminate-instances query %r" % query)
    print("shutting-down")
elif op == ["wait", "instance-running"] or op == ["wait", "instance-terminated"]:
    instance(one("--instance-ids"))
elif op == ["wait", "image-available"]:
    ami = one("--image-ids")
    if ami not in world["images"]:
        fail("no such image %s" % ami, 255)
    fails = world.get("image_wait_fails", 0)
    if fails > 0:
        world["image_wait_fails"] = fails - 1
        save()
        fail("Waiter ImageAvailable failed: Max attempts exceeded", 255)
else:
    fail("unrecognised subcommand %r" % words)
'''.replace("@BASE_AMI@", BASE_AMI).replace("@INSTANCE@", INSTANCE).replace("@NEW_AMI@", NEW_AMI)


def _aws_env(tmp_path: Path, os_family: str = "linux", config: str = "", **overrides):
    """_build_env from the core tests, switched to the real backends directory,
    an AWS config and the fake aws."""
    env, recipe = _build_env(tmp_path, os_family, **overrides)
    env = {k: v for k, v in env.items() if not k.startswith("AWS_")}
    env["IMAGE_BUILD_BACKENDS_DIR"] = str(AWS_BACKENDS)
    (tmp_path / "config.env").write_text(config or (
        f"AWS_BUILD_REGION='{REGION}'\n"
        f"AWS_BUILD_SUBNET_ID='{SUBNET}'\n"
        f"AWS_BUILD_SG_ID='{SG}'\n"))
    fake = tmp_path / "bin" / "aws"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_AWS)
    fake.chmod(0o755)
    env.update(FAKE_AWS_REGION=REGION, FAKE_AWS_STATE=str(tmp_path / "aws-world.json"))
    return env, recipe


def _aws_calls(tmp_path: Path) -> list:
    path = tmp_path / "calls.log.aws.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _ops(tmp_path: Path) -> list:
    return [" ".join(c["words"][1:]) for c in _aws_calls(tmp_path)]


def _only(tmp_path: Path, op: str) -> dict:
    found = [c for c in _aws_calls(tmp_path) if " ".join(c["words"][1:]) == op]
    assert len(found) == 1, (op, _ops(tmp_path))
    return found[0]


def _seed(env: dict, instances: dict, **extra) -> None:
    Path(env["FAKE_AWS_STATE"]).write_text(json.dumps({"instances": instances, **extra}))


def _tags(specs: list, resource: str) -> dict:
    found = [s for s in specs if s["ResourceType"] == resource]
    assert len(found) == 1, (resource, specs)
    return {t["Key"]: t["Value"] for t in found[0]["Tags"]}


def _run_id(stderr: str) -> str:
    m = re.search(r"run (ib-\d{12}-[0-9a-f]{4}):", stderr)
    assert m, stderr
    return m.group(1)


# --- builds against the fake ------------------------------------------------------


def test_aws_run_instances_tags_and_network(tmp_path):
    env, recipe = _aws_env(tmp_path)
    env["FAKE_AWS_ROOT_DEVICE"] = "/dev/xvda"  # read from the AMI, never assumed
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    run_id = _run_id(result.stderr)
    run = _only(tmp_path, "run-instances")["opts"]
    assert run["--image-id"] == [BASE_AMI]
    assert run["--count"] == ["1"]
    assert run["--instance-type"] == ["t3.small"]
    assert run["--subnet-id"] == [SUBNET]
    assert run["--security-group-ids"] == [SG]
    assert run["--associate-public-ip-address"] == []
    assert "--network-interfaces" not in run  # invalid together with the three above
    assert run["--client-token"] == [run_id]
    assert run["--metadata-options"] == ["HttpTokens=required,HttpEndpoint=enabled"]
    assert json.loads(run["--block-device-mappings"][0]) == [{
        "DeviceName": "/dev/xvda",
        "Ebs": {"VolumeSize": 20, "VolumeType": "gp3", "DeleteOnTermination": True}}]
    specs = json.loads(run["--tag-specifications"][0])
    assert sorted(s["ResourceType"] for s in specs) == ["instance", "volume"]
    for resource in ("instance", "volume"):
        tags = _tags(specs, resource)
        assert set(tags) == {"image-build", "image-build-expires"}
        assert tags["image-build"] == run_id
        assert re.fullmatch(r"\d{12}", tags["image-build-expires"])
        assert tags["image-build-expires"] > run_id[3:15]
    # The build VM is reached on the address the instance got.
    ssh_lines = [line for line in _calls(tmp_path).splitlines() if line.startswith("ssh ")]
    assert ssh_lines and all("builder@192.0.2.10" in line for line in ssh_lines)
    everything = (tmp_path / "calls.log.aws.jsonl").read_text()
    assert "lab-playground-site" not in everything
    for call in _aws_calls(tmp_path):
        assert call["opts"]["--region"] == [REGION]


def test_aws_order_save_and_templates(tmp_path):
    env, recipe = _aws_env(tmp_path)
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    assert _ops(tmp_path) == [
        "describe-images",                 # resolve base
        "describe-images",                 # its root device
        "run-instances",
        "wait instance-running",
        "describe-instances",              # public IP
        "create-image",
        "wait image-available",
        "describe-instances",              # tag re-check
        "terminate-instances",
        "wait instance-terminated",
    ]
    calls = _calls(tmp_path)
    # Setup, verify and seal happen between start and save.
    assert calls.index("aws ec2 wait instance-running") < calls.index("seal_linux.sh") \
        < calls.index("aws ec2 create-image")
    image = _only(tmp_path, "create-image")["opts"]
    assert image["--instance-id"] == [INSTANCE]
    name = image["--name"][0]
    assert re.fullmatch(r"enpm634-privesc-linux-abc1234-\d{12}", name)
    specs = json.loads(image["--tag-specifications"][0])
    assert sorted(s["ResourceType"] for s in specs) == ["image", "snapshot"]
    for resource in ("image", "snapshot"):
        # The product carries its provenance, never the build-expiry tags.
        assert _tags(specs, resource) == {"image-build-recipe": "labs/demo/recipe.env",
                                          "image-build-commit": "abc1234"}
    assert _only(tmp_path, "wait image-available")["opts"]["--image-ids"] == [NEW_AMI]
    assert _only(tmp_path, "terminate-instances")["opts"]["--instance-ids"] == [INSTANCE]
    assert _only(tmp_path, "wait instance-terminated")["opts"]["--instance-ids"] == [INSTANCE]
    assert result.stdout.rstrip("\n").splitlines()[-1] == (
        f'aws: {{image_id: "{NEW_AMI}", instance_type: "t3.small", disk_gb: 20}}')
    log = _build_log(tmp_path).read_text().splitlines()[1].split("\t")
    assert log[1:5] == ["aws", NEW_AMI, name, BASE_AMI]
    assert KEY_MATERIAL not in calls


def test_aws_linux_user_data_creates_builder(tmp_path):
    env, recipe = _aws_env(tmp_path)
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    user_data = _only(tmp_path, "run-instances")["user_data"]
    assert user_data.startswith("#cloud-config\n")
    assert "- name: builder" in user_data
    assert 'sudo: "ALL=(ALL) NOPASSWD:ALL"' in user_data
    assert f'- "{PUBKEY}"' in user_data
    assert KEY_MATERIAL not in user_data


def test_aws_windows_user_data_installs_openssh(tmp_path):
    env, recipe = _aws_env(tmp_path, "windows", DISK_GB="40")
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    user_data = _only(tmp_path, "run-instances")["user_data"]
    assert user_data.startswith("<powershell>\n") and user_data.rstrip().endswith("</powershell>")
    assert "Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'" in user_data
    assert "administrators_authorized_keys" in user_data
    assert f"$key = '{PUBKEY}'" in user_data
    assert "icacls.exe" in user_data and "/inheritance:r" in user_data
    assert "Start-Service -Name sshd" in user_data
    assert "-LocalPort 22" in user_data
    assert KEY_MATERIAL not in user_data
    run = _only(tmp_path, "run-instances")["opts"]
    assert json.loads(run["--block-device-mappings"][0])[0]["Ebs"]["VolumeSize"] == 40
    ssh_lines = [line for line in _calls(tmp_path).splitlines() if line.startswith("ssh ")]
    assert ssh_lines and all("Administrator@192.0.2.10" in line for line in ssh_lines)


def test_aws_resolve_base_none_fails(tmp_path):
    env, recipe = _aws_env(tmp_path, BASE_AWS="123456789012:ubuntu/images/*noble*")
    env["FAKE_AWS_BASE"] = "None"
    result = _build(env, recipe, "aws")
    assert result.returncode == 1, result.stderr
    assert "ubuntu/images/*noble*" in result.stderr
    assert _ops(tmp_path) == ["describe-images"]
    lookup = _aws_calls(tmp_path)[0]["opts"]
    assert lookup["--owners"] == ["123456789012"]
    assert lookup["--filters"] == ["Name=name,Values=ubuntu/images/*noble*",
                                   "Name=state,Values=available"]
    # An empty answer is no better than None.
    env["FAKE_AWS_BASE"] = ""
    result = _build(env, recipe, "aws")
    assert result.returncode == 1 and "ubuntu/images/*noble*" in result.stderr


def test_aws_resolve_base_bad_pattern_fails(tmp_path):
    for n, base in enumerate(("no-owner-glob", ":glob-*", "self:", "self:a,b")):
        env, recipe = _aws_env(tmp_path / str(n), BASE_AWS=base)
        result = _build(env, recipe, "aws")
        assert result.returncode == 1, (base, result.stderr)
        assert "BASE_AWS" in result.stderr, base
        assert _aws_calls(tmp_path / str(n)) == []


def test_aws_destroy_rechecks_tag(tmp_path):
    # This run's own instance: re-checked, then terminated and waited on.
    env, recipe = _aws_env(tmp_path / "own")
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    ops = _ops(tmp_path / "own")
    assert ops[-3:] == ["describe-instances", "terminate-instances", "wait instance-terminated"]
    # The instance now carries another run's tag: never terminated.
    env, recipe = _aws_env(tmp_path / "foreign")
    env["FAKE_AWS_RECHECK_TAG"] = "ib-202601010000-ffff"
    result = _build(env, recipe, "aws")
    assert result.returncode == 1
    assert "terminate-instances" not in _ops(tmp_path / "foreign")
    assert "ib-202601010000-ffff" in result.stderr and "--reap aws" in result.stderr
    # No tag at all: never terminated.
    env, recipe = _aws_env(tmp_path / "untagged")
    env["FAKE_AWS_RECHECK_TAG"] = "None"
    result = _build(env, recipe, "aws")
    assert result.returncode == 1
    assert "terminate-instances" not in _ops(tmp_path / "untagged")


def test_aws_destroy_failure_after_start_still_terminates(tmp_path):
    # Verify fails: the trap destroys the instance and no image is made.
    env, recipe = _aws_env(tmp_path)
    env["FAKE_SSH_FAIL_ON"] = "verify.sh"
    result = _build(env, recipe, "aws")
    assert result.returncode == 1
    ops = _ops(tmp_path)
    assert "create-image" not in ops
    assert ops[-3:] == ["describe-instances", "terminate-instances", "wait instance-terminated"]


FOREIGN = "i-0000000000000f00f"


def test_aws_unrecorded_instance_found_by_run_tag(tmp_path):
    # run-instances made the instance but failed before printing its ID: the
    # cleanup finds it by this run's tag and terminates it through the same
    # ownership re-check. Another run's instance is never touched.
    env, recipe = _aws_env(tmp_path)
    _seed(env, {FOREIGN: {"state": "running", "ip": None,
                          "tags": {"image-build": "ib-202601010000-ffff",
                                   "image-build-expires": "209912312359"}}})
    env["FAKE_AWS_RUN_FAIL"] = "1"
    result = _build(env, recipe, "aws")
    assert result.returncode == 1, result.stderr
    run_id = _run_id(result.stderr)
    world = json.loads(Path(env["FAKE_AWS_STATE"]).read_text())["instances"]
    assert world[INSTANCE]["state"] == "terminated"
    assert world[FOREIGN]["state"] == "running"
    assert _ops(tmp_path) == ["describe-images", "describe-images", "run-instances",
                              "describe-instances",          # lookup by run tag
                              "describe-instances",          # ownership re-check
                              "terminate-instances", "wait instance-terminated"]
    lookup = [c for c in _aws_calls(tmp_path)
              if c["words"][1:] == ["describe-instances"] and "--filters" in c["opts"]]
    assert len(lookup) == 1
    assert f"Name=tag:image-build,Values={run_id}" in lookup[0]["opts"]["--filters"]
    assert [c["opts"]["--instance-ids"] for c in _aws_calls(tmp_path)
            if c["words"][1:] == ["terminate-instances"]] == [[INSTANCE]]
    assert "could not destroy" not in result.stderr.lower()

    # The lookup finds nothing of this run: nothing is terminated.
    env, recipe = _aws_env(tmp_path / "none")
    _seed(env, {FOREIGN: {"state": "running", "ip": None,
                          "tags": {"image-build": "ib-202601010000-ffff",
                                   "image-build-expires": "202601010400"}}})
    recipe.write_text(recipe.read_text().replace("AWS_INSTANCE_TYPE=t3.small", "AWS_INSTANCE_TYPE='t3 small'"))
    result = _build(env, recipe, "aws")
    assert result.returncode == 1
    assert "terminate-instances" not in _ops(tmp_path / "none")
    world = json.loads(Path(env["FAKE_AWS_STATE"]).read_text())["instances"]
    assert world[FOREIGN]["state"] == "running"


def test_aws_image_wait_retries_while_pending(tmp_path):
    env, recipe = _aws_env(tmp_path)
    # The waiter gives up twice while the AMI is still pending, then succeeds.
    _seed(env, {}, image_wait_fails=2)
    env["FAKE_AWS_IMAGE_STATE"] = "pending"
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    assert _ops(tmp_path).count("wait image-available") == 3
    assert _ops(tmp_path).count("describe-images") == 2 + 2  # base, root device, two state checks
    assert result.stdout.rstrip("\n").splitlines()[-1].startswith(f'aws: {{image_id: "{NEW_AMI}"')


def test_aws_image_failed_is_not_recorded(tmp_path):
    env, recipe = _aws_env(tmp_path)
    _seed(env, {}, image_wait_fails=1)
    env["FAKE_AWS_IMAGE_STATE"] = "failed"
    result = _build(env, recipe, "aws")
    assert result.returncode == 1
    assert NEW_AMI in result.stderr and "failed" in result.stderr
    assert not _build_log(tmp_path).exists()
    assert "terminate-instances" in _ops(tmp_path)  # the build VM still goes


def test_aws_dry_run_calls_nothing(tmp_path):
    env, recipe = _aws_env(tmp_path)
    result = _build(env, "--dry-run", recipe, "aws")
    assert result.returncode == 0, result.stderr
    assert _aws_calls(tmp_path) == []
    for printed in ("DRY-RUN: aws ec2 describe-images --region eu-north-1 --owners self",
                    "DRY-RUN: aws ec2 run-instances --region eu-north-1",
                    "DRY-RUN: aws ec2 wait instance-running",
                    "DRY-RUN: aws ec2 create-image --region eu-north-1",
                    "DRY-RUN: aws ec2 wait image-available",
                    "DRY-RUN: aws ec2 terminate-instances --region eu-north-1",
                    "DRY-RUN: aws ec2 wait instance-terminated"):
        assert printed in result.stderr, printed
    assert result.stdout.rstrip("\n").splitlines()[-1].startswith("aws: {image_id: ")


def test_aws_check_config(tmp_path):
    cases = {
        "AWS_BUILD_SG_ID": f"AWS_BUILD_REGION='{REGION}'\nAWS_BUILD_SUBNET_ID='{SUBNET}'\n",
        "AWS_BUILD_SUBNET_ID": f"AWS_BUILD_REGION='{REGION}'\nAWS_BUILD_SUBNET_ID='vpc-fake'\n"
                               f"AWS_BUILD_SG_ID='{SG}'\n",
        "AWS_BUILD_REGION": f"AWS_BUILD_SUBNET_ID='{SUBNET}'\nAWS_BUILD_SG_ID='{SG}'\n",
    }
    for missing, config in cases.items():
        env, recipe = _aws_env(tmp_path / missing, config=config)
        result = _build(env, "--dry-run", recipe, "aws")
        assert result.returncode == 2, (missing, result.stderr)
        assert missing in result.stderr, missing
    # A bad instance type is refused before anything is launched.
    env, recipe = _aws_env(tmp_path / "type", AWS_INSTANCE_TYPE="t3 small")
    result = _build(env, recipe, "aws")
    assert result.returncode == 1 and "AWS_INSTANCE_TYPE" in result.stderr
    assert "run-instances" not in _ops(tmp_path / "type")


# --- reap ---------------------------------------------------------------------------

LEFTOVER_WORLD = {
    "i-00000000000000001": {"state": "running",
                            "tags": {"image-build": "ib-202601010000-aaaa", "image-build-expires": "202601010400"}},
    "i-00000000000000002": {"state": "stopped",
                            "tags": {"image-build": "ib-202601010000-bbbb", "image-build-expires": "202601010400"}},
    # Not expired.
    "i-00000000000000003": {"state": "running",
                            "tags": {"image-build": "ib-209912310000-cccc", "image-build-expires": "209912312359"}},
    # Already terminated.
    "i-00000000000000004": {"state": "terminated",
                            "tags": {"image-build": "ib-202601010000-dddd", "image-build-expires": "202601010400"}},
    # Not a build VM: no image-build tag.
    "i-00000000000000005": {"state": "running", "tags": {"image-build-expires": "202601010400"}},
    # A build tag without an expiry is left alone.
    "i-00000000000000006": {"state": "running", "tags": {"image-build": "ib-202601010000-eeee"}},
    # A portal lab VM.
    "i-00000000000000007": {"state": "running", "tags": {"lab-playground-site": "x"}},
}


def test_aws_leftovers_only_tagged_expired(tmp_path):
    env, _recipe = _aws_env(tmp_path)
    _seed(env, LEFTOVER_WORLD)
    result = _build(env, "--reap", "aws")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["destroyed i-00000000000000001",
                                          "destroyed i-00000000000000002"]
    terminated = [c["opts"]["--instance-ids"] for c in _aws_calls(tmp_path)
                  if c["words"][1:] == ["terminate-instances"]]
    assert terminated == [["i-00000000000000001"], ["i-00000000000000002"]]
    listing = _aws_calls(tmp_path)[0]["opts"]
    assert "Name=tag-key,Values=image-build" in listing["--filters"]
    states = [f for f in listing["--filters"] if f.startswith("Name=instance-state-name,")]
    assert len(states) == 1 and "terminated" not in states[0] and "running" in states[0]
    world = json.loads(Path(env["FAKE_AWS_STATE"]).read_text())["instances"]
    for iid in ("i-00000000000000003", "i-00000000000000005", "i-00000000000000006",
                "i-00000000000000007"):
        assert world[iid]["state"] == "running", iid


def test_aws_reap_dry_run_terminates_nothing(tmp_path):
    env, _recipe = _aws_env(tmp_path)
    _seed(env, LEFTOVER_WORLD)
    result = _build(env, "--reap", "aws", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "would destroy i-00000000000000001" in result.stdout
    assert "would destroy i-00000000000000002" in result.stdout
    assert "terminate-instances" not in _ops(tmp_path)
    assert "DRY-RUN: aws ec2 terminate-instances --region eu-north-1 --instance-ids i-00000000000000001" \
        in result.stderr


def test_aws_reap_rechecks_tag_and_expiry(tmp_path):
    world = {k: v for k, v in LEFTOVER_WORLD.items() if k == "i-00000000000000001"}
    for name, override in (("untagged", {"FAKE_AWS_RECHECK_TAG": "None"}),
                           ("foreign", {"FAKE_AWS_RECHECK_TAG": "web-server"}),
                           ("extended", {"FAKE_AWS_RECHECK_EXPIRES": "209912312359"}),
                           ("noexpiry", {"FAKE_AWS_RECHECK_EXPIRES": "None"})):
        env, _recipe = _aws_env(tmp_path / name)
        _seed(env, world)
        env.update(override)
        result = _build(env, "--reap", "aws")
        assert result.returncode == 1, (name, result.stderr)
        assert "could not destroy i-00000000000000001" in result.stderr, name
        assert "terminate-instances" not in _ops(tmp_path / name), name


# --- the real AWS CLI against an EC2 stub --------------------------------------------

REAL_AWS = shutil.which("aws")
real_cli = pytest.mark.skipif(REAL_AWS is None, reason="no AWS CLI on PATH")

EC2_NS = "http://ec2.amazonaws.com/doc/2016-11-15/"


class _Ec2Stub:
    """Just enough of the EC2 query API for the backend: a world of images and
    instances, every request body recorded."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.images = {
            "ami-00000000000000a01": {"name": "base-ubuntu-2404-20260101", "state": "available",
                                      "created": "2026-01-01T00:00:00.000Z", "owner": "self"},
            "ami-00000000000000a02": {"name": "base-ubuntu-2404-20260301", "state": "available",
                                      "created": "2026-03-01T00:00:00.000Z", "owner": "self"},
            "ami-00000000000000a03": {"name": "base-ubuntu-2404-20260401", "state": "pending",
                                      "created": "2026-04-01T00:00:00.000Z", "owner": "self"},
            "ami-00000000000000a04": {"name": "other-20260501", "state": "available",
                                      "created": "2026-05-01T00:00:00.000Z", "owner": "self"},
        }
        self.instances = {}

    # -- helpers --
    @staticmethod
    def _filters(p):
        out = {}
        for k, v in p.items():
            m = re.fullmatch(r"Filter\.(\d+)\.Name", k)
            if m:
                n = m.group(1)
                out[v] = [p[x] for x in sorted(p) if re.fullmatch(rf"Filter\.{n}\.Value\.\d+", x)]
        return out

    @staticmethod
    def _list(p, prefix):
        return [p[k] for k in sorted(p) if re.fullmatch(rf"{re.escape(prefix)}\.\d+", k)]

    @staticmethod
    def _tags_xml(tags):
        return "<tagSet>" + "".join(
            f"<item><key>{escape(k)}</key><value>{escape(v)}</value></item>" for k, v in tags.items()
        ) + "</tagSet>"

    def _instance_xml(self, iid):
        inst = self.instances[iid]
        codes = {"pending": 0, "running": 16, "shutting-down": 32, "terminated": 48,
                 "stopping": 64, "stopped": 80}
        ip = f"<ipAddress>{inst['ip']}</ipAddress>" if inst.get("ip") else ""
        return (f"<item><instanceId>{iid}</instanceId><imageId>{inst['image']}</imageId>"
                f"<instanceState><code>{codes[inst['state']]}</code><name>{inst['state']}</name>"
                f"</instanceState>{ip}{self._tags_xml(inst['tags'])}</item>")

    def handle(self, p):
        action = p.get("Action")
        with self.lock:
            self.requests.append(p)
            return getattr(self, "_" + action, self._unknown)(p)

    def _unknown(self, p):
        return 400, _error("InvalidAction", f"stub does not implement {p.get('Action')}")

    def _DescribeImages(self, p):
        ids, owners, flt = self._list(p, "ImageId"), self._list(p, "Owner"), self._filters(p)
        unknown = set(flt) - {"name", "state"}
        if unknown:
            return 400, _error("InvalidParameterValue", f"stub filter {unknown}")
        items = []
        for ami, img in self.images.items():
            if ids and ami not in ids:
                continue
            if owners and img["owner"] not in owners:
                continue
            if "name" in flt and not any(fnmatch.fnmatchcase(img["name"], g) for g in flt["name"]):
                continue
            if "state" in flt and img["state"] not in flt["state"]:
                continue
            items.append(
                f"<item><imageId>{ami}</imageId><name>{escape(img['name'])}</name>"
                f"<imageState>{img['state']}</imageState><creationDate>{img['created']}</creationDate>"
                f"<rootDeviceName>/dev/sda1</rootDeviceName><rootDeviceType>ebs</rootDeviceType>"
                f"</item>")
        return 200, _resp("DescribeImages", "<imagesSet>" + "".join(items) + "</imagesSet>")

    def _RunInstances(self, p):
        tags = {}
        n = 1
        while f"TagSpecification.{n}.ResourceType" in p:
            if p[f"TagSpecification.{n}.ResourceType"] == "instance":
                m = 1
                while f"TagSpecification.{n}.Tag.{m}.Key" in p:
                    tags[p[f"TagSpecification.{n}.Tag.{m}.Key"]] = p[f"TagSpecification.{n}.Tag.{m}.Value"]
                    m += 1
            n += 1
        iid = f"i-0000000000000{len(self.instances) + 1:04x}"
        self.instances[iid] = {"state": "running", "tags": tags, "ip": "192.0.2.20",
                               "image": p["ImageId"]}
        if getattr(self, "fail_run_after_create", False):
            # Created, but the caller never sees the reply's instance ID.
            return 500, _error("InternalError", "An internal error has occurred")
        return 200, _resp("RunInstances",
                          f"<reservationId>r-0000000000000001</reservationId><ownerId>000000000000</ownerId>"
                          f"<instancesSet>{self._instance_xml(iid)}</instancesSet>")

    def _DescribeInstances(self, p):
        ids, flt = self._list(p, "InstanceId"), self._filters(p)
        unknown = set(flt) - {"tag-key", "tag:image-build", "instance-state-name"}
        if unknown:
            return 400, _error("InvalidParameterValue", f"stub filter {unknown}")
        missing = [i for i in ids if i not in self.instances]
        if missing:
            return 400, _error("InvalidInstanceID.NotFound", f"The instance ID '{missing[0]}' does not exist")
        items = []
        for iid, inst in self.instances.items():
            if ids and iid not in ids:
                continue
            if "tag-key" in flt and not any(k in inst["tags"] for k in flt["tag-key"]):
                continue
            if "tag:image-build" in flt and inst["tags"].get("image-build") not in flt["tag:image-build"]:
                continue
            if "instance-state-name" in flt and inst["state"] not in flt["instance-state-name"]:
                continue
            items.append(f"<item><reservationId>r-{iid[2:]}</reservationId><ownerId>000000000000</ownerId>"
                         f"<instancesSet>{self._instance_xml(iid)}</instancesSet></item>")
        return 200, _resp("DescribeInstances", "<reservationSet>" + "".join(items) + "</reservationSet>")

    def _CreateImage(self, p):
        ami = f"ami-00000000000000b{len(self.images):02x}"
        self.images[ami] = {"name": p["Name"], "state": "available",
                            "created": "2026-11-08T00:00:00.000Z", "owner": "self"}
        return 200, _resp("CreateImage", f"<imageId>{ami}</imageId>")

    def _TerminateInstances(self, p):
        items = []
        for iid in self._list(p, "InstanceId"):
            prev = self.instances[iid]["state"]
            self.instances[iid]["state"] = "terminated"
            items.append(f"<item><instanceId>{iid}</instanceId>"
                         f"<currentState><code>32</code><name>shutting-down</name></currentState>"
                         f"<previousState><code>16</code><name>{prev}</name></previousState></item>")
        return 200, _resp("TerminateInstances", "<instancesSet>" + "".join(items) + "</instancesSet>")


def _resp(action, inner):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<{action}Response xmlns="{EC2_NS}">'
            f"<requestId>00000000-0000-0000-0000-000000000000</requestId>{inner}</{action}Response>")


def _error(code, message):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<Response><Errors><Error><Code>{code}</Code>'
            f"<Message>{escape(message)}</Message></Error></Errors>"
            f"<RequestID>00000000-0000-0000-0000-000000000000</RequestID></Response>")


@pytest.fixture
def ec2_stub():
    stub = _Ec2Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            status, xml = stub.handle(dict(parse_qsl(body, keep_blank_values=True)))
            data = xml.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/xml;charset=UTF-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield stub
    server.shutdown()
    server.server_close()


def _real_env(tmp_path: Path, stub: _Ec2Stub, os_family: str = "linux", **overrides):
    env, recipe = _aws_env(tmp_path, os_family, BASE_AWS="self:base-ubuntu-2404-*", **overrides)
    # Replace the fake with the real CLI, pointed at the stub. No profile, no
    # default region: a call that omits --region fails.
    (tmp_path / "bin" / "aws").write_text(
        "#!/bin/sh\n"
        'printf "%s %s\\n" aws "$*" >> "$CALL_LOG"\n'
        f'exec "{REAL_AWS}" --endpoint-url "{stub.url}" --cli-connect-timeout 5 "$@"\n')
    for key in [k for k in env if k.startswith("AWS_")]:
        del env[key]
    env.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
               AWS_CONFIG_FILE=str(tmp_path / "no-aws-config"),
               AWS_SHARED_CREDENTIALS_FILE=str(tmp_path / "no-aws-credentials"),
               AWS_EC2_METADATA_DISABLED="true", AWS_MAX_ATTEMPTS="1", AWS_PAGER="")
    return env, recipe


def _actions(stub: _Ec2Stub, action: str) -> list:
    return [p for p in stub.requests if p["Action"] == action]


@real_cli
def test_real_cli_linux_build(tmp_path, ec2_stub):
    env, recipe = _real_env(tmp_path, ec2_stub)
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    run_id = _run_id(result.stderr)
    # The newest available image matching the glob, not the newer pending one.
    assert "base: ami-00000000000000a02" in result.stderr
    (run,) = _actions(ec2_stub, "RunInstances")
    assert run["ImageId"] == "ami-00000000000000a02"
    assert run["MinCount"] == "1" and run["MaxCount"] == "1"
    assert run["InstanceType"] == "t3.small"
    assert run["ClientToken"] == run_id
    # The CLI folds subnet, SG and the public IP into the primary interface.
    assert run["NetworkInterface.1.DeviceIndex"] == "0"
    assert run["NetworkInterface.1.SubnetId"] == SUBNET
    assert run["NetworkInterface.1.SecurityGroupId.1"] == SG
    assert run["NetworkInterface.1.AssociatePublicIpAddress"] == "true"
    assert run["BlockDeviceMapping.1.DeviceName"] == "/dev/sda1"
    assert run["BlockDeviceMapping.1.Ebs.VolumeSize"] == "20"
    assert run["BlockDeviceMapping.1.Ebs.VolumeType"] == "gp3"
    assert run["BlockDeviceMapping.1.Ebs.DeleteOnTermination"] == "true"
    assert run["MetadataOptions.HttpTokens"] == "required"
    specs = {}
    for k, v in run.items():
        m = re.fullmatch(r"TagSpecification\.(\d+)\.ResourceType", k)
        if m:
            n = m.group(1)
            specs[v] = {run[f"TagSpecification.{n}.Tag.{t}.Key"]: run[f"TagSpecification.{n}.Tag.{t}.Value"]
                        for t in (1, 2)}
            assert f"TagSpecification.{n}.Tag.3.Key" not in run
    assert set(specs) == {"instance", "volume"}
    for tags in specs.values():
        assert tags["image-build"] == run_id
        assert re.fullmatch(r"\d{12}", tags["image-build-expires"])
    user_data = base64.b64decode(run["UserData"]).decode()
    assert user_data.startswith("#cloud-config\n") and PUBKEY in user_data
    # SSH went to the address the stub handed out.
    assert "builder@192.0.2.20" in _calls(tmp_path)
    (image,) = _actions(ec2_stub, "CreateImage")
    iid = next(iter(ec2_stub.instances))
    assert image["InstanceId"] == iid
    assert image["TagSpecification.1.ResourceType"] == "image"
    assert image["TagSpecification.2.ResourceType"] == "snapshot"
    assert image["TagSpecification.1.Tag.1.Key"] == "image-build-recipe"
    assert image["TagSpecification.1.Tag.1.Value"] == "labs/demo/recipe.env"
    assert image["TagSpecification.1.Tag.2.Value"] == "abc1234"
    new_ami = [a for a in ec2_stub.images if a.startswith("ami-00000000000000b")]
    assert result.stdout.rstrip("\n").splitlines()[-1] == (
        f'aws: {{image_id: "{new_ami[0]}", instance_type: "t3.small", disk_gb: 20}}')
    assert ec2_stub.instances[iid]["state"] == "terminated"
    assert [p["InstanceId.1"] for p in _actions(ec2_stub, "TerminateInstances")] == [iid]
    assert "lab-playground-site" not in json.dumps(ec2_stub.requests)


@real_cli
def test_real_cli_windows_user_data(tmp_path, ec2_stub):
    env, recipe = _real_env(tmp_path, ec2_stub, "windows", DISK_GB="40")
    result = _build(env, recipe, "aws")
    assert result.returncode == 0, result.stderr
    (run,) = _actions(ec2_stub, "RunInstances")
    user_data = base64.b64decode(run["UserData"]).decode()
    assert "OpenSSH.Server~~~~0.0.1.0" in user_data
    assert "administrators_authorized_keys" in user_data
    assert PUBKEY in user_data
    assert "Administrator@192.0.2.20" in _calls(tmp_path)


@real_cli
def test_real_cli_resolve_base_none(tmp_path, ec2_stub):
    env, recipe = _real_env(tmp_path, ec2_stub)
    recipe.write_text(recipe.read_text().replace("BASE_AWS='self:base-ubuntu-2404-*'",
                                                 "BASE_AWS='self:no-such-image-*'"))
    result = _build(env, recipe, "aws")
    assert result.returncode == 1
    assert "no-such-image-*" in result.stderr
    assert _actions(ec2_stub, "RunInstances") == []


@real_cli
def test_real_cli_reap(tmp_path, ec2_stub):
    env, _recipe = _real_env(tmp_path, ec2_stub)
    for iid, inst in LEFTOVER_WORLD.items():
        ec2_stub.instances[iid] = {"state": inst["state"], "tags": dict(inst["tags"]),
                                   "ip": None, "image": "ami-00000000000000a01"}
    result = _build(env, "--reap", "aws")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["destroyed i-00000000000000001",
                                          "destroyed i-00000000000000002"]
    states = {iid: inst["state"] for iid, inst in ec2_stub.instances.items()}
    assert states == {"i-00000000000000001": "terminated", "i-00000000000000002": "terminated",
                      "i-00000000000000003": "running", "i-00000000000000004": "terminated",
                      "i-00000000000000005": "running", "i-00000000000000006": "running",
                      "i-00000000000000007": "running"}
    # The reaper re-checks the tag itself: an expired instance whose
    # image-build tag is not a run ID is left alone.
    ec2_stub.instances["i-00000000000000003"]["tags"]["image-build-expires"] = "202601010400"
    ec2_stub.instances["i-00000000000000003"]["tags"]["image-build"] = "not-a-run-id"
    result = _build(env, "--reap", "aws")
    assert result.returncode == 1
    assert ec2_stub.instances["i-00000000000000003"]["state"] == "running"


@real_cli
def test_real_cli_unrecorded_instance_found_by_run_tag(tmp_path, ec2_stub):
    # The real CLI's filter and --output text shapes for the run-tag lookup.
    ec2_stub.instances[FOREIGN] = {"state": "running", "ip": None, "image": "ami-00000000000000a01",
                                   "tags": {"image-build": "ib-202601010000-ffff",
                                            "image-build-expires": "202601010400"}}
    ec2_stub.fail_run_after_create = True
    env, recipe = _real_env(tmp_path, ec2_stub)
    result = _build(env, recipe, "aws")
    assert result.returncode == 1, result.stderr
    run_id = _run_id(result.stderr)
    ours = [i for i, inst in ec2_stub.instances.items() if inst["tags"].get("image-build") == run_id]
    assert len(ours) == 1
    assert ec2_stub.instances[ours[0]]["state"] == "terminated"
    assert ec2_stub.instances[FOREIGN]["state"] == "running"
    assert [p["InstanceId.1"] for p in _actions(ec2_stub, "TerminateInstances")] == ours
