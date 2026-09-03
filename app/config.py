"""
Central configuration. All secrets come from environment variables —
never hardcode tokens/keys. See .env.example for what's required.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (one level up from this app/ folder),
# regardless of what directory the script was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# --- Anthropic ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# Set to "true" to run the pipeline with canned responses instead of real
# API calls — useful for demos/testing when you don't have API credits yet.
USE_MOCK_LLM = os.environ.get("USE_MOCK_LLM", "false").lower() == "true"

# --- GitHub ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")          # PAT or GitHub App installation token
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_API_URL = "https://api.github.com"

# --- Behavior tuning ---
# Issues at or above this confidence get an auto-opened fix PR.
# Below it, the bot only comments and tags a human.
AUTO_FIX_CONFIDENCE_THRESHOLD = float(os.environ.get("AUTO_FIX_CONFIDENCE_THRESHOLD", "0.85"))

# Max number of files reviewed per PR event (keeps latency/cost bounded)
MAX_FILES_PER_REVIEW = int(os.environ.get("MAX_FILES_PER_REVIEW", "15"))

# Where the memory/vector store persists between runs
MEMORY_DB_PATH = os.environ.get("MEMORY_DB_PATH", "./review_memory")

# Branch the 24/7 scheduled scan watches
DEFAULT_BRANCH = os.environ.get("DEFAULT_BRANCH", "main")


def require_config():
    if USE_MOCK_LLM:
        return  # no real credentials needed in mock mode
    missing = [
        name for name, val in [
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("GITHUB_TOKEN", GITHUB_TOKEN),
        ] if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )