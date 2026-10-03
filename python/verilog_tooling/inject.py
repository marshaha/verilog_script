"""verilog-mode ``verilog-inject-auto`` port: inject AUTO markers into legacy code.

Examines legacy non-AUTO code and inserts AUTO markers in appropriate places
(verilog-mode.el ``verilog-inject-auto`` / ``verilog-inject-arg`` /
``verilog-inject-sense`` / ``verilog-inject-inst``):

- ``inject_arg``: for each ``module`` whose header has no ``/*AUTOARG*/``,
  insert the marker before the closing paren of the port list.
- ``inject_sense``: for each ``always @(...)`` with a plain sensitivity list,
  compute the AUTOSENSE list (via :mod:`verilog_tooling.sense`); when the
  written list holds exactly the same signals, replace it with ``/*AS*/``.
- ``inject_inst``: for each instance pin list without ``/*AUTOINST*/`` (and
  not ``.*``), delete ``.name(name)`` identity pins (case-sensitive, like
  verilog-mode) and insert ``/*AUTOINST*/`` before the closing paren.

``inject_auto`` runs all three.  The caller is expected to run the normal
AUTO expansion afterwards (``verilog-inject-auto`` is ``(verilog-auto t)``);
the ``AINJ`` vim command does inject + full AALL in one go.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence

_AUTOARG_MARK = re.compile(r"/\*\s*AUTOARG\s*\*/", re.I)
_AUTOSENSE_MARK = re.compile(r"/\*\s*(AUTOSENSE|AS)\s*\*/", re.I)
_AUTOINST_MARK = re.compile(r"/\*\s*AUTOINST(\(.*?\))?\s*\*/", re.I | re.S)
_MODULE_KW = re.compile(r"(?<![\w$])module\s+[\w$]+", re.I)
_ALWAYS_AT = re.compile(r"(?<![\w$])always\s*@\s*\(", re.I)
_IDENT_PIN = re.compile(r"\.\s*([a-zA-Z0-9`_$]+)\s*\(\s*\1\s*\)")


def _mask_strings_comments(text: str) -> str:
    """Blank out // and /* */ comments and "..." strings (length-preserving)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i))
            i = j
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            j = min(n, j + 1)
            out.append(" " * (j - i))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _find_paren_pair(text: str, open_idx: int) -> int | None:
    depth = 0
    in_s = False
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if in_s:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_s = False
        elif c == '"':
            in_s = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def inject_arg(lines: Sequence[str]) -> list[str]:
    """Insert ``/*AUTOARG*/`` before the header closing paren of each module
    that does not already have one (verilog-inject-arg)."""
    text = "\n".join(lines)
    masked = _mask_strings_comments(text)
    edits: list[tuple[int, int, str]] = []  # (pos, 0, insert_text)
    for m in _MODULE_KW.finditer(masked):
        # find the header paren pair: skip a possible #(...) parameter list
        i = m.end()
        while i < len(masked) and masked[i] in " \t\n":
            i += 1
        if masked.startswith("#", i):
            j = masked.find("(", i)
            if j < 0:
                continue
            j = _find_paren_pair(masked, j)
            if j is None:
                continue
            i = j + 1
            while i < len(masked) and masked[i] in " \t\n":
                i += 1
        if i >= len(masked) or masked[i] != "(":
            continue  # old-style `module foo;` — nothing to inject into
        close = _find_paren_pair(masked, i)
        if close is None:
            continue
        header = text[m.start():close]
        if _AUTOARG_MARK.search(header):
            continue
        edits.append((close, close, "/*AUTOARG*/"))
    out = text
    for a, b, ins in sorted(edits, reverse=True):
        out = out[:a] + ins + out[b:]
    return out.split("\n")


def _always_blocks(masked: str) -> list[tuple[int, int]]:
    blocks = []
    for m in _ALWAYS_AT.finditer(masked):
        open_idx = m.end() - 1
        close = _find_paren_pair(masked, open_idx)
        if close is not None:
            blocks.append((open_idx, close))
    return blocks


