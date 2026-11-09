"""The GCP backend of the image builder (scripts/image_build/backends/gcp.sh),
driven through build_image.sh against a fake `gcloud` that keeps its own
instance state, so ordering (stop before image) and the label re-checks are
enforced by the fake and not only asserted on its log."""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from tests.test_image_build import IMAGE_BUILD, KEY_MATERIAL, _build, _build_env, _build_log, _calls

GCP_BACKEND = IMAGE_BUILD / "backends" / "gcp.sh"

PROJECT = "fake-build-project"
ZONE = "us-east1-b"
SUBNET = "build-subnet-fake"
NAT_IP = "192.0.2.20"
PUBKEY = "ssh-ed25519 AAAAC3FAKEPUBLIC image-build"
RUN_ID_RE = re.compile(r"^ib-[0-9]{12}-[0-9a-f]{4}$")
GCP_LABEL_RE = re.compile(r"^[a-z0-9_-]{1,63}$")
GCP_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
EXPIRED = "202601010000"
UNEXPIRED = "209901010000"

# A fake gcloud with just the commands the backend uses. Every call is logged
# to $CALL_LOG; files passed with --metadata-from-file are copied to
# $CALL_LOG.metadata (the builder deletes its temp dir on exit). Instances live
# in $GCP_STATE (JSON). Anything unrecognised fails loudly.
FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as f:
    f.write("gcloud " + " ".join(args) + "\n")
state_path = os.environ["GCP_STATE"]
state = {"instances": {}, "images": {}}
if os.path.exists(state_path):
    with open(state_path) as f:
        state = json.load(f)


def opt(name):
    if name in args:
        return args[args.index(name) + 1]
    return None


def fail(msg, rc=1):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)


def save():
    with open(state_path, "w") as f:
        json.dump(state, f)


project = opt("--project")
if not project:
    fail("fake gcloud: every call needs --project", 2)
mff = opt("--metadata-from-file")
if mff:
    with open(os.environ["CALL_LOG"] + ".metadata", "a") as out:
        for item in mff.split(","):
            key, path = item.split("=", 1)
            with open(path) as src:
                out.write("--- " + key + "\n" + src.read() + "\n")

cmd = tuple(args[:3])
name = args[3] if len(args) > 3 and not args[3].startswith("--") else None
fmt = opt("--format")
insts = state["instances"]
fail_on = os.environ.get("FAKE_GCP_FAIL", "")

if cmd == ("compute", "images", "describe-from-family"):
    if fmt != "value(selfLink)":
        fail("fake gcloud: unexpected format " + str(fmt), 2)
    if name in os.environ.get("FAKE_GCP_MISSING_FAMILIES", "").split():
        fail("ERROR: (gcloud.compute.images.describeFromFamily) Could not fetch resource:\n"
             " - The resource 'projects/%s/global/images/family/%s' was not found" % (project, name))
    print("https://www.googleapis.com/compute/v1/projects/%s/global/images/%s-v20260101" % (project, name))
elif cmd == ("compute", "instances", "create"):
    if "--quiet" not in args or not opt("--zone"):
        fail("fake gcloud: create needs --zone and --quiet", 2)
    if fail_on == "create-nothing":
        fail("ERROR: (gcloud.compute.instances.create) quota exceeded")
    labels = dict(kv.split("=", 1) for kv in (opt("--labels") or "").split(",") if kv)
    if os.environ.get("FAKE_GCP_CREATE_LABEL"):
        labels["image-build"] = os.environ["FAKE_GCP_CREATE_LABEL"]
    insts[name] = {"zone": opt("--zone"), "labels": labels, "status": "RUNNING"}
    save()
    if fail_on == "create":
        fail("ERROR: (gcloud.compute.instances.create) operation timed out")
    # The real command prints a table on stdout.
    print("NAME ZONE MACHINE_TYPE STATUS\n%s %s %s RUNNING" % (name, opt("--zone"), opt("--machine-type")))
