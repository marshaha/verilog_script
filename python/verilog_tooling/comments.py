"""Comment/string-aware text masking, shared by every parser in the package.

The naive ``re.sub(r"//.*$", "", line)`` idiom breaks on real code:

- ``//`` inside a block comment: ``/* // still comment */`` — stripping
  ``//`` first leaves an unbalanced ``/*`` that swallows following code
- ``/*`` inside a line comment: ``// /*`` — the ``/*`` must not open a block
- comment markers inside strings: ``"a//b"``, ``@"x/*y"`` template
  expressions, ``$display("100%%//done")``

:func:`mask_comments` walks the text once with a
NORMAL/LINE_COMMENT/BLOCK_COMMENT/STRING state machine and replaces every
comment character with a space.  Newlines are kept, so the mask has the
same length, offsets and line numbers as the input — any downstream regexp
or offset bookkeeping keeps working unchanged.  String literals
(``"..."`` with ``\\"`` escapes) are left intact, and ``(* ... *)``
attributes are not comments (``(*`` never matches ``/*``).
"""

from __future__ import annotations

import re
from functools import lru_cache

_MC_TOKEN = re.compile(r'//|/\*|"')


def _mask_comments_impl(text: str, line: bool, block: bool, cut_line: bool) -> str:
    # token-driven scan: only comment/string openers are visited, code
    # between them is copied through untouched — the char-by-char state
    # machine cost ~8ms per full-buffer call on big tops.
    n = len(text)
    edits: "list[tuple[int, int, bool]]" = []  # (start, end, cut?)
    i = 0
    search = _MC_TOKEN.search
    while i < n:
        m = search(text, i)
        if m is None:
            break
        c = m.group(0)
        s = m.start()
        if c == '"':
            # string literal: kept verbatim; \\ and \" escapes stay inside
            j = s + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "//":
            j = text.find("\n", s)
            j = n if j < 0 else j  # the newline is not part of the comment
            if line:
                edits.append((s, j, cut_line))
            i = j
            continue
        # /* */ block comment (unterminated runs to end of text)
        j = text.find("*/", s + 2)
        j = n if j < 0 else j + 2
        if block:
            edits.append((s, j, False))
        i = j
    if not edits:
        return text
    out = []
    pos = 0
    for s, e, cut in edits:
        out.append(text[pos:s])
        if not cut:
            out.append(re.sub(r"[^\n]", " ", text[s:e]))
        pos = e
    out.append(text[pos:])
    return "".join(out)


@lru_cache(maxsize=512)
def mask_comments(
    text: str, *, line: bool = True, block: bool = True, cut_line: bool = False
) -> str:
    """Same-length mask of TEXT with comments blanked to spaces.

    LINE/BLOCK select which comment styles are masked; an unmasked style is
    still tracked for state (a ``//`` inside a kept ``/* */`` block is never
    mistaken for a line comment, and vice versa).  Newlines and string
    literals are always preserved verbatim.

    With ``cut_line=True`` a line comment's characters are DELETED instead
    of masked (the text before the ``//`` is kept verbatim, including the
    space in front of it) — exact parity with the naive
    ``re.sub(r"//.*$", "", line)`` idiom, which some output-formatting
    callers depend on byte-for-byte.  The result is no longer same-length.
    """
    return _mask_comments_impl(text, line, block, cut_line)


def strip_comments(text: str) -> str:
    """TEXT with all comments removed: block comments masked to spaces,
    line comments cut like the naive ``re.sub(r"//.*$", "", ...)`` idiom —
    but correct for ``//`` inside ``/* */``, ``/*`` inside ``//`` and
    comment markers inside strings.  Use :func:`strip_line_comments` on
    text that may carry AUTO markers (``/*autoinst*/`` etc. are block
    comments and must stay visible)."""
    return mask_comments(text, cut_line=True)


def strip_line_comments(text: str) -> str:
    """Only ``//`` comments cut (naive-sub parity, preceding text verbatim);
    ``/* */`` blocks are kept (AUTO markers, template blocks) but still
    tracked — a ``//`` inside a block comment or a string is not treated
    as a comment."""
    if "//" not in text:
        return text
    if "\n" not in text and '"' not in text and "/*" not in text:
        return text.split("//", 1)[0]
    return mask_comments(text, block=False, cut_line=True)
