---
name: review-round
description: Advance a pull request by one review round in a fresh session. Fix the blocking findings the latest /review-pr summary lists on the head, push, and run /review-pr. When the review blocks again, start the next round as a new background session in the same worktree, for at most three fix rounds. /finalize-pr starts the first round when it hands off. Use when the user says "/review-round", "run the next round" or "fix the review findings", or after an Auto-fix push needs reviewing.
---

# Review round

One round of the loop between a PR's reviewers and its fixes, in a session of its own.

A review that blocks leads to a fix, a push and another review, and Copilot's passes add more. Run
in the session that built the Slice, every round re-reads everything that session already holds: by
the first review it is several hundred thousand tokens deep, and each round adds to it. So each
round is a new session. It starts from nothing but the PR, does one fix and one review, starts the
next round if there is one, and ends.

The PR is the state between rounds. The latest `/review-pr` summary lists what blocks, with
`path:line`, and [merge-gate.py](../../scripts/merge-gate.py) counts the rounds from the summaries.
Nothing is carried in memory or in arguments, so a round can be started by hand, by `/finalize-pr`,
or by the round before it, and does the same thing.

## Input

`$ARGUMENTS`: a PR number; `--launched-by {sessionId}` when `/finalize-pr` or an earlier round
started this one; and `--criteria {path}` when `/finalize-pr` starts the first round. Pass
`--criteria` through to `/review-pr` only while the head is still the commit the report proves (§3);
no later round has one.

Run from the PR's worktree. `{top}` = `git rev-parse --show-toplevel`.

## 1. Where the PR stands

**One round per worktree.** Before anything else, read `claude agents --json` and look for another
session at work in this checkout: an entry whose `cwd` is `{top}`, whose `status` is `busy`, and
whose `sessionId` is not `$CLAUDE_CODE_SESSION_ID`.

- This session's own entry is the one whose `sessionId` equals `$CLAUDE_CODE_SESSION_ID`. Leave it
  out, or the round finds itself and stops.
- Leave out the entry whose `sessionId` is the `--launched-by` value. That session started this
  one and is still `busy` while it writes its report, but its last edit and push came before the
  launch, so it is no rival.
- Read `status`, never `state`. A background session that has finished its turn still lists
  `state: working` beside `status: idle`, and a session a person runs in a terminal has `status` and
  no `state` at all. `busy` is the only value that says a session is doing something now.

Two sessions that edit and commit in one checkout push over each other. Any other `busy` session →
**stop**: fix nothing, review nothing, start no round, and report that session's `id` (its
`sessionId` when it has no `id`). No lock file is involved: the session list is the only record.

```bash
python3 <this skill dir>/../../scripts/merge-gate.py --pr {pr_number}
```

Exit 2 → **stop** and relay its `✘` line. Otherwise read `.reviewed.state` and `.rounds`:

- `fresh` → the latest summary is `Ready` on the head. There is nothing to fix: report it and end.
- `stale` or `none` → no summary has reviewed the head yet: the first round after `/finalize-pr`, or
  a push after the last review (Auto-fix's, or a person's). Go to §3.
- `blocked` → the latest summary is `Draft` on the head. Go to §2, unless `.rounds` is above **3**:
  three fixes have not cleared the review, so it needs a person. **Stop**: leave the PR a draft,
  start no round, and report the summary's URL (`.reviewed.summary_url`).

Bring the checkout to the PR's head before touching anything. `git fetch origin`; on the PR's branch
with a clean `git status --porcelain` and `HEAD` an ancestor of `origin/{headRefName}`,
`git merge --ff-only origin/{headRefName}`. Anything else (another branch, uncommitted changes,
local commits the remote lacks) → **stop** and say which: a round that fixes on top of a stale
checkout pushes over someone else's work.

## 2. Fix what blocks (`blocked` only)

**Hold Auto-fix first.** When `.autofix.session` names a session, an earlier `Ready` started it and
it may still push. Two fixers on one branch push over each other:

```bash
claude -p "PR #{pr_number} is back to draft. Push nothing more until /review-pr hands it back." --cloud {session} --output-format json
```

Read the reply before touching anything:

