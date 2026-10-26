"""Python rewrite of verilog-mode's AUTOINPUT / AUTOOUTPUT / AUTOINOUT.

Implements ``verilog-auto-input``, ``verilog-auto-output`` and
``verilog-auto-inout`` (verilog-mode.el ~lines 13482/13334/13570):

- ``/*AUTOINPUT*/`` declares an ``input`` port for every net connected to an
  input port of an /*autoinst*/ instance that is not otherwise declared in
  the module (port, wire/reg/usrdef, parameter, `define) and not driven by
  an instance output/inout.  Each declaration carries a trailing
  ``// To {inst} of {module}.v`` comment.
- ``/*AUTOOUTPUT*/`` declares an ``output`` port for every net connected to
  an output port of an /*autoinst*/ instance that is not already a port of
  this module and does not feed an instance input/inout (those stay internal
  and belong to /*AUTOWIRE*/).  Comment: ``// From {inst} of {module}.v``.
- ``/*AUTOINOUT*/`` declares an ``inout`` port for every net connected to an
  inout port of an /*autoinst*/ instance that is not a port of this module
  and is not seen on an instance input/output port.  Comment:
  ``// To/From {inst} of {module}.v``.

All regions end with the shared ``// End of automatics`` closer; the
headers are verilog-mode's::

    // Beginning of automatic inputs (from unused autoinst inputs)
    // Beginning of automatic outputs (from unused autoinst outputs)
    // Beginning of automatic inouts (from unused autoinst inouts)

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
    _cached_pin_scan,
    _inst_driven_nets,
    _kill_region,
    _module_tables,
    _name_col_max,
)

_INPUT_HEADER = "// Beginning of automatic inputs (from unused autoinst inputs)"
_OUTPUT_HEADER = "// Beginning of automatic outputs (from unused autoinst outputs)"
_INOUT_HEADER = "// Beginning of automatic inouts (from unused autoinst inouts)"

# NOTE: whether generated port declarations may carry the variable
# types reg/logic inherited from a submodule port is an explicit
# ALLOW_SV_TYPES parameter threaded through auto_output/auto_input/
# auto_inout (default True preserves the submodule's type).  It is
# deliberately NOT a module global: a global set by one CLI run would
# leak into later in-process calls (a .v aall run would demote a later
# direct auto_wire call's logic declarations) and into AUTOREG, which
# shares wire._regen but must keep its reg declarations.


def _mod_comment_suffix(module: str) -> str:
    """'.v' suffix for module comments, except for gate primitives."""
    from . import emacs as _emacs
    return "" if module in _emacs._GATE_PRIMITIVES else ".v"

_AUTOINPUT_MARK = re.compile(r"/\*\s*\bAUTOINPUT\b", re.IGNORECASE)
_AUTOOUTPUT_MARK = re.compile(r"/\*\s*\bAUTOOUTPUT\b", re.IGNORECASE)
_AUTOINOUT_MARK = re.compile(r"/\*\s*\bAUTOINOUT\b", re.IGNORECASE)

_END_OF_AUTOMATICS = "// End of automatics"
_INPUT_HEADER_RE = re.compile(r"^\s*// Beginning of automatic inputs\b")
# not "(every signal)": an AUTOOUTPUTEVERY region is not ours to delete
_OUTPUT_HEADER_RE = re.compile(
    r"^\s*// Beginning of automatic outputs\b(?!\s*\(every signal\b)"
)
_INOUT_HEADER_RE = re.compile(r"^\s*// Beginning of automatic inouts\b")

_IGNORE_RE = {
    "input": "verilog-auto-input-ignore-regexp",
    "output": "verilog-auto-output-ignore-regexp",
    "inout": "verilog-auto-inout-ignore-regexp",
}


# ---------------------------------------------------------------------------
# kill (verilog-delete-auto-buffer for these two regions)


def kill_auto_input(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOINPUT*/ region, keeping the marker."""
    return _kill_region(lines, _INPUT_HEADER_RE, _AUTOINPUT_MARK)


