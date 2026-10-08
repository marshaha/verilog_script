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

Configuration (file-local variables in the ``// Local Variables:``
section):

- ``verilog-auto-ignore-concat`` — non-nil (our default; emacs defaults to
  nil) skips pin connections in ``{...}`` or ``(...)``; nil extracts their
  identifiers instead (see :func:`_expr_nets`);
- ``verilog-auto-wire-comment`` — nil suppresses the ``// From`` comments.
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
    conn_net_name,
    _clean_dim,
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
# file-local configuration (Local Variables section)


# house default: skip {...}/(...) connections; emacs verilog-auto-ignore-concat
# defaults to nil (extract their signals) — our users wrap signals in {}
# precisely to exempt them from AUTOINPUT/AUTOOUTPUT/AUTOWIRE
_IGNORE_CONCAT_DEFAULT = True
_IGNORE_CONCAT = _IGNORE_CONCAT_DEFAULT


def set_ignore_concat(value: bool) -> None:
    global _IGNORE_CONCAT
    _IGNORE_CONCAT = value


def _local_bool(lines: Sequence[str], name: str, default: bool) -> bool:
    """A boolean file-local variable: ``// {name}: nil`` -> False,
    ``// {name}: t`` (or anything else) -> True, absent -> DEFAULT."""
    m = re.search(r"^\s*//\s*" + name + r"\s*:\s*(\w+)", "\n".join(lines), re.M)
    if not m:
        return default
    return m.group(1).lower() != "nil"


def parse_ignore_concat(lines: Sequence[str]) -> bool:
    """verilog-auto-ignore-concat file-local: non-nil skips pin connections
    in {...} or (...); nil extracts their signals (emacs default)."""
    return _local_bool(lines, "verilog-auto-ignore-concat", _IGNORE_CONCAT_DEFAULT)


def parse_param_value(lines: Sequence[str]) -> bool:
    """verilog-auto-inst-param-value file-local (default nil)."""
    return _local_bool(lines, "verilog-auto-inst-param-value", False)


def _wire_comment_enabled(lines: Sequence[str]) -> bool:
    """verilog-auto-wire-comment file-local (default t): nil suppresses the
    // To/From comments on generated declarations."""
    return _local_bool(lines, "verilog-auto-wire-comment", True)


# ---------------------------------------------------------------------------
# kill (verilog-delete-auto-buffer for these two regions)


def _kill_region(
    lines: Sequence[str], header_re: "re.Pattern[str]", marker_re: "re.Pattern[str]"
) -> list[str]:
    """Delete the generated region from the HEADER_RE line through the first
    ``// End of automatics``, keeping everything else (the marker line stays
    because it precedes the header).  A region whose preceding non-blank
    line is NOT the MARKER_RE marker belongs to a different AUTO command
    (e.g. /*AUTOLOGIC*/ — not implemented) and is left untouched."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if header_re.match(lines[i]):
            k = len(out) - 1
            while k >= 0 and not out[k].strip():
                k -= 1
            if k < 0 or not marker_re.search(out[k]):
                out.append(lines[i])  # someone else's region: keep it whole
                i += 1
                continue
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
    return _kill_region(lines, _WIRE_HEADER_RE, _AUTOWIRE_MARK_FULL)


def kill_auto_reg(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOREG*/ region (``// Beginning of automatic regs``
    through ``// End of automatics``), keeping the /*AUTOREG*/ marker."""
    return _kill_region(lines, _REG_HEADER_RE, _AUTOREG_MARK_FULL)


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
    unpacked_dims: tuple = ()  # unpacked port dims (the array's own decl)
    direction: str = ""  # port direction ('output' | 'inout' | 'input')
    signed: bool = False
    net_type: str = ""
    data_type: str = ""
    unpacked_idx: tuple = ()  # element indexes connected (AUTOWIRE merges)
    multi: bool = False  # driven/used by several instances (", ..." comment)


# ---------------------------------------------------------------------------
# concat/expression extraction (verilog-auto-ignore-concat = nil path,
# mirrors verilog-read-sub-decls-expr)


def _split_top_commas(text: str) -> list[str]:
    """Split TEXT on commas at brace/paren depth 0."""
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in text:
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


