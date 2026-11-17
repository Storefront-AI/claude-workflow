#!/usr/bin/env python3
"""Report a repo's toolchain and the gates its manifest declares.

Usage: toolchain.py <repo root>

Read-only. The first line of stdout is exactly one state:

    bun | pnpm | npm | yarn | uv     a lockfile at the root, first match in LOCKFILES order
    node, no lockfile                a package.json at the root and no lockfile
    unrecognised: <files>            another toolchain's manifest at the root
    below root: <paths>              no manifest at the root, tracked ones below it
    none                             no manifest anywhere in the tracked tree

A recognised toolchain is followed by one `<gate>: <command>` line per gate, in
GATES order, where the command is `not declared` when the manifest does not
declare it. The gates and commands are the ones /finalize-pr §5 and §6 run.

Exit 0 for every state. Exit 1 with a `✘` line when the root or a manifest
cannot be read.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

LOCKFILES = [
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("pnpm-lock.yaml", "pnpm"),
    ("package-lock.json", "npm"),
    ("yarn.lock", "yarn"),
    ("uv.lock", "uv"),
]
# A pyproject.toml is here because without uv.lock it may be Poetry, PDM or plain
# pip, and none of those is a toolchain the gates are known for.
OTHER_MANIFESTS = [
    "go.mod", "Cargo.toml", "Gemfile", "pom.xml",
    "build.gradle", "build.gradle.kts", "pyproject.toml",
]
GATES = ["lint", "typecheck", "format check", "test"]
NOT_DECLARED = "not declared"
# How many paths a `below root` line names before it counts the rest.
BELOW_ROOT_SHOWN = 10
TOML_KEY = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')"""
TOML_TABLE = re.compile(
    rf"^\s*(\[\[?)\s*({TOML_KEY}(?:\s*\.\s*{TOML_KEY})*)\s*(\]\]?)\s*(#.*)?$"
)


class Unreadable(Exception):
    pass


def is_other_manifest(name):
    return name in OTHER_MANIFESTS or name.endswith(".sln")


def node_gates(root, pm):
    manifest = root / "package.json"
    scripts = {}
    if manifest.is_file():
        try:
            scripts = json.loads(manifest.read_text(encoding="utf-8")).get("scripts")
        except (OSError, ValueError, AttributeError) as exc:
            raise Unreadable(f"package.json could not be read: {exc}")
        if not isinstance(scripts, dict):
            scripts = {}

    def declared(*names):
        for name in names:
            script = scripts.get(name)
            # `npm init` writes a test script that only echoes this and exits 1.
            if isinstance(script, str) and script.strip() and "no test specified" not in script:
                return f"{pm} run {name}"
        return NOT_DECLARED

    return {
        "lint": declared("lint"),
        "typecheck": declared("type-check", "typecheck"),
        "format check": declared("format:check"),
        "test": declared("test"),
    }


def open_string(line, closing):
    """The multiline-string delimiter still open at the end of line, or None.

    `closing` is the delimiter open at its start.
    """
    i = 0
    while i < len(line):
        if closing:
            while i < len(line) and not line.startswith(closing, i):
                # A backslash escapes the next character in a basic string only.
                i += 2 if line[i] == "\\" and closing == '"""' else 1
            if i >= len(line):
                return closing
            i += 3
            closing = None
        elif line[i] == "#":
            break
        elif line.startswith(('"""', "'''"), i):
            closing = line[i:i + 3]
            i += 3
        elif line[i] in "\"'":
            quote = line[i]
            i += 1
            while i < len(line) and line[i] != quote:
                i += 2 if line[i] == "\\" and quote == '"' else 1
            i += 1
        else:
            i += 1
    return closing


def toml_tables(path):
    """The tables a TOML file opens, each a tuple of its key's parts.

    Read off the header lines, because the scripts here run on a Python whose
    standard library has no TOML parser, and which tables exist is all the gates
    depend on. A header may quote a part or put spaces round its dots, and a
    line inside a multiline string is not a header whatever it looks like.
    """
    if not path.is_file():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{path.name} could not be read: {exc}")
    tables = set()
    closing = None
    for line in lines:
        header = None if closing else TOML_TABLE.match(line)
        if header and len(header.group(1)) == len(header.group(3)):
            parts = re.findall(TOML_KEY, header.group(2))
            tables.add(tuple(p[1:-1] if p[0] in "\"'" else p for p in parts))
        else:
            closing = open_string(line, closing)
    return tables


def uv_gates(root):
    tables = toml_tables(root / "pyproject.toml")

    def configures(*table):
        return any(t[:len(table)] == table for t in tables)

    ruff = configures("tool", "ruff")
    return {
        "lint": "uv run ruff check ." if ruff else NOT_DECLARED,
        "typecheck": "uv run mypy" if configures("tool", "mypy") else NOT_DECLARED,
        "format check": "uv run ruff format --check ." if ruff else NOT_DECLARED,
        "test": "uv run pytest -q" if configures("tool", "pytest", "ini_options") else NOT_DECLARED,
    }


def manifests_below(root):
    """Tracked manifests in directories below the root, sorted."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True
        )
    except OSError:
        return []
    if proc.returncode != 0:  # not a git repository: nothing is tracked
        return []
    found = []
    for path in proc.stdout.split("\0"):
        *dirs, name = path.split("/")
        if dirs and "node_modules" not in dirs and (
            name == "package.json" or is_other_manifest(name)
        ):
            found.append(path)
    return sorted(found)


def report(root):
    """The lines to print for the repo at root."""
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_file())
    except OSError as exc:
        raise Unreadable(f"{root} could not be read: {exc}")

    for lockfile, toolchain in LOCKFILES:
        if lockfile in names:
            gates = uv_gates(root) if toolchain == "uv" else node_gates(root, toolchain)
            return [toolchain, *(f"{gate}: {gates[gate]}" for gate in GATES)]

    if "package.json" in names:
        return ["node, no lockfile"]

    others = [name for name in names if is_other_manifest(name)]
    if others:
        return [f"unrecognised: {', '.join(others)}"]

    below = manifests_below(root)
    if below:
        shown = ", ".join(below[:BELOW_ROOT_SHOWN])
        rest = len(below) - BELOW_ROOT_SHOWN
        return [f"below root: {shown}" + (f" (and {rest} more)" if rest > 0 else "")]

    return ["none"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="the repo root to read")
    args = parser.parse_args()
    try:
        print("\n".join(report(args.root)))
    except Unreadable as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
