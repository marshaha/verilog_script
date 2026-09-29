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
    out = list(text)
    n = len(text)
    i = 0
    state = 0  # 0 normal, 1 line comment, 2 block comment, 3 string
    while i < n:
        two = text[i : i + 2]
        if state == 0:
            if two == "//":
                state = 1
                if line:
                    if cut_line:
                        j = text.find("\n", i)
                        end = n if j < 0 else j
                        out[i:end] = []
                        n -= end - i
                        text = text[:i] + text[end:]
                        state = 0
                        continue
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                i += 2
                continue
            if two == "/*":
                state = 2
                if block:
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                i += 2
                continue
            if text[i] == '"':
                state = 3
            i += 1
        elif state == 1:  # line comment
            if text[i] == "\n":
                state = 0
            elif line:
                out[i] = " "
            i += 1
        elif state == 2:  # block comment
            if two == "*/":
                if block:
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                i += 2
                state = 0
                continue
            if block and text[i] != "\n":
                out[i] = " "
            i += 1
        else:  # string literal
            if text[i] == "\\" and i + 1 < n:
                i += 2  # escaped char (\" or \\) stays inside the string
                continue
            if text[i] == '"':
                state = 0
            i += 1
    return "".join(out)


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
    return mask_comments(text, block=False, cut_line=True)