_EXPR_LEAD_OPS = re.compile(r"^\s*[~!&|^+\-]+")
_EXPR_CAST = re.compile(r"^\s*[a-zA-Z_]\w*\s*'")
_SIZED_LITERAL = re.compile(r"^\s*\d+'")
_REPLICATION = re.compile(r"^\s*\d+\s*(\{.*\})\s*$")
_ELEM_NET = re.compile(r"^([a-zA-Z_]\w*)\s*((?:\[[^\]]*\]\s*)*)$")


def _expr_nets(expr: str) -> "list[tuple[str, str]]":
    """(name, width) for every declarable identifier in a connection
    EXPRESSION: recurses into {…} concatenations and (…), strips unary
    operators and casts, accepts ``name`` / ``name[msb:lsb]`` (WIDTH is the
    msb text, 'c0' scalar); literals, numbers and `defines are dropped.
    Best effort, single line only."""
    expr = expr.strip()
    out: list[tuple[str, str]] = []
    repl = _REPLICATION.match(expr)
    if repl:  # {2{a}} — the count carries no signal
        expr = repl.group(1)
    if expr.startswith("{") and expr.endswith("}"):
        for part in _split_top_commas(expr[1:-1]):
            out.extend(_expr_nets(part))
        return out
    if expr.startswith("(") and expr.endswith(")"):
        return _expr_nets(expr[1:-1])
    expr = _EXPR_LEAD_OPS.sub("", expr)
    if _SIZED_LITERAL.match(expr) or expr.startswith(("'", "`")):
        return out
    expr = _EXPR_CAST.sub("", expr)
    if expr.startswith("(") and expr.endswith(")"):  # cast around parens
        return _expr_nets(expr[1:-1])
    m = _ELEM_NET.match(expr)
    if not m:
        return out
    ranges = re.findall(r"\[[^\]]*\]", m.group(2))
    width = "c0"
    if len(ranges) == 1 and ":" in ranges[0]:
        width = ranges[0][1:-1].split(":")[0].strip()
    out.append((m.group(1), width))
    return out


def _note_net(nets: "dict[str, InstNet]", rec: InstNet) -> None:
    """Add REC to NETS, first driver winning; a second driver from a
    DIFFERENT instance marks the net multi (its comment gets ", ...",
    like verilog-mode's sig comment for a repeated signal)."""
    prev = nets.get(rec.name)
    if prev is None:
        nets[rec.name] = rec
    elif (prev.inst, prev.module) != (rec.inst, rec.module) and not prev.multi:
        from dataclasses import replace

        nets[rec.name] = replace(prev, multi=True)


def _primitive_nets(
    text: str, open_idx: int, module: str, inst: str,
    dir_list: list[str],
    nets: dict,
    directions: "tuple[str, ...] | None",
    simple_only: bool,
) -> None:
    """Extract nets from a gate primitive instance (or/buf/etc.).

    Primitive ports are positional: e.g., or u_or (o, i0, i1) has o=output,
    i0/i1=inputs per _GATE_PRIMITIVES. Parses the comma-separated expressions,
    strips bit-selects ([31:0]), and adds InstNet entries for ports matching
    DIRECTIONS.
    """
    from . import emacs
    import re
    close_idx = emacs._matching_paren(text, open_idx)
    span = text[open_idx + 1 : close_idx]
    masked = emacs.mask_comments(span)
    # Split by commas at depth 0
    exprs = []
    depth = 0
    cur = []
    for ch in masked:
        if ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            exprs.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        exprs.append("".join(cur).strip())
    # Filter empty and AUTOINST markers
    exprs = [e for e in exprs if e and "AUTOINST" not in e]
    want_dirs = directions if directions is not None else ("output", "inout")
    for idx, expr in enumerate(exprs):
        # Skip named connections (.pin) - primitives use positional
        if expr.lstrip().startswith("."):
            continue
        m = re.match(r"(\w+)", expr.strip())
        if not m:
            continue
        net_name = m.group(1)
        # Direction: first port uses dir_list[0], rest are inputs (implied)
        if idx < len(dir_list):
            port_dir = dir_list[idx]
        else:
            port_dir = "inout" if "inout" in dir_list else "input"
        if port_dir not in want_dirs:
            continue
        if simple_only:
            # Allow bare name or name[range]
            stripped = expr.strip()
            if stripped != net_name and not re.match(
                rf"^{re.escape(net_name)}\s*\[[^\]]+\]$", stripped
            ):
                continue
        # Width: extract msb from [msb:lsb], or "c0" for scalar
        width = "c0"
        wm = re.search(r"\[\s*([^\]:\s]+)\s*:", expr)
        if wm:
            width = wm.group(1).strip()
        rec = InstNet(
            name=net_name, width=width, inst=inst, module=module,
            direction=port_dir,
        )
        if net_name not in nets:
            nets[net_name] = rec


