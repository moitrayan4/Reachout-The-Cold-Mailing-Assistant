"""Groq LLM client with automatic model selection and an on-disk SQLite cache."""

from __future__ import annotations
import logging
import httpx
from pathlib import Path
from langchain_groq import ChatGroq
from langchain_community.cache import SQLiteCache
from langchain_core.globals import set_llm_cache

_logger = logging.getLogger("assistant.llm")
_groq_chat: ChatGroq | None = None


# ---------------------------------------------------------------------------
# Model auto-selection
# ---------------------------------------------------------------------------

_PREFERRED_MODEL_KEYWORDS = [
    "llama-3.3-70b",
    "llama-3.1-405b",
    "llama-3.1-70b",
    "llama-3-70b",
    "mixtral-8x7b",
    "gemma2-9b",
    "llama-3.1-8b",
]


def _select_best_model(api_key: str, preferred_id: str) -> str:
    """Query Groq /models and return the best available chat model."""
    try:
        resp = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        models = [m["id"] for m in resp.json().get("data", [])]
        _logger.debug("Groq models available: %s", models)

        # Try preferred_id first (from config)
        if preferred_id in models:
            return preferred_id

        # Fall through the keyword ranking
        for kw in _PREFERRED_MODEL_KEYWORDS:
            for m in models:
                if kw in m and "whisper" not in m and "guard" not in m:
                    return m

        # Last resort: first model in list that's not whisper
        for m in models:
            if "whisper" not in m:
                return m

        return preferred_id
    except Exception as exc:
        _logger.warning("Could not fetch Groq model list (%s); using %s", exc, preferred_id)
        return preferred_id


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_llm(api_key: str, preferred_model: str, cache_db: Path) -> ChatGroq:
    """Initialise the Groq client (once per process). Returns the ChatGroq instance."""
    global _groq_chat
    if _groq_chat is not None:
        return _groq_chat

    # Enable LangChain's SQLite cache
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    set_llm_cache(SQLiteCache(database_path=str(cache_db)))
    _logger.info("LLM cache enabled at %s", cache_db)

    model_id = _select_best_model(api_key, preferred_model)
    _logger.info("Using Groq model: %s", model_id)

    _groq_chat = ChatGroq(
        api_key=api_key,
        model=model_id,
        temperature=0.2,
        # Survive transient network blips: retry with backoff and a bounded
        # per-request timeout instead of hanging or failing on the first error.
        max_retries=3,
        request_timeout=30,
    )
    return _groq_chat


def get_llm() -> ChatGroq:
    if _groq_chat is None:
        raise RuntimeError("LLM not initialised. Call init_llm() first.")
    return _groq_chat
