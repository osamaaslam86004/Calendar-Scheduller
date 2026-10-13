import pytest
from unittest.mock import patch

pytest.importorskip("utilities.tokenizer")
from utilities.tokenizer import count_tokens


@pytest.mark.unit
def test_count_tokens_for_simple_text():
    assert count_tokens("hello world") >= 1
    assert count_tokens("") == 0


@pytest.mark.unit
def test_count_tokens_uses_fallback_when_tiktoken_fails():
    with patch(
        "utilities.tokenizer.tiktoken.get_encoding", side_effect=Exception("boom")
    ):
        assert count_tokens("fallback test text") >= 1

    with patch("utilities.tokenizer._gemma_tokenizer", None):
        assert count_tokens("a longer sample", model="gemma-2-2b") >= 1