def _inst_driven_nets(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    directions: "tuple[str, ...] | None" = None,
    simple_only: bool = False,
    concat_ok: bool = False,
) -> dict[str, InstNet]:
    """net -> InstNet for every net connected to an output/inout port of an
    /*autoinst*/ instance whose module is in MODULES (first driver wins).

    DIRECTIONS restricts the port directions considered (default: outputs
    and inouts — the AUTOWIRE driver set; interface ports are never wire
    drivers).  SIMPLE_ONLY keeps only connections that are a single bare
    net (``AUTOINPUT``/``AUTOOUTPUT`` candidates — a concat or expression
    cannot be re-declared).

    A {...} or (...) connection is skipped when the ignore-concat setting is
    on (``verilog-auto-ignore-concat`` file-local, default on); when off,
    its identifiers are extracted (verilog-read-sub-decls-expr).

    The pin list is parsed with the same full-text machinery as EAI
    (:func:`emacs.inst_pin_connections` on the paren-stack instance
    resolution): pins before the marker, multi-pin lines, multi-line
    expressions and ``.pin`` shorthand all count.

    The port width is passed through the instance's ``#(...)`` parameter
    overrides (e.g. R_USER_WIDTH -> AR_INFO_WIDTH) so the declared wire
    names LOCAL symbols wherever the mapping provides them.  With
    ``verilog-auto-inst-param-value`` on, a width still naming a constant
    parameter of THIS module folds to its integer value."""
    from . import emacs
    from .autodef import _param_value_on

    const_map: dict[str, str] = {}
    if _param_value_on():
        from .autodef import _const_symbols

        const_map = {k: str(v) for k, v in _const_symbols(lines).items()}
    text = "\n".join(lines)
    markers = emacs.find_auto_markers(lines, "autoinst")
    stacks = emacs._scan_parens_at(text, [m.offset for m in markers])
    nets: dict[str, InstNet] = {}
    for marker in markers:
        st = stacks[marker.offset]
        if not st:
            continue
        open_idx = st[-1]
        try:
            module, inst = emacs._resolve_instance_at(text, open_idx)
        except ValueError:
            continue
        moddef = modules.get(module)
        if moddef is None:
            # Gate primitive (or/buf/etc.): parse positional ports.
            # Array instance resolution (u_or [31:0]) is handled in
            # _resolve_instance_at.
            if module in emacs._GATE_PRIMITIVES:
                _primitive_nets(
                    text, open_idx, module, inst,
                    emacs._GATE_PRIMITIVES[module],
                    nets, directions, simple_only,
                )
            continue
        if directions is None:
            # the AUTOWIRE driver set: outputs and inouts — an interface
            # port connection is an interface instance, never a wire driver
            inst_io = {
                p.name: p for p in moddef.ports if p.direction in ("output", "inout")
            }
        else:
            inst_io = {p.name: p for p in moddef.ports if p.direction in directions}
        param_values = emacs.effective_param_values(
            moddef, emacs.read_inst_param_values(text, open_idx)
        )
        for pin, expr in emacs.inst_pin_connections(text, open_idx):
            if pin not in inst_io:
                continue
            stripped = emacs._strip_comments(expr).strip()
            # only a `define/literal NET carries no declaration; a backtick
            # inside a bit-select (net[`MACRO-1:0]) is fine
            if (
                not stripped
                or stripped.startswith(("'", "`"))
                or stripped[0].isdigit()
            ):
                continue
            if stripped.startswith(("(", "{")):
                # verilog-auto-ignore-concat: skip (default) or extract.
                # CONCAT_OK is exclusion bookkeeping (AIO "is this net
                # wired internally anywhere"): there the identifiers in
                # a {...}/(...) expression always count as connected,
                # regardless of the candidacy exemption.
                if not _IGNORE_CONCAT or concat_ok:
                    for net, ewidth in _expr_nets(stripped):
                        if param_values:
                            ewidth = emacs._apply_param_values(ewidth, param_values)
                        if const_map:
                            ewidth = emacs._apply_param_values(ewidth, const_map)
                        _note_net(nets, InstNet(net, ewidth, inst, module))
                continue
            if simple_only and not _ELEM_NET.match(stripped):
                continue
            net = conn_net_name(stripped)
            if net is None:
                continue
            port = inst_io[pin]
            raw = port.width  # 'msb:lsb', None for scalar
            width = "c0" if raw is None else raw.split(":")[0].strip()
            if param_values:
                width = emacs._apply_param_values(width, param_values)
            if const_map:
                width = emacs._apply_param_values(width, const_map)
            # multi-dim packed port: prefer the dims in the EAI connection
            # note (already param-value substituted), else the port's own
            # packed ranges
            pdims = _conn_packed_dims(expr) or (
                tuple(_clean_dim(d) for d in port.packed)
                if len(port.packed) > 1 else ()
            )
            if pdims and param_values:
                pdims = tuple(
                    _clean_dim(emacs._apply_param_values(d, param_values))
                    for d in pdims
                )
            if pdims and const_map:
                pdims = tuple(
                    _clean_dim(emacs._apply_param_values(d, const_map))
                    for d in pdims
                )
            _note_net(
                nets,
                InstNet(
                    net, width, inst, module, pdims, port.unpacked,
                    port.direction, port.signed, port.net_type, port.data_type,
                ),
            )
            # unpacked-array element index: the note's ``.[idx]`` part, or
            # the connection's own select when the port is unpacked
            uidx = ""
            um = re.search(r"\.\s*\[([^\]:]+)\]\s*\*/", expr)
            if um:
                uidx = um.group(1).strip()
            elif port.unpacked:
                em = _ELEM_NET.match(stripped)
                if em:
                    ranges = re.findall(r"\[([^\]]+)\]", em.group(2))
                    # a select equal to the PACKED range is a packed
                    # bit-select of the whole array, not an element index
                    packed_txt = re.sub(r"\s+", "", port.packed[-1] if port.packed else "")
                    if ranges and re.sub(r"\s+", "", ranges[-1]) != packed_txt:
                        uidx = ranges[-1].strip()
            if uidx:
                rec = nets[net]
                if uidx not in rec.unpacked_idx:
                    from dataclasses import replace

                    nets[net] = replace(
                        rec, unpacked_idx=tuple(rec.unpacked_idx) + (uidx,)
                    )
    return nets


