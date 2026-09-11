"""Unicode helpers. Lengths are counted in user-perceived characters (grapheme clusters),
so a family emoji or a flag counts as one character, matching what mobile clients show."""

from __future__ import annotations

import regex

_GRAPHEME = regex.compile(r"\X")


def grapheme_length(text: str) -> int:
    return len(_GRAPHEME.findall(text))
