"""Python rewrite of automatic.vim's AutoArg (AR) / KillAutoArg commands.

Covers the module-header argument-generation pair bound in ~/.vimrc:

- ``kill_auto_arg``  <- KillAutoArg (automatic.vim 3225-3252)
- ``auto_arg``       <- AutoArg     (automatic.vim 3254-3373, ``AR`` / <S-F1>)

``auto_arg`` regenerates the port list of every module whose header carries a
``/*autoarg*/`` marker: it collapses the previous expansion first (KillAutoArg
is idempotent regeneration), scans the buffer for ``input``/``output``/``inout``
declarations and packs the signal names into the header, four spaces in and
wrapped past ``s:vlog_max_col`` (40).

The buffer-facing commands live on :class:`~verilog_tooling.inst.VerilogBuffer`
as ``kill_auto_arg`` / ``auto_arg`` (attached on import, like
:mod:`verilog_tooling.fmt`); the module-level functions are pure ``lines in ->
lines out`` wrappers.

Intentional deviations from the Vim originals (kept output-identical
otherwise):

- the filter drops whole ``task``/``endtask`` bodies as well as
  ``function``/``endfunction`` ones (``s:Filter`` only knows functions, so a
  task's ``input`` arguments leak into the generated port list);
- the marker/close scans stop at end-of-buffer instead of looping forever on
  a buffer whose marker line is never terminated by ``);``;
- a line ending in ``);`` plus trailing blanks is recognised as terminated
  (Vim's ``);$`` misses it and then eats the rest of the buffer).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence

from .inst import VerilogBuffer

_ARG_MARGIN = " " * 4  # s:vlog_arg_margin
_ARG_MAX_COL = 40  # s:vlog_max_col

_MARK = re.compile(r"/\*\s*\b(?:autoarg|AUTOARG)\b")
_MARK_LINE = re.compile(r"/\*\s*\b(?:autoarg|AUTOARG)\b.*")
_CLOSE = re.compile(r"\);\s*$")

_LINE_COMMENT = re.compile(r"//.*$")
_BLOCK_TAIL = re.compile(r"/\*.*$")
_BLOCK_HEAD = re.compile(r"^.*\*/")
_FUNCTION_OPEN = re.compile(r"^\s*function\b")
_FUNCTION_CLOSE = re.compile(r"^\s*endfunction\b")
_TASK_OPEN = re.compile(r"^\s*task\b")
_TASK_CLOSE = re.compile(r"^\s*endtask\b")
_ENDMODULE = re.compile(r"^\s*endmodule\b")

_PORT_DECL = re.compile(r"^\s*(input|output|inout)\b")
_PORT_PREFIX = re.compile(
    r"^\s*(?:input|output|inout)\b\s*"
    r"(?:\b(?:wire|reg|parameter|localparam|genvar|integer)\b)*\s*"
    r"(?:\[.*:.*\])*\s*"
)
_PORT_TAIL = re.compile(r"\s*;.*$")


# ---------------------------------------------------------------------------
# KillAutoArg


def kill_auto_arg(lines: Sequence[str]) -> list[str]:
    """Collapse every regenerated port list back to a ``... (/*autoarg*/);``
    stub: the marker line is closed with ``);`` and the generated lines up to
    (and including) the ``);`` terminator are deleted.

    A marker line already terminated by ``);`` is copied verbatim.  All other
    lines are copied unchanged.
    """
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not _MARK.search(line):
            out.append(line)
            i += 1
            continue
        if _CLOSE.search(line):
            out.append(line)
        else:
            out.append(line + ");")
            i += 1
            while i < n and not _CLOSE.search(lines[i]):
                i += 1
            if i >= n:  # unterminated marker: nothing left to keep
                break
        i += 1
    return out


# ---------------------------------------------------------------------------
# s:Filter (comment / function / task / endmodule handling)


def _filter_lines(lines: Sequence[str]) -> list[str]:
    """Drop comments and subprogram bodies, stop at ``endmodule``.

    Line-oriented port of ``s:Filter``: ``//`` tails are removed, a ``/*``
    without its ``*/`` truncates the line and swallows the block, a line that
    still holds a ``*/`` keeps only the text after it, and function (plus, as
    a deviation, task) bodies are skipped whole.
    """
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = _LINE_COMMENT.sub("", lines[i])
        if "/*" in line and "*/" not in line:
            out.append(_BLOCK_TAIL.sub("", line))
            i += 1
            while i < n and "*/" not in lines[i]:
                i += 1
            continue  # the '*/' line is reprocessed by the else branch
        if _FUNCTION_OPEN.match(line) or _TASK_OPEN.match(line):
            closer = _FUNCTION_CLOSE if _FUNCTION_OPEN.match(line) else _TASK_CLOSE
            i += 1
            while i < n and not closer.match(_LINE_COMMENT.sub("", lines[i])):
                i += 1
            continue
        if _ENDMODULE.match(line):
            out.append(line)
            break
        if "/*" not in line:
            line = _BLOCK_HEAD.sub("", line)
        line = _FUNCTION_CLOSE.sub("", line)
        i += 1
        if line:
            out.append(line)
    return out


def _collect_ports(lines: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """(inputs, outputs, inouts) signal texts, in buffer order.

    One signal per line (a comma-separated declaration stays one entry, as in
    the Vim original); the direction, data-type and packed-width prefix is
    stripped, so a vector port contributes its bare name.
    """
    inputs: list[str] = []
    outputs: list[str] = []
    inouts: list[str] = []
    buckets = {"input": inputs, "output": outputs, "inout": inouts}
    for line in lines:
        m = _PORT_DECL.match(line)
        if not m:
            continue
        name = _PORT_PREFIX.sub("", line)
        buckets[m.group(1)].append(_PORT_TAIL.sub("", name).strip())
    return inputs, outputs, inouts


# ---------------------------------------------------------------------------
# AutoArg emission


def _pack_ports(ports: Sequence[str], *, trailing_comma: bool) -> list[str]:
    """Greedily pack PORTS onto ``    a, b, c`` lines, like the Vim loop.

    The running column starts at the 4-char margin and grows by
    ``len(name) + 2``; before each port but the last it is checked against
    ``s:vlog_max_col``, and a full line is flushed with its dangling ``, ``.
    TRAILING_COMMA appends ``, `` after the last port when a later section
    follows.
    """
    out: list[str] = []
    col = len(_ARG_MARGIN)
    line = _ARG_MARGIN
    for name in ports[:-1]:
        if col > _ARG_MAX_COL:
            out.append(line)
            line = _ARG_MARGIN + name + ", "
            col = len(_ARG_MARGIN) + len(name) + 2
        else:
            line += name + ", "
            col += len(name) + 2
    if col > _ARG_MAX_COL:
        out.append(line)
        line = _ARG_MARGIN + ports[-1]
    else:
        line += ports[-1]
    if trailing_comma:
        line += ", "
    out.append(line)
    return out


def auto_arg(lines: Sequence[str]) -> list[str]:
    """Regenerate the ``/*autoarg*/`` port list in every module header.

    The previous expansion is collapsed first (so re-running is idempotent);
    each marker line is then truncated at its first ``)`` and followed by the
    Inputs/Outputs/Inouts sections of the declarations found in THAT MODULE's
    body (the buffer may hold several modules), separated by blank lines, and
    finally by ``);``.  Lines outside the marker region are copied verbatim.
    A section with no signal is omitted entirely.
    """
    lines = kill_auto_arg(lines)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if not re.match(r"\s*module\b", lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < n and not _ENDMODULE.match(lines[j]):
            j += 1
        j = min(j + 1, n)  # [i, j) is this module's span
        body = lines[i:j]
        inputs, outputs, inouts = _collect_ports(_filter_lines(body))
        out.extend(_expand_arg_markers(body, inputs, outputs, inouts))
        i = j
    return out


def _expand_arg_markers(
    lines: Sequence[str],
    inputs: Sequence[str],
    outputs: Sequence[str],
    inouts: Sequence[str],
) -> list[str]:
    """Expand every /*autoarg*/ marker in one module's LINES with the given
    Inputs/Outputs/Inouts sections."""
    sections = (("//Inputs", inputs), ("//Outputs", outputs), ("//Inouts", inouts))
    out: list[str] = []
    for line in lines:
        if not _MARK_LINE.search(line):
            out.append(line)
            continue
        if not any(ports for _, ports in sections):
            # nothing to declare: the collapsed one-line stub stays as-is
            out.append(line if ")" in line else line + ");")
            continue
        out.append(re.sub(r"\).*", "", line))
        for pos, (header, ports) in enumerate(sections):
            if not ports:
                continue
            if pos and any(ports_ for _, ports_ in sections[:pos]):
                out.append("")
            later = any(ports_ for _, ports_ in sections[pos + 1 :])
            out.append(_ARG_MARGIN + header)
            out.extend(_pack_ports(ports, trailing_comma=later))
        out.append(");")
    return out


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.fmt)


def _kill_auto_arg(self: VerilogBuffer) -> VerilogBuffer:
    """Collapse expanded ``/*autoarg*/`` port lists back to header stubs."""
    return VerilogBuffer(kill_auto_arg(self._lines))


def _auto_arg(self: VerilogBuffer) -> VerilogBuffer:
    """Regenerate the ``/*autoarg*/`` port list in every module header."""
    return VerilogBuffer(auto_arg(self._lines))


VerilogBuffer.kill_auto_arg = _kill_auto_arg
VerilogBuffer.auto_arg = _auto_arg


# ---------------------------------------------------------------------------
# CLI (mirrors gen_empty.py / verilog_tooling.inst so Vim can call it the same
# way)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.arg",
        description="automatic.vim AR (AutoArg) / KillAutoArg rewrite",
    )
    parser.add_argument(
        "command", choices=["ar", "kill"], help="ar: AutoArg; kill: KillAutoArg"
    )
    parser.add_argument(
        "-i", "--in_file", required=True, help="buffer file with /*autoarg*/ markers"
    )
    parser.add_argument("-o", "--out_file", required=True, help="output file")
    # accepted and ignored, so the shared Vim front-end can always pass them
    parser.add_argument("-y", "--libdir", action="append", default=[],
                        help="(unused by these commands)")
    parser.add_argument("--ref_file", default=None,
                        help="(unused by these commands)")
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    out = auto_arg(lines) if args.command == "ar" else kill_auto_arg(lines)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
