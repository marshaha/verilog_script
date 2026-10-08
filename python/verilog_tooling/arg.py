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
- a misplaced marker (after the header's own ``);``) is left verbatim by
  both commands — the Vim original's kill scan eats everything up to the
  next instance's ``);``;
- header-style declarations (``input clk,`` inside the parens) have their
  trailing comma stripped, so the generated list reads ``clk, din`` instead
  of ``clk,, din,,``;
- a line ending in ``);`` plus trailing blanks is recognised as terminated
  (Vim's ``);$`` misses it and then eats the rest of the buffer).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Mapping, Sequence

from .inst import VerilogBuffer

_ARG_MARGIN = " " * 4  # s:vlog_arg_margin
_ARG_MAX_COL = 40  # s:vlog_max_col

_MARK = re.compile(r"/\*\s*\b(?:autoarg|AUTOARG)\b")
_MARK_LINE = re.compile(r"/\*\s*\b(?:autoarg|AUTOARG)\b.*")
_CLOSE = re.compile(r"\);\s*$")

_FUNCTION_OPEN = re.compile(r"^\s*function\b")
_FUNCTION_CLOSE = re.compile(r"^\s*endfunction\b")
_TASK_OPEN = re.compile(r"^\s*task\b")
_TASK_CLOSE = re.compile(r"^\s*endtask\b")
_ENDMODULE = re.compile(r"^\s*endmodule\b")

_PORT_PREFIX = re.compile(
    r"^\s*(?:input|output|inout)\b\s*"
    r"(?:\b(?:wire|reg|parameter|localparam|genvar|integer)\b)*\s*"
    r"(?:\[[^\]]*:[^\]]*\]\s*)*"
)
# a user-defined type word (optionally pkg::-scoped or an interface modport)
# plus its packed dims, ahead of the port name: `output axi_req_t [3:0] x`.
# logic/signed deliberately NOT stripped: the Vim original keeps them
# (tests/test_arg.py locks that).  Keyword-like first words are excluded.
_UDT_PREFIX = re.compile(
    r"^[A-Za-z_][\w$]*(?:::[A-Za-z_][\w$]*)*(?:\.[A-Za-z_][\w$]*)?"
    r"(?:\s*\[[^\]]*\])*\s+(?=[A-Za-z_])"
)
_UDT_STRIP_EXEMPT = frozenset(
    {"logic", "bit", "signed", "unsigned", "var", "int", "time", "tri", "supply0", "supply1"}
)
_AIO_MARK = re.compile(r"/\*\s*\b(?:autoinput|autooutput)\b", re.IGNORECASE)
_PREPROC_LINE = re.compile(r"^\s*`[A-Za-z_]")
_MODULE_NAME = re.compile(r"\bmodule\s+([A-Za-z_][\w$]*)")
_PORT_TAIL = re.compile(r"\s*;.*$")
_PORT_TRAIL_COMMA = re.compile(r"\s*,\s*$")  # header-style decl: `input clk,`
_PORT_TRAIL_PAREN = re.compile(r"\s*\)+\s*$")  # single-line header: `input b)`
_UNPACKED_TAIL = re.compile(r"(?:\s*\[[^\]]*\])+\s*$")  # `val[3:0]` / `val [3:0][7:0]`
_DIR_BOUNDARY = re.compile(r"\b(input|output|inout)\b")


def _dir_segments(text: str) -> "list[tuple[str, str]]":
    """Split TEXT at direction keywords: one line may hold several
    declarations (``input a, input [3:0] b, output c``).  Names can never be
    keywords, so the boundaries are unambiguous."""
    marks = list(_DIR_BOUNDARY.finditer(text))
    return [
        (
            m.group(1),
            text[m.start() : marks[k + 1].start() if k + 1 < len(marks) else len(text)],
        )
        for k, m in enumerate(marks)
    ]


# ---------------------------------------------------------------------------
# KillAutoArg


# a generated port-list region ends with a bare `);` line (anything else on
# the line means it is someone else's close paren, e.g. an instance's)
_REGION_END = re.compile(r"^\s*\);\s*$")
# a generated port-list region holds only section comments, bare names and
# blank lines; anything else (a `;`, a declaration/keyword) means the marker
# sits OUTSIDE a header (e.g. after the header's own `);`) and the scan must
# not consume real code
_NOT_PORT_LIST = re.compile(
    r";|\b(?:input|output|inout|wire|reg|logic|assign|always|module|endmodule)\b"
)


def _auto_arg_region_names(lines: Sequence[str]) -> set[str]:
    """Names currently listed in the /*autoarg*/ port region(s) — the
    baseline for the dropped-name warning (alias of
    :func:`verilog_tooling.inst.auto_arg_port_names`)."""
    from .inst import auto_arg_port_names

    return auto_arg_port_names(lines)


def kill_auto_arg(lines: Sequence[str]) -> list[str]:
    """Collapse every regenerated port list back to a ``... (/*autoarg*/);``
    stub: the marker line is closed with ``);`` and the generated lines up to
    (and including) the bare ``);`` terminator are deleted.

    A marker line already terminated by ``);`` is copied verbatim.  A marker
    whose following lines are not a plausible port list (misplaced marker,
    e.g. after the header's ``);``) or that has no bare ``);`` terminator is
    left untouched — nothing is deleted.  All other lines are copied
    unchanged.
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
            i += 1
            continue
        # find the bare `);` region terminator, bailing out on lines that
        # clearly are not generated port-list content (misplaced marker) or
        # on AUTOINPUT/AUTOOUTPUT markers (the header is AIO-owned — never
        # eat those markers as if they were a collapsed port list)
        j = i + 1
        plausible = True
        while j < n and not _REGION_END.search(lines[j]):
            if _NOT_PORT_LIST.search(lines[j]) or _AIO_MARK.search(lines[j]):
                plausible = False
                break
            j += 1
        if plausible and j < n:
            out.append(line + ");")
            i = j + 1
        else:
            out.append(line)  # misplaced/unterminated marker: keep everything
            i += 1
    return out


# ---------------------------------------------------------------------------
# s:Filter (comment / function / task / endmodule handling)


def _filter_lines(lines: Sequence[str]) -> list[str]:
    """Drop comments and subprogram bodies, stop at ``endmodule``.

    Comments are blanked up front by the shared state-machine masker
    (:mod:`verilog_tooling.comments`) — correct for ``//`` inside ``/* */``,
    ``/*`` inside ``//``, inline one-line blocks and comment markers inside
    strings.  Function (plus, as a deviation, task) bodies are skipped whole.
    """
    from .comments import mask_comments

    lines = mask_comments("\n".join(lines)).split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _FUNCTION_OPEN.match(line) or _TASK_OPEN.match(line):
            closer = _FUNCTION_CLOSE if _FUNCTION_OPEN.match(line) else _TASK_CLOSE
            i += 1
            while i < n and not closer.match(lines[i]):
                i += 1
            continue
        if _ENDMODULE.match(line):
            out.append(line)
            break
        line = _FUNCTION_CLOSE.sub("", line)
        i += 1
        if line.strip():
            out.append(line)
    return out


def _collect_ports(lines: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """(inputs, outputs, inouts) signal texts, in buffer order.

    One signal per declaration (a comma-separated declaration stays one
    entry, as in the Vim original); the direction, data-type and packed-width
    prefix is stripped, so a vector port contributes its bare name.
    Robustness: a line may hold several direction groups (``input a, input
    [3:0] b, output c``, including the whole header on one line); a
    declaration split across lines (``input [7:0]`` newline ``din,``) is
    joined first; unpacked dimensions after the name (``val[3:0]`` /
    ``val [3:0]``) are dropped.
    """
    inputs: list[str] = []
    outputs: list[str] = []
    inouts: list[str] = []
    buckets = {"input": inputs, "output": outputs, "inout": inouts}
    i = 0
    n = len(lines)
    while i < n:
        segs = _dir_segments(lines[i])
        if not segs:
            i += 1
            continue
        # join following lines while the LAST segment is unfinished:
        # either it still has no name (``input [7:0]`` newline ``din,``)
        # or its name list ends with a comma that wraps onto the next
        # line (``output wire o_a, o_b,`` newline ``o_c, o_d``).
        # after a join the segment text is re-split — it may itself carry
        # more direction groups, and the new last segment may continue the
        # chain.
        while i + 1 < n and _seg_unfinished(segs[-1][1]):
            if not _joinable_continuation(segs[-1][1], lines[i + 1]):
                break
            i += 1
            merged = segs[-1][1].rstrip() + " " + lines[i].strip()
            segs = segs[:-1] + _dir_segments(merged)
        for direction, seg in segs:
            name = _PORT_PREFIX.sub("", seg)
            first = re.match(r"[A-Za-z_][\w$]*", name.strip())
            if first and first.group(0) not in _UDT_STRIP_EXEMPT:
                name = _UDT_PREFIX.sub("", name, count=1)
            name = _PORT_TAIL.sub("", name)  # body style: `input [7:0] a, b; // c`
            name = _PORT_TRAIL_COMMA.sub("", name)  # header style: `input clk,`
            name = _PORT_TRAIL_PAREN.sub("", name)  # single-line header: `input b)`
            name = _UNPACKED_TAIL.sub("", name)  # `val[3:0]` / `val [3:0]`
            name = name.strip(" ,\t")
            if name:
                buckets[direction].append(name)
        i += 1
    return inputs, outputs, inouts


def _seg_unfinished(text: str) -> bool:
    """A direction-segment's statement is unfinished: either it carries
    no name yet (a bare prefix split across lines) or its name list ends
    with a comma that wraps onto the next line."""
    s = text.rstrip()
    if not s:
        return False
    if s.endswith(","):
        return True
    return not _PORT_PREFIX.sub("", s.rstrip(",)").rstrip()).strip()


_STRUCTURAL_WORDS = {
    "always", "always_comb", "always_ff", "always_latch", "and", "assert",
    "assign", "assume", "begin", "bind", "buf", "bufif0", "bufif1", "case",
    "casex", "casez", "checker", "class", "clocking", "cmos", "config",
    "cover", "defparam", "else", "end", "endcase", "endchecker", "endclass",
    "endclocking", "endconfig", "endfunction", "endgenerate", "endinterface",
    "endmodule", "endpackage", "endprimitive", "endprogram", "endproperty",
    "endsequence", "endtable", "endtask", "enum", "final", "for", "foreach",
    "fork", "function", "generate", "genvar", "if", "import", "initial",
    "interface", "localparam", "module", "nand", "nmos", "nor", "not",
    "notif0", "notif1", "or", "package", "parameter", "pmos", "primitive",
    "program", "property", "pulldown", "pullup", "rcmos", "return", "rnmos",
    "rpmos", "rtran", "rtranif0", "rtranif1", "sequence", "struct", "task",
    "tran", "tranif0", "tranif1", "typedef", "union", "while", "xor", "xnor",
}

_TYPE_STARTERS = {
    "bit", "genvar", "int", "integer", "logic", "reg", "signed", "supply0",
    "supply1", "time", "tri", "tri0", "tri1", "unsigned", "var", "wire",
}


def _joinable_continuation(cur_text: str, next_line: str) -> bool:
    """Whether NEXT_LINE can continue an unfinished direction segment.

    A wrapped ANSI/body declaration continues with a bare name, a packed
    range, or a type-led following port item.  It never continues into a
    preprocessor directive, the header close, a new direction statement,
    or a structural statement.  This matters for conditional port groups
    such as ibex's RVFI ports: the line after ``output logic x,`` in the
    comment-filtered view is `` `ifdef RVFI``; joining it would turn the
    directive into a bogus port-name entry and orphan the real ports.
    """
    nxt = next_line.strip()
    if not nxt or nxt.startswith(("`", ")")):
        return False
    if _dir_segments(next_line):
        return False
    if nxt.startswith("["):
        return True
    first = re.match(r"[A-Za-z_][\w$]*", nxt)
    if first is None:
        return True
    word = first.group(0)
    if word in _STRUCTURAL_WORDS:
        return False
    has_name = bool(_PORT_PREFIX.sub("", cur_text.rstrip().rstrip(",)").rstrip()).strip())
    if has_name and word in _TYPE_STARTERS and ";" in nxt:
        return False
    return True


# ---------------------------------------------------------------------------
# AutoArg emission


def _consume_ansi_header(lines: Sequence[str]) -> "tuple[list[str], list[str]]":
    """Move ANSI io declarations of a /*autoarg*/ header into the body.

    verilog-mode/autoarg house style is 1995: the header holds only the
    regenerated name list.  When the io declarations live INSIDE the parens
    (``module m (/*autoarg*/ input wire a, output wire [3:0] b);``) they are
    consumed and returned as semicolon-terminated body declarations
    (interleaved comments/blanks move with them); otherwise the marker's
    regenerated ``);`` would leave them dangling after the header — an
    ``output wire a,`` with no ``;`` is a syntax error.  Returns
    (new_lines, body_decls); no ANSI declarations -> (lines, [])."""
    mk = next((k for k, ln in enumerate(lines) if _MARK_LINE.search(ln)), None)
    if mk is None:
        return list(lines), []
    m = _MARK.search(lines[mk])
    # header close: the line holding the ')' that closes the port list the
    # marker sits in (comment-masked paren scan)
    from .comments import mask_comments

    text = "\n".join(lines)
    masked = mask_comments(text)
    mstart = sum(len(ln) + 1 for ln in lines[:mk]) + m.start()
    depth = 0
    for ch in masked[:mstart]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    if depth < 1:
        return list(lines), []  # misplaced marker (not inside a port list)
    target = depth - 1
    hc = None
    i = mstart
    while i < len(masked):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == target:
                hc = text.count("\n", 0, i)
                break
        i += 1
    if hc is None:
        return list(lines), []
    cend = lines[mk].find("*/", m.end())
    cend = len(lines[mk]) if cend < 0 else cend + 2
    pieces: list[tuple[int, str]] = []
    if lines[mk][cend:].strip():
        pieces.append((mk, lines[mk][cend:]))
    pieces.extend((k, lines[k]) for k in range(mk + 1, hc + 1))
    if not any(re.match(r"\s*(?:input|output|inout)\b", t) for _, t in pieces):
        return list(lines), []
    if any(_PREPROC_LINE.search(t) for _, t in pieces):
        # Conditional port groups cannot be represented by the regenerated
        # name list without moving directives or losing their association
        # with the gated declarations.  Leave such headers untouched.
        return list(lines), []

    decls: list[str] = []
    keep: dict[int, str] = {}
    cur: list[str] = []
    cur_indent = ""

    def flush() -> None:
        stmt = " ".join(cur)
        cur.clear()
        stmt, _, comment = stmt.partition("//")
        stmt = re.sub(r"[,\s]+$", "", stmt)
        stmt = re.sub(r"\)\s*;\s*$|\)+\s*$", "", stmt)
        stmt = re.sub(r"[,\s]+$", "", stmt)
        segs = _dir_segments(stmt)
        for _, seg in segs or [("", stmt)]:
            seg = seg.strip(" ,\t")
            if seg:
                decls.append(
                    cur_indent + seg + ";" + ("  // " + comment.strip() if comment else "")
                )

    def complete(stmt: str) -> bool:
        """The statement carries a name (not just a direction/width prefix)
        and its name list does not wrap: a trailing comma means more names
        follow on the next line (``output wire a,`` newline ``b``)."""
        last = _dir_segments(stmt)
        if not last:
            return False
        tail = last[-1][1].partition("//")[0].rstrip()
        if tail.endswith(","):
            return False
        tail = tail.rstrip(")").rstrip()
        return bool(_PORT_PREFIX.sub("", tail).strip())

    for k, piece in pieces:
        stripped = piece.strip()
        if not stripped or stripped.startswith("//"):
            # Comments/blanks move with the declarations.  Settle any
            # pending statement first: a comment line is a hard boundary,
            # so text after an inline comment can never be absorbed into
            # the previous declaration's comment by flush().
            if cur:
                flush()
            decls.append(piece.rstrip())
            continue
        if stripped.startswith("`"):
            if cur:
                flush()
            keep[k] = piece
            continue
        if re.match(r"^\s*\)\s*;?\s*$", piece):
            if cur:
                flush()
            continue  # the header's own close: _expand_arg_markers re-adds it
        if re.match(r"\s*(?:input|output|inout)\b", piece):
            # A new direction statement settles the previous one.  Merely
            # appending it to the pending statement makes flush() take the
            # first ``//`` in the combined text as the comment delimiter,
            # which silently swallows the following declaration.
            if cur:
                flush()
            cur_indent = re.match(r"\s*", piece).group(0)
            cur.append(piece.strip())
            if complete(piece):
                flush()
            continue
        if cur:
            cur.append(piece.strip())
            if complete(" ".join(cur)):
                flush()
            continue
        keep[k] = piece  # foreign header content (e.g. a bare name): stay
    if cur:
        flush()
    out: list[str] = []
    for k, ln in enumerate(lines):
        if k == mk:
            out.append(ln[:cend])
        elif mk < k <= hc and k not in keep:
            continue  # consumed into the body declarations
        else:
            out.append(ln)
    return out, decls


def _local_arg_sort(lines: Sequence[str]) -> bool:
    """verilog-auto-arg-sort file-local (default nil): non-nil sorts AUTOARG
    signal names instead of using declaration order."""
    m = re.search(r"^\s*//\s*verilog-auto-arg-sort\s*:\s*(\w+)", "\n".join(lines), re.M)
    return bool(m) and m.group(1).lower() != "nil"


def _local_arg_format(lines: Sequence[str]) -> str:
    """verilog-auto-arg-format file-local (default packed): 'single puts one
    signal per line, 'packed fills lines up to the column limit."""
    m = re.search(r"^\s*//\s*verilog-auto-arg-format\s*:\s*(\w+)", "\n".join(lines), re.M)
    if m and m.group(1).lower() == "single":
        return "single"
    return "packed"