def kill_auto_output(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOOUTPUT*/ region, keeping the marker."""
    return _kill_region(lines, _OUTPUT_HEADER_RE, _AUTOOUTPUT_MARK)


def kill_auto_inout(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOINOUT*/ region, keeping the marker."""
    return _kill_region(lines, _INOUT_HEADER_RE, _AUTOINOUT_MARK)


# ---------------------------------------------------------------------------
# signal sets


def _local_syms(lines: Sequence[str]) -> set[str]:
    from .autodef import _const_symbols

    return set(get_all_paras(lines)) | set(_const_symbols(lines))


def _declarable(net, local_syms: set[str]) -> bool:
    """A candidate can be declared unless its unpacked element indexes
    do not merge into a range (then no array form exists).  Widths are
    NOT visibility-checked: an instance-derived width (an output's
    driver dimension, or the dimension of the pin a net feeds) is
    declared as written when it does not fold to local constants."""
    from .autodef import _merge_unpacked_evidence

    if net.unpacked_idx or net.unpacked_ranges:
        merged, _c = _merge_unpacked_evidence(net.unpacked_idx, net.unpacked_ranges)
        return merged is not None
    return True


def _sig_dims(net, local_syms: set[str]) -> tuple:
    """Unpacked dims for the emitted declaration: merged element indexes,
    else the port's own unpacked decl."""
    from .autodef import _merge_unpacked_evidence

    if net.unpacked_idx or net.unpacked_ranges:
        merged, _c = _merge_unpacked_evidence(net.unpacked_idx, net.unpacked_ranges)
        return (merged,) if merged is not None else ()
    return tuple(net.unpacked_dims)


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


def _connected_names(
    lines: Sequence[str], modules: Mapping[str, ModuleDef],
    directions: "tuple[str, ...] | None",
    pin_scan_cache: "dict | None" = None,
) -> set[str]:
    """Names appearing in pin expressions of /*autoinst*/ instances in
    DIRECTIONS, under the verilog-auto-ignore-concat candidacy exemption:
    a net inside ``.din ({2'b0, val})`` does NOT count as connected —
    live Emacs 29.3 with ignore-concat: t promotes such a val to
    AUTOOUTPUT (autoinput_concat_ignore.v: signal_f/signal_c are
    outputs) and promotes a net fed only through a concat output to
    AUTOINPUT.  With ignore-concat nil the {...} members extract
    regardless, so this is neutral there.
    """
    return set(
        _inst_driven_nets(
            lines, modules, directions, concat_ok=False,
            _pin_scan=_cached_pin_scan(pin_scan_cache, lines, modules),
        )
    )


def _input_sigs(
    lines: Sequence[str], modules: Mapping[str, ModuleDef],
    full: "Sequence[str] | None" = None,
    pin_scan_cache: "dict | None" = None,
) -> "tuple[list[Signal], dict[str, str]]":
    """AUTOINPUT candidates: nets feeding instance INPUT ports, minus
    everything declared or driven inside the module.  An undeclared
    assign-driven net IS a candidate (verilog-mode parity — promote it to a
    port and drop the assign yourself)."""
    in_nets = _inst_driven_nets(
        lines, modules, ("input",), simple_only=True,
        _pin_scan=_cached_pin_scan(pin_scan_cache, lines, modules),
    )
    driven = _connected_names(lines, modules, None, pin_scan_cache)
    ports, usrdef, _ = _module_tables(lines)
    declared = set(ports.signals) | _declared_port_names(lines)
    declared |= set(usrdef.signals) | driven
    declared |= get_all_defs(full or lines) | get_all_paras(lines)
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
        if not _declarable(net, local_syms):
            continue
        sigs.append(
            Signal(
                width=net.width, type="io_input", name=name,
                packed_dims=net.packed_dims, signed=net.signed,
                net_type=net.net_type, data_type=net.data_type,
                dims=_sig_dims(net, local_syms), multidim=net.multidim,
            )
        )
        comments[name] = (
            f"// To {net.inst} of {net.module}{_mod_comment_suffix(net.module)}"
            + (", ..." if net.multi else "")
        )
    return sigs, comments


def _output_sigs(
    lines: Sequence[str], modules: Mapping[str, ModuleDef],
    full: "Sequence[str] | None" = None,
    pin_scan_cache: "dict | None" = None,
) -> "tuple[list[Signal], dict[str, str]]":
    """AUTOOUTPUT candidates: nets driven by instance OUTPUT ports, minus
    this module's ports and nets feeding an instance input/inout (those are
    internal — /*AUTOWIRE*/ territory).  Nets explicitly declared as
    wire/reg/logic in the body are internal by user intent and are NOT
    promoted to a port — the same rule AUTOINPUT/AUTOINOUT apply (a house
    deviation from verilog-mode, which keeps promoting declared wires)."""
    out_nets = _inst_driven_nets(
        lines, modules, ("output",), simple_only=True,
        _pin_scan=_cached_pin_scan(pin_scan_cache, lines, modules),
    )
    ports, usrdef, _ = _module_tables(lines)
    excluded = set(ports.signals) | _declared_port_names(lines)
    excluded |= set(usrdef.signals)
    excluded |= _connected_names(lines, modules, ("input",), pin_scan_cache)
    excluded |= _connected_names(lines, modules, ("inout",), pin_scan_cache)
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
        if not _declarable(net, local_syms):
            continue
        sigs.append(
            Signal(
                width=net.width, type="io_output", name=name,
                packed_dims=net.packed_dims, signed=net.signed,
                net_type=net.net_type, data_type=net.data_type,
                dims=_sig_dims(net, local_syms), multidim=net.multidim,
            )
        )
        comments[name] = (
            f"// From {net.inst} of {net.module}{_mod_comment_suffix(net.module)}"
            + (", ..." if net.multi else "")
        )
    return sigs, comments


def _inout_sigs(
    lines: Sequence[str], modules: Mapping[str, ModuleDef],
    full: "Sequence[str] | None" = None,
    pin_scan_cache: "dict | None" = None,
) -> "tuple[list[Signal], dict[str, str]]":
    """AUTOINOUT candidates: nets on instance INOUT ports, minus this
    module's ports, nets seen on instance input/output ports, and nets
    used internally (connected to multiple instances — those are
    /*AUTOWIRE*/ territory, declared as wire, not inout).
    """
    inout_nets = _inst_driven_nets(
        lines, modules, ("inout",), simple_only=True,
        _pin_scan=_cached_pin_scan(pin_scan_cache, lines, modules),
    )
    ports, usrdef, _ = _module_tables(lines)
    excluded = set(ports.signals) | _declared_port_names(lines)
    excluded |= _connected_names(lines, modules, ("input",), pin_scan_cache)
    excluded |= _connected_names(lines, modules, ("output",), pin_scan_cache)
    # Nets explicitly declared as wire/reg/logic in the body are internal by
    # user intent — never promote them to top-level inout ports.
    excluded |= set(usrdef.signals)
    from .libdirs import parse_typedef_regexp
    from .inst import set_typedef_regexp

    typedef_re = parse_typedef_regexp(lines)
    set_typedef_regexp(typedef_re)
    local_syms = _local_syms(lines)
    sigs: list[Signal] = []
    comments: dict[str, str] = {}
    for name in sorted(inout_nets):
        if name in excluded:
            continue
        if typedef_re and re.search(typedef_re, name):
            continue
        net = inout_nets[name]
        if not _declarable(net, local_syms):
            continue
        sigs.append(
            Signal(
                width=net.width, type="io_inout", name=name,
                packed_dims=net.packed_dims, signed=net.signed,
                net_type=net.net_type, data_type=net.data_type,
                dims=_sig_dims(net, local_syms), multidim=net.multidim,
            )
        )
        comments[name] = (
            f"// To/From {net.inst} of {net.module}{_mod_comment_suffix(net.module)}"
            + (", ..." if net.multi else "")
        )
    return sigs, comments


# ---------------------------------------------------------------------------
# emission


def _local_declare_nettype(lines: Sequence[str]) -> str | None:
    """verilog-auto-declare-nettype file-local: e.g. ``// verilog-auto-declare-nettype: wire``."""
    m = re.search(r'^\s*//\s*verilog-auto-declare-nettype\s*:\s*"?(\w+)"?', "\n".join(lines), re.M)
    return m.group(1) if m else None


def _apply_declare_nettype(sigs: Sequence[Signal], nettype: str | None) -> list[Signal]:
    """Copy SIGS, filling net_type with NETTYPE for signals without a type
    (verilog-auto-declare-nettype, for `default_nettype none code)."""
    if not nettype:
        return list(sigs)
    out = []
    for s in sigs:
        if not s.data_type and not s.net_type:
            # dataclasses.replace-style copy; Signal is a dataclass
            import dataclasses
            out.append(dataclasses.replace(s, net_type=nettype))
        else:
            out.append(s)
    return out


def _emit_io_line(sig: Signal, max_len: int, keyword: str, indent: int, v2k: bool, comment: str, allow_sv_types: bool = True, wire_type: str = "") -> str:
    body = _emit_signal(sig, max_len, keyword, emacs_dims=True, allow_sv_types=allow_sv_types, wire_type=wire_type)[:-1] + ("," if v2k else ";")
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
    allow_sv_types: bool = True,
    msigs: "Sequence[Signal] | None" = None,
    wire_type: str = "",
) -> str:
    """Expand one /*AUTOINPUT*// /*AUTOOUTPUT*/ marker (its own regexp
    filter, v2k comma repairs) in TEXT.  MSIGS, when given, is the
    marker's pre-filtered signal list (see _regen's first-claim pass);
    otherwise the marker's own regexp filter is applied here."""
    from . import emacs

    masked = mask_comments(text)
    v2k = _in_paren(masked, marker.offset)
    if msigs is None:
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
                _emit_io_line(sig, max_len, keyword, indent, v2k, comments.get(sig.name, ""), allow_sv_types, wire_type)
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
    full: "Sequence[str] | None" = None,
    allow_sv_types: bool = True,
) -> list[str]:
    """Expand every marker of MARK_KEYWORD (each with its own regexp
    filter); the region goes after the marker line, Verilog-2001 comma style
    inside the module header parens, ``;`` style in the body.  The ignore
    regexp is a FILE-local — read it from FULL (the whole buffer) when given,
    since Local Variables sit outside any module span.

    First claim wins, in document order: a signal matching several
    regions is declared only in the first (Emacs behavior — the bare
    /*AUTOOUTPUT*/ after "^a_"/"^z_" regions in autoinst_attr.v stays
    empty because those regions already claimed every output).  Without
    this the later region re-declared every port and the header carried
    duplicate port declarations.
    """
    from . import emacs

    markers = emacs.find_auto_markers(lines, mark_keyword)
    if not markers:
        return list(lines)
    text = "\n".join(lines)
    m = re.search(
        r'^\s*//\s*' + ignore_var + r'\s*:\s*"([^"]+)"',
        "\n".join(full) if full else text,
        re.M,
    )
    ignore_re = m.group(1) if m else None
    claimed: set[str] = set()
    per_marker: list[Sequence[Signal]] = []
    for marker in markers:
        msigs = emacs._filter_regexp(list(sigs), marker.regexp)
        if ignore_re:
            msigs = emacs._filter_regexp(msigs, "?!" + ignore_re)
        fresh = [s for s in msigs if s.name not in claimed]
        claimed.update(s.name for s in fresh)
        per_marker.append(fresh)
    # verilog-auto-wire-type: file-local value wins; otherwise any
    # /*AUTOLOGIC*/ in the buffer makes it "logic" (verilog-auto-
    # logic-setup).  Untyped AIO declarations take this type.
    _full = full if full is not None else lines
    wire_type = emacs._local_wire_type(_full)
    if not wire_type and emacs.find_auto_markers(_full, "AUTOLOGIC"):
        wire_type = "logic"
    for marker, msigs in reversed(list(zip(markers, per_marker))):
        text = _expand_marker(
            text, marker, header, keyword, sigs, comments, ignore_re, allow_sv_types, msigs=msigs, wire_type=wire_type
        )
    return text.split("\n")


