---
name: review-pr
description: Run the local code review and the security review on a pull request with fresh-context reviewers and the spec-drift question, reviewing only the commits since the last Ready review when one passed, mark the PR ready when nothing blocks, hand it to Copilot and a cloud Auto-fix session, and post one summary comment that carries /finalize-pr's Criteria when it handed off. The only skill that marks a PR ready. Use when the user says "/review-pr", or when /review-round runs a round. Not for addressing or replying to review comments on a PR.
---

# Review PR

Review a pull request and decide whether it is ready for a human. `/finalize-pr` proves the
Criteria; this skill reviews the code and its security, says what it found in one comment, and is
the only skill in the workflow that marks a PR ready. Ready therefore always means two reviewers that
did not write the code, one for the code and one for its security, have looked at the head commit
and nothing either found blocks.

Everything mechanical lives in [review-pr.py](../../scripts/review-pr.py): the pre-flight, the scope,
the candidate Specs, both reviewer prompts, the verdict, the ready state, the Copilot request, the
Auto-fix session and the summary. This session does the three things that need a model: dispatch the
code reviewer, walk the security chain, and, in a project with no reviewer agent, run the fallback
review. Keep it that way. Do not read the prompt files, the references they come from, or the
script's source: what this session holds is what every review round pays for.

`<scripts>` below is `<this skill dir>/../../scripts`.

**What it reviews.** The first review of a PR reviews all of it. Once a summary has said `Ready`,
everything up to the head it named was reviewed whole and passed, so a later run reviews only the
commits after that head. A rebase or force-push that rewrote that head, or `--full`, brings back a
full review. The summary's `Scope:` line says which ran.

**Where it runs.** In a session whose working directory is the PR's checkout, usually a
`/review-round` session that `/finalize-pr` started. Never inside a subagent: it dispatches
subagents of its own.

## Input

`$ARGUMENTS`: a PR number (`42` or `#42`). If empty, use the current branch's PR:
`gh pr list --head {branch} --json number --limit 1`. No PR → **stop**: there is nothing to review.

- `--criteria {path}`: the Criteria report `/finalize-pr` wrote for this head. Only a hand-off
  passes it.
- `--full`: review the whole PR even when an earlier `Ready` would narrow it.

## 1. Prepare

```bash
python3 <scripts>/review-pr.py prepare --pr {pr_number} [--criteria {path}] [--full]
```

It prints the run's manifest as JSON. Exit 1 → **stop** and relay its `✘` line: the checkout is not
the PR's head, the Security reviewers list cannot be walked, or the Specs could not be listed. The
script has already returned a ready PR to draft and said so. Exit 2 → **stop**: `gh` could not read
the PR.

The script reads the Security reviewers list through `security-reviewers.py` and the implemented
Specs through `spec.py`, and from nowhere else. The manifest's `chain` is that list in order, with
the Claude floor last. `chain_note` is set when
there was no list.

From the manifest you need `run`, `prompts`, `reports`, `chain` and `agents`. Leave the rest to
`finish`.

## 2. Dispatch both reviewers, together

Both review the same head. Dispatch the code reviewer and the first security entry in one message,
as two Agent calls, so they run at the same time and neither sees the other's findings. With no
project reviewer agent the code review is a skill, which runs inline: launch the security entry
first, then run the skill while it works.

Every reviewer gets a pointer to its prompt, never the prompt's text:

> Read the file `{prompt path}` and follow it exactly. It is your whole task.

When a reviewer replies, write its reply, unchanged, to the matching `reports` path, then check it:

```bash
python3 <scripts>/review-pr.py check {code|security} {report path}
```

Exit 0 is a result. Exit 1 means the reply carried no sentinel lines, which is no result.

### The code reviewer

**With a project agent.** `agents` lists `.claude/agents/`, each with its frontmatter `name` and
`description`. Pick the one whose description says it reviews code: projects name their agents for
their domain, so match the role, not the filename. Launch it with `subagent_type` set to that `name`
and the pointer to `prompts.code`. Its reply is the code report. A reply with no sentinels →
**stop**:

```bash
python3 <scripts>/review-pr.py abort --run {run} --why "the code reviewer returned no sentinel lines"
```

**With none.** Invoke `mattpocock-skills:code-review` through the Skill tool. Its fixed point is the
manifest's `since` when that is set, and otherwise `git merge-base origin/{default} HEAD`. Name the
manifest's `candidates` as its spec source, and read `prompts.code` yourself for the drift and
AI-debris instructions it gives the agent; this is the one case where this session reads a prompt.
Ask it to grade every **Standards** finding Critical, High, Medium or Low, because an ungraded
finding cannot block. Then write `reports.code` in the form `prompts.code` asks for: its review,
one `FINDING:` line per **Standards** finding, one `spec-drift:` line per **Spec**-axis finding, and
the three sentinels, with `changes_requested` when any **Standards** finding is Critical or High.
The reviewer's name is `code-review`.