def _is_typedef_decl(line: str) -> bool:
    """A ``reqcmd_t BReq;``-style declaration: first word matches the
    buffer's verilog-typedef-regexp, so it is a TYPE, and the second word is
    the declared signal name.  With no regexp configured, falls back to
    the structural check in :func:`verilog_tooling.autodef._is_typedef_decl`."""
    from .autodef import _is_typedef_decl as _impl

    return _impl(line)


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
            max_len = max(max_len, _sig_decl_len(sig, emacs_dims=True))
    return max_len


def _emit_decl(sig: Signal, max_len: int, keyword: str, indent: int, comment: str = "") -> str:
    line = " " * indent + _emit_signal(sig, max_len, keyword, emacs_dims=True)
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
    full = list(lines)
    from .inst import map_module_spans

    return map_module_spans(
        lines, _AUTOWIRE_MARK_FULL, lambda span: _auto_wire_single(span, modules, full)
    )


def _auto_wire_single(
    lines: Sequence[str], modules: Mapping[str, ModuleDef], full: Sequence[str]
) -> list[str]:
    """AUTOWIRE for ONE module span (FULL is the whole buffer, for the
    compilation-unit `define set)."""
    from .libdirs import parse_typedef_regexp
    from .inst import set_typedef_regexp

    typedef_re = parse_typedef_regexp(full)
    set_typedef_regexp(typedef_re)
    set_ignore_concat(parse_ignore_concat(full))
    from .autodef import set_param_value

    set_param_value(parse_param_value(full))
    driven = _inst_driven_nets(lines, modules)
    if not driven:
        return list(lines)
    ports, usrdef, _ = _module_tables(lines)
    declared = set(ports.signals) | set(usrdef.signals)
    declared |= get_all_defs(full) | get_all_paras(lines)
    # a port-list name on an instance inout pin is an inout PORT by intent
    # (autoarg emits the inout declaration) — never an AUTOWIRE wire
    from .inst import auto_arg_port_names

    port_names = auto_arg_port_names(lines)
    if port_names:
        declared |= {
            name
            for name, net in driven.items()
            if net.direction == "inout" and name in port_names
        }
    # every net here is instance-driven, so a width still naming foreign
    # symbols (a submodule parameter the constants do not cover) is the
    # DRIVER's dimension: declare with it, matching autodef's rule,
    # instead of skipping the net
    from .autodef import _const_symbols

    local_syms = set(get_all_paras(lines)) | set(_const_symbols(lines))
    from .autodef import _merge_unpacked_indexes

    sigs: list[Signal] = []
    comments: dict[str, str] = {}
    for name in sorted(driven):
        if name in declared:
            continue
        if typedef_re and re.search(typedef_re, name):
            continue  # a typedef, not a net (verilog-typedef-regexp)
        net = driven[name]
        # unpacked array: merge the element indexes from all instances into
        # one range (abc[0]+abc[2] -> [0:2]); a whole-array connection of an
        # unpacked port keeps the port's own dims
        dims: tuple = ()
        if net.unpacked_idx:
            merged = _merge_unpacked_indexes(net.unpacked_idx, local_syms)
            if merged is None:
                continue
            dims = (merged,)
        elif net.unpacked_dims:
            # driver-owned dims, same rule as the packed width above
            dims = tuple(net.unpacked_dims)
        sigs.append(
            Signal(
                width=net.width, type="inst_wire", name=name,
                packed_dims=net.packed_dims, signed=net.signed,
                net_type=net.net_type, data_type=net.data_type, dims=dims,
            )
        )
        # an inout-driven net is commented To/From (verilog-mode), an
        # output-driven one From
        direction = "To/From" if net.direction == "inout" else "From"
        # Primitives (or/buf/etc.) are not files, so no .v suffix
        from . import emacs as _emacs
        mod_suffix = "" if net.module in _emacs._GATE_PRIMITIVES else ".v"
        comments[name] = (
            f"// {direction} {net.inst} of {net.module}{mod_suffix}"
            + (", ..." if net.multi else "")
        )
    if not _wire_comment_enabled(full):
        comments = {}
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
    full = list(lines)
    from .inst import map_module_spans

    return map_module_spans(
        lines, _AUTOREG_MARK_FULL, lambda span: _auto_reg_single(span, modules, full)
    )


