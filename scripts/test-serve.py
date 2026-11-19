#!/usr/bin/env python3
"""Cases for serve.py.

The Seam is its command line: run against a temporary directory with a port,
observed through stdout and the exit code. The first line is the state, and
`key: value` lines follow it.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("serve.py")
PORT = "20621"


def run(root):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--port", PORT],
        capture_output=True, text=True,
    )


def report(root):
    """(state, {key: value}) from a run that must exit 0."""
    proc = run(root)
    if proc.returncode != 0:
        raise AssertionError(f"exit {proc.returncode}: {proc.stderr.strip()}")
    lines = proc.stdout.splitlines()
    if not lines:
        raise AssertionError("printed nothing")
    fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    return lines[0], fields


def package_json(where, scripts, deps=None, dev_deps=None):
    where.mkdir(parents=True, exist_ok=True)
    (where / "package.json").write_text(json.dumps({
        "name": "x", "scripts": scripts,
        "dependencies": deps or {}, "devDependencies": dev_deps or {},
    }))


def git_track(root):
    run_git = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
        cwd=root, check=True, capture_output=True,
    )
    run_git("init", "-q")
    run_git("add", "-A")


def expect(state, fields, want_state, **want):
    wrong = []
    if state != want_state:
        wrong.append(f"state {state!r}, expected {want_state!r}")
    for key, value in want.items():
        key = key.replace("_", " ")
        if fields.get(key) != value:
            wrong.append(f"{key} {fields.get(key)!r}, expected {value!r}")
    return not wrong, "; ".join(wrong)


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


def manifest_serve_line_wins(root):
    package_json(root, {"dev": "vite"}, dev_deps={"vite": "5"})
    (root / "pnpm-lock.yaml").write_text("")
    (root / "docs/agents").mkdir(parents=True)
    (root / "docs/agents/frames.md").write_text(
        "**Serve:** `pnpm --dir web preview --port {port} --strictPort`\n"
    )
    state, fields = report(root)
    return expect(state, fields, "manifest",
                  serve=f"pnpm --dir web preview --port {PORT} --strictPort",
                  install="pnpm install --frozen-lockfile")


def manifest_serve_line_without_port_is_invalid(root):
    (root / "docs/agents").mkdir(parents=True)
    (root / "docs/agents/frames.md").write_text("**Serve:** `npm run dev`\n")
    state, _ = report(root)
    ok = state.startswith("invalid: ") and "{port}" in state
    return ok, f"printed {state!r}"


def a_frames_md_with_no_serve_line_falls_through_to_detection(root):
    package_json(root, {"dev": "vite"}, dev_deps={"vite": "5"})
    (root / "package-lock.json").write_text("")
    (root / "docs/agents").mkdir(parents=True)
    (root / "docs/agents/frames.md").write_text("**Auth:** `/login`\n")
    state, fields = report(root)
    return expect(state, fields, "detected", framework="vite")


def vite_with_pnpm(root):
    package_json(root, {"dev": "vite"}, dev_deps={"vite": "5"})
    (root / "pnpm-lock.yaml").write_text("")
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"PORT={PORT} pnpm run dev --port {PORT} --strictPort",
                  framework="vite", dir=".",
                  install="pnpm install --frozen-lockfile")


def next_with_npm_passes_args_after_a_separator(root):
    package_json(root, {"dev": "next dev"}, deps={"next": "15", "react": "19"})
    (root / "package-lock.json").write_text("")
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"PORT={PORT} npm run dev -- --port {PORT}",
                  framework="next", install="npm ci")


def react_router_and_remix_are_named_and_take_vite_flags(root):
    wrong = []
    for dep, name in [("@react-router/dev", "react-router"), ("@remix-run/dev", "remix")]:
        repo = root / name
        package_json(repo, {"dev": f"{name} dev"}, dev_deps={dep: "7", "vite": "6"})
        (repo / "package-lock.json").write_text("")
        state, fields = report(repo)
        ok, why = expect(state, fields, "detected", framework=name,
                         serve=f"PORT={PORT} npm run dev -- --port {PORT} --strictPort")
        if not ok:
            wrong.append(f"{dep}: {why}")
    return not wrong, "; ".join(wrong)


def create_react_app_takes_the_port_from_the_environment_only(root):
    package_json(root, {"start": "react-scripts start"}, deps={"react-scripts": "5"})
    (root / "yarn.lock").write_text("")
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"PORT={PORT} BROWSER=none yarn run start",
                  framework="create-react-app")


def a_dev_script_running_several_commands_is_not_guessed(root):
    package_json(root, {"dev": "concurrently \"vite\" \"tsx watch server.ts\""},
                 dev_deps={"vite": "5"})
    (root / "package-lock.json").write_text("")
    state, _ = report(root)
    ok = state.startswith("none: ") and "**Serve:**" in state
    return ok, f"printed {state!r}"


def an_unknown_dev_script_gets_port_from_the_environment(root):
    package_json(root, {"dev": "node server.js"}, deps={"express": "4"})
    (root / "package-lock.json").write_text("")
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"PORT={PORT} npm run dev", framework="unknown, PORT only")


def a_package_json_with_no_dev_script_is_none(root):
    package_json(root, {"build": "tsc"}, deps={"lodash": "4"})
    (root / "package-lock.json").write_text("")
    state, _ = report(root)
    return state.startswith("none: "), f"printed {state!r}"


def one_frontend_below_the_root_is_served_from_its_directory(root):
    package_json(root / "frontend", {"dev": "vite"}, dev_deps={"vite": "5"})
    (root / "frontend/pnpm-lock.yaml").write_text("")
    package_json(root / "api", {"start": "node index.js"}, deps={"express": "4"})
    git_track(root)
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"cd frontend && PORT={PORT} pnpm run dev --port {PORT} --strictPort",
                  dir="frontend", install="cd frontend && pnpm install --frozen-lockfile")


def two_frontends_below_the_root_are_ambiguous(root):
    package_json(root / "apps/web", {"dev": "next dev"}, deps={"next": "15"})
    package_json(root / "apps/admin", {"dev": "vite"}, dev_deps={"vite": "5"})
    git_track(root)
    state, _ = report(root)
    ok = state.startswith("ambiguous: ") and "apps/web" in state and "apps/admin" in state
    return ok, f"printed {state!r}"


def django_under_uv(root):
    (root / "manage.py").write_text("")
    (root / "uv.lock").write_text("")
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"uv run python manage.py runserver 127.0.0.1:{PORT}",
                  framework="django", install="uv sync --frozen")


def rails(root):
    (root / "bin").mkdir()
    (root / "bin/rails").write_text("")
    (root / "Gemfile.lock").write_text("")
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"bin/rails server -p {PORT} -b 127.0.0.1",
                  framework="rails", install="bundle install")


def a_static_site_is_served_as_files(root):
    (root / "index.html").write_text("<h1>hi</h1>")
    state, fields = report(root)
    return expect(state, fields, "detected",
                  serve=f"python3 -m http.server {PORT} --bind 127.0.0.1",
                  framework="static", install="not needed")


def an_empty_directory_is_none(root):
    state, _ = report(root)
    return state.startswith("none: "), f"printed {state!r}"


def a_missing_port_fails_with_usage(root):
    proc = subprocess.run([sys.executable, str(SCRIPT), str(root)],
                          capture_output=True, text=True)
    return proc.returncode == 2, f"exit {proc.returncode}"


CASES = [
    ("a **Serve:** line in docs/agents/frames.md wins, with {port} substituted",
     manifest_serve_line_wins),
    ("a **Serve:** line without {port} is invalid and says so",
     manifest_serve_line_without_port_is_invalid),
    ("a frames.md with no **Serve:** line falls through to detection",
     a_frames_md_with_no_serve_line_falls_through_to_detection),
    ("vite under pnpm gets --port and --strictPort, and pnpm's frozen install",
     vite_with_pnpm),
    ("next under npm passes the port after npm's -- separator", next_with_npm_passes_args_after_a_separator),
    ("react-router and remix are named for themselves and take vite's flags",
     react_router_and_remix_are_named_and_take_vite_flags),
    ("create-react-app takes its port from PORT and runs start",
     create_react_app_takes_the_port_from_the_environment_only),
    ("a dev script that runs several commands is none and names the **Serve:** line",
     a_dev_script_running_several_commands_is_not_guessed),
    ("a dev script for no known framework is served with PORT alone",
     an_unknown_dev_script_gets_port_from_the_environment),
    ("a package.json with no dev, start, develop or serve script is none",
     a_package_json_with_no_dev_script_is_none),
    ("one tracked frontend below the root is served and installed from its directory",
     one_frontend_below_the_root_is_served_from_its_directory),
    ("two tracked frontends below the root are ambiguous and both are named",
     two_frontends_below_the_root_are_ambiguous),
    ("manage.py under uv runs Django's runserver on 127.0.0.1", django_under_uv),
    ("bin/rails runs rails server bound to 127.0.0.1", rails),
    ("an index.html and no manifest is served as static files", a_static_site_is_served_as_files),
    ("an empty directory is none", an_empty_directory_is_none),
    ("no --port is a usage error", a_missing_port_fails_with_usage),
]


def main():
    bad = [label for label, check in CASES if not case(label, check)]
    if bad:
        print(f"✘ {len(bad)} of {len(CASES)} serve cases behaved unexpectedly")
        return 1
    print(f"✔ {len(CASES)} serve cases behave as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