### The security reviewer

Walk the `chain` by the rules in [chain-walking.md](../../references/chain-walking.md): which entries
are available, when an entry counts as having produced a result, where the walk ends, and which entry
to name. Read it before dispatching anything. The walk is sequential within the chain and runs
alongside the code reviewer.

Every entry gets the same prompt, `prompts.security`, rendered from
[security-review-prompt.md](../../references/security-review-prompt.md), so whichever vendor runs it
asks the same questions of the same diff. Each vendor gets it through the subagent and model prefix
`/critique-spec` §6 gives under **Dispatching an entry**:

- **`codex`**: `codex:codex-rescue`, prefixed `Run with --model {model}` unless the model is
  `default`, and told the review is read-only, so it does not pass `--write`.
- **`google`**: `antigravity:agy-rescue`, prefixed `MODEL: {model}`, with no `--add-dir`.
- **`claude`**: the Agent tool with `subagent_type: "general-purpose"` and the entry's model, as
  `/critique-spec` §6 describes. With `default`, pass none.

**A result** is a report `check security` passes, zeros included. An entry that returns no result
is skipped: note it as `{vendor} ({model}): {why}` for `--skipped`, and dispatch the next. When no
entry produces a result → **stop**:

```bash
python3 <scripts>/review-pr.py abort --run {run} --why "no Security reviewer produced a result"
```

A PR with no security review has not been reviewed, whatever the code reviewer said.

## 3. Finish

```bash
python3 <scripts>/review-pr.py finish --run {run} --reviewer {name} \
  --security "{vendor} ({model})" [--skipped "{vendor} ({model}): {why}"]...
```

`--security` names the entry that produced the result, never one that was skipped.

It decides the verdict, sets the PR ready or draft and reads it back, and, on a ready PR, requests
Copilot and starts or reuses the Auto-fix session. Then it posts the summary and reads it back. It
prints the local summary: relay it as it is. Exit 1 → relay its `✘` line and the line under it,
which says what reached the PR. Usually nothing was posted and the PR is a draft. When a `Ready`
summary may have been posted but could not be read back, the PR stays ready: a draft under a `Ready`
summary would read as reviewed and strand there, while a ready PR with no summary reads as unreviewed
to the merge gate.

## What `finish` decides

So a reader can check the script against the workflow's rules without reading it:

- **Blocking** when the code report says `changes_requested`, counts any Critical or High, or lists a
  Critical or High `FINDING:`, or when the security report counts or lists any High. A report that
  says `approved` beside a non-zero count is read by its count.
- **Spec drift never blocks.** It is a question for a person, resolved as `CONTRACTS.md` describes
  under **Keeping an implemented Spec true**. Medium and Low findings never block: the security
  reviewer's are listed under `### Security findings (not blocking)`, and the code reviewer's are in
  its full report.
- **Not blocking** → the PR is marked ready and read back. If it still reads as a draft, the
  outcome is `Draft`, because `/merge-pr` stops on drafts. **Blocking** → it stays a draft, or is
  returned to one.
- **Copilot and Auto-fix only follow a PR read back as ready.** Copilot is requested by the
  repository's ruleset when one applies, and by `gh pr edit --add-reviewer @copilot` otherwise,
  never by a comment. An Auto-fix session still running on the PR is reused; one session per PR. A new one starts only
  when the earlier one answers that it is gone: a reuse that times out is reported as unconfirmed.
  Each line says what was read back, never what was attempted.
- **The summary** is the shape `CONTRACTS.md` gives under **The `/review-pr` summary comment**: a
  new comment on every run, never an edit. With `--criteria` it carries `/finalize-pr`'s report
  unchanged; without, the `Criteria:` line names where they were last proven and no earlier report
  is copied.

## Rules

- This is the only skill that marks a PR ready, and only after a review of its current head that
  does not block. Never `--approve`.
- The reviewers are always fresh-context agents or skills, never this session's own reading of the
  diff.
- Reviewers get a pointer to their prompt file. Never paste a prompt into a dispatch.
- A stop is never a pass. `prepare` and `abort` post nothing and return a ready PR to draft.
- The security review always runs, alongside the code review, on the same head, with the one prompt
  whichever vendor runs it. The claude floor is required: a list that does not end in `claude` stops
  the run in `prepare`.
- Every run reviews the current head and posts a new summary. A run after a `Ready` summary reviews
  the commits since its head, never less.
- Never wait for Copilot or for the Auto-fix session.
- Do not modify source files. Fixing what the review found is the next round's job.
