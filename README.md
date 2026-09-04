# 24/7 Intelligent Code Reviewer
testing the reviewer bot
An automated, always-on code reviewer for the next generation of engineers.

Unlike PR-triggered tools (CodeRabbit, Qodo, Greptile, GitHub Copilot Code
Review), this reviewer runs on **two independent loops**:

1. **Reactive core** — reviews every PR the moment it's opened/updated
   (`app/main.py`), same as any standard AI reviewer.
2. **24/7 background scan** — a scheduled job (`scripts/scheduled_scan.py`,
   wired up in `.github/workflows/scheduled_scan.yml`) that re-scans the
   default branch every few hours, independent of any open PR. This is
   what actually earns "24/7" — it catches drift, stale TODOs, and newly
   disclosed dependency vulnerabilities that no PR-only tool would notice.

It's also built to **teach, not just gatekeep**: every comment explains
*why* an issue matters, in plain language, and cites precedent from the
team's own past code when available (`app/memory.py`) — aimed squarely at
onboarding and junior engineers rather than just senior-engineer efficiency.

## Architecture

```
GitHub PR event ──► app/main.py (FastAPI webhook)
                          │
                          ▼
                    app/agents.py
              ┌───────────┼────────────┬─────────────┐
              ▼           ▼            ▼              ▼
          Detect  ──►  Explain  ──►  Fix  ──►   Judge (confidence)
              │           │            │              │
              │           ▼            │              │
              │   app/memory.py        │      ≥ threshold → auto-fix PR
              │   (precedent +         │      < threshold → comment only,
              │    feedback store)     │                    tag a human
              ▼
      app/github_client.py ──► posts comments / opens fix PRs

Independently, on a cron schedule:
scripts/scheduled_scan.py ──► scans default branch ──► posts digest issue
```

## Setup

```bash
cp .env.example .env        # fill in ANTHROPIC_API_KEY and GITHUB_TOKEN
pip install -r requirements.txt
```

Run the webhook server locally:

```bash
python -m app.main
# or: uvicorn app.main:app --reload --port 8000
```

Point a GitHub webhook (repo Settings → Webhooks) at
`https://<your-host>/webhook/github`, content type `application/json`,
event: **Pull requests**. For local testing, tunnel with `ngrok http 8000`.

Run a one-off scheduled scan manually:

```bash
python scripts/scheduled_scan.py <owner> <repo>
```

The GitHub Actions workflow (`.github/workflows/scheduled_scan.yml`) runs
this automatically every 4 hours once `ANTHROPIC_API_KEY` and
`GITHUB_TOKEN` are added as repo secrets.

## Demo script (for a hackathon pitch)

1. Open a PR with a deliberate bug → show the bot commenting live, with a
   plain-language explanation (not just "this is wrong").
2. Show a low-confidence issue getting flagged for human review vs. a
   high-confidence one getting an auto-fix PR opened automatically.
3. Trigger the scheduled scan manually (`workflow_dispatch`) and show the
   digest issue it posts — this is the scene that proves "24/7," since it
   has no PR attached to it at all.

## Why this is different

| | CodeRabbit / Qodo / Greptile / Copilot | This project |
|---|---|---|
| Trigger | PR open/update only | PR open/update **+** scheduled background scan |
| Comment style | "What's wrong" | "What's wrong **and why**, with team precedent" |
| Fix action | Comment / suggest | Auto-opens a fix PR above a confidence threshold |
| Audience | Senior-engineer efficiency | Junior-engineer mentorship + efficiency |

## Notes on scope

This is a working MVP scaffold, not a production system. Before real use:
add rate limiting, handle GitHub App installation auth (not just a PAT),
add retries/backoff on the Anthropic and GitHub calls, and expand test
coverage. The vector memory store (`chromadb`, local persistent client) is
sized for a demo — swap for a hosted vector DB for multi-repo/team scale.
