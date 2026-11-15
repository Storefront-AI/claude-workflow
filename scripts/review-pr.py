#!/usr/bin/env python3
"""The mechanical half of /review-pr, so the session that runs it holds only the judgement.

Usage (run from the PR's checkout):

  review-pr.py workdir
  review-pr.py prepare --pr {n} [--criteria {path}] [--full]
  review-pr.py check {code|security} {report}
  review-pr.py finish --run {dir} --reviewer {name} --security "{vendor} ({model})" [--skipped "{entry}: {why}"]...
  review-pr.py abort --run {dir} --why "{reason}"

/review-pr used to carry every step in its own prose: the pre-flight, the
scope, the Spec heuristic, the prompts, the ready state, Copilot, Auto-fix and
the summary. A session that ran it read all of that, read the references it
pasted into prompts, and composed the summary by hand, on every round. What is
left to the session is what needs a model: dispatching the two reviewers,
walking the security chain, and, with no project reviewer, running the
fallback review.

workdir prints the directory a run's files live in,
`{toplevel}/.claude/review-pr`, after making sure git ignores it locally
(`info/exclude`, never a tracked file). It sits inside the checkout because a
codex or agy reviewer is pointed at a prompt file there and reads within its
workspace. /finalize-pr writes its Criteria report there too.

prepare runs the pre-flight, works out the scope, the candidate Specs and the
security chain, renders both reviewer prompts into `{workdir}/{pr}-{head7}/`,
and prints the run's manifest as JSON. The manifest names the prompt files and
the report files the session writes each reviewer's reply to.

check exits 0 when a reviewer's report carries its sentinel lines, and 1 when
it does not. The session walks the security chain on it: a report without them
is no result, and the walk moves on.

abort is the stop the session itself reaches: a code reviewer that returned
no sentinels, or a security chain where no entry produced a result.

finish reads both reports, decides the verdict, sets the PR ready or draft and
reads it back, hands a ready PR to Copilot and an Auto-fix session, posts the
summary CONTRACTS.md describes and reads it back, and prints the local
summary.

A stop is never a pass. Wherever prepare or finish stops, it posts nothing,
returns a ready PR to draft and reads it back, and exits 1 with a `✘` line.
Exit 2 is a failure before the PR could be read, so its state is unknown.
"""

import argparse
import datetime
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge_gate = _load("merge_gate", "merge-gate.py")
security_reviewers = _load("security_reviewers", "security-reviewers.py")
spec_lib = _load("spec_lib", "spec.py")

WORKDIR = ".claude/review-pr"
EXCLUDE = "/.claude/review-pr/"

SEVERITIES = ("Critical", "High", "Medium", "Low")
FINDING = re.compile(
    r"^[\s>*-]*FINDING:\s*(Critical|High|Medium|Low)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*$", re.M | re.I
)
DRIFT = re.compile(r"^[\s>*-]*spec-drift:\s*#(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*$", re.M | re.I)
CODE_SENTINEL = re.compile(r"^\s*(VERDICT|CRITICAL_COUNT|HIGH_COUNT):\s*(\S+)\s*$", re.M)
SECURITY_SENTINEL = re.compile(r"^\s*SECURITY_(HIGH|MEDIUM|LOW):\s*(\S+)\s*$", re.M)
CRITERION_STATE = re.compile(r"^- (verified|weak|manual|unverified)\s", re.M)

STOPWORDS = {"the", "and", "for", "with", "from", "into", "that", "this", "its", "not", "are", "was"}

ON_PUSH = {
    True: "reviews every push to a ready PR, so each push you make brings the next Copilot pass",
    False: (
        "does not review on push, so expect no Copilot pass after your first push; push once and do "
        "not wait for another"
    ),
}


class Stop(Exception):
    """A reason this run cannot produce a review. The PR goes back to draft."""


class Unread(Exception):
    """The PR itself could not be read, so its state is unknown."""


# ---------------------------------------------------------------- shell

