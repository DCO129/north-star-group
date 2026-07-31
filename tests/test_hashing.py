'''S1-1 targeted tests: shared sha256_text helper + canonical_json preservation.

These tests verify the Ponytail-safe reduction:
- 5 former local sha256_text definitions collapsed into one shared helper
  (private_ai_company._hashing.sha256_text).
- Module-level legacy imports keep working (same function object).
- canonical_json output format in each module is untouched.
'''
from __future__ import annotations

import importlib

import pytest

from private_ai_company._hashing import sha256_text as shared_sha256_text
from private_ai_company import (
    economic_ledger,
    novel_knowledge,
    novel_mvp,
    novel_operations,
    platform_adapter,
)


# --------------------------------------------------------------------------- #
# 1) UTF-8 byte-stable SHA-256 across inputs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "hello world",
    "",                                     # empty string
    "你好，世界",                            # CJK
    "🚀🔥💡",                               # emoji
    "line1\r\nline2\nline3",                # mixed CRLF / LF
    "a" * 1000,                             # longer ASCII
])
def test_sha256_text_utf8_bytes(text: str) -> None:
    import hashlib
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert shared_sha256_text(text) == expected


# --------------------------------------------------------------------------- #
# 2) Legacy module-level entries equal shared helper output
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["abc", "中文", "x" * 50, ""])
def test_legacy_module_outputs_equal_shared(text: str) -> None:
    assert economic_ledger.sha256_text(text) == shared_sha256_text(text)
    assert novel_knowledge.sha256_text(text) == shared_sha256_text(text)
    assert novel_mvp.sha256_text(text) == shared_sha256_text(text)
    assert novel_operations.sha256_text(text) == shared_sha256_text(text)
    assert platform_adapter.sha256_text(text) == shared_sha256_text(text)


# --------------------------------------------------------------------------- #
# 3) All 5 legacy entries point to the SAME shared function object
# --------------------------------------------------------------------------- #
def test_legacy_entries_are_same_function_object() -> None:
    assert economic_ledger.sha256_text is shared_sha256_text
    assert novel_knowledge.sha256_text is shared_sha256_text
    assert novel_mvp.sha256_text is shared_sha256_text
    assert novel_operations.sha256_text is shared_sha256_text
    assert platform_adapter.sha256_text is shared_sha256_text


# --------------------------------------------------------------------------- #
# 4) Non-str input keeps the original failure semantics (no auto-coercion)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [None, 123, b"bytes", ["x"], {"k": 1}])
def test_non_str_input_raises_like_before(bad) -> None:
    with pytest.raises((AttributeError, TypeError, UnicodeError)):
        shared_sha256_text(bad)


# --------------------------------------------------------------------------- #
# 5) canonical_json unchanged (novel_knowledge spaced; others compact)
# --------------------------------------------------------------------------- #
def test_canonical_json_preservation() -> None:
    sample = {"b": 1, "a": 2}
    # novel_knowledge: default separators -> spaced
    assert novel_knowledge.canonical_json(sample) == '{"a": 2, "b": 1}'
    # other 4 modules: compact separators
    for mod in (economic_ledger, novel_mvp, novel_operations, platform_adapter):
        assert mod.canonical_json(sample) == '{"a":2,"b":1}'


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
