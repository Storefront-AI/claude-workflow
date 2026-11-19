---
name: frames
description: Capture before and after screenshots of every screen a pull request changes and publish them where a reviewer sees them without building the branch. Works with no configuration - detects how to serve the app with scripts/serve.py, serves the head and the base side by side on the Slice's own ports, picks the screens from the diff and the [manual] Criteria, drives Chrome through the chrome-devtools MCP, and writes each Frame to the ui-evidence branch of the pull request's repository. docs/agents/frames.md is optional and only overrides. Use when the user says "/frames", "capture the screenshots", "show the UI change", "before and after", or when /finalize-pr or a HITL session reaches the Frames step.
---

# Frames

Show the reviewer what the pull request changes on screen, before and after, so they never have to
check out the branch and run a dev server to see it.

`/finalize-pr` runs this as a step. A HITL session runs it directly before it stops, so the smoke
test starts from a picture rather than from a checkout. A human can type it on any pull request,
with or without a Slice behind it.

## The shapes this skill reads and writes

`CONTRACTS.md` at the repo root, under **The frame**: the Frame, its name, the optional
`docs/agents/frames.md`, where a Frame lands, what blocks, and the port block. Read it before
changing anything below.

## Input

`$ARGUMENTS`: a PR number (`42` or `#42`). If empty, detect from the current branch.

## 1. Pre-flight

- Find the PR: `gh pr view {n} --json number,url,headRefName,baseRefName,closingIssuesReferences`.
  Without an argument, `gh pr list --head $(git branch --show-current) --json … --limit 1`.
- The repository: `gh repo view --json nameWithOwner,visibility`.
- The changed files: `gh pr diff {n} --name-only`.
- **No UI file changed** → report `frames: no UI change` and stop, blocking nothing. A UI file is a
  `.tsx`, `.jsx`, `.vue`, `.svelte`, `.astro`, `.html`, `.erb`, `.css`, `.scss`, `.sass` or `.less`
  file, or a template under a `templates/` or `views/` directory, that is not a test, a story or a
  fixture. A Slice whose `[manual]` Criteria have a manifest row is the one exception: it runs
  whatever the diff touched, because the manifest said it would.
- **Public repository with no `docs/agents/frames.md`** → report
  `frames: public repository; add docs/agents/frames.md to opt in` and stop, blocking nothing. A
  Frame shows whatever the local data holds, and on a public repository `ui-evidence` is public.
  An empty `frames.md` is enough to opt in.
- **The Slice**, when the PR closes one. Read it from **the repository the link names**, never from
  the pull request's own:

  ```bash
  gh pr view {n} --json closingIssuesReferences \
    --jq '.closingIssuesReferences[] | "\(.repository.owner.login)/\(.repository.name)#\(.number)"'
  gh issue view {issue} --repo {owner}/{repo} --json title,body
  ```

  Parse `## Acceptance criteria` for its `[manual]` Criteria and their ids. No Slice, or Criteria
  with no ids, is not a stop: the screens then come from the diff alone, and no Frame binds to a
  Criterion.

## 2. Read the overrides

`docs/agents/frames.md` is optional. Every part of it is an override for something this skill
otherwise works out for itself:

- `**Serve:**` replaces detection in §4.
- `**Auth:**` is how §8 signs in.
- `**Viewport:**` replaces the default of `1440x900`.
- A table row pins a Criterion to a screen. A row whose Criterion this Slice owns is **declared**:
  a Frame is expected, and its absence blocks.

## 3. Compute the ports

`base = 20000 + (n mod 4000) * 10`, per `CONTRACTS.md`, where `n` is the Slice's issue number, or
the PR's own number when there is no Slice. Issues and pull requests share one sequence in a
repository, so the PR number cannot collide with another Slice's.

- `base + 1` serves the **head**, the after.
- `base + 3` serves the **base**, the before.

A dispatched session was told its block in its prompt. Use that when it is there.

## 4. Find the serve command

```bash
python3 <this skill dir>/../../scripts/serve.py "$(git rev-parse --show-toplevel)" --port {base+1}
```

([serve.py](../../scripts/serve.py).) Its first line is one state:

| First line | What to do |
| --- | --- |
| `manifest` or `detected` | Use its `serve:` line. Keep its `install:` line for §5. |
| `ambiguous: …`, `invalid: …` or `none: …` | Stop. Report it in the script's words, which name the **Serve:** line to add. Declared rows are `MISSING`; nothing else blocks. |

## 5. Prepare the base checkout

The before is the pull request's merge base, served from a checkout of its own so the Slice's
worktree is never touched:

```bash
git fetch -q origin {baseRefName}
sha=$(git merge-base origin/{baseRefName} HEAD)
before="${TMPDIR:-/tmp}/frames-{repo}-pr{n}-base"
git worktree add --detach "$before" "$sha"
```

Give it dependencies. When no lockfile differs between `$sha` and `HEAD`, which is

```bash
git diff --quiet "$sha" HEAD -- '*package-lock.json' '*pnpm-lock.yaml' '*yarn.lock' \
  '*bun.lock' '*bun.lockb' '*uv.lock' '*Gemfile.lock'
```

exiting 0, symlink each `node_modules` and `.venv`
that the head checkout has, at the same relative paths. Otherwise run the `install:` line inside
`$before`. `install: not known` with a changed lockfile means no before: continue with after only.

Copy the head checkout's ignored `.env*` files (`git ls-files --others --ignored --exclude-standard`)
into the same paths in `$before`. A dev server that reads `.env.local` serves nothing useful
without it, and the copy is removed with the checkout in §11.

Run `serve.py` again against `$before` with `--port {base+3}`. The base may serve differently from
the head. A state other than `manifest` or `detected` means no before, and the report says why.

## 6. Load the browser tools before using them

The chrome-devtools MCP tools arrive deferred. One call, before anything else:

```
ToolSearch: select:mcp__chrome-devtools__new_page,mcp__chrome-devtools__navigate_page,mcp__chrome-devtools__resize_page,mcp__chrome-devtools__take_screenshot,mcp__chrome-devtools__take_snapshot,mcp__chrome-devtools__click,mcp__chrome-devtools__fill,mcp__chrome-devtools__wait_for,mcp__chrome-devtools__list_network_requests,mcp__chrome-devtools__close_page
```

No `mcp__chrome-devtools__*` tools after that call → **stop and say so**, naming the command in
`CONTRACTS.md` under **The browser**. Declared rows are `MISSING`.

## 7. Serve both, with a bound

Start each `serve:` command in the background, the head from the worktree root and the base from
`$before`, and wait for each to answer, for a bounded number of seconds and never indefinitely:

```bash
for i in $(seq 1 90); do
  curl -s -o /dev/null "http://localhost:{port}/" && break
  sleep 1
done
curl -s -o /dev/null "http://localhost:{port}/" && echo ready || echo "no answer on {port} after 90s"
```

`curl -s` without `-f`: an app whose `/` is a 404 or a redirect is still up.

- The head not `ready` → **stop**, carrying the serve command's own output. Declared rows are
  `MISSING`.
- The base not `ready` → carry on with after only, and report the base's output.

**The bound is the point, not the number.** This skill runs inside `claude --bg` sessions with
nobody watching, and an unbounded wait turns a server that never binds into a worktree held open
until somebody notices. Whatever happens from here, §11 stops both servers.

## 8. Choose the screens

At most **six** screens, in this order, so a reviewer gets the ones that matter rather than forty:

1. **Declared rows** for this Slice's Criteria. The row's path, viewport and kind are used as written.
2. **`[manual]` Criteria with no row.** Find the screen the Criterion describes in the code: the
   route, and the steps that bring the screen into the state the Criterion names.
3. **Changed UI files.** Map each to the route that renders it:

   | Convention | File | Route |
   | --- | --- | --- |
   | Next.js app router | `app/billing/page.tsx` | `/billing` |
   | Next.js pages | `pages/billing/index.tsx` | `/billing` |
   | SvelteKit | `src/routes/billing/+page.svelte` | `/billing` |
   | Nuxt, Astro | `pages/billing.vue`, `src/pages/billing.astro` | `/billing` |
   | Remix, React Router 7 | `app/routes/billing.tsx` | `/billing` |
   | Static | `billing.html` | `/billing.html` |

   Anything else, such as a component or a stylesheet, find its importers up to a route definition
   in the code, a few hops at most. A component every screen uses, such as a layout or a button,
   gets one representative screen, and the report says it is representative. A route segment that
   takes a parameter (`/orders/[id]`) needs a value: take one from the app's own seed or fixture
   data, never one made up.