def _split_top_level_commas(text: str) -> list[str]:
    """Split TEXT on commas that are not nested in (), [] or {}."""
    parts: list[str] = []
    start = 0
    par = bracket = brace = 0
    for idx, ch in enumerate(text):
        if ch == "(":
            par += 1
        elif ch == ")":
            par = max(0, par - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace = max(0, brace - 1)
        elif ch == "," and par == bracket == brace == 0:
            parts.append(text[start:idx])
            start = idx + 1
    parts.append(text[start:])
    return parts


def _bare_port_names(signal_text: str) -> list[str]:
    """Bare port names from a collected declaration, for a converted ANSI
    header.

    A regenerated 1995-style header is a name list: directions, types,
    packed ranges and unpacked dimensions belong to the body declaration
    emitted for the same port.  Leaving them in the header makes Verilator
    report ranges in a port list as unsupported.  Body-declared AUTOARG
    lists keep their historical text; this helper is only applied when
    ``_consume_ansi_header`` converted the header, or when the existing
    region already lists bare names (the converted shape, kept stable
    across re-runs).
    """
    names: list[str] = []
    for item in _split_top_level_commas(signal_text):
        item = item.strip().strip(";").strip()
        if not item:
            continue
        item = item.split("=", 1)[0].strip()  # drop any default value
        item = re.sub(r"\[[^\]]*\]", " ", item).strip(" ,)")
        if not item:
            continue
        # Drop leading type words (possibly several: ``signed logic`` or a
        # pkg-scoped UDT) until only the name remains.  A single remaining
        # identifier is the name itself and is not stripped.
        while re.search(r"\s+[A-Za-z_]", item):
            item = re.sub(
                r"^[A-Za-z_][\w$]*(?:::[A-Za-z_][\w$]*)*"
                r"(?:\.[A-Za-z_][\w$]*)?\s+",
                "",
                item,
                count=1,
            ).strip()
        m = re.search(r"[A-Za-z_][\w$]*", item)
        if m:
            names.append(m.group(0))
    return names


def _header_region_is_bare_name_list(lines: Sequence[str]) -> bool:
    """Whether the existing /*autoarg*/ region already lists bare names
    only (no direction/type keyword, no range).

    This is the shape a converted ANSI header is left in, so recognising
    it keeps a later run's list bare even though the moved body
    declarations still carry their types.  A stub header (``/*autoarg*/);``)
    or a region carrying typed entries returns False, preserving the
    historical body-declaration behaviour locked by the Vim-parity tests.
    """
    mk = next((k for k, ln in enumerate(lines) if _MARK_LINE.search(ln)), None)
    if mk is None:
        return False
    end = next(
        (k for k in range(mk, len(lines)) if _REGION_END.search(lines[k])), None
    )
    if end is None:
        return False
    keyword = re.compile(
        r"\b(?:input|output|inout|logic|signed|unsigned|wire|reg|bit|int|"
        r"integer|time|var|genvar|tri|tri0|tri1|supply0|supply1|parameter|"
        r"localparam)\b|\["
    )
    names = 0
    for idx in range(mk, end + 1):
        ln = lines[idx]
        if idx == mk:
            # only what follows the marker comment is region content; the
            # module keyword and module name must not count as port names
            ln = ln.split("*/", 1)[1] if "*/" in ln else ""
        text = re.sub(r"/\*.*?\*/", "", ln.split("//", 1)[0])
        if keyword.search(text):
            return False
        names += len(re.findall(r"[A-Za-z_][\w$]*", text))
    return names > 0


def _autoarg_header_has_preproc(lines: Sequence[str]) -> bool:
    """Whether a preprocessor directive occurs between an /*autoarg*/
    marker and the header's bare ``);`` close."""
    mk = next((k for k, ln in enumerate(lines) if _MARK_LINE.search(ln)), None)
    if mk is None:
        return False
    end = next(
        (k for k in range(mk, len(lines)) if _REGION_END.search(lines[k])),
        len(lines) - 1,
    )
    return any(_PREPROC_LINE.search(ln) for ln in lines[mk : end + 1])


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


def auto_arg(lines: Sequence[str], modules: "Mapping[str, ModuleDef] | None" = None) -> list[str]:
    """Regenerate the ``/*autoarg*/`` port list in every module header.

    The previous expansion is collapsed first (so re-running is idempotent);
    each marker line is then truncated at its first ``)`` and followed by the
    Inputs/Outputs/Inouts sections of the declarations found in THAT MODULE's
    body (the buffer may hold several modules), separated by blank lines, and
    finally by ``);``.  Lines outside the marker region are copied verbatim.
    A section with no signal is omitted entirely.

    A name in the old list that has no ``input``/``output``/``inout``
    declaration (e.g. only ``wire`` — not a legal port: verilator reports
    "Pin is not an in/out/inout/interface") is dropped, with a warning per
    name — EXCEPT the inout inference: with MODULES given, a direction-less
    port-list name connected to an inout pin of an instantiated module (a
    pad, e.g. ``.GPIO0_A00 (GPIO0_A00)``) is KEPT in the //Inouts section
    and an ``inout`` body declaration is emitted for it, making the port
    legal.
    """
    old_names = _auto_arg_region_names(lines)
    inout_nets: dict[str, str] = {}
    if modules:
        from .wire import _inst_driven_nets

        for name, net in _inst_driven_nets(lines, modules, ("inout",), simple_only=True).items():
            inout_nets[name] = net.width
    # Per-module header facts must be read before kill_auto_arg collapses
    # the generated regions; the collapse deletes region lines but never
    # a module line, so modules keep their order and can be matched by
    # ordinal below.  Skipped modules must re-emit their ORIGINAL span:
    # after a collapse the region is already gone, and copying the
    # collapsed body through would delete the port list.
    bare_by_module: list[bool] = []
    skip_by_module: list[bool] = []
    orig_spans: list[list[str]] = []
    _k = 0
    while _k < len(lines):
        if re.match(r"\s*module\b", lines[_k]):
            _j = _k
            while _j < len(lines) and not _ENDMODULE.match(lines[_j]):
                _j += 1
            _span = list(lines[_k : _j + 1])
            orig_spans.append(_span)
            bare_by_module.append(_header_region_is_bare_name_list(_span))
            skip_by_module.append(_autoarg_header_has_preproc(_span))
            _k = _j + 1
        else:
            _k += 1
    lines = kill_auto_arg(lines)
    out: list[str] = []
    i = 0
    mod_idx = -1
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
        mod_idx += 1
        close_idx = next((k for k, ln in enumerate(body) if _CLOSE.search(ln)), None)
        if close_idx is not None and any(
            _AIO_MARK.search(ln) for ln in body[: close_idx + 1]
        ):
            # the header carries AUTOINPUT/AUTOOUTPUT markers: those regions
            # own the port declarations — a packed /*autoarg*/ name list
            # would duplicate them (and its `);` would split the header).
            # The canonical combo is /*autoarg*/ in the header with the
            # AIO markers in the body; leave this header alone.
            out.extend(body)
            i = j
            continue
        if mod_idx < len(skip_by_module) and skip_by_module[mod_idx]:
            # A name-list regeneration cannot preserve conditional port
            # groups: it would have to move the directives or drop the
            # association between a directive and the ports it gates.
            # Leaving the header untouched is strictly better than turning
            # `` `ifdef``/`` `endif`` into bogus port-name entries.  Emit
            # the pre-collapse span so a re-run cannot lose the list.
            import sys

            orig = orig_spans[mod_idx]
            mname = _MODULE_NAME.search("\n".join(orig[:4]))
            label = f"module {mname.group(1)}: " if mname else ""
            print(
                "warning: autoarg: "
                f"{label}preprocessor directives in the /*autoarg*/ "
                "header; module left unchanged",
                file=sys.stderr,
            )
            out.extend(orig)
            i = j
            continue
        inputs, outputs, inouts = _collect_ports(_filter_lines(body))
        # verilog-auto-arg-sort: declaration order is the default; sorted
        # order reduces churn when declarations move around
        if _local_arg_sort(lines):
            inputs, outputs, inouts = sorted(inputs), sorted(outputs), sorted(inouts)
        declared = set(inputs) | set(outputs) | set(inouts)
        infer = sorted(
            name for name in old_names if name in inout_nets and name not in declared
        )
        # ANSI io declarations in the header move to the body (1995 style):
        # the regenerated name list would otherwise leave them dangling
        keep_bare = mod_idx < len(bare_by_module) and bare_by_module[mod_idx]
        body, ansi_decls = _consume_ansi_header(body)
        if ansi_decls or keep_bare:
            # The header now holds only the port list, so its entries must
            # be bare names: a typed/ranged entry duplicated in the header
            # and the emitted body declaration is what Verilator rejects
            # as ranges in a port list.
            inputs = [name for entry in inputs for name in _bare_port_names(entry)]
            outputs = [name for entry in outputs for name in _bare_port_names(entry)]
            inouts = [name for entry in inouts for name in _bare_port_names(entry)]
            declared = set(inputs) | set(outputs) | set(inouts)
            infer = [name for name in infer if name not in declared]
            if _local_arg_sort(lines):
                inputs, outputs, inouts = sorted(inputs), sorted(outputs), sorted(inouts)
        expanded = _expand_arg_markers(
            body, inputs, outputs, inouts + infer,
            arg_format=_local_arg_format(lines),
        )
        if ansi_decls:
            ins = next(
                (k for k, ln in enumerate(expanded) if re.match(r"\s*\);\s*$", ln)),
                0,
            ) + 1
            expanded[ins:ins] = ansi_decls
        if infer:
            # inferred inout declarations go after the module's last inout
            # declaration (the port-declaration area): on the next run the
            # collected inouts keep this exact order, so the section is
            # byte-stable across runs.  With no prior inout decl, they go
            # right after the header close (never past endmodule).
            decls = _emit_inout_decls(infer, inout_nets)
            ins = next(
                (k for k, ln in enumerate(expanded) if re.match(r"\s*\);\s*$", ln)),
                len(expanded) - 1,
            ) + 1
            for k in range(len(expanded) - 1, -1, -1):
                if re.match(r"\s*inout\b", expanded[k]):
                    ins = k + 1
                    break
            expanded[ins:ins] = decls
        out.extend(expanded)
        i = j
    dropped = sorted(old_names - _auto_arg_region_names(out))
    if dropped:
        import sys

        print(
            f"warning: autoarg: {len(dropped)} name(s) dropped from the port "
            f"list (no input/output/inout declaration): {dropped}",
            file=sys.stderr,
        )
    return out


def _emit_inout_decls(
    names: Sequence[str], inout_nets: Mapping[str, str]
) -> list[str]:
    """1995-style body declarations for inferred inout ports, in the autodef
    house style.  ``inout wire`` marks the port fully defined (a bare
    ``inout`` would earn a supplementary wire from autodef)."""
    from .autodef import Signal, _emit_signal, _sig_decl_len

    sigs = [
        Signal(width=inout_nets.get(name) or "c0", type="io_inout", name=name)
        for name in names
    ]
    max_len = 39
    for sig in sigs:
        if sig.width != "c0":
            max_len = max(max_len, _sig_decl_len(sig))
    return [_emit_signal(sig, max_len, "inout wire") for sig in sigs]


def _single_ports(ports: Sequence[str], *, trailing_comma: bool) -> list[str]:
    """One port per line (verilog-auto-arg-format 'single)."""
    out = [_ARG_MARGIN + name + "," for name in ports[:-1]]
    last = _ARG_MARGIN + ports[-1]
    if trailing_comma:
        last += ","
    out.append(last)
    return out


def _expand_arg_markers(
    lines: Sequence[str],
    inputs: Sequence[str],
    outputs: Sequence[str],
    inouts: Sequence[str],
    *,
    arg_format: str = "packed",
) -> list[str]:
    """Expand every /*autoarg*/ marker in one module's LINES with the given
    Inputs/Outputs/Inouts sections.

    Only markers inside the header (at or before the module's first ``);``
    line) are expanded; a marker after the header close is misplaced and is
    left verbatim."""
    # emacs verilog-auto-arg order: Outputs, Inouts, Inputs
    sections = (("//Outputs", outputs), ("//Inouts", inouts), ("//Inputs", inputs))
    close_idx = next((k for k, ln in enumerate(lines) if _CLOSE.search(ln)), None)
    out: list[str] = []
    for idx, line in enumerate(lines):
        if not _MARK_LINE.search(line):
            out.append(line)
            continue
        if close_idx is not None and idx > close_idx:
            out.append(line)  # misplaced marker (after the header): leave alone
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
            if arg_format == "single":
                out.extend(_single_ports(ports, trailing_comma=later))
            else:
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
    parser.add_argument(
        "-y",
        "--libdir",
        action="append",
        default=[],
        help="library dir holding <module>.v/.sv (repeatable; used by the "
        "inout inference to read instance port directions)",
    )
    parser.add_argument(
        "-I",
        "--interface",
        action="append",
        default=[],
        help="user-known SystemVerilog interface type name (repeatable)",
    )
    parser.add_argument(
        "--ref_file",
        default=None,
        help="real source file anchoring Local-Variables relative paths (default: in_file)",
    )
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    from .inst import (
        _cli_resolve,
        _resolve_module_files,
        buffer_module_defs,
        _module_lines,
        find_interfaces,
        parse_module_ports,
    )

    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    if args.command == "kill":
        out = kill_auto_arg(lines)
    else:
        # resolve instance modules for the inout inference (best effort —
        # without them the inference is simply inactive)
        modules = {}
        names = set()
        buf = VerilogBuffer(lines)
        for idx in buf.markers():
            try:
                names.add(buf.resolve_instance(idx)[0])
            except ValueError:
                pass
        if names:
            libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
            files = _resolve_module_files(names, libdirs, inst_files, vc_entries, extensions)
            buffer_mods = buffer_module_defs("\n".join(lines))
            from .libdirs import parse_typedef_regexp

            td_re = parse_typedef_regexp(lines)
            interfaces = set(find_interfaces(libdirs)) | set(args.interface)
            for name in names:
                src = _module_lines(name, files, buffer_mods)
                if src is not None:
                    modules[name] = parse_module_ports(
                        src, typedef_regexp=td_re, interfaces=interfaces
                    )
        out = auto_arg(lines, modules)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