def _auto_reg_single(
    lines: Sequence[str], modules: Mapping[str, ModuleDef] | None, full: Sequence[str]
) -> list[str]:
    """AUTOREG for ONE module span (FULL for the compilation-unit `define set)."""
    set_ignore_concat(parse_ignore_concat(full))
    from .autodef import set_param_value

    set_param_value(parse_param_value(full))
    ports, usrdef, assigns = _module_tables(lines)
    driven = set(_inst_driven_nets(lines, modules or {}))
    excluded = set(usrdef.signals) | assigns | driven
    excluded |= get_all_defs(full) | get_all_paras(lines)
    sigs = [
        Signal(
            width=sig.width, type="io_reg", name=name, signed=sig.signed,
            net_type=sig.net_type, data_type=sig.data_type,
        )
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
    parser.add_argument(
        "-I",
        "--interface",
        action="append",
        default=[],
        help="user-known SystemVerilog interface type name (repeatable)",
    )
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    from .inst import _resolve_module_files, _cli_resolve, buffer_module_defs, _module_lines, find_interfaces

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
            interfaces = set(find_interfaces(libdirs)) | set(args.interface)
            for name in names:
                src = _module_lines(name, files, buffer_mods)
                if src is not None:
                    modules[name] = parse_module_ports(
                        src, with_params=True, typedef_regexp=td_re, interfaces=interfaces
                    )
        out = auto_wire(lines, modules) if args.command == "aw" else auto_reg(lines, modules)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
