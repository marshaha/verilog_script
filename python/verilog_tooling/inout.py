"""Python rewrite of verilog-mode's AUTOINPUT / AUTOOUTPUT declarations.

Implements ``verilog-auto-input`` and ``verilog-auto-output``
(verilog-mode.el ~lines 13482/13334):

- ``/*AUTOINPUT*/`` declares an ``input`` port for every net connected to an
  input port of an /*autoinst*/ instance that is not otherwise declared in
  the module (port, wire/reg/usrdef, parameter, `define) and not driven by
  an instance output/inout.  Each declaration carries a trailing
  ``// To {inst} of {module}.v`` comment.
- ``/*AUTOOUTPUT*/`` declares an ``output`` port for every net connected to
  an output port of an /*autoinst*/ instance that is not already a port of
  this module and does not feed an instance input/inout (those stay internal
  and belong to /*AUTOWIRE*/).  Comment: ``// From {inst} of {module}.v``.

Both regions end with the shared ``// End of automatics`` closer; the
headers are verilog-mode's::

    // Beginning of automatic inputs (from unused autoinst inputs)
    // Beginning of automatic outputs (from unused autoinst outputs)

Marker regexp argument (``/*AUTOINPUT("^i_")*/``, ``?!`` inverts) and the
``verilog-auto-input-ignore-regexp`` / ``verilog-auto-output-ignore-regexp``
file-local variables are honoured, as in verilog-mode.  Each marker expands
its own region (two markers both matching a signal duplicate the
declaration — the user's responsibility, as in emacs).

Placement (``verilog-in-paren-quick``):

- inside the module header parens the declarations are Verilog-2001 style:
  every line ends with ``,`` and the open/close comma repairs of
  verilog-mode run (a comma is added after a preceding port unless it
  follows ``(``, ``,``, ``*)`` or a `define; a dangling comma before the
  header's ``)`` is removed);
- in the module body the declarations are Verilog-1995 style (``;``).

Fidelity decisions vs verilog-mode:

- the declaration body uses the autodef arithmetic of
  :mod:`verilog_tooling.wire` (type field padded to 12, name column floor
  39, comment at column max(48, indent+40)) instead of verilog-mode's
  ``indent-to (max 24 (+ indent-pt 16))`` — consistent with our
  AUTOWIRE/AUTOREG blocks;
- the width comes from the submodule PORT (with the instance's ``#(...)``
  parameter overrides substituted), not from the connection's bit-select —
  verilog-mode only gets widths right because AUTOINST rewrites connections
  as ``net[width]``; ours also works for hand-written connections;
- a net driven by a continuous ``assign`` is NOT made an input (verilog-mode
  would — an input port driven inside the module does not compile);
- a marker immediately after the header ``(`` expands normally —
  verilog-mode's ``verilog-read-auto-params`` errors out there
  ("Mismatching ()") because its backward-sexp hits the open paren;
- nets whose port carries unpacked dimensions are skipped (cannot be
  re-declared from the connection);
- AUTOOUTPUT keeps verilog-mode's exclusion set exactly: this module's
  ports plus instance input/inout nets — a hand-declared wire that is an
  instance output and feeds nothing still becomes an output port.

Configuration (file-local variables in the ``// Local Variables:``
section):

- ``verilog-auto-ignore-concat`` — non-nil (our default; emacs defaults to
  nil) skips pin connections in ``{...}`` or ``(...)``; nil extracts their
  identifiers instead (nested concats/unary operators/casts stripped, an
  element keeps its own ``[msb:lsb]`` width or is scalar).  Our default
  matches the common workflow of wrapping a signal in {} precisely to
  exempt it from AUTOINPUT/AUTOOUTPUT;
- ``verilog-auto-wire-comment`` — nil suppresses the ``// To``/``// From``
  comments on the generated declarations.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Mapping, Sequence

from .autodef import (
    Signal,
    _emit_signal,
    get_all_defs,
    get_all_paras,
)
from .comments import mask_comments
from .inst import ModuleDef, VerilogBuffer, parse_module_ports
from .wire import (
    _COMMENT_COL,
    _inst_driven_nets,
    _kill_region,
    _module_tables,
    _name_col_max,
)

_INPUT_HEADER = "// Beginning of automatic inputs (from unused autoinst inputs)"
_OUTPUT_HEADER = "// Beginning of automatic outputs (from unused autoinst outputs)"
_END_OF_AUTOMATICS = "// End of automatics"
_INPUT_HEADER_RE = re.compile(r"^\s*// Beginning of automatic inputs\b")
# not "(every signal)": an AUTOOUTPUTEVERY region is not ours to delete
_OUTPUT_HEADER_RE = re.compile(
    r"^\s*// Beginning of automatic outputs\b(?!\s*\(every signal\b)"
)

_IGNORE_RE = {
    "input": "verilog-auto-input-ignore-regexp",
    "output": "verilog-auto-output-ignore-regexp",
}


# ---------------------------------------------------------------------------
# kill (verilog-delete-auto-buffer for these two regions)


def kill_auto_input(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOINPUT*/ region, keeping the marker."""
    return _kill_region(lines, _INPUT_HEADER_RE)