# ---------------------------------------------------------------------------
# AUTOINPUT / AUTOOUTPUT (verilog-auto-input / verilog-auto-output)


def auto_input(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, pin_scan_cache: "dict | None" = None) -> list[str]:
    """Regenerate the /*AUTOINPUT*/ input declarations of a module.

    Declares an input port for every net feeding an input port of an
    /*autoinst*/ instance that is not declared or driven inside the module.
    MODULES maps instance module names to their parsed definitions, as in
    :func:`verilog_tooling.wire.auto_wire`.
    ALLOW_SV_TYPES=False (pure Verilog input) drops reg/logic inherited
    from a submodule port, falling back to the implicit net.
    PIN_SCAN_CACHE shares the per-span instance pin scan with the other
    aall passes (see wire._cached_pin_scan); None scans inline.
    """
    from .wire import parse_ignore_concat, set_ignore_concat, _wire_comment_enabled
    from .autodef import set_param_value, set_param_value_type
    from .wire import parse_param_value, parse_param_value_type

    set_ignore_concat(parse_ignore_concat(lines))
    set_param_value(
        param_value if param_value is not None else parse_param_value(lines)
    )
    set_param_value_type(parse_param_value_type(lines))
    from . import emacs as _emacs
    from .wire import set_simplify

    set_simplify(_emacs._local_simplify_expressions(lines))
    lines = kill_auto_input(lines)
    full = list(lines)
    from .inst import map_module_spans

    def _single(span: Sequence[str]) -> list[str]:
        sigs, comments = _input_sigs(span, modules, full, pin_scan_cache)
        if not _wire_comment_enabled(full):
            comments = {}
        sigs = _apply_declare_nettype(sigs, _local_declare_nettype(full))
        return _regen(
            span, "AUTOINPUT", _INPUT_HEADER, "input", sigs, comments, _IGNORE_RE["input"], full, allow_sv_types
        )

    return map_module_spans(lines, _AUTOINPUT_MARK, _single)