elif cmd == ("compute", "instances", "describe"):
    inst = insts.get(name)
    if inst is None or inst["zone"] != opt("--zone"):
        fail("ERROR: (gcloud.compute.instances.describe) Could not fetch resource:\n"
             " - The resource 'projects/%s/zones/%s/instances/%s' was not found" % (project, opt("--zone"), name))
    if fmt == "value(networkInterfaces[0].accessConfigs[0].natIP)":
        print(os.environ.get("FAKE_GCP_NAT_IP", "192.0.2.20"))
    elif fmt == "csv[no-heading](labels.image-build,labels.image-build-expires)":
        label = os.environ.get("FAKE_GCP_DESCRIBE_LABEL") or inst["labels"].get("image-build", "")
        print("%s,%s" % (label, inst["labels"].get("image-build-expires", "")))
    else:
        fail("fake gcloud: unexpected describe format " + str(fmt), 2)
elif cmd == ("compute", "instances", "list"):
    if fmt != "csv[no-heading](name,zone.basename(),labels.image-build,labels.image-build-expires)":
        fail("fake gcloud: unexpected list format " + str(fmt), 2)
    for n, inst in sorted(insts.items()):
        if opt("--filter") == "labels.image-build:*" and "image-build" not in inst["labels"]:
            continue
        print("%s,%s,%s,%s" % (n, inst["zone"], inst["labels"].get("image-build", ""),
                               inst["labels"].get("image-build-expires", "")))
elif cmd == ("compute", "instances", "stop"):
    if name not in insts or "--quiet" not in args:
        fail("fake gcloud: stop of unknown instance " + str(name))
    insts[name]["status"] = "TERMINATED"
    save()
elif cmd == ("compute", "instances", "delete"):
    if name not in insts or insts[name]["zone"] != opt("--zone") or "--quiet" not in args:
        fail("fake gcloud: delete of unknown instance " + str(name))
    del insts[name]
    save()
elif cmd == ("compute", "images", "create"):
    src = insts.get(opt("--source-disk"))
    if src is None or src["zone"] != opt("--source-disk-zone"):
        fail("fake gcloud: no source disk " + str(opt("--source-disk")))
    if src["status"] != "TERMINATED":
        fail("ERROR: (gcloud.compute.images.create) The disk resource is already being used")
    if name in state["images"]:
        fail("ERROR: (gcloud.compute.images.create) image already exists")
    state["images"][name] = {"family": opt("--family")}
    save()
else:
    fail("fake gcloud: unrecognised command: " + " ".join(args), 2)