def kill_auto_output(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOOUTPUT*/ region, keeping the marker."""
    return _kill_region(lines, _OUTPUT_HEADER_RE)


# ---------------------------------------------------------------------------
# signal sets


def _local_syms(lines: Sequence[str]) -> set[str]:
    from .autodef import _const_symbols

    return set(get_all_paras(lines)) | set(_const_symbols(lines))


def _widths_ok(net, local_syms: set[str]) -> bool:
    """A declaration compiles only when every width/dim names local symbols
    (same rule as AUTOWIRE)."""
    from .autodef import _width_syms_known

    if net.unpacked_dims:
        return False
    if net.packed_dims:
        return all(_width_syms_known(d, local_syms) for d in net.packed_dims)
    return net.width in ("", "c0") or _width_syms_known(net.width, local_syms)


def _declared_port_names(lines: Sequence[str]) -> set[str]:
    """Every port name of the buffer's own modules — parsed with the real
    module-header parser, so ANSI single-line headers
    (``module m (input a, output b);``) count too."""
    from .inst import buffer_module_defs

    names: set[str] = set()
    for mod_lines in buffer_module_defs("\n".join(lines)).values():
        for port in parse_module_ports(mod_lines).ports:
            if port.direction != "interface":
                names.add(port.name)
    return names


def _input_sigs(
    lines: Sequence[str], modules: Mapping[str, ModuleDef]
) -> "tuple[list[Signal], dict[str, str]]":
    """AUTOINPUT candidates: nets feeding instance INPUT ports, minus
    everything declared or driven inside the module."""
    in_nets = _inst_driven_nets(lines, modules, ("input",), simple_only=True)
    driven = set(_inst_driven_nets(lines, modules))
    ports, usrdef, assigns = _module_tables(lines)
    declared = set(ports.signals) | _declared_port_names(lines)
    declared |= set(usrdef.signals) | assigns | driven
    declared |= get_all_defs(lines) | get_all_paras(lines)
    from .libdirs import parse_typedef_regexp
    from .inst import set_typedef_regexp

    typedef_re = parse_typedef_regexp(lines)
    set_typedef_regexp(typedef_re)
    local_syms = _local_syms(lines)
    sigs: list[Signal] = []
    comments: dict[str, str] = {}
    for name in sorted(in_nets):
        if name in declared:
            continue
        if typedef_re and re.search(typedef_re, name):
            continue
        net = in_nets[name]
        if not _widths_ok(net, local_syms):
            continue
        sigs.append(
            Signal(width=net.width, type="io_input", name=name, packed_dims=net.packed_dims)
        )
        comments[name] = f"// To {net.inst} of {net.module}.v"
    return sigs, comments


def _output_sigs(
    lines: Sequence[str], modules: Mapping[str, ModuleDef]
) -> "tuple[list[Signal], dict[str, str]]":
    """AUTOOUTPUT candidates: nets driven by instance OUTPUT ports, minus
    this module's ports and nets feeding an instance input/inout (those are
    internal — /*AUTOWIRE*/ territory).  Declared wires/params are NOT
    excluded, exactly as verilog-mode."""
    out_nets = _inst_driven_nets(lines, modules, ("output",), simple_only=True)
    in_nets = _inst_driven_nets(lines, modules, ("input",), simple_only=True)
    inout_nets = _inst_driven_nets(lines, modules, ("inout",), simple_only=True)
    ports, _, _ = _module_tables(lines)
    excluded = set(ports.signals) | _declared_port_names(lines)
    excluded |= set(in_nets) | set(inout_nets)
    from .libdirs import parse_typedef_regexp
    from .inst import set_typedef_regexp

    typedef_re = parse_typedef_regexp(lines)
    set_typedef_regexp(typedef_re)
    local_syms = _local_syms(lines)
    sigs: list[Signal] = []
    comments: dict[str, str] = {}
    for name in sorted(out_nets):
        if name in excluded:
            continue
        if typedef_re and re.search(typedef_re, name):
            continue
        net = out_nets[name]
        if not _widths_ok(net, local_syms):
            continue
        sigs.append(
            Signal(width=net.width, type="io_output", name=name, packed_dims=net.packed_dims)
        )
        comments[name] = f"// From {net.inst} of {net.module}.v"
    return sigs, comments


# ---------------------------------------------------------------------------
# emission


def _emit_io_line(sig: Signal, max_len: int, keyword: str, indent: int, v2k: bool, comment: str) -> str:
    body = _emit_signal(sig, max_len, keyword)[:-1] + ("," if v2k else ";")
    line = " " * indent + body
    if comment:
        col = max(_COMMENT_COL, indent + 40)
        line += " " * max(col - len(line), 1) + comment
    return line


def _in_paren(masked: str, offset: int) -> bool:
    """verilog-in-paren-quick: OFFSET sits inside an unclosed paren (the
    module header's, for a well-formed buffer)."""
    depth = 0
    for ch in masked[:offset]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth > 0


def _repair_open_comma(text: str, masked: str, offset: int) -> "tuple[str, str, int]":
    """verilog-repair-open-comma: insert ``,`` after the code preceding the
    marker unless it is ``(``, ``,``, ``*)`` or a backtick token.  Returns
    (text, masked, shift) — SHIFT is +1 when a comma was inserted."""
    j = offset - 1
    while j >= 0 and masked[j] in " \t\n":
        j -= 1
    if j < 0 or masked[j] in "(,":
        return text, masked, 0
    if masked[j] == ")" and j - 1 >= 0 and masked[j - 1] == "*":
        return text, masked, 0  # attribute close *)
    k = j
    while k >= 0 and (masked[k].isalnum() or masked[k] in "_`"):
        k -= 1
    if k + 1 <= j and masked[k + 1] == "`":
        return text, masked, 0  # `define / `endif / ...
    return text[: j + 1] + "," + text[j + 1 :], masked[: j + 1] + "," + masked[j + 1 :], 1


def _repair_close_comma(text: str, from_offset: int) -> str:
    """verilog-repair-close-comma: delete a dangling ``,`` before the close
    paren of the paren level open at FROM_OFFSET (the module header's)."""
    masked = mask_comments(text)
    i = from_offset
    depth = 0
    close = -1
    while i < len(masked):
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                close = i
                break
            depth -= 1
        i += 1
    if close < 0:
        return text
    j = close - 1
    while j >= 0 and masked[j] in " \t\n":
        j -= 1
    if j >= 0 and masked[j] == ",":
        return text[:j] + text[j + 1 :]
    return text


def _expand_marker(
    text: str,
    marker,
    header: str,
    keyword: str,
    sigs: Sequence[Signal],
    comments: Mapping[str, str],
    ignore_re: "str | None",
) -> str:
    """Expand one /*AUTOINPUT*// /*AUTOOUTPUT*/ marker (its own regexp
    filter, v2k comma repairs) in TEXT."""
    from . import emacs

    masked = mask_comments(text)
    v2k = _in_paren(masked, marker.offset)
    msigs = emacs._filter_regexp(list(sigs), marker.regexp)
    if ignore_re:
        msigs = emacs._filter_regexp(msigs, "?!" + ignore_re)
    shift = 0
    if v2k:
        text, masked, shift = _repair_open_comma(text, masked, marker.offset)
    offset = marker.offset + shift
    end = marker.end + shift
    line_start = text.rfind("\n", 0, offset) + 1
    indent = len(re.match(r"[ \t]*", text[line_start:]).group(0))
    nl = text.find("\n", end)
    if v2k:
        # the header's close paren sits on the marker line itself
        # (e.g. ``/*AUTOOUTPUT*/);``): break the line after the marker so
        # the region stays INSIDE the header — the tail (``);``) moves to
        # its own line below the region
        depth = 0
        for c in masked[end : nl if nl != -1 else len(masked)]:
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    text = text[:end] + "\n" + text[end:]
                    nl = end
                    break
                depth -= 1
    if nl == -1:
        text += "\n"
        nl = len(text) - 1
    insert_at = nl + 1
    region = ""
    if msigs:
        pad = " " * indent
        max_len = _name_col_max(msigs)
        rows = [pad + header]
        for sig in msigs:
            rows.append(
                _emit_io_line(sig, max_len, keyword, indent, v2k, comments.get(sig.name, ""))
            )
        rows.append(pad + _END_OF_AUTOMATICS)
        region = "\n".join(rows) + "\n"
        text = text[:insert_at] + region + text[insert_at:]
    if v2k:
        text = _repair_close_comma(text, insert_at + len(region))
    return text


def _regen(
    lines: Sequence[str],
    mark_keyword: str,
    header: str,
    keyword: str,
    sigs: Sequence[Signal],
    comments: Mapping[str, str],
    ignore_var: str,
) -> list[str]:
    """Expand every marker of MARK_KEYWORD (each with its own regexp
    filter); the region goes after the marker line, Verilog-2001 comma style
    inside the module header parens, ``;`` style in the body."""
    from . import emacs

    markers = emacs.find_auto_markers(lines, mark_keyword)
    if not markers:
        return list(lines)
    text = "\n".join(lines)
    m = re.search(
        r'^\s*//\s*' + ignore_var + r'\s*:\s*"([^"]+)"', text, re.M
    )
    ignore_re = m.group(1) if m else None
    for marker in reversed(markers):
        text = _expand_marker(text, marker, header, keyword, sigs, comments, ignore_re)
    return text.split("\n")


# ---------------------------------------------------------------------------
# AUTOINPUT / AUTOOUTPUT (verilog-auto-input / verilog-auto-output)


def auto_input(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate the /*AUTOINPUT*/ input declarations of a module.

    Declares an input port for every net feeding an input port of an
    /*autoinst*/ instance that is not declared or driven inside the module.
    MODULES maps instance module names to their parsed definitions, as in
    :func:`verilog_tooling.wire.auto_wire`.
    """
    from .wire import parse_ignore_concat, set_ignore_concat, _wire_comment_enabled

    set_ignore_concat(parse_ignore_concat(lines))
    lines = kill_auto_input(lines)
    sigs, comments = _input_sigs(lines, modules)
    if not _wire_comment_enabled(lines):
        comments = {}
    return _regen(
        lines, "AUTOINPUT", _INPUT_HEADER, "input", sigs, comments, _IGNORE_RE["input"]
    )


def auto_output(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate the /*AUTOOUTPUT*/ output declarations of a module.

    Declares an output port for every net driven by an output port of an
    /*autoinst*/ instance that is not a port of this module and does not
    feed an instance input/inout.
    """
    from .wire import parse_ignore_concat, set_ignore_concat, _wire_comment_enabled

    set_ignore_concat(parse_ignore_concat(lines))
    lines = kill_auto_output(lines)
    sigs, comments = _output_sigs(lines, modules)
    if not _wire_comment_enabled(lines):
        comments = {}
    return _regen(
        lines, "AUTOOUTPUT", _OUTPUT_HEADER, "output", sigs, comments, _IGNORE_RE["output"]
    )


def auto_inout(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """AUTOOUTPUT then AUTOINPUT — the verilog-auto order (AIO command)."""
    return auto_input(auto_output(lines, modules), modules)


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.wire)


def _vb_auto_input(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate the /*AUTOINPUT*/ input declarations of the buffer."""
    return VerilogBuffer(auto_input(self._lines, modules))


def _vb_auto_output(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate the /*AUTOOUTPUT*/ output declarations of the buffer."""
    return VerilogBuffer(auto_output(self._lines, modules))


VerilogBuffer.auto_input = _vb_auto_input
VerilogBuffer.auto_output = _vb_auto_output


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.wire)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.inout",
        description="verilog-mode AUTOINPUT/AUTOOUTPUT rewrite",
    )
    parser.add_argument(
        "command",
        choices=["aio", "ain", "aout", "kill-ain", "kill-aout"],
        help="aio: AUTOOUTPUT+AUTOINPUT; ain/aout: individually; "
        "kill-ain/kill-aout: delete the region",
    )
    parser.add_argument("-i", "--in_file", required=True, help="buffer file with /*AUTOINPUT*// /*AUTOOUTPUT*/ markers")
    parser.add_argument("--ref_file", default=None, help="real source file anchoring Local-Variables relative paths (default: in_file)")
    parser.add_argument("-o", "--out_file", required=True, help="output file")
    parser.add_argument(
        "-y",
        "--libdir",
        action="append",
        default=[],
        help="library dir holding <module>.v/.sv (repeatable; default: .)",
    )
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    from .inst import _resolve_module_files, _cli_resolve, buffer_module_defs, _module_lines

    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    if args.command == "kill-ain":
        out = kill_auto_input(lines)
    elif args.command == "kill-aout":
        out = kill_auto_output(lines)
    else:
        buf = VerilogBuffer(lines)
        modules = {}
        names = set()
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
            for name in names:
                src = _module_lines(name, files, buffer_mods)
                if src is not None:
                    modules[name] = parse_module_ports(src, typedef_regexp=td_re)
        out = lines
        if args.command in ("aio", "aout"):
            out = auto_output(out, modules)
        if args.command in ("aio", "ain"):
            out = auto_input(out, modules)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
