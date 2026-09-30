"""Python rewrite of verilog-mode's AUTOWIRE / AUTOREG declarations.

Implements ``verilog-auto-wire`` and ``verilog-auto-reg`` (verilog-mode.el
~lines 13131/13268):

- ``/*AUTOWIRE*/`` declares ``wire`` for nets driven by instance output or
  inout ports (the connection of ``.port(net)`` inside an /*autoinst*/
  instance) that are not already declared as a port, wire/reg/usrdef,
  parameter, or `define.  Each declaration carries a trailing
  ``// From {inst} of {module}.v`` comment.
- ``/*AUTOREG*/`` declares ``reg`` for output ports of the current module
  that carry no wire/reg keyword, are not declared in the body, are not
  driven by a continuous ``assign``, and are not driven by an instance
  output/inout.

Both regions end with the shared ``// End of automatics`` closer;
:func:`kill_auto_wire` / :func:`kill_auto_reg` delete only the region
introduced by their own ``// Beginning of automatic wires`` /
``// Beginning of automatic regs`` header, keeping the marker line.

Formatting (matching :mod:`verilog_tooling.autodef`):

- the type field is padded to column 12 (``wire ``/``reg  `` + CalMargin);
- the name column is padded to max_len (floor 39), where each signal
  contributes ``5 + len(width) + 4`` (only when width != 'c0');
- signals are sorted by name (verilog-signals-sort-compare).

Fidelity decisions vs verilog-mode:

- the declaration body uses the autodef arithmetic above instead of
  verilog-mode's ``indent-to (max 24 (+ indent-pt 16))`` name column;
- generated lines are prefixed with the marker's indentation (indent-pt),
  and the ``// From ...`` comment starts at column
  ``max(48, indent-pt + 40)``, as in verilog-insert-definition;
- the width text is the submodule port's msb, re-emitted as ``[msb:0]``
  (no range simplification);
- with an empty ``modules`` mapping, instance-driven nets cannot be
  resolved, so AUTOREG's not-driven-by-an-instance exclusion is skipped.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .autodef import (
    Signal,
    SignalTable,
    _ASSIGN,
    _DATA_LINE,
    _ENDMODULE,
    _PORT_LINE,
    _conn_packed_dims,
    _emit_signal,
    _normalize_dim,
    _sig_decl_len,
    _skip_comment_line,
    _strip_line,
    get_all_defs,
    get_all_paras,
)
from .inst import ModuleDef, VerilogBuffer, parse_module_ports

_AUTOWIRE_MARK_FULL = re.compile(r"^\s*/\*\s*\b(autowire|AUTOWIRE)\b\*/")
_AUTOREG_MARK_FULL = re.compile(r"^\s*/\*\s*\b(autoreg|AUTOREG)\b\*/")
_WIRE_HEADER = "// Beginning of automatic wires (for undeclared instantiated-module outputs)"
_REG_HEADER = "// Beginning of automatic regs (for this module's undeclared outputs)"
_WIRE_HEADER_RE = re.compile(r"^\s*// Beginning of automatic wires\b")
_REG_HEADER_RE = re.compile(r"^\s*// Beginning of automatic regs\b")
_END_OF_AUTOMATICS = "// End of automatics"

_MAX_LEN_FLOOR = 39  # same floor as automatic.vim s:autodef_max_len
_COMMENT_COL = 48  # verilog-insert-definition: indent-to (max 48 (+ indent-pt 40))


# ---------------------------------------------------------------------------
# kill (verilog-delete-auto-buffer for these two regions)


def _kill_region(lines: Sequence[str], header_re: "re.Pattern[str]") -> list[str]:
    """Delete the generated region from the HEADER_RE line through the first
    ``// End of automatics``, keeping everything else (the marker line stays
    because it precedes the header)."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if header_re.match(lines[i]):
            i += 1
            while i < len(lines) and _END_OF_AUTOMATICS not in lines[i]:
                i += 1
            if i < len(lines):
                i += 1  # drop the closer too
            continue
        out.append(lines[i])
        i += 1
    return out


