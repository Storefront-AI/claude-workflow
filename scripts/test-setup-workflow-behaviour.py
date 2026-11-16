#!/usr/bin/env python3
"""Behavioural case for /setup-workflow, observed on the draft a dry run prints.

The Seam is what a session following skills/setup-workflow/SKILL.md with
`--dry-run` prints for a scratch repo. That takes a signed-in `claude` and a
model run, so validate.sh does not call it, the same as
test-review-pr-behaviour.py. Run it by hand after changing the skill's §1:

    test-setup-workflow-behaviour.py

The session is pointed at this checkout's SKILL.md by path, not at an
installed plugin, so the prose under test is the prose on this branch.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "setup-workflow" / "SKILL.md"
ON = re.compile(r"Specs:(\*\*)? on\b")
OFF = re.compile(r"Specs:(\*\*)? off\b")


def dry_run(repo):
    prompt = (
        f"Read {SKILL} and follow it exactly, as if I had typed /setup-workflow --dry-run "
        f"in this directory. `<this skill dir>` is {SKILL.parent}. Write nothing."
    )
    return subprocess.run(
        ["claude", "-p", prompt, "--allowedTools", "Read", "Glob", "Grep", "Bash"],
        cwd=repo, capture_output=True, text=True, timeout=900,
    )


def scratch_repo(root):
    (root / "bun.lock").write_text('{\n  "lockfileVersion": 1\n}\n')
    (root / "package.json").write_text('{"name": "x", "scripts": {"test": "vitest run"}}\n')
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=root, check=True, capture_output=True,
        )


def c10_bun_lock_alone_proposes_specs_on(root):
    scratch_repo(root)
    proc = dry_run(root)
    if proc.returncode != 0:
        return False, f"claude exit {proc.returncode}: {proc.stderr.strip()[:300]}"
    draft = proc.stdout
    if OFF.search(draft):
        return False, f"the draft proposes Specs: off:\n{draft}"
    if not ON.search(draft):
        return False, f"the draft names no Specs value:\n{draft}"
    if (root / "docs").exists():
        return False, "a dry run wrote docs/"
    return True, ""


CASES = [
    ("#77 C10 a repo whose only lockfile is bun.lock gets a draft proposing Specs: on",
     c10_bun_lock_alone_proposes_specs_on),
]


def main():
    bad = []
    for label, check in CASES:
        root = Path(tempfile.mkdtemp())
        try:
            ok, why = check(root)
        except Exception as exc:
            ok, why = False, f"raised {exc!r}"
        finally:
            shutil.rmtree(root)
        if not ok:
            print(f"  BAD  {label}: {why}")
            bad.append(label)
    if bad:
        print(f"✘ {len(bad)} of {len(CASES)} setup-workflow cases behaved unexpectedly")
        return 1
    print(f"✔ {len(CASES)} setup-workflow cases behave as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
