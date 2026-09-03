"""
The core review pipeline, split into four stages so each one stays
simple and debuggable:

  1. detect_issues   -> find candidate problems in a diff
  2. explain_issue    -> teach WHY it matters (uses precedent from memory)
  3. propose_fix       -> generate an actual patch for the issue
  4. judge_confidence  -> decide: auto-fix PR, or comment + tag a human?

Each stage is a separate, focused Claude call with a strict JSON contract,
rather than one giant prompt doing everything at once. This keeps outputs
parseable and makes it easy to swap/tune a single stage later.
"""
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

import anthropic

from app.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, USE_MOCK_LLM
from app.memory import find_precedent

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY or "mock-key-unused")

# --------------------------------------------------------------------------
# Mock responses — used when USE_MOCK_LLM=true, so the full pipeline can be
# demoed with zero API calls and zero cost.
# --------------------------------------------------------------------------
_MOCK_DETECT_RESPONSE = json.dumps({
    "issues": [
        {
            "line": 4,
            "severity": "high",
            "category": "bug",
            "summary": "Silently flipping a negative amount to positive can mask a "
                       "caller bug (e.g. a refund accidentally passed to charge_card) "
                       "instead of rejecting the invalid input.",
        },
        {
            "line": 2,
            "severity": "medium",
            "category": "test-gap",
            "summary": "The unresolved currency-conversion TODO means charge_card "
                       "silently mishandles non-USD amounts with no test coverage.",
        },
    ]
})

# Per-issue mock content, keyed by a keyword that's guaranteed to appear in
# that issue's `summary` (and therefore in the user prompt for every stage).
# This lets the mock pipeline behave realistically even when detect_issues
# returns multiple issues in one run, instead of every issue collapsing onto
# a single canned explain/fix/judge response.
_MOCK_ISSUES = {
    "negative": {
        "explain": (
            "This matters because silently correcting a negative amount hides a bug "
            "instead of surfacing it. If a caller ever passes a negative value by "
            "mistake (say, a refund amount routed to the wrong function), this code "
            "will happily charge the customer instead of raising an error — which "
            "is much harder to debug later, and could mean a real customer gets "
            "charged incorrectly. In this repo's other payment functions, invalid "
            "input is normally rejected explicitly with a raised exception rather "
            "than silently corrected. Consider raising a ValueError here instead."
        ),
        "fix": json.dumps({
            "fixable": True,
            "patch": (
                "def charge_card(card_token, amount_cents):\n"
                "    # TODO: handle currency conversion\n"
                "    if amount_cents < 0:\n"
                "        raise ValueError(\"amount_cents must be non-negative for a charge\")\n"
                "    response = payment_gateway.charge(card_token, amount_cents)\n"
                "    return response"
            ),
        }),
        "judge": json.dumps({
            "confidence": 0.72,
            "reason": "The fix is safe and minimal, but changes calling behavior "
                      "(raises instead of silently succeeding), so a human should "
                      "confirm no caller relies on the old silent-correction behavior.",
        }),
    },
    "currency": {
        "explain": (
            "This matters because an unresolved TODO on currency conversion means "
            "charge_card silently accepts amounts it can't actually handle correctly "
            "for non-USD cases — there's no test proving what happens for those "
            "inputs today, so a regression here could ship unnoticed. Precedent in "
            "this repo is to pair any TODO that changes financial behavior with a "
            "regression test that pins down current behavior, even if the real fix "
            "lands later. Consider adding a test that documents (and guards) how "
            "non-USD amounts are handled right now."
        ),
        # A test-gap issue isn't safely auto-fixable without knowing the intended
        # currency-handling behavior, so the Fixer agent declines rather than
        # guessing — this is intentional, not a bug.
        "fix": json.dumps({"fixable": False, "patch": ""}),
        "judge": json.dumps({
            "confidence": 0.0,
            "reason": "Not auto-fixable: adding real currency-conversion logic needs "
                      "product input on rounding/rate-source behavior, so this should "
                      "stay a human-reviewed comment.",
        }),
    },
}

_MOCK_DEFAULT_ISSUE = "negative"  # fallback if no keyword matches


def _match_mock_issue(user: str) -> dict:
    for keyword, content in _MOCK_ISSUES.items():
        if keyword in user.lower():
            return content
    return _MOCK_ISSUES[_MOCK_DEFAULT_ISSUE]


@dataclass
class Issue:
    file: str
    line: int
    severity: str          # "low" | "medium" | "high" | "critical"
    category: str          # e.g. "bug", "security", "style", "test-gap"
    summary: str
    explanation: str = ""
    fix_diff: Optional[str] = None
    confidence: float = 0.0
    auto_fixable: bool = False


def _extract_json(text: str):
    """Claude is instructed to return raw JSON, but strip fences defensively."""
    text = text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def _call(system: str, user: str, max_tokens: int = 1500) -> str:
    if USE_MOCK_LLM:
        return _mock_call(system, user)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _mock_call(system: str, user: str) -> str:
    """Route to the right canned response based on which stage is calling,
    AND which issue the call is about (matched via keyword in the prompt).
    This keeps multi-issue mock runs realistic instead of every issue
    collapsing onto one canned explain/fix/judge response."""
    if system is DETECT_SYSTEM or "Detector agent" in system:
        return _MOCK_DETECT_RESPONSE
    mock_issue = _match_mock_issue(user)
    if "Explainer agent" in system:
        return mock_issue["explain"]
    if "Fixer agent" in system:
        return mock_issue["fix"]
    if "Judge agent" in system:
        return mock_issue["judge"]
    return "{}"


