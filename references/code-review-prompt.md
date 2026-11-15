# Code review prompt

**Version:** 1

The prompt `/review-pr` sends to the code reviewer: the project's code-reviewer agent, or the
`code-review` fallback when a project has none. `review-pr.py prepare` fills it in and writes it to
the run's directory, and the reviewer is told to read that file, so the session running `/review-pr`
never holds the prompt's text. A change to the prompt bumps the version.

`review-pr.py` fills every placeholder:

- `{pr_number}`, `{head}`: the PR and the commit under review.
- `{scope}`: empty for a review of the whole PR. For a review of only the commits after the last
  `Ready` head, the paragraph that says so and names the range.
- `{specs}`: one line per candidate implemented Spec, or the sentence saying none match. Saying so
  explicitly matters: a reviewer told there is nothing to drift from answers differently from one
  told nothing at all.
- `{tracker}`: the repository the Specs live in.
- `{ai_debris}`: [ai-debris.md](ai-debris.md) from its first `##` heading to the end. It is pasted
  into the rendered file rather than linked, because the reviewer runs in the project's repository,
  where the plugin's files are not.

The `FINDING:` and `spec-drift:` lines and the three sentinels are what `review-pr.py finish` reads.
`CONTRACTS.md` describes them under **The reviewer reports**.

## Prompt

~~~text
Review pull request #{pr_number} at head {head}.
{scope}
These Specs describe behaviour that already ships:

{specs}

Each Spec is the body of its issue. Read it with `gh issue view {n} --repo {tracker}`.

Flag any behaviour this diff changes that one of those Specs describes. Report each as one line:

spec-drift: #{spec} | {the behaviour} | {path:line}

These drift findings are advisory. Do not count them in CRITICAL_COUNT or HIGH_COUNT, and do not let
them change VERDICT.

Also walk the diff for these AI-debris patterns. A pattern rated Blocking is a High finding, and one
rated Not blocking is Medium or Low:

{ai_debris}

Write your review as you normally would. Then, after it, list every finding on one line each, most
severe first, in exactly this form:

FINDING: {Critical | High | Medium | Low} | {path:line} | {what is wrong, in one sentence}

Then append these exact sentinel lines at the very end of your response, outside any code block:

VERDICT: approved
CRITICAL_COUNT: 0
HIGH_COUNT: 0

OR if there are Critical or High findings:

VERDICT: changes_requested
CRITICAL_COUNT: <number>
HIGH_COUNT: <number>

VERDICT must be "approved" only if both counts are 0, and the counts must equal the number of
Critical and High FINDING lines.
~~~
