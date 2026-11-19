"""The repository is public and general-purpose: it carries no deployment's
or course's identity and no local tool-state files.

This file holds the forbidden list itself, so it is the one file the scan
skips. Everything else git tracks, or would add, is read. Without git the
test fails rather than skipping: a guard that cannot list the tree has
checked nothing.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

# Paths that must not exist in the tree. The ignore files may still name the
# tool-state directories; only files inside them are refused.
FORBIDDEN_PATHS = re.compile(
    r"(?:^|/)CLAUDE\.md$|^docs/superpowers/|(?:^|/)\.claude/|(?:^|/)\.superpowers/")
# Text that must not appear in any file.
FORBIDDEN_TEXT = re.compile(
    r"docs/superpowers/|CLAUDE\.md|ENPM|University of Maryland|UMD lab"
    r"|\bruling [CI]\d", re.IGNORECASE)
# A listing that misses these, or holds fewer files, scanned the wrong tree.
SENTINELS = {"README.md", "app/__init__.py", "pyproject.toml"}
MIN_FILES = 200


def _listed():
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"], check=True, capture_output=True).stdout
    return sorted({p.decode() for p in out.split(b"\0") if p})


# Catches a tool-state file, a course or institution name, or a numbered
# planning-decision reference landing in the public tree, and a scan that
# passes because it read nothing.
def test_tree_carries_no_course_identity_or_tool_state_files():
    hits, scanned = [], set()
    for rel in _listed():
        if rel == SELF:
            continue
        if FORBIDDEN_PATHS.search(rel):
            hits.append(f"{rel}: forbidden path")
        path = ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        scanned.add(rel)
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            match = FORBIDDEN_TEXT.search(line)
            if match:
                hits.append(f"{rel}:{number}: {match.group(0)!r}")
    assert SENTINELS <= scanned, sorted(SENTINELS - scanned)
    assert len(scanned) >= MIN_FILES, len(scanned)
    assert not hits, "\n".join(hits)