def auto_output(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, pin_scan_cache: "dict | None" = None) -> list[str]:
    """Regenerate the /*AUTOOUTPUT*/ output declarations of a module.

    Declares an output port for every net driven by an output port of an
    /*autoinst*/ instance that is not a port of this module and does not
    feed an instance input/inout.  ALLOW_SV_TYPES as in auto_input.
    PIN_SCAN_CACHE shares the per-span instance pin scan with the other
    aall passes (see wire._cached_pin_scan); None scans inline.
    """
    from .wire import parse_ignore_concat, set_ignore_concat, _wire_comment_enabled
    from .autodef import set_param_value, set_param_value_type
    from .wire import parse_param_value, parse_param_value_type

    set_ignore_concat(parse_ignore_concat(lines))
    set_param_value(
        param_value if param_value is not None else parse_param_value(lines)
    )
    set_param_value_type(parse_param_value_type(lines))
    from . import emacs as _emacs
    from .wire import set_simplify

    set_simplify(_emacs._local_simplify_expressions(lines))
    lines = kill_auto_output(lines)
    full = list(lines)
    from .inst import map_module_spans

    def _single(span: Sequence[str]) -> list[str]:
        sigs, comments = _output_sigs(span, modules, full, pin_scan_cache)
        if not _wire_comment_enabled(full):
            comments = {}
        sigs = _apply_declare_nettype(sigs, _local_declare_nettype(full))
        return _regen(
            span, "AUTOOUTPUT", _OUTPUT_HEADER, "output", sigs, comments, _IGNORE_RE["output"], full, allow_sv_types
        )

    return map_module_spans(lines, _AUTOOUTPUT_MARK, _single)


