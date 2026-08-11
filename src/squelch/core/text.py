"""Fitting text into limits somebody else set.

Issue titles, Discord thread names and embed fields all cap out at some
number of characters that is not ours to argue with, and the same rule fits
text into every one of them: cut at a word boundary when that still leaves
most of the text, and make it visible that something was cut.
"""

from __future__ import annotations


def trim(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, preferring a word boundary."""
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    cut = clean[: max(0, limit - 1)]
    space = cut.rfind(" ")
    # Only honour the word boundary if it does not throw away most of the text.
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"