Every path comes from a manifest row or a route definition in the code. A changed file with no
route the code can name is reported as `no screen found`, never captured from a guessed URL.

Each screen keeps: a **name**, the path, the steps that reach its state (none, for most), the
viewport, and the Criterion id it binds to, if any.

## 9. Capture each screen, before then after

Per screen, the same steps on the base server and then on the head server:

1. `new_page` at `http://localhost:{port}{path}` with `isolatedContext` set to `frames-before` or
   `frames-after`. Keep the `pageId`: every later call needs it.
2. `resize_page` to the viewport.
3. **Signed out?** The page landed on a path containing `login`, `signin` or `auth`, or
   `take_snapshot` shows a password field. With an `**Auth:**` line whose variables are set, sign in
   once per context with `fill` and `click`, then navigate back to the path. Otherwise skip this
   screen as `behind auth`, naming the `**Auth:**` line or the variable that is not set.
4. Perform the screen's steps with `click`, `fill` and `wait_for`. Then `wait_for` text the screen
   shows when it has rendered, so the capture does not race the render.
5. `take_screenshot` with `pageId`, `fullPage: true` and `filePath` set to
   `{worktree}/.frames/{frame}-before.png` or `{worktree}/.frames/{frame}-after.png`, absolute.
   The MCP server refuses a path outside the session's workspace, so a temporary directory does not
   work. `.frames/` is never committed, and §11 removes it.
6. `list_network_requests` with `resourceTypes: ["fetch", "xhr", "document"]`. Note every host that
   is not `localhost` or `127.0.0.1`; the report names them.
7. `close_page`.

Never navigate to anything but `localhost`. A screen whose page redirects off it is skipped as
`left localhost: {host}`.

**The before can be absent.** A route that does not exist on the base, a 404 or the app's own
not-found page, is a `new screen`: the after is published alone. A before that fails for any other
reason is reported, and never blocks.

A `filmstrip` row is several captures of one page as it changes, before and after alike, stitched
top to bottom with `magick {first}.png … {last}.png -append {frame}-after.png`. Capture one before
the first interaction and one after the last, so the strip reads as a sequence rather than as an
ending. Without `magick`, publish the captures as `{frame}-after-1.png`, `-2`, and so on.

**Read every Frame back before publishing it.** `file {path}` must say `PNG image data`, with a
width no greater than the viewport's and no more than 20 pixels under it, which is a scrollbar.

A page taller than three viewports is cropped to its top three, with
`magick {path} -crop {width}x{3*height}+0+0 +repage {path}`, and the report says `cropped`. A
full-page capture of a long document is tens of thousands of pixels tall and several megabytes,
which nobody reviews side by side. Without `magick`, capture with `fullPage: false` instead.

Then look at it. A blank page, a spinner or a framework error overlay is not a Frame of the screen:

- **An error page on the first load:** reload once with `navigate_page` `type: reload`, then
  `wait_for` and capture again. A dev server that has just started often rebuilds its dependencies
  on the first request and reloads, and the page in between is an error the code did not cause.
- On the **after**, an error that survives the reload is the finding. Publish it, and report the
  screen as `error on head`. A declared row in that state is `MISSING`.
- A blank page or a spinner: capture once more after a longer `wait_for`, then report it as
  `blank`. A declared row in that state is `MISSING`.

A Frame showing a credential, a token, a real card number or anything that looks like a real
person's data is deleted, not published, and reported as `withheld: {what}`.

## 10. Publish to the evidence branch

Create `ui-evidence` in the **pull request's** repository the first time, as an orphan branch
carrying no history. The contents API cannot create a branch, and a ref cut from the default branch
would carry a copy of the whole tree, so this is three calls through the git data API:

```bash
gh api "repos/{owner}/{repo}/git/ref/heads/ui-evidence" >/dev/null 2>&1 || {
  tree=$(jq -n '{tree:[{path:"README.md",mode:"100644",type:"blob",
    content:"Frames published by /frames, one directory per pull request. Never merged."}]}' \
    | gh api -X POST "repos/{owner}/{repo}/git/trees" --input - --jq '.sha')
  commit=$(jq -n --arg t "$tree" '{message:"ui-evidence",tree:$t,parents:[]}' \
    | gh api -X POST "repos/{owner}/{repo}/git/commits" --input - --jq '.sha')
  gh api -X POST "repos/{owner}/{repo}/git/refs" -f ref=refs/heads/ui-evidence -f sha="$commit"
}
```

Then one call per Frame through the contents API, so nothing is committed from the worktree and the
Slice's own branch stays exactly what the author wrote. Replacing a file needs its current `sha`:

```bash
sha=$(gh api "repos/{owner}/{repo}/contents/pr-{n}/{file}?ref=ui-evidence" --jq .sha 2>/dev/null)
gh api -X PUT "repos/{owner}/{repo}/contents/pr-{n}/{file}" \
  -f branch=ui-evidence -f message="frames for #{n}" -f "content=$(base64 -i {path})" \
  ${sha:+-f sha="$sha"}
```

Read the response's `content.sha` back. A 2xx from GitHub is not proof the write landed.

Then delete every file under `pr-{n}/` that this run did not write, with
`gh api -X DELETE … -f branch=ui-evidence -f sha=… -f message=…`, so a re-run leaves exactly this
run's Frames. Never touch another pull request's directory.

The URL a reviewer opens is
`https://github.com/{owner}/{repo}/blob/ui-evidence/pr-{n}/{file}?raw=true`.

## 11. Clean up

Whatever happened above, including a stop in §4 to §10: stop both servers this skill started,
then `git worktree remove --force "$before"`, which takes the copied `.env*` files with it, and
`rm -rf {worktree}/.frames`. A server started through a package manager runs as a child of it, so
stop whatever listens on each port you started (`lsof -ti tcp:{port} -sTCP:LISTEN`), not only the
process you launched, and check that both ports are closed.

## 12. Report

One row per screen, and one per `[manual]` Criterion that has no screen, then the Screens block
`/finalize-pr` puts in the PR body:

```
frames: 4 screens, 4 after, 3 before, published to ui-evidence

  #60 C2  /cart              before + after   60-c2-cart-summary-balance
  screen  /settings/billing  before + after   screen-settings-billing
  screen  /onboarding        new screen       screen-onboarding
  #60 C7  /checkout          MISSING          declared at docs/agents/frames.md:9, {reason}
  #60 C9  no frame           no screen found for "{the Criterion's wording}"
  screen  /admin             behind auth      add an **Auth:** line to docs/agents/frames.md
  hosts outside localhost: api.stripe.com (on /checkout)

## Screens

| Screen | Before | After |
| --- | --- | --- |
| `/cart`, #60 C2 | ![before](https://github.com/o/r/blob/ui-evidence/pr-64/60-c2-cart-summary-balance-before.png?raw=true) | ![after](https://github.com/o/r/blob/ui-evidence/pr-64/60-c2-cart-summary-balance-after.png?raw=true) |
| `/onboarding` | new screen | ![after](https://github.com/o/r/blob/ui-evidence/pr-64/screen-onboarding-after.png?raw=true) |
```

`MISSING` blocks. Nothing else does. `CONTRACTS.md` has the asymmetry and why it is the only part
of this that is a gate.

## Rules

- Never change a Criterion's state. A `[manual]` Criterion with a Frame is still `manual`, and a
  Frame is never evidence that a behaviour holds.
- Only `localhost`. Never navigate to another host, never sign in anywhere but the local app, and
  never type a credential that did not come from a variable the `**Auth:**` line names.
- Never publish a Frame nobody looked at, and never one showing a secret or a real person's data.
- Never commit a Frame to the Slice's branch. Frames go to `ui-evidence` through the API, and
  `.frames/` is removed before the skill ends.
- Never capture from a guessed URL. Every path comes from the manifest or a route in the code.
- Never start a server on a port outside this Slice's block, and never kill a process or remove a
  checkout this skill did not start.
- Only a declared row that did not produce its Frame blocks. Everything this skill works out for
  itself is best effort.
- Idempotent. Re-running replaces the Frames for this PR and nothing else.