def auto_inout(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, pin_scan_cache: "dict | None" = None) -> list[str]:
    """Regenerate the /*AUTOINOUT*/ inout declarations of a module.

    Declares an inout port for every net connected to an inout port of an
    /*autoinst*/ instance that is not a port of this module and is not seen
    on an instance input/output port.  ALLOW_SV_TYPES as in auto_input.
    PIN_SCAN_CACHE shares the per-span instance pin scan with the other
    aall passes (see wire._cached_pin_scan); None scans inline.
    """
    from .wire import parse_ignore_concat, set_ignore_concat, _wire_comment_enabled
    from .autodef import set_param_value, set_param_value_type
    from .wire import parse_param_value, parse_param_value_type

    set_ignore_concat(parse_ignore_concat(lines))
    set_param_value(
        param_value if param_value is not None else parse_param_value(lines)
    )
    set_param_value_type(parse_param_value_type(lines))
    from . import emacs as _emacs
    from .wire import set_simplify

    set_simplify(_emacs._local_simplify_expressions(lines))
    lines = kill_auto_inout(lines)
    full = list(lines)
    from .inst import map_module_spans

    def _single(span: Sequence[str]) -> list[str]:
        sigs, comments = _inout_sigs(span, modules, full, pin_scan_cache)
        if not _wire_comment_enabled(full):
            comments = {}
        return _regen(
            span, "AUTOINOUT", _INOUT_HEADER, "inout", sigs, comments, _IGNORE_RE["inout"], full, allow_sv_types
        )

    return map_module_spans(lines, _AUTOINOUT_MARK, _single)


