"""
Webhook entrypoint. GitHub calls this on every `pull_request` event.
This is Phase 1 (reactive core) from the plan — parity with existing
PR-triggered reviewers. Phase 2 (the 24/7 background scan) lives in
scripts/scheduled_scan.py and runs independently on a cron schedule.
"""
import hashlib
import hmac
import logging
import traceback
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException

from app.config import GITHUB_WEBHOOK_SECRET, MAX_FILES_PER_REVIEW, require_config
from app.github_client import (
    get_pr_files, get_pr_head_sha, post_review_comment,
    post_issue_comment, get_file_content, get_default_branch_sha, open_fix_pr,
)
from app.agents import run_pipeline
from app.config import AUTO_FIX_CONFIDENCE_THRESHOLD

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("code-reviewer")

# Full tracebacks always get written here, regardless of what the terminal
# shows or scrolls away — check this file if a webhook returns a 500.
_ERROR_LOG_PATH = Path(__file__).resolve().parent.parent / "webhook_errors.log"

app = FastAPI(title="24/7 Intelligent Code Reviewer")


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET:
        return True  # allow running without a secret in local/dev mode
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()

    if event == "pull_request" and payload.get("action") in ("opened", "synchronize", "reopened"):
        try:
            handle_pull_request(payload)
        except Exception:
            tb = traceback.format_exc()
            log.error("Webhook handler failed:\n%s", tb)
            with open(_ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 80 + "\n")
                f.write(tb)
            # Still return 200 so GitHub doesn't keep retrying, but the
            # error is fully captured in webhook_errors.log for debugging.
            return {"received": True, "error": "See webhook_errors.log for details"}

    return {"received": True}


def handle_pull_request(payload: dict):
    repo = payload["repository"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    repo_full_name = repo["full_name"]
    pr_number = payload["pull_request"]["number"]
    base_branch = payload["pull_request"]["base"]["ref"]

    log.info("Reviewing PR #%s on %s", pr_number, repo_full_name)

    files = get_pr_files(owner, repo_name, pr_number, max_files=MAX_FILES_PER_REVIEW)
    head_sha = get_pr_head_sha(owner, repo_name, pr_number)

    all_issues = []
    for f in files:
        try:
            content, _ = get_file_content(owner, repo_name, f["filename"], head_sha)
        except Exception:
            content = ""  # new/binary/deleted files
        issues = run_pipeline(f["filename"], f["patch"], content, repo_full_name)
        all_issues.extend(issues)

    if not all_issues:
        post_issue_comment(owner, repo_name, pr_number,
                            "✅ **24/7 Reviewer:** No issues found in this change.")
        return

    summary_lines = [f"### 🤖 24/7 Intelligent Code Reviewer — {len(all_issues)} finding(s)\n"]

    for issue in all_issues:
        comment_body = (
            f"**[{issue.severity.upper()} · {issue.category}]** {issue.summary}\n\n"
            f"{issue.explanation}"
        )
        post_review_comment(owner, repo_name, pr_number, comment_body, head_sha,
                             issue.file, issue.line)
        summary_lines.append(f"- `{issue.file}:{issue.line}` — {issue.summary} "
                              f"({issue.severity})")

        if issue.auto_fixable and issue.confidence >= AUTO_FIX_CONFIDENCE_THRESHOLD:
            try:
                base_sha = get_default_branch_sha(owner, repo_name, base_branch)
                branch_name = f"auto-fix/pr-{pr_number}-{issue.file.replace('/', '-')}-{issue.line}"
                pr = open_fix_pr(
                    owner, repo_name, base_branch, branch_name,
                    issue.file, issue.fix_diff, base_sha,
                    title=issue.summary,
                    body=f"Auto-generated fix (confidence {issue.confidence:.2f}) for "
                         f"an issue found in #{pr_number}.\n\n{issue.explanation}",
                )
                summary_lines.append(f"  → 🔧 Auto-fix opened: {pr.get('html_url', '')}")
            except Exception as e:
                log.warning("Auto-fix PR failed for %s: %s", issue.file, e)
        elif issue.confidence:
            summary_lines.append(f"  → confidence {issue.confidence:.2f}, below threshold — "
                                  f"flagged for human review instead of auto-fixing")

    post_issue_comment(owner, repo_name, pr_number, "\n".join(summary_lines))


if __name__ == "__main__":
    import uvicorn
    require_config()
    uvicorn.run(app, host="0.0.0.0", port=8000)
