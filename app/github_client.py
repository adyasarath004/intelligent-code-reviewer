"""
Thin wrapper around the GitHub REST API. Handles everything the reviewer
needs to touch: reading PR diffs, posting inline review comments,
reading/writing files, and opening an auto-fix PR.
"""
import base64
import requests

from app.config import GITHUB_TOKEN, GITHUB_API_URL


def _headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_files(owner: str, repo: str, pr_number: int, max_files: int = 15):
    """Return a list of {filename, patch, status} for a PR, capped at max_files."""
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    resp = requests.get(url, headers=_headers(), params={"per_page": 100})
    resp.raise_for_status()
    files = resp.json()
    return [
        {"filename": f["filename"], "patch": f.get("patch", ""), "status": f["status"]}
        for f in files
        if f.get("patch")  # skip binary/renamed-only files with no diff
    ][:max_files]


def get_pr_head_sha(owner: str, repo: str, pr_number: int) -> str:
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    resp = requests.get(url, headers=_headers())
    resp.raise_for_status()
    return resp.json()["head"]["sha"]


def post_review_comment(owner: str, repo: str, pr_number: int, body: str,
                         commit_sha: str, path: str, line: int):
    """Post a single inline PR review comment."""
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
    payload = {
        "body": body,
        "commit_id": commit_sha,
        "path": path,
        "line": line,
        "side": "RIGHT",
    }
    resp = requests.post(url, headers=_headers(), json=payload)
    if resp.status_code >= 300:
        # Inline comment can fail if the line isn't part of the diff hunk;
        # fall back to a general PR comment so feedback isn't lost.
        post_issue_comment(owner, repo, pr_number, f"**{path}:{line}**\n\n{body}")
    return resp


def post_issue_comment(owner: str, repo: str, number: int, body: str):
    """Post a general (non-inline) comment on a PR or issue."""
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/issues/{number}/comments"
    resp = requests.post(url, headers=_headers(), json={"body": body})
    resp.raise_for_status()
    return resp.json()


def get_file_content(owner: str, repo: str, path: str, ref: str):
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.get(url, headers=_headers(), params={"ref": ref})
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return content, data["sha"]


def get_default_branch_sha(owner: str, repo: str, branch: str) -> str:
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/ref/heads/{branch}"
    resp = requests.get(url, headers=_headers())
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


def list_repo_tree(owner: str, repo: str, branch: str):
    """List all file paths in the repo at a given branch (for scheduled scans)."""
    sha = get_default_branch_sha(owner, repo, branch)
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/trees/{sha}"
    resp = requests.get(url, headers=_headers(), params={"recursive": "true"})
    resp.raise_for_status()
    tree = resp.json().get("tree", [])
    return [item["path"] for item in tree if item["type"] == "blob"]


def open_fix_pr(owner: str, repo: str, base_branch: str, new_branch: str,
                 file_path: str, new_content: str, base_sha: str,
                 title: str, body: str):
    """Create a branch, commit a single-file fix, and open a PR against base_branch."""
    headers = _headers()

    # 1. Create new branch from base
    ref_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs"
    requests.post(ref_url, headers=headers, json={
        "ref": f"refs/heads/{new_branch}",
        "sha": base_sha,
    })

    # 2. Get current file sha on the new branch (needed to update, not create)
    content_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{file_path}"
    existing = requests.get(content_url, headers=headers, params={"ref": new_branch})
    file_sha = existing.json().get("sha") if existing.status_code == 200 else None

    # 3. Commit the fix
    commit_payload = {
        "message": f"fix: {title}",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8"),
        "branch": new_branch,
    }
    if file_sha:
        commit_payload["sha"] = file_sha
    requests.put(content_url, headers=headers, json=commit_payload)

    # 4. Open the PR
    pr_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls"
    resp = requests.post(pr_url, headers=headers, json={
        "title": f"🤖 Auto-fix: {title}",
        "head": new_branch,
        "base": base_branch,
        "body": body,
    })
    resp.raise_for_status()
    return resp.json()
