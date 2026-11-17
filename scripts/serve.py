#!/usr/bin/env python3
"""Report the command that serves a repo's web application on a given port.

Usage: serve.py <repo root> --port <port>

Read-only. The first line of stdout is exactly one state:

    manifest                 docs/agents/frames.md has a **Serve:** line, which wins
    detected                 no **Serve:** line, and the repo's shape names a command
    ambiguous: <dirs>        several tracked frontends below the root, none chosen
    invalid: <reason>        a **Serve:** line that cannot be used as written
    none: <reason>           nothing here says how to serve a web application

`manifest` and `detected` are followed by these lines, the last three only for
`detected`:

    serve: <command>         run from the repo root; it binds the given port
    install: <command>       installs dependencies in a fresh checkout, or
                             `not known` / `not needed`
    framework: <name>        what the command was chosen for
    dir: <path>              the directory served, `.` for the root

/frames runs `serve` once for the pull request's head and once for its base, in a
checkout of its own that `install` prepares. Detection is a guess at what most
repos of a shape do, so /frames proves it by waiting for the port to answer, and
a repo where it guesses wrong adds a **Serve:** line instead.

Exit 0 for every state. Exit 1 with a `✘` line when the root or a manifest
cannot be read.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from toolchain import LOCKFILES  # noqa: E402

MANIFEST = Path("docs/agents/frames.md")
SERVE_LINE = re.compile(r"^\*\*Serve:\*\*\s*`([^`]+)`\s*$", re.M)

# First match wins, so a framework that is built on vite comes before vite.
# `flags` follow the script's own arguments; `env` goes before the command.
FRAMEWORKS = [
    ("next", "next", "--port {port}", ""),
    ("nuxt", "nuxt", "--port {port}", ""),
    ("astro", "astro", "--port {port}", ""),
    ("gatsby", "gatsby", "--port {port}", ""),
    ("@angular/cli", "angular", "--port {port}", ""),
    ("react-scripts", "create-react-app", "", "BROWSER=none"),
    ("@sveltejs/kit", "sveltekit", "--port {port} --strictPort", ""),
    ("@react-router/dev", "react-router", "--port {port} --strictPort", ""),
    ("@remix-run/dev", "remix", "--port {port} --strictPort", ""),
    ("vite", "vite", "--port {port} --strictPort", ""),
]
SCRIPTS = ["dev", "start", "develop", "serve"]
# A script that starts several processes passes a --port to whichever reads it
# first, and that is a guess this script does not make.
SEVERAL = re.compile(r"concurrently|npm-run-all|run-p\b|\bturbo\b|\bnx\b|\blerna\b|&&|\s&\s|\|")

INSTALL = {
    "npm": "npm ci",
    "pnpm": "pnpm install --frozen-lockfile",
    "yarn": "yarn install --frozen-lockfile",
    "bun": "bun install --frozen-lockfile",
    "uv": "uv sync --frozen",
}
# Below this depth a package.json is a fixture or a vendored copy, not an app.
MAX_DEPTH = 3


class Unreadable(Exception):
    pass


def lockfile_pm(where):
    for lockfile, pm in LOCKFILES:
        if (where / lockfile).is_file():
            return pm
    return None


def read_package(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{path} could not be read: {exc}")
    return data if isinstance(data, dict) else {}


def framework_of(pkg):
    deps = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    for dep, name, flags, env in FRAMEWORKS:
        if dep in deps:
            return name, flags, env
    return None


def run_prefix(pm, script, flags):
    if not flags:
        return f"{pm} run {script}"
    # npm alone swallows arguments that do not follow its own `--`.
    sep = " --" if pm == "npm" else ""
    return f"{pm} run {script}{sep} {flags}"


def in_dir(directory, command):
    return command if directory == "." else f"cd {directory} && {command}"


def node_serve(root, directory, port):
    """(state line, fields) for the package.json in directory."""
    where = root / directory
    pkg = read_package(where / "package.json")
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    script = next((s for s in SCRIPTS if isinstance(scripts.get(s), str) and scripts[s].strip()), None)
    if script is None:
        return f"none: {directory}/package.json has no {', '.join(SCRIPTS)} script", {}
    if SEVERAL.search(scripts[script]):
        return (f"none: the {script} script runs several commands ({scripts[script]}); "
                f"add a **Serve:** line to {MANIFEST}"), {}

    found = framework_of(pkg)
    name, flags, env = found if found else ("unknown, PORT only", "", "")
    pm_dir = where if lockfile_pm(where) else root
    pm = lockfile_pm(pm_dir) or "npm"
    if pm == "uv":  # a Python project that happens to carry a package.json
        pm = "npm"
    flags = flags.format(port=port)
    command = " ".join(p for p in (f"PORT={port}", env, run_prefix(pm, script, flags)) if p)
    install = INSTALL.get(pm, "not known")
    install_dir = "." if pm_dir == root else directory
    return "detected", {
        "serve": in_dir(directory, command),
        "install": in_dir(install_dir, install) if install != "not known" else install,
        "framework": name,
        "dir": directory,
    }


def tracked_packages(root):
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    dirs = []
    for path in proc.stdout.split("\0"):
        *parts, name = path.split("/")
        if name == "package.json" and parts and len(parts) <= MAX_DEPTH and "node_modules" not in parts:
            dirs.append("/".join(parts))
    return sorted(dirs)


def root_install(root):
    pm = lockfile_pm(root)
    if pm:
        return INSTALL[pm]
    if (root / "Gemfile.lock").is_file():
        return "bundle install"
    return "not known"


def manifest_serve(root, port):
    path = root / MANIFEST
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{MANIFEST} could not be read: {exc}")
    match = SERVE_LINE.search(text)
    if not match:
        return None
    command = match.group(1)
    if "{port}" not in command:
        return f"invalid: the **Serve:** line in {MANIFEST} has no {{port}}", {}
    return "manifest", {"serve": command.replace("{port}", str(port)), "install": root_install(root)}


def detect(root, port):
    if (root / "package.json").is_file() and framework_of(read_package(root / "package.json")):
        return node_serve(root, ".", port)

    if (root / "manage.py").is_file():
        py = "uv run python" if (root / "uv.lock").is_file() else "python3"
        return "detected", {
            "serve": f"{py} manage.py runserver 127.0.0.1:{port}",
            "install": root_install(root), "framework": "django", "dir": ".",
        }
    if (root / "bin/rails").is_file():
        return "detected", {
            "serve": f"bin/rails server -p {port} -b 127.0.0.1",
            "install": root_install(root), "framework": "rails", "dir": ".",
        }

    if (root / "package.json").is_file():
        return node_serve(root, ".", port)

    frontends = [d for d in tracked_packages(root) if framework_of(read_package(root / d / "package.json"))]
    if len(frontends) == 1:
        return node_serve(root, frontends[0], port)
    if len(frontends) > 1:
        return (f"ambiguous: {', '.join(frontends)}; add a **Serve:** line to {MANIFEST}"), {}

    if (root / "index.html").is_file():
        return "detected", {
            "serve": f"python3 -m http.server {port} --bind 127.0.0.1",
            "install": "not needed", "framework": "static", "dir": ".",
        }
    return f"none: no web application found; add a **Serve:** line to {MANIFEST}", {}


def report(root, port):
    if not root.is_dir():
        raise Unreadable(f"{root} is not a directory")
    state, fields = manifest_serve(root, port) or detect(root, port)
    order = ["serve", "install", "framework", "dir"]
    return [state, *(f"{key}: {fields[key]}" for key in order if key in fields)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="the repo root to read")
    parser.add_argument("--port", type=int, required=True, help="the port to serve on")
    args = parser.parse_args()
    try:
        print("\n".join(report(args.root, args.port)))
    except Unreadable as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
