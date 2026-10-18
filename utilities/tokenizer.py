"""Model Specific Token counting utility with optional `tiktoken` support.

Falls back to a characters-based heuristic when `tiktoken` is unavailable.
"""

import tiktoken
from math import ceil
from typing import Optional

# Load Gemma tokenizer locally (caches to disk automatically)
# Pre-load HuggingFace Gemma tokenizer once to prevent high-latency re-loads on every request
# "google/gemma-2-2b" uses a 256k-vocabulary SentencePiece model similar to Gemini
try:
    from transformers import AutoTokenizer

    _gemma_tokenizer = AutoTokenizer.from_pretrained("google/gemma-2-2b")
except Exception:
    _gemma_tokenizer = None

# Optional GenAI Client for Gemini fallback/direct counts
try:
    from google import genai

    _genai_client = genai.Client()
except Exception:
    _genai_client = None


def count_tokens(text: str, model: Optional[str] = None) -> int:
    """Return an estimated token count for `text`.

    Uses `cl100k_base` or model-specific encoding.
    Otherwise falls back to `ceil(len(text)/4)`.
    """
    if not text:
        return 0

    if model in ("gpt-4o", "gpt-4o-mini"):
        # Use model-specific encoding
        encoding_name = "o200k_base"
        # basic mapping for common models; tiktoken will raise if unknown
        try:
            enc = tiktoken.get_encoding(encoding_name)
            return len(enc.encode(text))
        except Exception:
            # Fallback heuristic
            # gpt-4o and gpt-4o-mini models aremore efficient than other models.
            # Empirically, they use about 3.2 characters per token on average.
            return max(1, ceil(len(text) / 3.2))

    elif model in (
        "gemma-2-2b",
        "gemma-2-7b",
        "google/gemma-2-2b",
        "google/gemma-2-7b",
    ):

        """Fast, local token estimation using Gemma's HuggingFace tokenizer.
        Prevents extra API network calls to Google Cloud."""

        if _gemma_tokenizer:
            return len(_gemma_tokenizer.encode(text, add_special_tokens=False))

        # Fallback heuristic if local tokenizer fails to load
        return max(1, len(text) // 4)

    # Remote estimation via Google GenAI SDK
    elif model and model.startswith("gemini"):
        """Return an accurate token count for Gemini models using Google GenAI SDK.
        Falls back to a character-based heuristic on network/API failure.
        """

        if _genai_client:
            try:
                target_model = model if model != "gemini" else "gemini-2.5-flash-lite"
                response = _genai_client.models.count_tokens(
                    model=target_model,
                    contents=text,
                )
                return response.total_tokens
            except Exception:
                pass
        return max(1, ceil(len(text) / 4))

    else:
        # Default to cl100k_base encoding for other models
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            # Fallback heuristic
            return max(1, ceil(len(text) / 4))