- `"ok": true` → the session has the message. Go on. The message alone does not stop it pushing
  (on #74 one answered `ok: true` and pushed anyway); its prompt does, by forbidding any push while
  the PR is a draft, which it is now. A session started before that prompt's version 2 lacks the
  rule, so a push from it shows up as a head §3 did not expect.
- `"ok": false` → the session is archived or gone, and cannot push. Go on.
- Anything else (a timeout, a non-zero exit with no JSON, a reply without `ok`) → **stop** before
  fixing or pushing anything: leave the PR a draft, start no round, and report the session and the
  command's output. An unanswered hold does not show that Auto-fix has stopped pushing.

**Read the findings, and only the findings.** The comment's `### Blocking findings` section names
each one and where it is:

```bash
gh api "repos/{owner}/{repo}/issues/comments/{id}" --jq '.body' \
  | awk '/^### Blocking findings/{on=1; next} /^(### |<details>)/{on=0} on'
```

`{id}` is the number after `#issuecomment-` in `.reviewed.summary_url`. Open a reviewer's full
report, the `<details>` blocks at the end of the same comment, only for a finding whose line does not
say enough to fix it.

When the section says `None.`, nothing in the code blocked: the summary is `Draft` because the PR
could not be marked ready, and its `### Outcome` line says why. There is nothing to fix and nothing
to push. Go to §3, so `/review-pr` tries again.

**Fix each one** at its `path:line`. A finding that is a behaviour bug gets a failing test first,
then the fix. Keep the change to what the findings name: a round that wanders makes the next review
a review of new code.

**A finding you believe is wrong** is not fixed to make the review pass. **Stop** before pushing
anything: leave the PR a draft, start no round, and report the finding and why you think it is
wrong. Overruling a reviewer is a person's call.

**Push once.** Run the tests that cover what you changed, commit naming the findings it fixes
(`Fix review round {n}: the token is no longer logged`), and `git push`. Never force-push, rebase, or
merge the base in.

## 3. Review

Run `/review-pr {pr_number}` through the Skill tool, here, in this session. It reviews the head,
posts its summary, and marks the PR ready or leaves it a draft.

Add `--criteria {path}` only when this round was given one and the head is still the commit the
report proves. `/finalize-pr` names the report `finalize-{pr_number}-{head, first 7}.md`: forward it
when `git rev-parse HEAD` starts with those seven characters, and leave it out otherwise. The head
moves when §2 pushed a fix, and when §1 fast-forwarded onto a push made after `/finalize-pr` ran;
either way the summary would call the Criteria proven on a head they were never run against.

`/review-pr` ends by printing its local summary, whose `Summary:` line is the URL of the comment it
posted. When it stops instead (a `✘` line, no `Summary:` line), no review of this head was posted.
**Stop** the round: start no next round, and report the `✘` line. Do not go to §4, which would read
the summary an earlier round left and take it for this one's.

## 4. The next round

Only after §3 posted a summary. Ask the gate again: `merge-gate.py --pr {pr_number}`.
`.reviewed.summary_url` must be the URL §3 printed; when it is not, **stop** as §3 says.

- `fresh` → `Ready`. Copilot and Auto-fix take it from here. Report and end.
- `blocked` with `.rounds` at **3** or below → start the next round and end this session:

  ```bash
  cd {top} && claude --bg --dangerously-skip-permissions \
    "/review-round {pr_number} --launched-by $CLAUDE_CODE_SESSION_ID"
  ```

  The shell fills in this session's id, which the next round leaves out of its §1 check.

  It prints `backgrounded · {id}`, the eight hex characters `claude agents --json` lists as `id`.
  Without one, the round did not start: say so, with the command's output, so a person can run it.
  A background session has nobody to answer a permission prompt, which is why it gets the bypass
  flag, as `/dispatch-slices` explains.
- `blocked` with `.rounds` above 3 → **stop**, as in §1.

Do not fix the new findings here. This session now holds a fix and a review, and the next round
exists so that the third fix does not cost the first two again.

## 5. Report

```
/review-round — {Ready | next round started | stopped}

PR:      #{pr_number} — {title}
Round:   {n} of at most 3 fixes | review only
Fixed:   {one line per finding fixed, with its commit} | nothing to fix
Review:  {the /review-pr outcome, and its summary URL}
Next:    {session {id} runs round {n+1} | Copilot and Auto-fix have it | a person: {why}}
```

## Rules

- One round per session: at most one push and one review. The next round is a new session.
- The PR is the state. Read the round count and the findings from GitHub, never from this session's
  memory or from an argument.
- At most three fix rounds since the last `Ready`. Past that, a person decides.
- Never fix a finding you believe is wrong. Stop and say so.
- Never force-push, rebase, or merge the base in. Never mark the PR ready: `/review-pr` does that.
- One round per worktree. A `busy` session in this checkout other than this one and its
  `--launched-by` launcher is a stop.
- Hold the Auto-fix session before pushing a fix.
- The background launch is the only one in this skill, and it is the one place it passes
  `--dangerously-skip-permissions`.
