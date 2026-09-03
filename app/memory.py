"""
Feedback + precedent memory. This is what lets the reviewer improve over
time instead of repeating the same noisy comments forever, and lets the
Explainer agent cite the team's own past code instead of generic advice.

Uses ChromaDB with its built-in default embeddings so there's no separate
embedding-provider API key required to get a working demo running.
"""
import chromadb
from chromadb.config import Settings

from app.config import MEMORY_DB_PATH

_client = chromadb.PersistentClient(
    path=MEMORY_DB_PATH,
    settings=Settings(anonymized_telemetry=False),
)
_precedent_collection = _client.get_or_create_collection("precedent")
_feedback_collection = _client.get_or_create_collection("feedback")


def index_precedent(repo_full_name: str, doc_id: str, text: str, metadata: dict):
    """Store a snippet (e.g. a past fix, a convention doc) for later retrieval."""
    _precedent_collection.upsert(
        ids=[f"{repo_full_name}:{doc_id}"],
        documents=[text],
        metadatas=[{**metadata, "repo": repo_full_name}],
    )


def find_precedent(repo_full_name: str, query: str, n_results: int = 1) -> str:
    """Return the closest matching precedent snippet for this repo, if any."""
    try:
        results = _precedent_collection.query(
            query_texts=[query],
            n_results=n_results,
            where={"repo": repo_full_name},
        )
    except Exception:
        return ""
    docs = results.get("documents", [[]])[0]
    return docs[0] if docs else ""


def record_feedback(repo_full_name: str, issue_summary: str, accepted: bool):
    """
    Log whether a human accepted or dismissed a comment. Over time this lets
    you filter the Detector's prompt/few-shot examples toward what this
    specific team actually finds useful, cutting the "AI review is just
    noise" problem that's the #1 complaint about existing tools.
    """
    _feedback_collection.add(
        ids=[f"{repo_full_name}:{hash(issue_summary)}"],
        documents=[issue_summary],
        metadatas=[{"repo": repo_full_name, "accepted": accepted}],
    )


def get_recent_false_positive_patterns(repo_full_name: str, n_results: int = 5):
    """Pull recently-dismissed comment summaries to steer the Detector away
    from repeating the same low-value flags for this repo."""
    try:
        results = _feedback_collection.get(
            where={"$and": [{"repo": repo_full_name}, {"accepted": False}]},
            limit=n_results,
        )
    except Exception:
        return []
    return results.get("documents", [])