def kill_auto_wire(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOWIRE*/ region (``// Beginning of automatic wires``
    through ``// End of automatics``), keeping the /*AUTOWIRE*/ marker."""
    return _kill_region(lines, _WIRE_HEADER_RE)


def kill_auto_reg(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOREG*/ region (``// Beginning of automatic regs``
    through ``// End of automatics``), keeping the /*AUTOREG*/ marker."""
    return _kill_region(lines, _REG_HEADER_RE)


# ---------------------------------------------------------------------------
# buffer scans


@dataclass(frozen=True)
class InstNet:
    """A net connected to an output/inout port of an /*autoinst*/ instance.
    ``width`` is the port's msb text ('7', 'W-1') or 'c0' for a scalar;
    ``packed_dims`` holds every packed range of a multi-dim port."""

    name: str
    width: str
    inst: str
    module: str
    packed_dims: tuple = ()


def _inst_driven_nets(
    lines: Sequence[str], modules: Mapping[str, ModuleDef]
) -> dict[str, InstNet]:
    """net -> InstNet for every net connected to an output/inout port of an
    /*autoinst*/ instance whose module is in MODULES (first driver wins).

    The port width is passed through the instance's ``#(...)`` parameter
    overrides (e.g. R_USER_WIDTH -> AR_INFO_WIDTH) so the declared wire
    names LOCAL symbols wherever the mapping provides them."""
    from . import emacs

    text = "\n".join(lines)
    markers = emacs.find_auto_markers(lines, "autoinst")
    stacks = emacs._scan_parens_at(text, [m.offset for m in markers])
    param_by_line: dict[int, dict[str, str]] = {}
    for mk in markers:
        st = stacks[mk.offset]
        if st:
            param_by_line[text.count("\n", 0, mk.offset)] = emacs.read_inst_param_values(
                text, st[-1]
            )

    buf = VerilogBuffer(lines)
    nets: dict[str, InstNet] = {}
    n = len(lines)
    for idx in buf.markers("autoinst"):
        try:
            module, inst = buf.resolve_instance(idx)
        except ValueError:
            continue
        moddef = modules.get(module)
        if moddef is None:
            continue
        inst_io = {p.name: p for p in moddef.ports if p.direction != "input"}
        param_values = param_by_line.get(idx) or {}
        i = idx + 1
        while i < n:
            j = _skip_comment_line(lines, i)
            if j == -1:
                return nets
            i = j
            line = lines[i]
            m = re.match(r"^\s*\.(\w+)\s*\((.*)", line)
            if m:
                port_name, rest = m.group(1), m.group(2)
                # only a `define/literal NET carries no declaration; a
                # backtick inside a bit-select (net[`MACRO-1:0]) is fine
                if port_name in inst_io and not rest.lstrip().startswith(("'", "`")):
                    nm = re.search(r"\w+", rest)
                    if nm:
                        net = nm.group(0)
                        port = inst_io[port_name]
                        raw = port.width  # 'msb:lsb', None for scalar
                        width = "c0" if raw is None else raw.split(":")[0].strip()
                        if param_values:
                            width = emacs._apply_param_values(width, param_values)
                        # multi-dim packed port: prefer the dims in the EAI
                        # connection note (already param-value substituted),
                        # else the port's own packed ranges
                        pdims = _conn_packed_dims(rest) or (
                            tuple(_normalize_dim(d) for d in port.packed)
                            if len(port.packed) > 1 else ()
                        )
                        if pdims and param_values:
                            pdims = tuple(
                                _normalize_dim(emacs._apply_param_values(d, param_values))
                                for d in pdims
                            )
                        nets.setdefault(net, InstNet(net, width, inst, module, pdims))
            if re.search(r"\);\s*$", line) or ");" in line:
                i += 1
                break
            i += 1
    return nets


def _is_typedef_decl(line: str) -> bool:
    """A ``reqcmd_t BReq;``-style declaration: first word matches the
    buffer's verilog-typedef-regexp, so it is a TYPE, and the second word is
    the declared signal name."""
    from .inst import _TYPEDEF_REGEXP

    if _TYPEDEF_REGEXP is None:
        return False
    m = re.match(r"^\s*(\w+)\s+\w+", line)
    return bool(m and _TYPEDEF_REGEXP.search(m.group(1)))


def _module_tables(lines: Sequence[str]) -> tuple[SignalTable, SignalTable, set[str]]:
    """(ports, user declarations, assign-LHS names) of the current module."""
    ports = SignalTable()
    usrdef = SignalTable()
    assigns: set[str] = set()
    io_seq = 0
    usr_seq = 0
    i = 0
    n = len(lines)
    while i < n:
        j = _skip_comment_line(lines, i)
        if j == -1:
            break
        i = j
        line = _strip_line(lines[i])
        if _PORT_LINE.match(line):
            io_seq = ports.extend_io_from_line(line, io_seq)
        elif _DATA_LINE.match(line) or _is_typedef_decl(line):
            usr_seq = usrdef.extend_usrdef_from_line(line, usr_seq)
        elif _ASSIGN.match(line):
            m = re.match(r"\s*assign\s+(\w+)", line)
            if m:
                assigns.add(m.group(1))
        elif _ENDMODULE.match(line):
            break
        i += 1
    return ports, usrdef, assigns


# ---------------------------------------------------------------------------
# emission


def _name_col_max(sigs: Sequence[Signal]) -> int:
    """autodef name-column width: floor 39, plus the declaration length of
    each non-scalar signal (multi-dim packed dims included)."""
    max_len = _MAX_LEN_FLOOR
    for sig in sigs:
        if sig.width != "c0" or sig.packed_dims:
            max_len = max(max_len, _sig_decl_len(sig))
    return max_len


def _emit_decl(sig: Signal, max_len: int, keyword: str, indent: int, comment: str = "") -> str:
    line = " " * indent + _emit_signal(sig, max_len, keyword)
    if comment:
        col = max(_COMMENT_COL, indent + 40)
        line += " " * max(col - len(line), 1) + comment
    return line


def _regen(
    lines: Sequence[str],
    mark_full: "re.Pattern[str]",
    header: str,
    keyword: str,
    sigs: Sequence[Signal],
    comments: Mapping[str, str],
) -> list[str]:
    """Splice the generated region after the marker line(s).  The signal set
    is emitted ONCE (after the first marker); further markers get an empty
    region — duplicating the declarations would be a compile error."""
    max_len = _name_col_max(sigs)
    out: list[str] = []
    emitted = False
    for line in lines:
        out.append(line)
        if mark_full.match(line) and sigs:
            indent = len(line) - len(line.lstrip())
            pad = " " * indent
            out.append(pad + header)
            if not emitted:
                for sig in sigs:
                    out.append(_emit_decl(sig, max_len, keyword, indent, comments.get(sig.name, "")))
                emitted = True
            out.append(pad + _END_OF_AUTOMATICS)
    return out


# ---------------------------------------------------------------------------
# AUTOWIRE (verilog-auto-wire)


def auto_wire(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate the /*AUTOWIRE*/ wire declarations of a module.

    Declares every net driven by an output/inout port of an /*autoinst*/
    instance that is not already declared (port, wire/reg/usrdef, parameter,
    or `define).  MODULES maps instance module names to their parsed
    definitions, as in :func:`verilog_tooling.inst.auto_inst`.
    """
    lines = kill_auto_wire(lines)
    from .libdirs import parse_typedef_regexp
    from .inst import set_typedef_regexp

    typedef_re = parse_typedef_regexp(lines)
    set_typedef_regexp(typedef_re)
    driven = _inst_driven_nets(lines, modules)
    if not driven:
        return list(lines)
    ports, usrdef, _ = _module_tables(lines)
    declared = set(ports.signals) | set(usrdef.signals)
    declared |= get_all_defs(lines) | get_all_paras(lines)
    # widths must name LOCAL symbols; a still-foreign width (a submodule
    # parameter the instance map does not cover) would not compile — skip it
    # here and let autodef flag the net as unresolved instead
    from .autodef import _const_symbols, _width_syms_known

    local_syms = set(get_all_paras(lines)) | set(_const_symbols(lines))
    sigs: list[Signal] = []
    comments: dict[str, str] = {}
    for name in sorted(driven):
        if name in declared:
            continue
        if typedef_re and re.search(typedef_re, name):
            continue  # a typedef, not a net (verilog-typedef-regexp)
        net = driven[name]
        if net.packed_dims:
            # multi-dim declaration compiles only when every dim's symbols
            # are visible here (the EAI note / #(...) map usually provides them)
            if any(
                not _width_syms_known(d, local_syms) for d in net.packed_dims
            ):
                continue
        elif net.width not in ("", "c0") and not _width_syms_known(net.width, local_syms):
            continue
        sigs.append(
            Signal(width=net.width, type="inst_wire", name=name, packed_dims=net.packed_dims)
        )
        comments[name] = f"// From {net.inst} of {net.module}.v"
    return _regen(lines, _AUTOWIRE_MARK_FULL, _WIRE_HEADER, "wire ", sigs, comments)


# ---------------------------------------------------------------------------
# AUTOREG (verilog-auto-reg)


def auto_reg(lines: Sequence[str], modules: Mapping[str, ModuleDef] | None = None) -> list[str]:
    """Regenerate the /*AUTOREG*/ reg declarations of a module.

    Declares every output port of the current module that carries no
    wire/reg keyword and is not otherwise declared, assign-driven, or driven
    by an instance output/inout.  MODULES (optional) is used for the
    instance-driven exclusion, as in :func:`auto_wire`.
    """
    lines = kill_auto_reg(lines)
    ports, usrdef, assigns = _module_tables(lines)
    driven = set(_inst_driven_nets(lines, modules or {}))
    excluded = set(usrdef.signals) | assigns | driven
    excluded |= get_all_defs(lines) | get_all_paras(lines)
    sigs = [
        Signal(width=sig.width, type="io_reg", name=name)
        for name, sig in ports.signals.items()
        if sig.io_dir == "output" and not sig.has_defined and name not in excluded
    ]
    sigs.sort(key=lambda s: s.name)
    return _regen(lines, _AUTOREG_MARK_FULL, _REG_HEADER, "reg  ", sigs, {})


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.fmt)


def _vb_auto_wire(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate the /*AUTOWIRE*/ wire declarations of the buffer."""
    return VerilogBuffer(auto_wire(self._lines, modules))


def _vb_auto_reg(
    self: VerilogBuffer, modules: Mapping[str, ModuleDef] | None = None
) -> VerilogBuffer:
    """Regenerate the /*AUTOREG*/ reg declarations of the buffer."""
    return VerilogBuffer(auto_reg(self._lines, modules))


VerilogBuffer.auto_wire = _vb_auto_wire
VerilogBuffer.auto_reg = _vb_auto_reg


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.autodef)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.wire",
        description="verilog-mode AUTOWIRE/AUTOREG rewrite",
    )
    parser.add_argument(
        "command",
        choices=["aw", "ar", "kill-aw", "kill-ar"],
        help="aw: AUTOWIRE; ar: AUTOREG; kill-aw/kill-ar: delete the region",
    )
    parser.add_argument("-i", "--in_file", required=True, help="buffer file with /*AUTOWIRE*///*AUTOREG*/ markers")
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
    if args.command == "kill-aw":
        out = kill_auto_wire(lines)
    elif args.command == "kill-ar":
        out = kill_auto_reg(lines)
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
        out = auto_wire(lines, modules) if args.command == "aw" else auto_reg(lines, modules)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
