#!/usr/bin/env python3
"""Cases for toolchain.py.

The Seam is its command line: run against a temporary directory, observed
through stdout and the exit code. The first line is the state, and a
recognised toolchain follows it with one `{gate}: {command}` line per gate.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("toolchain.py")
GATES = ["lint", "typecheck", "format check", "test"]
NPM_INIT_TEST = 'echo "Error: no test specified" && exit 1'


def run(root):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True
    )


def report(root):
    """(state, {gate: command}) from a run that must exit 0."""
    proc = run(root)
    if proc.returncode != 0:
        raise AssertionError(f"exit {proc.returncode}: {proc.stderr.strip()}")
    lines = proc.stdout.splitlines()
    if not lines:
        raise AssertionError("printed nothing")
    gates = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    return lines[0], gates


def package_json(root, scripts):
    (root / "package.json").write_text(json.dumps({"name": "x", "scripts": scripts}))


def git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def case(label, check):
    root = Path(tempfile.mkdtemp())
    try:
        ok, why = check(root)
    except Exception as exc:  # a crash is a failed case, not a crashed suite
        ok, why = False, f"raised {exc!r}"
    finally:
        shutil.rmtree(root)
    if not ok:
        print(f"  BAD  {label}: {why}")
    return ok


def c1_each_lockfile_names_its_toolchain(root):
    want = {
        "bun.lockb": "bun",
        "bun.lock": "bun",
        "pnpm-lock.yaml": "pnpm",
        "package-lock.json": "npm",
        "yarn.lock": "yarn",
        "uv.lock": "uv",
    }
    wrong = []
    for lockfile, toolchain in want.items():
        repo = root / lockfile.replace(".", "-")
        repo.mkdir()
        (repo / lockfile).write_text("")
        state, _ = report(repo)
        if state != toolchain:
            wrong.append(f"{lockfile} printed {state!r}, expected {toolchain!r}")
    return not wrong, "; ".join(wrong)


def c2_declared_scripts_become_commands(root):
    (root / "package-lock.json").write_text("{}")
    package_json(root, {"lint": "eslint .", "test": "vitest run"})
    state, gates = report(root)
    want = {
        "lint": "npm run lint",
        "typecheck": "not declared",
        "format check": "not declared",
        "test": "npm run test",
    }
    return (state, gates) == ("npm", want), f"printed {state!r} {gates}"


def c3_type_check_spelling_is_accepted(root):
    (root / "package-lock.json").write_text("{}")
    package_json(root, {"type-check": "tsc --noEmit"})
    _, gates = report(root)
    got = gates.get("typecheck")
    return got == "npm run type-check", f"typecheck gate is {got!r}"


def c4_npm_init_placeholder_is_not_a_test(root):
    (root / "package-lock.json").write_text("{}")
    package_json(root, {"test": NPM_INIT_TEST})
    _, gates = report(root)
    got = gates.get("test")
    return got == "not declared", f"test gate is {got!r}"


def c5_uv_gates_come_from_pyproject(root):
    (root / "uv.lock").write_text("")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n'
        "[tool.ruff]\nline-length = 100\n\n"
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    state, gates = report(root)
    want = {
        "lint": "uv run ruff check .",
        "typecheck": "not declared",
        "format check": "uv run ruff format --check .",
        "test": "uv run pytest -q",
    }
    return (state, gates) == ("uv", want), f"printed {state!r} {gates}"


def c6_package_json_without_a_lockfile(root):
    package_json(root, {"test": "vitest run"})
    state, _ = report(root)
    return state == "node, no lockfile", f"printed {state!r}"


def c7_another_toolchains_manifest_is_unrecognised(root):
    wrong = []
    for manifest, content in (("go.mod", "module x\n"), ("pyproject.toml", '[project]\nname = "x"\n')):
        repo = root / manifest.replace(".", "-")
        repo.mkdir()
        (repo / manifest).write_text(content)
        state, _ = report(repo)
        if not state.startswith("unrecognised") or manifest not in state:
            wrong.append(f"{manifest} printed {state!r}")
    return not wrong, "; ".join(wrong)


def c8_a_tracked_manifest_below_the_root(root):
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "package.json").write_text("{}")
    (root / "README.md").write_text("# x\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    state, _ = report(root)
    ok = state.startswith("below root") and "apps/web/package.json" in state
    return ok, f"printed {state!r}"


def c9_an_empty_directory_is_none_and_stays_empty(root):
    proc = run(root)
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {proc.stderr.strip()}"
    if proc.stdout.splitlines() != ["none"]:
        return False, f"printed {proc.stdout!r}"
    left = sorted(p.name for p in root.iterdir())
    return not left, f"left {left} behind"


CASES = [
    ("#77 C1 each of the six lockfiles prints its toolchain, bun.lock included",
     c1_each_lockfile_names_its_toolchain),
    ("#77 C2 declared lint and test print their npm commands, the other gates not declared",
     c2_declared_scripts_become_commands),
    ("#77 C3 a type-check script is the typecheck gate's command",
     c3_type_check_spelling_is_accepted),
    ("#77 C4 the npm init placeholder test script is not a declared gate",
     c4_npm_init_placeholder_is_not_a_test),
    ("#77 C5 uv prints ruff and pytest as pyproject.toml configures them, typecheck not declared",
     c5_uv_gates_come_from_pyproject),
    ("#77 C6 a package.json with no lockfile prints node, no lockfile",
     c6_package_json_without_a_lockfile),
    ("#77 C7 go.mod, or a pyproject.toml with no uv.lock, prints unrecognised and names the file",
     c7_another_toolchains_manifest_is_unrecognised),
    ("#77 C8 a tracked manifest below the root prints below root and names its path",
     c8_a_tracked_manifest_below_the_root),
    ("#77 C9 an empty directory prints none, exits 0 and stays empty",
     c9_an_empty_directory_is_none_and_stays_empty),
]


def main():
    bad = [label for label, check in CASES if not case(label, check)]
    if bad:
        print(f"✘ {len(bad)} of {len(CASES)} toolchain cases behaved unexpectedly")
        return 1
    print(f"✔ {len(CASES)} toolchain cases behave as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