def run(*args, cwd=None, stdin=None, check=True, timeout=None):
    proc = subprocess.run(
        list(args), capture_output=True, text=True, cwd=cwd, input=stdin, timeout=timeout
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-300:]
        raise Stop(f"`{' '.join(args[:4])}` failed: {tail}")
    return proc


def git(*args, check=True):
    return run("git", *args, check=check).stdout.strip()


def gh_json(*args):
    return json.loads(run("gh", *args).stdout)


def paged(path):
    # --paginate concatenates one JSON array per page; --slurp wraps them.
    return [item for page in gh_json("api", "--paginate", "--slurp", path) for item in page]


def toplevel():
    return Path(git("rev-parse", "--show-toplevel"))


# ---------------------------------------------------------------- workdir

def workdir():
    """The run directory's parent, ignored by git in every worktree of this repository."""
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if EXCLUDE not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as f:
            if lines and lines[-1] != "":
                f.write("\n")
            f.write(EXCLUDE + "\n")
    path = toplevel() / WORKDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- reports

def parse_code(text):
    """The code reviewer's report, or None when its sentinels are missing or malformed."""
    found = {}
    for key, value in CODE_SENTINEL.findall(text or ""):
        found[key] = value
    try:
        verdict = found["VERDICT"]
        critical, high = int(found["CRITICAL_COUNT"]), int(found["HIGH_COUNT"])
    except (KeyError, ValueError):
        return None
    if verdict not in ("approved", "changes_requested"):
        return None
    return {
        "verdict": verdict,
        "critical": critical,
        "high": high,
        "findings": findings(text),
        "drift": [
            {"spec": int(n), "behaviour": what, "where": where} for n, what, where in DRIFT.findall(text)
        ],
    }


def parse_security(text):
    """The security reviewer's report, or None when its sentinels are missing or malformed."""
    found = {}
    for key, value in SECURITY_SENTINEL.findall(text or ""):
        found[key] = value
    try:
        counts = {sev: int(found[sev.upper()]) for sev in ("High", "Medium", "Low")}
    except (KeyError, ValueError):
        return None
    return {**{k.lower(): v for k, v in counts.items()}, "findings": findings(text)}


def findings(text):
    return [
        {"severity": sev.capitalize(), "where": where.strip("`"), "what": what}
        for sev, where, what in FINDING.findall(text or "")
    ]


def code_blocks(code):
    # The prompt asks for "approved" only at zero counts; a report that says
    # approved beside a non-zero count is read by its counts.
    return (
        code["verdict"] == "changes_requested"
        or code["critical"] + code["high"] > 0
        or any(f["severity"] in ("Critical", "High") for f in code["findings"])
    )


def security_blocks(sec):
    return sec["high"] > 0 or any(f["severity"] == "High" for f in sec["findings"])


def blocking_lines(code, sec):
    lines = [
        f"- {f['severity']} — `{f['where']}` — {f['what']}"
        for f in code["findings"] if f["severity"] in ("Critical", "High")
    ]
    if code_blocks(code) and not lines:
        lines.append(
            f"- {code['verdict']}, {code['critical']} Critical, {code['high']} High, with no FINDING "
            "line naming where; see the full reviewer report"
        )
    sec_lines = [
        f"- Security High — `{f['where']}` — {f['what']}" for f in sec["findings"] if f["severity"] == "High"
    ]
    if security_blocks(sec) and not sec_lines:
        sec_lines.append(
            f"- Security High — {sec['high']} counted with no FINDING line naming where; see the full "
            "security report"
        )
    return lines + sec_lines


# ---------------------------------------------------------------- scope and Specs

def decide_scope(head, since, full, is_ancestor):
    """{scope, since} for this run. is_ancestor(since) says whether since is in head's history."""
    if full or not since or head.startswith(since) or not is_ancestor(since):
        return {"scope": "full", "since": None}
    return {"scope": f"since {since}", "since": since}


def words(text):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return re.findall(r"[a-z0-9]+", spaced.lower())