def auto_io(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, pin_scan_cache: "dict | None" = None) -> list[str]:
    """AUTOOUTPUT then AUTOINPUT then AUTOINOUT — the verilog-auto order
    (the AIO command).  ALLOW_SV_TYPES as in auto_input.  PARAM_VALUE
    threads verilog-auto-inst-param-value through (None = the buffer's
    file-local setting); without it the CLI flag never reached these
    entry points — each reset the global from file-local only.
    PIN_SCAN_CACHE shares one per-span pin scan across all three passes."""
    lines = auto_output(lines, modules, allow_sv_types, param_value, pin_scan_cache)
    lines = auto_input(lines, modules, allow_sv_types, param_value, pin_scan_cache)
    return auto_inout(lines, modules, allow_sv_types, param_value, pin_scan_cache)


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.wire)


def _vb_auto_input(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate the /*AUTOINPUT*/ input declarations of the buffer."""
    return VerilogBuffer(auto_input(self._lines, modules))


def _vb_auto_output(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate the /*AUTOOUTPUT*/ output declarations of the buffer."""
    return VerilogBuffer(auto_output(self._lines, modules))


def _vb_auto_inout(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate the /*AUTOINOUT*/ inout declarations of the buffer."""
    return VerilogBuffer(auto_inout(self._lines, modules))


VerilogBuffer.auto_input = _vb_auto_input
VerilogBuffer.auto_output = _vb_auto_output
VerilogBuffer.auto_inout = _vb_auto_inout


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.wire)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.inout",
        description="verilog-mode AUTOINPUT/AUTOOUTPUT/AUTOINOUT rewrite",
    )
    parser.add_argument(
        "command",
        choices=["aio", "ain", "aout", "ainout", "kill-ain", "kill-aout", "kill-ainout"],
        help="aio: AUTOOUTPUT+AUTOINPUT+AUTOINOUT; ain/aout/ainout: individually; "
        "kill-*: delete the region",
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
    # Dialect anchor: --ref_file is the real source file (the Vim
    # front-end operates on a temp buffer); sequential-pipeline
    # intermediates (.o) must not flip the rule mid-chain.
    allow_sv_types = Path(args.ref_file or args.in_file).suffix.lower() != ".v"
    if args.command == "kill-ain":
        out = kill_auto_input(lines)
    elif args.command == "kill-aout":
        out = kill_auto_output(lines)
    elif args.command == "kill-ainout":
        out = kill_auto_inout(lines)
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
        out = lines
        if args.command in ("aio", "aout"):
            out = auto_output(out, modules, allow_sv_types, getattr(args, "param_value", None))
        if args.command in ("aio", "ain"):
            out = auto_input(out, modules, allow_sv_types, getattr(args, "param_value", None))
        if args.command in ("aio", "ainout"):
            out = auto_inout(out, modules, allow_sv_types, getattr(args, "param_value", None))
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