def inject_sense(lines: Sequence[str]) -> list[str]:
    """Replace a hand-written sensitivity list with ``/*AS*/`` when it holds
    exactly the signals AUTOSENSE would compute (verilog-inject-sense)."""
    from .sense import auto_sense

    text = "\n".join(lines)
    masked = _mask_strings_comments(text)
    out = text
    # work back to front so offsets stay valid
    for open_idx, close_idx in sorted(_always_blocks(masked), reverse=True):
        inner = text[open_idx + 1 : close_idx]
        if _AUTOSENSE_MARK.search(inner):
            continue
        pre = sorted(set(re.findall(r"[a-zA-Z_][\w$]*", inner)))
        if not pre:
            continue
        synth = out[: open_idx + 1] + "/*AS*/" + out[close_idx:]
        expanded = auto_sense(synth.split("\n"))
        got_m = re.search(
            r"/\*\s*(?:AUTOSENSE|AS)\s*\*/([^*]*)", "\n".join(expanded), re.I
        )
        got = sorted(set(re.findall(r"[a-zA-Z_][\w$]*", got_m.group(1)))) if got_m else []
        if set(pre) == set(got) and got:
            out = synth
    return out.split("\n")


def _instance_pin_spans(masked: str) -> list[tuple[int, int]]:
    """(open_paren, close_paren) of probable instance pin lists.

    Heuristic port of verilog-mode's pin search: a ``.name (`` pin pattern,
    then walk back to the enclosing paren; skip ``#(...)`` parameter lists.
    """
    spans = []
    for m in re.finditer(r"\.\s*[a-zA-Z0-9`_$]+\s*\(", masked):
        # walk back to enclosing open paren
        depth = 0
        i = m.start()
        open_idx = None
        while i >= 0:
            c = masked[i]
            if c == ")":
                depth += 1
            elif c == "(":
                if depth == 0:
                    open_idx = i
                    break
                depth -= 1
            i -= 1
        if open_idx is None:
            continue
        if open_idx > 0 and masked[open_idx - 1] == "#":
            continue  # #(...) parameter section
        close = _find_paren_pair(masked, open_idx)
        if close is None:
            continue
        if any(a <= open_idx and close <= b for a, b in spans):
            continue
        spans.append((open_idx, close))
    return spans


def inject_inst(lines: Sequence[str]) -> list[str]:
    """Delete ``.name(name)`` identity pins and insert ``/*AUTOINST*/`` into
    pin lists that lack one (verilog-inject-inst)."""
    text = "\n".join(lines)
    masked = _mask_strings_comments(text)
    out = text
    for open_idx, close_idx in sorted(_instance_pin_spans(masked), reverse=True):
        body = out[open_idx + 1 : close_idx]
        if _AUTOINST_MARK.search(body) or ".*" in body:
            continue
        # delete identity pins, case-sensitive (verilog-mode binds case-fold-search nil);
        # after each deletion swallow the immediately following separators/comments
        pieces, pos = [], 0
        for m in _IDENT_PIN.finditer(body):
            pieces.append(body[pos : m.start()])
            pos = m.end()
            while pos < len(body):
                if body[pos] in " \t\n\f,":
                    pos += 1
                elif body.startswith("//", pos):
                    j = body.find("\n", pos)
                    pos = len(body) if j < 0 else j
                else:
                    break
        pieces.append(body[pos:])
        new_body = "".join(pieces)
        # insert /*AUTOINST*/ before the closing paren, on its own line,
        # indented one past the opening paren (verilog-insert-indent)
        line_start = out.rfind("\n", 0, open_idx) + 1
        pad = " " * (open_idx - line_start + 1)
        new_body = new_body.rstrip()
        if new_body and not new_body.endswith(","):
            new_body += ","
        new_body += f"\n{pad}/*AUTOINST*/"
        out = out[: open_idx + 1] + new_body + out[close_idx:]
    return out.split("\n")


def inject_auto(lines: Sequence[str]) -> list[str]:
    """All three injections (verilog-inject-auto without the expansion pass)."""
    lines = inject_arg(lines)
    lines = inject_sense(lines)
    lines = inject_inst(lines)
    return lines


# ---------------------------------------------------------------------------
# CLI


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.inject",
        description="verilog-mode verilog-inject-auto rewrite",
    )
    parser.add_argument(
        "command",
        choices=["inject"],
        help="inject: insert AUTOARG/AS/AUTOINST markers into legacy code",
    )
    parser.add_argument("-i", "--in_file", required=True)
    parser.add_argument("-o", "--out_file", required=True)
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    out = inject_auto(lines)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