def glossary_terms(text):
    """The bold terms under GLOSSARY.md's `## Language` heading."""
    m = re.search(r"^## Language\s*$(.*?)(?=^## |\Z)", text or "", re.M | re.S)
    if not m:
        return []
    return [t.strip() for t in re.findall(r"^\*\*(.+?)\*\*:?", m.group(1), re.M)]


def candidates(specs, paths, terms, parent):
    """The implemented Specs whose title word, or glossary term their Criteria use, names a changed path."""
    path_words = [set(words(p)) for p in paths]
    out = []
    for s in specs:
        if s["spec"] == parent:
            continue
        keys = [{w} for w in words(s["title"]) if len(w) >= 3 and w not in STOPWORDS]
        criteria_words = set(words(" ".join(c["text"] for c in s["criteria"])))
        for term in terms:
            tw = set(words(term))
            if tw and tw <= criteria_words:
                keys.append(tw)
        if any(key <= pw for key in keys for pw in path_words):
            out.append({"number": s["spec"], "title": s["title"]})
    return out


def parent_of(slice_body):
    lines = spec_lib.section(slice_body, "Parent") or []
    m = re.search(r"#(\d+)", "\n".join(lines))
    return int(m.group(1)) if m else None


def specs_off(workflow_text):
    return bool(re.search(r"^\*\*Specs:\*\*\s*off\b", workflow_text or "", re.M))


# ---------------------------------------------------------------- prompts

def prompt_of(reference):
    m = re.search(r"^## Prompt\n+~~~text\n(.*?)\n~~~$", reference, re.M | re.S)
    if not m:
        raise Stop("a prompt reference has no fenced text under `## Prompt`")
    return m.group(1)


def fill(template, values):
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def code_prompt(pr, head, since, specs, tracker):
    debris = (ROOT / "references" / "ai-debris.md").read_text(encoding="utf-8")
    debris = debris[debris.index("\n## ") + 1:] if "\n## " in debris else debris
    scope = ""
    if since:
        scope = (
            f"\nThe PR was reviewed whole at {since} and nothing blocked. Review only the commits "
            f"after it: `git diff {since}...{head}`. Read `gh pr diff {pr}` and the code around the "
            "change for context, and report a finding only on a line those commits change, or on code "
            "elsewhere that they break. Do not report what the earlier review already saw.\n"
        )
    lines = "\n".join(f"#{s['number']} {s['title']}" for s in specs) or (
        "No implemented Specs match the changed paths."
    )
    template = prompt_of((ROOT / "references" / "code-review-prompt.md").read_text(encoding="utf-8"))
    return fill(template, {
        "pr_number": str(pr), "head": head, "scope": scope, "specs": lines,
        "tracker": tracker, "ai_debris": debris.strip(),
    })


def security_prompt(pr, base, head):
    template = prompt_of((ROOT / "references" / "security-review-prompt.md").read_text(encoding="utf-8"))
    return fill(template, {"pr_number": str(pr), "base": base, "head": head})


def autofix_prompt(url, pr, head_ref, on_push):
    template = prompt_of((ROOT / "references" / "autofix-instructions.md").read_text(encoding="utf-8"))
    return fill(template, {
        "pr_url": url, "pr_number": str(pr), "head_ref": head_ref, "on_push": ON_PUSH[on_push],
    })


# ---------------------------------------------------------------- the summary

def criteria_counts(report):
    states = CRITERION_STATE.findall(report or "")
    return {s: states.count(s) for s in ("verified", "weak", "manual", "unverified")}


def last_proven(found):
    """The head of the newest summary that carried a Criteria report, or None."""
    for c in reversed(found):
        if re.search(r"^### Criteria\s*$", c["body"], re.M):
            m = merge_gate.HEAD_LINE.search(c["body"])
            if m:
                return m.group(1)
    return None


