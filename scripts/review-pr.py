#!/usr/bin/env python3
"""The mechanical half of /review-pr, so the session that runs it holds only the judgement.

Usage (run from the PR's checkout):

  review-pr.py workdir
  review-pr.py prepare --pr {n} [--criteria {path}] [--full]
  review-pr.py check {code|security} {report}
  review-pr.py finish --run {dir} --reviewer {name} --security "{vendor} ({model})" [--skipped "{entry}: {why}"]...
  review-pr.py abort --run {dir} --why "{reason}"

/review-pr runs once per review round, in a session that holds what it reads.
Everything here is deterministic, so it is tested once rather than re-read by a
model on every round, and the session keeps only what needs judgement:
dispatching the two reviewers, walking the security chain, and, with no
project reviewer, running the fallback review.

workdir prints the directory a run's files live in,
`{toplevel}/.claude/review-pr`, after making sure git ignores it locally
(`info/exclude`, never a tracked file). It sits inside the checkout because a
codex or agy reviewer is pointed at a prompt file there and reads within its
workspace. /finalize-pr writes its Criteria report there too.

prepare runs the pre-flight, works out the scope, the candidate Specs and the
security chain, renders both reviewer prompts into a directory of the run's own,
`{workdir}/{pr}-{head7}-{UTC timestamp}/`,
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
    """A reason this run cannot produce a review. The PR goes back to draft.

    `posted` says what reached the PR, because a stop after the comment exists
    must not report that nothing was posted.
    """

    def __init__(self, message, posted="nothing was posted", maybe_posted=False):
        super().__init__(message)
        self.posted = posted
        self.maybe_posted = maybe_posted
        # Set by `finish` when the comment that may be up is a Ready summary.
        self.keep_ready = False


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


def new_run_dir(pr, head):
    """A new, empty directory for one prepare run.

    Each run gets its own, so two runs on one PR and head never share a
    manifest, a prompt or a report. `mkdir` fails on a name already taken,
    which is what makes the name unique within a second.
    """
    base = f"{pr}-{head[:7]}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    parent = workdir()
    n = 1
    while True:
        path = parent / (base if n == 1 else f"{base}-{n}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            n += 1


KEEP_RUNS = 3
RUN_DIR = re.compile(r"(?P<pr>\d+)-[0-9a-f]{7}-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<n>\d+))?")


def prune_runs(run_dir):
    """Delete this PR's run directories older than the newest KEEP_RUNS.

    Called once a run has posted its summary, so a run that stopped keeps the
    evidence of the runs before it. A directory `new_run_dir` did not name is
    never touched.
    """
    run_dir = Path(run_dir)
    me = RUN_DIR.fullmatch(run_dir.name)
    if not me:
        return
    runs = []
    for p in run_dir.parent.iterdir():
        m = RUN_DIR.fullmatch(p.name)
        if p.is_dir() and m and m["pr"] == me["pr"]:
            runs.append(((m["stamp"], int(m["n"] or 1)), p))
    runs.sort()
    keep = {p for _, p in runs[-KEEP_RUNS:]} | {run_dir}
    for _, p in runs:
        if p not in keep:
            shutil.rmtree(p, ignore_errors=True)


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


def criteria_line(report, proven):
    if report is not None:
        n = criteria_counts(report)
        return (
            f"proven on this head by /finalize-pr: {n['verified']} verified, {n['weak']} weak, "
            f"{n['manual']} manual, {n['unverified']} unverified"
        )
    if proven:
        return f"not re-proven on this head; last proven on {proven}"
    return "not re-proven on this head; no earlier summary carries a Criteria report"


def compose(s):
    """The summary comment CONTRACTS.md describes, from a finished run's facts."""
    code, sec = s["code"], s["security"]
    out = [f"## /review-pr — {s['outcome']}", ""]
    if s["outcome"] == "Ready" and s["copilot_ran"] is False:
        out += [f"Copilot did not run: {s['copilot_why']}.", ""]
    out += [
        f"Head: `{s['head'][:7]}`",
        f"Scope: {s['scope']}",
        f"Criteria: {criteria_line(s['criteria'], s['last_proven'])}",
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


def copilot_requests(repo, pr):
    """{event key: created_at} for every Copilot review request on the PR's timeline.

    Requests are told apart by the timeline's own ids, never by this machine's
    clock: a local clock running ahead of GitHub's would hide a real request.
    """
    return {
        str(e.get("id") or e.get("created_at")): e.get("created_at") or ""
        for e in paged(f"repos/{repo}/issues/{pr}/timeline?per_page=100")
        if e.get("event") == "review_requested"
        and ((e.get("requested_reviewer") or {}).get("login") or "").lower().startswith("copilot")
    }


UNREAD = object()


def new_since(repo, pr, before):
    return bool(set(copilot_requests(repo, pr)) - set(before))


def wait_for(predicate, seconds, step=10):
    deadline = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def hand_to_copilot(repo, pr, base_ref, before_ready, last_summary_at):
    """(ran, why, line, on_push) for the Copilot step.

    before_ready is the request snapshot taken just before this run marked the
    PR ready, or None when it was ready already. Then a ruleset that reviews on
    push made its request after the push, which is after the last summary, and
    both timestamps are GitHub's. UNREAD means the snapshot failed: with no
    baseline, every earlier request would look new, so nothing is credited to
    the ruleset and the explicit request, which reads its own baseline, runs.
    """
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
    if auto and isinstance(before_ready, dict) and wait_for(lambda: new_since(repo, pr, before_ready), 60):
        return True, None, "requested by the repository's ruleset" + later, on_push
    if auto and before_ready is None and last_summary_at and any(
        at >= last_summary_at for at in copilot_requests(repo, pr).values()
    ):
        return True, None, "requested by the repository's ruleset" + later, on_push
    asked = copilot_requests(repo, pr)
    proc = run("gh", "pr", "edit", str(pr), "--repo", repo, "--add-reviewer", "@copilot", check=False)
    if proc.returncode == 0 and wait_for(lambda: new_since(repo, pr, asked), 20, step=5):
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
        link = f"[{session}](https://claude.ai/code/{session})"
        try:
            proc = run(
                "claude", "-p",
                f"/review-pr reviewed {head} and the PR is ready. Carry on watching it under the same rules.",
                "--cloud", session, "--output-format", "json", check=False, timeout=120,
            )
            ok = json.loads(proc.stdout or "{}").get("ok")
        except subprocess.TimeoutExpired:
            ok = "it did not answer within two minutes"
        except OSError as exc:
            ok = str(exc)
        except (ValueError, AttributeError):
            ok = "its reply was not the expected JSON"
        if ok is True:
            return f"{link} — already watching"
        # Only an explicit `"ok": false` says the session is gone. Anything
        # else may be a live session, and a second one would push to the same branch.
        if ok is not False:
            why = ok if isinstance(ok, str) else "its reply carried no `ok`"
            return f"{link} — reuse unconfirmed: {why}; no new session started"
    if not shutil.which("script"):
        return "not started: `script` is not on PATH, and `claude --cloud` needs a terminal"
    try:
        proc = run(
            "script", "-q", "/dev/null", "claude", "--cloud", autofix_prompt(url, pr, head_ref, on_push),
            stdin="", check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "not started: `claude --cloud` printed no session within two minutes"
    except OSError as exc:
        return f"not started: {exc}"
    m = re.search(r"\b(session_[A-Za-z0-9]+)", proc.stdout + proc.stderr)
    if not m:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or ["no output"]
        return f"not started: {tail[0][:200]}"
    return f"[{m.group(1)}](https://claude.ai/code/{m.group(1)}) — started"


def post(repo, pr, body):
    """Post the summary and read it back. Returns its URL."""
    proc = run("gh", "pr", "comment", str(pr), "--repo", repo, "--body-file", "-", stdin=body)
    url = proc.stdout.strip()
    unsure = f"a comment may have been posted ({url or 'no URL printed'}); check the PR's comments"
    m = re.search(r"#issuecomment-(\d+)", url)
    if not m:
        raise Stop("`gh pr comment` printed no comment URL", posted=unsure, maybe_posted=True)
    try:
        back = gh_json("api", f"repos/{repo}/issues/comments/{m.group(1)}")["body"]
    except Exception as exc:
        raise Stop(f"the posted summary could not be read back: {exc}", posted=unsure, maybe_posted=True)
    if not back.lstrip().startswith("## /review-pr"):
        raise Stop(
            "the posted comment does not read back as a /review-pr summary",
            posted=f"a comment that is not a summary was posted at {url}",
        )
    return url


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
    except Exception as exc:
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
    # The reviewers read the code around the diff from this checkout, so an
    # uncommitted change would be reviewed as if it were the head.
    if git("status", "--porcelain") != "":
        raise Stop(
            "the checkout has uncommitted changes, which the reviewers would read as part of "
            f"{head[:7]}. Commit and push, or stash, then run again"
        )
    if git("rev-parse", "HEAD") != head:
        behind = run("git", "merge-base", "--is-ancestor", "HEAD", head, check=False).returncode == 0
        if behind:
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
        fm = re.match(r"\A---\n(.*?)\n---\s*$", f.read_text(encoding="utf-8"), re.S | re.M)
        if fm:
            meta = dict(
                (k.strip(), v.strip()) for k, _, v in
                (line.partition(":") for line in fm.group(1).splitlines() if ":" in line)
            )
            agents.append({"name": meta.get("name"), "description": meta.get("description"), "file": f.name})

    run_dir = new_run_dir(pr, head)
    files = {k: run_dir / f"{k}.md" for k in ("code-prompt", "security-prompt", "code-report", "security-report")}
    files["code-prompt"].write_text(code_prompt(pr, head, scope["since"], specs, tracker), encoding="utf-8")
    files["security-prompt"].write_text(security_prompt(pr, security_base, head), encoding="utf-8")
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
    manifest = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    repo, pr, head = manifest["repo"], manifest["pr"], manifest["head"]
    texts = {
        k: Path(p).read_text(encoding="utf-8") if Path(p).is_file() else ""
        for k, p in manifest["reports"].items()
    }
    code, sec = parse_code(texts["code"]), parse_security(texts["security"])
    if code is None:
        raise Stop(f"the code reviewer's report at {manifest['reports']['code']} has no sentinel lines")
    if sec is None:
        raise Stop(f"the security report at {manifest['reports']['security']} has no sentinel lines")
    now_head = gh_json("pr", "view", str(pr), "--repo", repo, "--json", "headRefOid")["headRefOid"]
    if now_head != head:
        raise Stop(f"the PR's head moved to {now_head[:7]} during the review of {head[:7]}; run it again")

    found = merge_gate.summaries({"comments": comments(repo, pr)})
    blocking = code_blocks(code) or security_blocks(sec)
    was_draft = is_draft(repo, pr)
    before_ready = None
    if blocking:
        count = len(blocking_lines(code, sec))
        error = to_draft(repo, pr)
        if error:
            raise Stop(f"the review blocks and the PR could not be returned to draft: {error}")
        outcome = "Draft"
        outcome_line = (
            f"Left as a draft: {count} blocking finding(s)." if was_draft
            else f"Returned to draft: {count} blocking finding(s)."
        )
    else:
        if was_draft:
            before_ready = best_effort(lambda: copilot_requests(repo, pr), lambda why: UNREAD)
            run("gh", "pr", "ready", str(pr), "--repo", repo, check=False)
        if is_draft(repo, pr):
            outcome, outcome_line = "Draft", "Left as a draft: `gh pr ready` did not take."
        else:
            outcome, outcome_line = "Ready", "Marked ready."
            # A push between the head check and `gh pr ready` would leave an
            # unreviewed head ready; main returns the PR to draft on this stop.
            moved = gh_json("pr", "view", str(pr), "--repo", repo, "--json", "headRefOid")["headRefOid"]
            if moved != head:
                raise Stop(f"the PR's head moved to {moved[:7]} while {head[:7]} was being marked ready; run it again")

    copilot_ran, copilot_why = None, None
    if outcome == "Ready":
        last_at = found[-1]["created_at"] if found else None
        copilot_ran, copilot_why, copilot_line, on_push = best_effort(
            lambda: hand_to_copilot(repo, pr, manifest["base_ref"], before_ready, last_at),
            lambda why: (False, why, f"did not run: {why}", False),
        )
        autofix_line = best_effort(
            lambda: start_autofix(found, manifest["url"], pr, head, manifest["head_ref"], on_push),
            lambda why: f"not started: {why}",
        )
    else:
        copilot_line, autofix_line = "not requested: Draft", "not started: Draft"

    criteria = None
    if manifest.get("criteria") and Path(manifest["criteria"]).is_file():
        criteria = Path(manifest["criteria"]).read_text(encoding="utf-8")
    proven = last_proven(found)
    body = compose({
        "outcome": outcome, "outcome_line": outcome_line, "head": head, "scope": manifest["scope"],
        "criteria": criteria, "last_proven": proven, "reviewer": reviewer,
        "security_entry": security_entry, "code": code, "security": sec,
        "copilot_ran": copilot_ran, "copilot_why": copilot_why, "copilot_line": copilot_line,
        "autofix_line": autofix_line, "candidates": manifest["candidates"],
        "code_report": texts["code"], "security_report": texts["security"],
    })
    (Path(run_dir) / "summary.md").write_text(body, encoding="utf-8")
    try:
        url = post(repo, pr, body)
    except Stop as exc:
        # A Ready summary on a draft PR reads as fresh to merge-gate.py, so
        # /review-round would end there and leave the PR stranded. A ready PR
        # with no summary reads as stale instead, which the next round reviews
        # and /merge-pr refuses.
        exc.keep_ready = exc.maybe_posted and outcome == "Ready"
        raise
    best_effort(lambda: prune_runs(run_dir), lambda why: None)

    drift = len(code["drift"])
    lines = [
        f"/review-pr — {outcome}",
        "",
        f"PR:       #{pr} — {manifest['title']}",
        f"URL:      {manifest['url']}",
        f"Summary:  {url}",
        f"Head:     {head}",
        f"Scope:    {'full PR' if manifest['scope'] == 'full' else 'the commits since ' + manifest['since']}",
        f"Criteria: {criteria_line(criteria, proven)}",
        f"Reviewer: {reviewer} — {code['verdict']}, {code['critical']} Critical, {code['high']} High",
        f"Security: {security_entry} — {sec['high']} High, {sec['medium']} Medium, {sec['low']} Low",
        *[f"          skipped {s}" for s in skipped],
        *([f"          {manifest['chain_note']}"] if manifest.get("chain_note") else []),
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


def best_effort(step, fallback):
    """Run a step whose failure is reported on its own line rather than stopping the run.

    Copilot and Auto-fix follow a review that passed. When either cannot run,
    the PR stays ready and the summary says why, so their errors never become
    a stop.
    """
    try:
        return step()
    except (Stop, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return fallback(str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__)


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
    except Exception as caught:
        # An error after `finish` marked the PR ready would otherwise leave it
        # ready with no summary, so every error is a stop.
        exc = caught if isinstance(caught, Stop) else Stop(f"{type(caught).__name__}: {caught}")
        manifest = getattr(caught, "manifest", None)
        if manifest is None and a.cmd in ("finish", "abort"):
            try:
                manifest = json.loads((Path(a.run) / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = None
        print(f"✘ {exc}", file=sys.stderr)
        if exc.keep_ready:
            print(f"  {exc.posted}; the PR stays ready, because a Ready summary may be on it", file=sys.stderr)
            return 1
        error = to_draft(manifest["repo"], manifest["pr"]) if manifest else "the PR is unknown"
        print(
            f"  {exc.posted}; " + (f"the PR could not be returned to draft: {error}" if error
                                   else "the PR is a draft"),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