# --------------------------------------------------------------------------
# Stage 1: Detect
# --------------------------------------------------------------------------
DETECT_SYSTEM = """You are the Detector agent in an automated code review pipeline.
Given a unified diff patch for one file, find real, concrete issues: bugs,
security problems, missing error handling, missing tests for new logic,
and meaningful style/convention violations. Do NOT invent issues — if the
diff looks fine, return an empty list. Be precise about line numbers: use
the line number in the NEW version of the file (the "+" side of the diff).

Respond with ONLY raw JSON, no prose, no markdown fences, matching:
{"issues": [{"line": <int>, "severity": "low|medium|high|critical",
"category": "bug|security|style|test-gap", "summary": "<one sentence>"}]}
"""


def detect_issues(filename: str, patch: str) -> List[Issue]:
    user = f"File: {filename}\n\nDiff:\n{patch}"
    raw = _call(DETECT_SYSTEM, user)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    return [
        Issue(
            file=filename,
            line=item.get("line", 1),
            severity=item.get("severity", "low"),
            category=item.get("category", "style"),
            summary=item.get("summary", ""),
        )
        for item in data.get("issues", [])
    ]


# --------------------------------------------------------------------------
# Stage 2: Explain (the "teach, don't just gatekeep" differentiator)
# --------------------------------------------------------------------------
EXPLAIN_SYSTEM = """You are the Explainer agent. You write PR review comments
for engineers who may be junior or new to this codebase. For the given issue:
- Explain WHY it matters in plain language (not just what's wrong)
- If precedent from the team's own past code is given, reference it briefly
- Keep it warm and constructive, never condescending
- Keep it under 120 words

Respond with ONLY the comment text, no JSON, no headers.
"""


def explain_issue(issue: Issue, repo_full_name: str) -> str:
    precedent = find_precedent(repo_full_name, issue.summary)
    precedent_block = f"\nRelevant precedent from this repo: {precedent}" if precedent else ""
    user = (
        f"Issue in {issue.file}, line {issue.line} "
        f"[{issue.severity}/{issue.category}]: {issue.summary}{precedent_block}"
    )
    return _call(EXPLAIN_SYSTEM, user, max_tokens=400)


# --------------------------------------------------------------------------
# Stage 3: Propose fix
# --------------------------------------------------------------------------
FIX_SYSTEM = """You are the Fixer agent. Given a file's current content and a
described issue at a specific line, produce a corrected version of ONLY the
relevant function or block (not the whole file) as a unified diff hunk-style
patch. Be minimal and safe — do not refactor unrelated code.

Respond with ONLY raw JSON: {"patch": "<diff text>", "fixable": true|false}
If you cannot safely auto-fix this (too risky / needs human judgment / needs
broader context), set "fixable": false and "patch": "".
"""


def propose_fix(issue: Issue, file_content: str) -> Issue:
    user = (
        f"File: {issue.file}\nIssue at line {issue.line}: {issue.summary}\n\n"
        f"Current file content:\n{file_content[:6000]}"
    )
    raw = _call(FIX_SYSTEM, user, max_tokens=1200)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        data = {"patch": "", "fixable": False}
    issue.fix_diff = data.get("patch") or None
    issue.auto_fixable = bool(data.get("fixable")) and bool(issue.fix_diff)
    return issue


# --------------------------------------------------------------------------
# Stage 4: Judge confidence
# --------------------------------------------------------------------------
JUDGE_SYSTEM = """You are the Judge agent, the final gate before any automated
action is taken. Given an issue and its proposed fix, score your confidence
from 0.0 to 1.0 that the fix is CORRECT and SAFE to open as a PR without
human review first. Be conservative: security-critical or architecturally
significant changes should score low even if the diff looks plausible.

Respond with ONLY raw JSON: {"confidence": <float 0-1>, "reason": "<short>"}
"""


def judge_confidence(issue: Issue) -> Issue:
    if not issue.auto_fixable:
        issue.confidence = 0.0
        return issue
    user = (
        f"Issue: {issue.summary} (severity={issue.severity}, category={issue.category})\n"
        f"Proposed fix diff:\n{issue.fix_diff}"
    )
    raw = _call(JUDGE_SYSTEM, user, max_tokens=200)
    try:
        data = _extract_json(raw)
        issue.confidence = float(data.get("confidence", 0.0))
    except (json.JSONDecodeError, ValueError, TypeError):
        issue.confidence = 0.0
    return issue


def run_pipeline(filename: str, patch: str, file_content: str, repo_full_name: str) -> List[Issue]:
    """Run all four stages for one file's diff. Returns fully-scored issues."""
    issues = detect_issues(filename, patch)
    for issue in issues:
        issue.explanation = explain_issue(issue, repo_full_name)
        if issue.severity in ("high", "critical", "medium"):
            issue = propose_fix(issue, file_content)
            issue = judge_confidence(issue)
    return issues