def compose(s):
    """The summary comment CONTRACTS.md describes, from a finished run's facts."""
    code, sec = s["code"], s["security"]
    out = [f"## /review-pr — {s['outcome']}", ""]
    if s["outcome"] == "Ready" and s["copilot_ran"] is False:
        out += [f"Copilot did not run: {s['copilot_why']}.", ""]
    if s["criteria"] is not None:
        n = criteria_counts(s["criteria"])
        criteria_line = (
            f"proven on this head by /finalize-pr: {n['verified']} verified, {n['weak']} weak, "
            f"{n['manual']} manual, {n['unverified']} unverified"
        )
    elif s["last_proven"]:
        criteria_line = f"not re-proven on this head; last proven on {s['last_proven']}"
    else:
        criteria_line = "not re-proven on this head; no earlier summary carries a Criteria report"
    out += [
        f"Head: `{s['head'][:7]}`",
        f"Scope: {s['scope']}",
        f"Criteria: {criteria_line}",
        f"Reviewer: `{s['reviewer']}` — {code['verdict']}, {code['critical']} Critical, {code['high']} High",
        f"Security: `{s['security_entry']}` — {sec['high']} High, {sec['medium']} Medium, {sec['low']} Low",
        f"Copilot: {s['copilot_line']}",
        f"Auto-fix: {s['autofix_line']}",
        "",
    ]
    if s["criteria"] is not None:
        out += ["### Criteria", s["criteria"].strip(), ""]
    blocking = blocking_lines(code, sec)
    out += ["### Blocking findings", *(blocking or ["- None."]), ""]
    advisory = [
        f"- {f['severity']} — `{f['where']}` — {f['what']}" for f in sec["findings"] if f["severity"] != "High"
    ]
    out += ["### Security findings (not blocking)", *(advisory or ["- None."]), ""]
    titles = {c["number"]: c["title"] for c in s["candidates"]}
    drift = [
        f"- #{d['spec']} {titles.get(d['spec'], '')}".rstrip() + f", {d['behaviour']}, `{d['where'].strip('`')}`"
        for d in code["drift"]
    ]
    if not drift:
        drift = ["- None."] if s["candidates"] else ["- No implemented Specs match the changed paths."]
    out += ["### Spec drift (advisory)", *drift, ""]
    out += ["### Outcome", s["outcome_line"], ""]
    out += [
        "<details><summary>Full reviewer report</summary>", "", s["code_report"].strip(), "", "</details>",
        "<details><summary>Full security report</summary>", "", s["security_report"].strip(), "", "</details>",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- GitHub state

def is_draft(repo, pr):
    return gh_json("api", f"repos/{repo}/pulls/{pr}")["draft"]


def to_draft(repo, pr):
    """Return a ready PR to draft and read it back. Returns an error string, or None."""
    try:
        if is_draft(repo, pr):
            return None
        run("gh", "pr", "ready", str(pr), "--undo", "--repo", repo)
        if not is_draft(repo, pr):
            return "`gh pr ready --undo` returned success and the PR still reads as ready"
    except Stop as exc:
        return str(exc)
    return None


def comments(repo, pr):
    return [
        {"author": (c.get("user") or {}).get("login"), "body": c["body"],
         "created_at": c["created_at"], "url": c["html_url"]}
        for c in paged(f"repos/{repo}/issues/{pr}/comments?per_page=100")
    ]


def copilot_requests(repo, pr, after):
    return [
        e for e in paged(f"repos/{repo}/issues/{pr}/timeline?per_page=100")
        if e.get("event") == "review_requested"
        and ((e.get("requested_reviewer") or {}).get("login") or "").lower().startswith("copilot")
        and (e.get("created_at") or "") >= after
    ]


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_for(predicate, seconds, step=10):
    deadline = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def hand_to_copilot(repo, pr, base_ref, ready_at):
    """(ran, why, line, on_push) for the Copilot step."""
    try:
        rules = gh_json("api", f"repos/{repo}/rules/branches/{base_ref}")
        params = [r.get("parameters") or {} for r in rules if r.get("type") == "copilot_code_review"]
        auto, on_push, rules_error = bool(params), any(p.get("review_on_push") for p in params), None
    except (Stop, ValueError) as exc:
        auto, on_push, rules_error = False, False, str(exc)
    later = (
        "; later passes: on push" if on_push
        else "; later passes: none, the repository does not review on push"
    )
    if auto and ready_at and wait_for(lambda: copilot_requests(repo, pr, ready_at), 60):
        return True, None, "requested by the repository's ruleset" + later, on_push
    asked = now()
    proc = run("gh", "pr", "edit", str(pr), "--repo", repo, "--add-reviewer", "@copilot", check=False)
    if proc.returncode == 0 and wait_for(lambda: copilot_requests(repo, pr, asked), 20, step=5):
        return True, None, "requested by /review-pr" + later, on_push
    if proc.returncode != 0:
        why = ((proc.stderr or proc.stdout).strip().splitlines() or ["`gh pr edit` failed"])[-1]
    else:
        why = "the request returned success and no review_requested event followed"
    if rules_error:
        why += f" (reading the Copilot rules failed: {rules_error})"
    return False, why, f"did not run: {why}", on_push


def start_autofix(found, url, pr, head, head_ref, on_push):
    """The Auto-fix line."""
    session = merge_gate.autofix(found)["session"]
    if session:
        proc = run(
            "claude", "-p",
            f"/review-pr reviewed {head} and the PR is ready. Carry on watching it under the same rules.",
            "--cloud", session, "--output-format", "json", check=False, timeout=120,
        )
        try:
            ok = json.loads(proc.stdout or "{}").get("ok") is True
        except ValueError:
            ok = False
        if ok:
            return f"[{session}](https://claude.ai/code/{session}) — already watching"
    if not shutil.which("script"):
        return "not started: `script` is not on PATH, and `claude --cloud` needs a terminal"
    try:
        proc = run(
            "script", "-q", "/dev/null", "claude", "--cloud", autofix_prompt(url, pr, head_ref, on_push),
            stdin="", check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "not started: `claude --cloud` printed no session within two minutes"
    m = re.search(r"\b(session_[A-Za-z0-9]+)", proc.stdout + proc.stderr)
    if not m:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or ["no output"]
        return f"not started: {tail[0][:200]}"
    return f"[{m.group(1)}](https://claude.ai/code/{m.group(1)}) — started"


def post(repo, pr, body):
    proc = run("gh", "pr", "comment", str(pr), "--repo", repo, "--body-file", "-", stdin=body)
    m = re.search(r"#issuecomment-(\d+)", proc.stdout)
    if not m:
        raise Stop(f"`gh pr comment` printed no comment URL: {proc.stdout.strip()[-200:]}")
    back = gh_json("api", f"repos/{repo}/issues/comments/{m.group(1)}")["body"]
    if not back.lstrip().startswith("## /review-pr"):
        raise Stop("the posted summary does not read back as a /review-pr summary")
    return proc.stdout.strip()


# ---------------------------------------------------------------- commands

def cmd_prepare(pr, criteria, full):
    try:
        view = gh_json(
            "pr", "view", str(pr), "--json",
            "number,title,url,isDraft,headRefName,headRefOid,baseRefName,closingIssuesReferences,body",
        )
        repo = gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
        default = gh_json("repo", "view", "--json", "defaultBranchRef")["defaultBranchRef"]["name"]
    except (Stop, ValueError, KeyError) as exc:
        raise Unread(str(exc))
    manifest = {"pr": view["number"], "repo": repo, "is_draft": view["isDraft"]}
    try:
        manifest.update(prepare(view, repo, default, criteria, full))
    except Stop as exc:
        exc.manifest = manifest
        raise
    return manifest


def prepare(view, repo, default, criteria, full):
    pr, head = view["number"], view["headRefOid"]
    head_ref, base_ref = view["headRefName"], view["baseRefName"]
    git("fetch", "--quiet", "origin", *dict.fromkeys([head_ref, base_ref, default]))

    branch = git("branch", "--show-current")
    if branch != head_ref:
        raise Stop(f"the checkout is on `{branch or 'a detached HEAD'}`, not the PR's branch `{head_ref}`")
    if git("rev-parse", "HEAD") != head:
        clean = git("status", "--porcelain") == ""
        behind = run("git", "merge-base", "--is-ancestor", "HEAD", head, check=False).returncode == 0
        if clean and behind:
            git("merge", "--quiet", "--ff-only", f"origin/{head_ref}")
        if git("rev-parse", "HEAD") != head:
            raise Stop(
                f"the checkout is not the PR's head {head[:7]}: {git('status', '-sb').splitlines()[0]}. "
                "Commit and push, or pull, then run again"
            )

    found = merge_gate.summaries({"comments": comments(repo, pr)})
    since = merge_gate.last_ready(found)["head"]
    scope = decide_scope(
        head, since, full,
        lambda s: run("git", "merge-base", "--is-ancestor", s, head, check=False).returncode == 0,
    )
    rng = f"{scope['since']}...{head}" if scope["since"] else f"origin/{base_ref}...{head}"
    security_base = scope["since"] or f"origin/{base_ref}"

    top = toplevel()
    workflow = top / "docs" / "agents" / "workflow.md"
    workflow_text = workflow.read_text(encoding="utf-8") if workflow.exists() else ""
    try:
        entries = security_reviewers.chain(workflow_text)
    except ValueError as exc:
        raise Stop(f"the Security reviewers list in docs/agents/workflow.md cannot be walked: {exc}")
    chain_note = None
    if entries is None:
        entries = [security_reviewers.FLOOR]
        chain_note = "No Security reviewers list; /setup-workflow adds it."

    tracker, parent = repo, None
    refs = view.get("closingIssuesReferences") or []
    if refs:
        ref = refs[0]
        tracker = f"{ref['repository']['owner']['login']}/{ref['repository']['name']}"
        slice_number = ref["number"]
    else:
        m = re.search(r"\b(?:Closes|Fixes|Resolves)\s+#(\d+)", view.get("body") or "", re.I)
        slice_number = int(m.group(1)) if m else None
    if slice_number and not specs_off(workflow_text):
        body = gh_json("issue", "view", str(slice_number), "--repo", tracker, "--json", "body")["body"]
        parent = parent_of(body)

    try:
        implemented = spec_lib.listed(tracker, "implemented")
    except spec_lib.GhError as exc:
        raise Stop(f"the implemented Specs could not be listed: {exc}")
    paths = [p for p in git("diff", "--name-only", rng).splitlines() if p]
    glossary = top / "GLOSSARY.md"
    terms = glossary_terms(glossary.read_text(encoding="utf-8")) if glossary.exists() else []
    specs = candidates(implemented, paths, terms, parent)

    agents = []
    for f in sorted((top / ".claude" / "agents").glob("*.md")):
        fm = f.read_text(encoding="utf-8").split("---")
        if len(fm) >= 3:
            meta = dict(
                (k.strip(), v.strip()) for k, _, v in
                (line.partition(":") for line in fm[1].splitlines() if ":" in line)
            )
            agents.append({"name": meta.get("name"), "description": meta.get("description"), "file": f.name})

    run_dir = workdir() / f"{pr}-{head[:7]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    files = {k: run_dir / f"{k}.md" for k in ("code-prompt", "security-prompt", "code-report", "security-report")}
    files["code-prompt"].write_text(code_prompt(pr, head, scope["since"], specs, tracker), encoding="utf-8")
    files["security-prompt"].write_text(security_prompt(pr, security_base, head), encoding="utf-8")
    for k in ("code-report", "security-report"):
        files[k].unlink(missing_ok=True)
    criteria_file = None
    if criteria:
        source = Path(criteria)
        if not source.is_file():
            raise Stop(f"--criteria names {criteria}, which is not a file")
        criteria_file = run_dir / "criteria.md"
        if source.resolve() != criteria_file.resolve():
            shutil.copyfile(source, criteria_file)

    manifest = {
        "title": view["title"], "url": view["url"], "head": head, "head_ref": head_ref,
        "base_ref": base_ref, "default": default, "scope": scope["scope"], "since": scope["since"],
        "range": rng, "security_base": security_base, "tracker": tracker, "parent": parent,
        "candidates": specs, "chain": [list(e) for e in entries], "chain_note": chain_note,
        "agents": agents, "run": str(run_dir),
        "prompts": {"code": str(files["code-prompt"]), "security": str(files["security-prompt"])},
        "reports": {"code": str(files["code-report"]), "security": str(files["security-report"])},
        "criteria": str(criteria_file) if criteria_file else None,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps({**manifest, "pr": pr, "repo": repo, "is_draft": view["isDraft"]}, indent=2),
        encoding="utf-8",
    )
    return manifest


def cmd_check(kind, report):
    text = Path(report).read_text(encoding="utf-8") if Path(report).is_file() else ""
    parsed = parse_code(text) if kind == "code" else parse_security(text)
    if parsed is None:
        print(f"✘ {report} carries no {kind} sentinel lines, so it is not a result")
        return 1
    if kind == "code":
        print(f"✔ {parsed['verdict']}, {parsed['critical']} Critical, {parsed['high']} High")
    else:
        print(f"✔ {parsed['high']} High, {parsed['medium']} Medium, {parsed['low']} Low")
    return 0


def cmd_finish(run_dir, reviewer, security_entry, skipped):
    m = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    repo, pr, head = m["repo"], m["pr"], m["head"]
    texts = {k: Path(p).read_text(encoding="utf-8") if Path(p).is_file() else "" for k, p in m["reports"].items()}
    code, sec = parse_code(texts["code"]), parse_security(texts["security"])
    if code is None:
        raise Stop(f"the code reviewer's report at {m['reports']['code']} has no sentinel lines")
    if sec is None:
        raise Stop(f"the security report at {m['reports']['security']} has no sentinel lines")
    now_head = gh_json("pr", "view", str(pr), "--repo", repo, "--json", "headRefOid")["headRefOid"]
    if now_head != head:
        raise Stop(f"the PR's head moved to {now_head[:7]} during the review of {head[:7]}; run it again")

    blocking = code_blocks(code) or security_blocks(sec)
    was_draft = is_draft(repo, pr)
    ready_at = None
    if blocking:
        n = len(blocking_lines(code, sec))
        error = to_draft(repo, pr)
        if error:
            raise Stop(f"the review blocks and the PR could not be returned to draft: {error}")
        outcome = "Draft"
        outcome_line = (
            f"Left as a draft: {n} blocking finding(s)." if was_draft
            else f"Returned to draft: {n} blocking finding(s)."
        )
    else:
        if was_draft:
            ready_at = now()
            run("gh", "pr", "ready", str(pr), "--repo", repo, check=False)
        if is_draft(repo, pr):
            outcome, outcome_line = "Draft", "Left as a draft: `gh pr ready` did not take."
        else:
            outcome, outcome_line = "Ready", "Marked ready."

    found = merge_gate.summaries({"comments": comments(repo, pr)})
    copilot_ran, copilot_why = None, None
    if outcome == "Ready":
        copilot_ran, copilot_why, copilot_line, on_push = hand_to_copilot(repo, pr, m["base_ref"], ready_at)
        autofix_line = start_autofix(found, m["url"], pr, head, m["head_ref"], on_push)
    else:
        copilot_line, autofix_line = "not requested: Draft", "not started: Draft"

    criteria = None
    if m.get("criteria") and Path(m["criteria"]).is_file():
        criteria = Path(m["criteria"]).read_text(encoding="utf-8")
    body = compose({
        "outcome": outcome, "outcome_line": outcome_line, "head": head, "scope": m["scope"],
        "criteria": criteria, "last_proven": last_proven(found), "reviewer": reviewer,
        "security_entry": security_entry, "code": code, "security": sec,
        "copilot_ran": copilot_ran, "copilot_why": copilot_why, "copilot_line": copilot_line,
        "autofix_line": autofix_line, "candidates": m["candidates"],
        "code_report": texts["code"], "security_report": texts["security"],
    })
    (Path(run_dir) / "summary.md").write_text(body, encoding="utf-8")
    try:
        url = post(repo, pr, body)
    except Stop:
        to_draft(repo, pr)
        raise

    drift = len(code["drift"])
    n = criteria_counts(criteria) if criteria is not None else None
    lines = [
        f"/review-pr — {outcome}",
        "",
        f"PR:       #{pr} — {m['title']}",
        f"URL:      {m['url']}",
        f"Summary:  {url}",
        f"Head:     {head}",
        f"Scope:    {'full PR' if m['scope'] == 'full' else 'the commits since ' + m['since']}",
        "Criteria: " + (
            f"proven on this head by /finalize-pr: {n['verified']} verified, {n['weak']} weak, "
            f"{n['manual']} manual, {n['unverified']} unverified" if n
            else f"not re-proven; last proven on {last_proven(found) or 'no earlier summary'}"
        ),
        f"Reviewer: {reviewer} — {code['verdict']}, {code['critical']} Critical, {code['high']} High",
        f"Security: {security_entry} — {sec['high']} High, {sec['medium']} Medium, {sec['low']} Low",
        *[f"          skipped {s}" for s in skipped],
        *([f"          {m['chain_note']}"] if m.get("chain_note") else []),
        f"Drift:    {drift} advisory finding(s)" if drift else "Drift:    none",
        f"Outcome:  {outcome_line}",
        f"Copilot:  {copilot_line}",
        f"Auto-fix: {autofix_line}",
        "",
        "Next: " + (
            "Auto-fix is answering Copilot; when it posts \"## Auto-fix — Clean\", run /review-round "
            f"{pr} if it pushed, then /merge-pr" if outcome == "Ready"
            else f"/review-round {pr} fixes the blocking findings in a new session (a round already running starts it)"
        ),
    ]
    print("\n".join(lines))
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="review-pr.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("workdir")
    pp = sub.add_parser("prepare")
    pp.add_argument("--pr", type=int, required=True)
    pp.add_argument("--criteria")
    pp.add_argument("--full", action="store_true")
    pc = sub.add_parser("check")
    pc.add_argument("kind", choices=["code", "security"])
    pc.add_argument("report")
    pf = sub.add_parser("finish")
    pf.add_argument("--run", required=True)
    pf.add_argument("--reviewer", required=True)
    pf.add_argument("--security", required=True)
    pf.add_argument("--skipped", action="append", default=[])
    pa = sub.add_parser("abort")
    pa.add_argument("--run", required=True)
    pa.add_argument("--why", required=True)
    a = p.parse_args(argv)

    if a.cmd == "check":
        return cmd_check(a.kind, a.report)
    try:
        if a.cmd == "workdir":
            print(workdir())
            return 0
        if a.cmd == "prepare":
            print(json.dumps(cmd_prepare(a.pr, a.criteria, a.full), indent=2))
            return 0
        if a.cmd == "abort":
            raise Stop(a.why)
        return cmd_finish(a.run, a.reviewer, a.security, a.skipped)
    except Unread as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 2
    except Stop as exc:
        manifest = getattr(exc, "manifest", None)
        if manifest is None and a.cmd in ("finish", "abort"):
            try:
                manifest = json.loads((Path(a.run) / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = None
        error = to_draft(manifest["repo"], manifest["pr"]) if manifest else "the PR is unknown"
        print(f"✘ {exc}", file=sys.stderr)
        print(
            "  nothing was posted; " + (f"the PR could not be returned to draft: {error}" if error
                                        else "the PR is a draft"),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
