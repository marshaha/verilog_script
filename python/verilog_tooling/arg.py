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
_AIO_MARK = re.compile(r"/\*\s*\b(?:autoinput|autooutput)\b", re.IGNORECASE)
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
        # join following lines while the LAST segment still has no name
        # (after a join the segment text is re-split — it may itself carry
        # more direction groups, and the new last segment may continue the
        # chain)
        while i + 1 < n and not _PORT_PREFIX.sub(
            "", segs[-1][1].rstrip().rstrip(",").rstrip()
        ).strip():
            i += 1
            merged = segs[-1][1].rstrip().rstrip(",").rstrip() + " " + lines[i].strip()
            segs = segs[:-1] + _dir_segments(merged)
        for direction, seg in segs:
            name = _PORT_PREFIX.sub("", seg)
            name = _PORT_TAIL.sub("", name)  # body style: `input [7:0] a, b; // c`
            name = _PORT_TRAIL_COMMA.sub("", name)  # header style: `input clk,`
            name = _PORT_TRAIL_PAREN.sub("", name)  # single-line header: `input b)`
            name = _UNPACKED_TAIL.sub("", name)  # `val[3:0]` / `val [3:0]`
            name = name.strip(" ,\t")
            if name:
                buckets[direction].append(name)
        i += 1
    return inputs, outputs, inouts


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
        """The statement carries a name (not just a direction/width prefix)."""
        last = _dir_segments(stmt)
        if not last:
            return False
        tail = last[-1][1].rstrip().rstrip(",").rstrip(")").rstrip()
        return bool(_PORT_PREFIX.sub("", tail).strip())

    for k, piece in pieces:
        if cur:
            cur.append(piece.strip())
            if complete(" ".join(cur)):
                flush()
            continue
        if re.match(r"\s*(?:input|output|inout)\b", piece):
            cur_indent = re.match(r"\s*", piece).group(0)
            cur.append(piece.strip())
            if complete(piece):
                flush()
        elif re.match(r"^\s*\)\s*;?\s*$", piece):
            pass  # the header's own close: _expand_arg_markers re-adds it
        elif not piece.strip() or piece.strip().startswith("//"):
            decls.append(piece.rstrip())  # comments/blanks move with the decls
        else:
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
        body, ansi_decls = _consume_ansi_header(body)
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
