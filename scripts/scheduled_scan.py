"""
Phase 2 — the "24/7" differentiator.

Every PR-triggered reviewer on the market (CodeRabbit, Qodo, Greptile,
Copilot Code Review) only runs when a PR is open. This script runs on a
cron schedule (see .github/workflows/scheduled_scan.yml) and reviews the
DEFAULT BRANCH independently of any open PR — catching:
  - Drift: code that's grown inconsistent with newer conventions elsewhere
    in the repo
  - Stale TODO/FIXME markers that have sat untouched for a long time
  - Dependency files (requirements.txt, package.json) that may now contain
    packages with newly-disclosed vulnerabilities

Findings are posted as a single digest GitHub issue, so the team wakes up
to "here's what the bot found overnight" rather than a flood of comments.

Usage:
    python scripts/scheduled_scan.py <owner> <repo>
"""
import sys
import os
import re
import time
import requests

# Make sure the project root is importable regardless of invocation path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DEFAULT_BRANCH, require_config
from app.github_client import list_repo_tree, get_file_content, _headers, GITHUB_API_URL
from app.agents import detect_issues, explain_issue

# File extensions worth reviewing; keep this list tight to control cost/time.
REVIEWABLE_EXTENSIONS = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java")
DEPENDENCY_FILES = ("requirements.txt", "package.json", "go.mod", "Pipfile")
TODO_PATTERN = re.compile(r"(TODO|FIXME|HACK)[:\s]", re.IGNORECASE)
MAX_FILES_TO_SCAN = 40  # keeps a single run bounded on large repos


def find_stale_todos(content: str, filename: str):
    hits = []
    for i, line in enumerate(content.splitlines(), start=1):
        if TODO_PATTERN.search(line):
            hits.append(f"`{filename}:{i}` — {line.strip()[:120]}")
    return hits


def check_dependency_advisories(owner: str, repo: str, filename: str, content: str):
    """
    Lightweight advisory check via the GitHub Security Advisories API.
    This is intentionally simple — swap in `pip-audit --format json` or
    `npm audit --json` locally for a deeper check if you have those
    ecosystems set up in CI.
    """
    findings = []
    if filename == "requirements.txt":
        packages = [
            line.split("==")[0].strip()
            for line in content.splitlines()
            if "==" in line and not line.strip().startswith("#")
        ]
        for pkg in packages[:20]:  # bound the number of advisory lookups per run
            resp = requests.get(
                f"{GITHUB_API_URL}/advisories",
                headers=_headers(),
                params={"ecosystem": "pip", "affects": pkg, "per_page": 1},
            )
            if resp.status_code == 200 and resp.json():
                adv = resp.json()[0]
                findings.append(f"⚠️ `{pkg}` has a known advisory: {adv.get('summary', '')}")
            time.sleep(0.2)  # be polite to the API
    return findings


def run_scan(owner: str, repo: str):
    require_config()
    repo_full_name = f"{owner}/{repo}"
    print(f"[scheduled_scan] Scanning {repo_full_name}@{DEFAULT_BRANCH} ...")

    paths = list_repo_tree(owner, repo, DEFAULT_BRANCH)
    reviewable = [p for p in paths if p.endswith(REVIEWABLE_EXTENSIONS)][:MAX_FILES_TO_SCAN]
    dep_files = [p for p in paths if p.split("/")[-1] in DEPENDENCY_FILES]

    all_findings = []

    for path in reviewable:
        try:
            content, _ = get_file_content(owner, repo, path, DEFAULT_BRANCH)
        except Exception:
            continue

        for hit in find_stale_todos(content, path):
            all_findings.append(f"📌 Stale marker: {hit}")

        # Treat the whole file as a "patch" so we reuse the same Detector
        # prompt used for PR review — it still works fine on full content.
        issues = detect_issues(path, content[:8000])
        for issue in issues:
            if issue.severity in ("high", "critical"):
                explanation = explain_issue(issue, repo_full_name)
                all_findings.append(
                    f"🚨 `{path}:{issue.line}` [{issue.severity}] {issue.summary}\n  {explanation}"
                )

    for dep_file in dep_files:
        try:
            content, _ = get_file_content(owner, repo, dep_file, DEFAULT_BRANCH)
            all_findings.extend(check_dependency_advisories(owner, repo, dep_file, content))
        except Exception:
            continue

    post_digest(owner, repo, all_findings)


def post_digest(owner: str, repo: str, findings: list):
    if not findings:
        print("[scheduled_scan] No findings this run.")
        return

    body = (
        f"### 🌙 Overnight scan — {len(findings)} finding(s) on `{DEFAULT_BRANCH}`\n\n"
        "This ran independently of any open PR — part of the always-on scan.\n\n"
        + "\n\n".join(f"- {f}" for f in findings)
    )
    resp = requests.post(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/issues",
        headers=_headers(),
        json={"title": "🤖 24/7 Reviewer: overnight scan digest",
              "body": body, "labels": ["automated-review"]},
    )
    resp.raise_for_status()
    print(f"[scheduled_scan] Posted digest issue: {resp.json().get('html_url')}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scripts/scheduled_scan.py <owner> <repo>")
        sys.exit(1)
    run_scan(sys.argv[1], sys.argv[2])