'''


def _gcp_env(tmp_path: Path, os_family: str = "linux", config: dict = None, **overrides):
    env, recipe = _build_env(tmp_path, os_family, **overrides)
    # The real backend, from the default directory next to build_image.sh.
    env.pop("IMAGE_BUILD_BACKENDS_DIR")
    cfg = {"GCP_BUILD_PROJECT": PROJECT, "GCP_BUILD_ZONE": ZONE, "GCP_BUILD_SUBNET": SUBNET}
    cfg.update(config or {})
    (tmp_path / "config.env").write_text(
        "".join(f"{k}='{v}'\n" for k, v in cfg.items() if v is not None))
    gcloud = tmp_path / "bin" / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    env["GCP_STATE"] = str(tmp_path / "gcp-state.json")
    return env, recipe


def _gcloud(calls: str) -> list:
    return [line for line in calls.splitlines() if line.startswith("gcloud ")]


def _metadata(tmp_path: Path) -> str:
    path = tmp_path / "calls.log.metadata"
    return path.read_text() if path.exists() else ""


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "gcp-state.json").read_text())


def _seed(tmp_path: Path, instances: dict) -> None:
    (tmp_path / "gcp-state.json").write_text(json.dumps({"instances": instances, "images": {}}))


def _inst(zone: str = ZONE, **labels) -> dict:
    return {"zone": zone, "status": "RUNNING",
            "labels": {k.replace("_", "-"): v for k, v in labels.items()}}


def _create_call(calls: str) -> str:
    create = [l for l in _gcloud(calls) if l.startswith("gcloud compute instances create ")]
    assert len(create) == 1, _gcloud(calls)
    return create[0]


def test_gcp_create_labels_and_subnet(tmp_path):
    env, recipe = _gcp_env(tmp_path)
    result = _build(env, recipe, "gcp")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    create = _create_call(calls)
    run_id = create.split()[4]
    assert RUN_ID_RE.match(run_id) and GCP_NAME_RE.match(run_id), run_id
    for arg in (f"--project {PROJECT}", f"--zone {ZONE}", "--machine-type e2-small",
                f"--subnet {SUBNET}", "--boot-disk-size 20GB", "--quiet",
                "--image https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/"
                "ubuntu-2404-lts-amd64-v20260101",
                "--no-service-account", "--no-scopes",
                "--metadata enable-oslogin=FALSE,block-project-ssh-keys=TRUE"):
        assert f" {arg}" in create, (arg, create)
    labels = dict(kv.split("=", 1) for kv in create.split(" --labels ")[1].split()[0].split(","))
    assert set(labels) == {"image-build", "image-build-expires"}
    assert labels["image-build"] == run_id
    assert re.fullmatch(r"[0-9]{12}", labels["image-build-expires"])
    for key, value in labels.items():
        assert GCP_LABEL_RE.match(key) and GCP_LABEL_RE.match(value), (key, value)
    # The build key goes in from a file: its line is builder:<pubkey>.
    assert re.search(r" --metadata-from-file ssh-keys=\S+", create), create
    assert f"--- ssh-keys\nbuilder:{PUBKEY}\n" in _metadata(tmp_path)
    assert "windows-startup-script" not in create
    # The base resolves in the base's project; every other gcloud call is
    # scoped to the configured build project. Reads name a format; writes --quiet.
    resolve = [l for l in _gcloud(calls) if " describe-from-family " in l]
    assert resolve == ["gcloud compute images describe-from-family ubuntu-2404-lts-amd64 "
                       "--project ubuntu-os-cloud --format value(selfLink)"]
    for line in _gcloud(calls):
        verb = line.split()[3]
        if verb != "describe-from-family":
            assert f" --project {PROJECT}" in line, line
        if verb in ("describe", "describe-from-family", "list"):
            assert " --format " in line, line
        else:
            assert " --quiet" in line, line
    assert "lab-playground-site" not in calls + _metadata(tmp_path) + result.stderr
    # The host is the instance's external IP; the build user is builder.
    assert any(l.startswith("ssh ") and f" builder@{NAT_IP} " in l for l in calls.splitlines())
    # Stdout carries only the templates.yaml line, not gcloud's table.
    image_name = _build_log(tmp_path).read_text().splitlines()[1].split("\t")[3]
    image_id = f"projects/{PROJECT}/global/images/{image_name}"
    assert result.stdout == f"gcp: {{image: {image_id}, machine_type: e2-small, disk_gb: 20}}\n"
    assert _build_log(tmp_path).read_text().splitlines()[1].split("\t")[2] == image_id
    # The build VM is gone; the image exists.
    state = _state(tmp_path)
    assert state["instances"] == {}
    assert list(state["images"]) == [image_name]
    for text in (result.stdout, result.stderr, calls, _metadata(tmp_path)):
        assert KEY_MATERIAL not in text


def test_gcp_windows_startup_script_installs_openssh(tmp_path):
    env, recipe = _gcp_env(tmp_path, "windows")
    result = _build(env, recipe, "gcp")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    create = _create_call(calls)
    assert re.search(r" --metadata-from-file windows-startup-script-ps1=\S+\.ps1", create), create
    assert "ssh-keys" not in create
    meta = _metadata(tmp_path)
    assert meta.startswith("--- windows-startup-script-ps1\n")
    script = meta.split("\n", 1)[1]
    for needle in ("OpenSSH.Server~~~~0.0.1.0", "Add-WindowsCapability",
                   "administrators_authorized_keys", "icacls", "/inheritance:r",
                   "*S-1-5-32-544:F", "*S-1-5-18:F", "Start-Service", "sshd",
                   "-LocalPort 22", "Enable-LocalUser", f"'{PUBKEY}'"):
        assert needle in script, needle
    # The Administrator password is random, generated in the guest, never output.
    assert "RandomNumberGenerator" in script
    assert "Write-Host" not in script and "Write-Output" not in script
    # No default shell change: the core's commands assume cmd.exe.
    assert "DefaultShell" not in script
    assert any(l.startswith("ssh ") and f" Administrator@{NAT_IP} " in l for l in calls.splitlines())
    assert "lab-playground-site" not in calls + meta
    assert KEY_MATERIAL not in calls + meta + result.stderr + result.stdout


def test_gcp_resolve_base_missing_family_fails(tmp_path):
    env, recipe = _gcp_env(tmp_path)
    env["FAKE_GCP_MISSING_FAMILIES"] = "ubuntu-2404-lts-amd64"
    result = _build(env, recipe, "gcp")
    assert result.returncode == 1
    assert "ubuntu-2404-lts-amd64" in result.stderr
    assert "ubuntu-os-cloud" in result.stderr
    calls = _calls(tmp_path)
    assert f"gcloud compute images describe-from-family ubuntu-2404-lts-amd64 --project ubuntu-os-cloud" in calls
    assert "instances create" not in calls

    # The base must be <project>/<family>.
    for i, bad in enumerate(("ubuntu-2404-lts-amd64", "ubuntu-os-cloud/", "/fam", "a/b/c")):
        env, recipe = _gcp_env(tmp_path / f"bad{i}", BASE_GCP=bad)
        result = _build(env, recipe, "gcp")
        assert result.returncode == 1, bad
        assert "<project>/<family>" in result.stderr, (bad, result.stderr)
        assert "gcloud" not in _calls(tmp_path / f"bad{i}"), bad


def test_gcp_leftovers_only_labelled_expired(tmp_path):
    other_zone = "europe-west1-c"
    old_1 = "ib-202601010000-aaaa"
    old_2 = "ib-202601010000-bbbb"
    young = "ib-209901010000-cccc"
    instances = {
        old_1: _inst(image_build=old_1, image_build_expires=EXPIRED),
        old_2: _inst(zone=other_zone, image_build=old_2, image_build_expires=EXPIRED),
        young: _inst(image_build=young, image_build_expires=UNEXPIRED),
        "lab-vm-1": _inst(lab_playground_site="site-1"),
        "renamed-vm": _inst(image_build=old_1, image_build_expires=EXPIRED),
        "ib-202601010000-dddd": _inst(image_build="ib-202601010000-dddd", image_build_expires="soon"),
        "ib-202601010000-eeee": _inst(image_build="ib-202601010000-eeee"),
    }
    env, _recipe = _gcp_env(tmp_path)
    _seed(tmp_path, instances)

    result = _build(env, "--reap", "gcp", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"would destroy {ZONE}/{old_1}\nwould destroy {other_zone}/{old_2}\n"
    assert "instances delete" not in _calls(tmp_path)
    assert f"DRY-RUN: gcloud compute instances delete {old_1} --project {PROJECT} --zone {ZONE}" in result.stderr
    assert _state(tmp_path)["instances"].keys() == instances.keys()

    result = _build(env, "--reap", "gcp")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert f"gcloud compute instances list --project {PROJECT} --filter labels.image-build:* " in calls
    deletes = [l for l in _gcloud(calls) if " instances delete " in l]
    assert deletes == [
        f"gcloud compute instances delete {old_1} --project {PROJECT} --zone {ZONE} --quiet",
        f"gcloud compute instances delete {old_2} --project {PROJECT} --zone {other_zone} --quiet",
    ]
    assert set(_state(tmp_path)["instances"]) == set(instances) - {old_1, old_2}

    # Nothing left to reap.
    result = _build(env, "--reap", "gcp")
    assert result.returncode == 0, result.stderr
    assert "nothing to reap" in result.stderr


def test_gcp_destroy_rechecks_label(tmp_path):
    # Reap: the listing said build VM, the re-read label says otherwise.
    old = "ib-202601010000-aaaa"
    env, _recipe = _gcp_env(tmp_path / "reap")
    _seed(tmp_path / "reap", {old: _inst(image_build=old, image_build_expires=EXPIRED)})
    env["FAKE_GCP_DESCRIBE_LABEL"] = "someone-else"
    result = _build(env, "--reap", "gcp")
    assert result.returncode == 1
    calls = _calls(tmp_path / "reap")
    assert f"gcloud compute instances describe {old} --project {PROJECT} --zone {ZONE}" in calls
    assert "instances delete" not in calls
    assert "someone-else" in result.stderr
    assert old in _state(tmp_path / "reap")["instances"]

    # Build: an instance whose label is not this run's is never deleted.
    env, recipe = _gcp_env(tmp_path / "build")
    env["FAKE_GCP_CREATE_LABEL"] = "ib-202601010000-ffff"
    result = _build(env, recipe, "gcp")
    assert result.returncode == 1
    calls = _calls(tmp_path / "build")
    run_id = _create_call(calls).split()[4]
    assert f"gcloud compute instances describe {run_id} --project {PROJECT} --zone {ZONE}" in calls
    assert "instances delete" not in calls
    assert "--reap gcp" in result.stderr
    assert run_id in _state(tmp_path / "build")["instances"]

    # Build: a failed verify destroys this run's instance (after re-reading it).
    env, recipe = _gcp_env(tmp_path / "verify")
    env.update(FAKE_SSH_FAIL_ON="verify.sh", FAKE_SSH_FAIL_RC="3")
    result = _build(env, recipe, "gcp")
    assert result.returncode == 1
    calls = _calls(tmp_path / "verify")
    run_id = _create_call(calls).split()[4]
    gcloud = _gcloud(calls)
    assert gcloud[-2].startswith(f"gcloud compute instances describe {run_id} ")
    assert gcloud[-1] == f"gcloud compute instances delete {run_id} --project {PROJECT} --zone {ZONE} --quiet"
    assert "images create" not in calls
    assert _state(tmp_path / "verify")["instances"] == {}

    # Build: create failed after the instance came to exist; it is deleted.
    env, recipe = _gcp_env(tmp_path / "create")
    env["FAKE_GCP_FAIL"] = "create"
    result = _build(env, recipe, "gcp")
    assert result.returncode == 1
    calls = _calls(tmp_path / "create")
    run_id = _create_call(calls).split()[4]
    assert _gcloud(calls)[-1] == f"gcloud compute instances delete {run_id} --project {PROJECT} --zone {ZONE} --quiet"
    assert _state(tmp_path / "create")["instances"] == {}
    assert "could not destroy" not in result.stderr

    # Build: create failed and left nothing; destroy finds nothing and succeeds.
    env, recipe = _gcp_env(tmp_path / "nothing")
    env["FAKE_GCP_FAIL"] = "create-nothing"
    result = _build(env, recipe, "gcp")
    assert result.returncode == 1
    calls = _calls(tmp_path / "nothing")
    assert "gcloud compute instances describe " in calls
    assert "instances delete" not in calls
    assert "could not destroy" not in result.stderr


def test_gcp_save_stops_before_image(tmp_path):
    env, recipe = _gcp_env(tmp_path)
    result = _build(env, recipe, "gcp")
    assert result.returncode == 0, result.stderr
    gcloud = _gcloud(_calls(tmp_path))
    run_id = _create_call(_calls(tmp_path)).split()[4]
    verbs = [" ".join(l.split()[1:4]) for l in gcloud]
    stop = verbs.index("compute instances stop")
    image = verbs.index("compute images create")
    assert stop < image < verbs.index("compute instances delete")
    assert gcloud[stop] == f"gcloud compute instances stop {run_id} --project {PROJECT} --zone {ZONE} --quiet"
    create = gcloud[image]
    image_name = create.split()[4]
    assert image_name.startswith("enpm634-privesc-linux-abc1234-")
    for arg in (f"--project {PROJECT}", f"--source-disk {run_id}", f"--source-disk-zone {ZONE}",
                "--family enpm634-privesc-linux", "--quiet",
                "--labels image-build-recipe=labs_demo_recipe_env,image-build-commit=abc1234"):
        assert f" {arg}" in create, (arg, create)
    assert "lab-playground-site" not in create
    assert _state(tmp_path)["images"] == {image_name: {"family": "enpm634-privesc-linux"}}

    env, recipe = _gcp_env(tmp_path / "family", GCP_IMAGE_FAMILY="privesc-linux")
    assert _build(env, recipe, "gcp").returncode == 0
    assert " --family privesc-linux " in _calls(tmp_path / "family")


def test_gcp_dry_run_runs_no_gcloud(tmp_path):
    env, recipe = _gcp_env(tmp_path)
    result = _build(env, "--dry-run", recipe, "gcp")
    assert result.returncode == 0, result.stderr
    assert _gcloud(_calls(tmp_path)) == []
    for printed in ("DRY-RUN: gcloud compute images describe-from-family ubuntu-2404-lts-amd64 ",
                    "DRY-RUN: gcloud compute instances create ib-",
                    "DRY-RUN: gcloud compute instances stop ib-",
                    "DRY-RUN: gcloud compute images create enpm634-privesc-linux-abc1234-",
                    "DRY-RUN: gcloud compute instances delete ib-"):
        assert printed in result.stderr, printed
    last = result.stdout.rstrip("\n").splitlines()[-1]
    assert re.fullmatch(rf"gcp: \{{image: projects/{PROJECT}/global/images/enpm634-privesc-linux-abc1234-"
                        r"[0-9]{12}, machine_type: e2-small, disk_gb: 20\}", last), last
    assert KEY_MATERIAL not in result.stderr + result.stdout


def test_gcp_config_checked(tmp_path):
    for i, (cfg, named) in enumerate((
        ({"GCP_BUILD_SUBNET": None}, "GCP_BUILD_SUBNET"),
        ({"GCP_BUILD_PROJECT": ""}, "GCP_BUILD_PROJECT"),
        ({"GCP_BUILD_ZONE": "us-east1"}, "GCP_BUILD_ZONE"),
        ({"GCP_BUILD_PROJECT": "Bad_Project"}, "GCP_BUILD_PROJECT"),
    )):
        env, recipe = _gcp_env(tmp_path / f"c{i}", config=cfg)
        result = _build(env, "--dry-run", recipe, "gcp")
        assert result.returncode == 2, (cfg, result.stderr)
        assert named in result.stderr, (cfg, result.stderr)
    env, recipe = _gcp_env(tmp_path / "family", GCP_IMAGE_FAMILY="Bad.Family")
    result = _build(env, "--dry-run", recipe, "gcp")
    assert result.returncode == 2
    assert "GCP_IMAGE_FAMILY" in result.stderr


def test_gcp_check_config_requires_gcloud_outside_dry_run(tmp_path):
    # The check itself, with a PATH that holds no gcloud (as AWS and Proxmox
    # check their tools): only a dry run may go on without the CLI.
    bash = shutil.which("bash")
    (tmp_path / "with").mkdir()
    (tmp_path / "with" / "gcloud").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "with" / "gcloud").chmod(0o755)
    (tmp_path / "without").mkdir()

    def check(dry_run, path_dir):
        script = (f"set -euo pipefail\nsource {shlex.quote(str(GCP_BACKEND))}\n"
                  f"GCP_BUILD_PROJECT={PROJECT} GCP_BUILD_ZONE={ZONE} GCP_BUILD_SUBNET={SUBNET}\n"
                  f"DRY_RUN={dry_run}\nPATH={shlex.quote(str(path_dir))}\nbackend_check_config\n")
        return subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ["PATH"]})

    result = check(0, tmp_path / "without")
    assert result.returncode != 0
    assert "gcloud" in result.stderr
    assert check(1, tmp_path / "without").returncode == 0
    result = check(0, tmp_path / "with")
    assert result.returncode == 0, result.stderr
