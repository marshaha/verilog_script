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
                guard=net.guard,
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
                guard=net.guard,
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
                guard=net.guard,
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


def _emit_io_line(sig: Signal, max_len: int, keyword: str, indent: int, v2k: bool, comment: str, allow_sv_types: bool = True, wire_type: str = "", comma: "bool | None" = None, leading: bool = False) -> str:
    if comma is None:
        term = "," if v2k else ";"
    else:
        term = "," if comma else ("" if v2k else ";")
    body = _emit_signal(sig, max_len, keyword, emacs_dims=True, allow_sv_types=allow_sv_types, wire_type=wire_type)[:-1] + term
    line = " " * indent + (", " if leading else "") + body
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


def _combine_guards(*parts: "tuple[tuple[str, bool], ...]") -> "tuple | None":
    """Merge guard conditions, or None on a contradiction.  Mirrors the
    combine step inside :func:`inst.guard_separator_plan`."""
    seen: dict[str, bool] = {}
    out: list[tuple[str, bool]] = []
    for part in parts:
        for macro, pol in part:
            if macro in seen:
                if seen[macro] != pol:
                    return None
                continue
            seen[macro] = pol
            out.append((macro, pol))
    return tuple(out)


def _is_bare_v2k_names(code: str) -> bool:
    return re.match(
        r"^,?\s*[A-Za-z_][\w$]*(?:\s*,\s*[A-Za-z_][\w$]*)*\s*,?$",
        code.strip(),
    ) is not None


def _parse_following_v2k(text: str, insert_at: int):
    """Parse declarations following a v2k marker, up to the header close.
    Later generated regions already exist because markers expand in reverse
    document order; hand-written ANSI and bare-name ports participate in the
    same boundary plan.  Returns (decls, ok), where decls contains
    (guard, has_leading_comma) in document order."""
    from .comments import mask_comments
    from .inst import _PP_BRANCH, _flat_guard, _guard_stack_after

    masked = mask_comments(text)
    depth = 0
    close = -1
    i = insert_at
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
        return [], False
    stack: list = []
    for ln in text[:insert_at].split("\n"):
        _guard_stack_after(stack, ln)
    lines = text[insert_at:close].split("\n")
    code_lines = masked[insert_at:close].split("\n")
    decls: list = []
    in_region = False
    in_hand_decl = False
    for li, ln in enumerate(lines):
        s = ln.strip()
        code = code_lines[li].strip()
        if "Beginning of automatic" in ln:
            in_region = True
            in_hand_decl = False
            continue
        if "End of automatics" in ln:
            in_region = False
            in_hand_decl = False
            continue
        if not code or s.startswith("//") or s.startswith("/*"):
            # A /*AUTO...*/ marker line aborts only when no generated
            # region follows it (an unexpanded later marker); an
            # expanded region's own marker line is followed by its
            # Beginning comment.
            if s.startswith("/*AUTO"):
                rest = lines[li + 1 :]
                nxt = next(
                    (k for k, l2 in enumerate(rest) if l2.strip().startswith("/*AUTO") or "Beginning of automatic" in l2),
                    None,
                )
                if nxt is None or "Beginning of automatic" not in rest[nxt]:
                    return [], False
            continue
        if _PP_BRANCH.match(code):
            _guard_stack_after(stack, code)
            continue
        if code == ",":
            continue  # a separator line from another region's plan
        if in_region:
            decls.append((_flat_guard(stack), code.startswith(",")))
            continue
        if re.match(r"^,?\s*(?:input|output|inout)\b", code):
            decls.append((_flat_guard(stack), code.startswith(",")))
            in_hand_decl = True
            continue
        if in_hand_decl:
            continue  # continuation of a multi-line hand-written declaration
        if _is_bare_v2k_names(code):
            decls.append((_flat_guard(stack), code.startswith(",")))
            continue
        return [], False
    return decls, True


def _parse_preceding_v2k(text: str, insert_at: int):
    """Locate the nearest preceding ANSI or bare-name port and its guard.
    Continuation lines advance the comma position to the declaration's actual
    end; a bare name after a completed declaration starts a new node with its
    own guard.  The result also records whether the port belongs to a generated
    region, whose own boundary plan retains ownership of cross-region commas."""
    from .comments import mask_comments
    from .inst import _PP_BRANCH, _flat_guard, _guard_stack_after

    prefix = text[:insert_at]
    masked = mask_comments(prefix)
    stack: list = []
    found = None
    decl_guard = None
    decl_generated = False
    decl_complete = False
    in_generated = False
    offset = 0
    for raw, ln in zip(
        prefix.splitlines(keepends=True), masked.splitlines(keepends=True)
    ):
        if "Beginning of automatic" in raw:
            in_generated = True
        elif "End of automatics" in raw:
            in_generated = False
        code = ln.rstrip("\r\n")
        if _PP_BRANCH.match(code):
            _guard_stack_after(stack, code)
        else:
            if re.search(r"\bmodule\b", code):
                found = None
                decl_guard = None
                decl_generated = False
                decl_complete = False
            end = len(code.rstrip())
            if end and code.strip() != ",":
                if re.search(r"\b(?:input|output|inout)\b", code):
                    decl_guard = _flat_guard(stack)
                    decl_generated = in_generated
                elif _is_bare_v2k_names(code) and (found is None or decl_complete):
                    decl_guard = _flat_guard(stack)
                    decl_generated = in_generated
                elif found is None:
                    decl_guard = None
                if decl_guard is not None:
                    has_comma = code[end - 1] == ","
                    pos = end - 1 if has_comma else end
                    found = (decl_guard, offset + pos, has_comma, decl_generated)
                    decl_complete = has_comma
        offset += len(ln)
    return found


def _set_preceding_v2k_comma(text: str, preceding, want: bool):
    _guard, pos, has_comma, _generated = preceding
    if want == has_comma:
        return text, 0
    if want:
        return text[:pos] + "," + text[pos:], 1
    return text[:pos] + text[pos + 1 :], -1


def _ansiify_following_bare_v2k(text: str, offset: int) -> str:
    """Move full body declarations onto bare ports following generated ANSI ports.

    A body-declared bare name is legal before the first ANSI declaration, but
    is a duplicate declaration after one.  Convert only names with a matching
    body declaration under the same guard, preserving types, dimensions, header
    position, commas, comments and preprocessor conditions.  A body trailing
    comment remains as a standalone comment in the same guard.
    """
    from .comments import mask_comments
    from .inst import _PP_BRANCH, _flat_guard, _guard_stack_after

    masked = mask_comments(text)
    depth = 0
    close = -1
    i = offset
    while i < len(masked):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            if depth == 0:
                close = i
                break
            depth -= 1
        i += 1
    if close < 0:
        return text
    endmodule = re.search(r"^\s*endmodule\b", masked[close + 1 :], re.M)
    body_end = close + 1 + endmodule.start() if endmodule else len(text)

    candidates = []
    stack: list = []
    for ln in text[:offset].split("\n"):
        _guard_stack_after(stack, ln)
    pos = offset
    for code_line in masked[offset:close].splitlines(keepends=True):
        code = code_line.rstrip("\r\n")
        _guard_stack_after(stack, code)
        if _is_bare_v2k_names(code):
            for match in re.finditer(r"[A-Za-z_][\w$]*", code):
                candidates.append((match.group(0), _flat_guard(stack), pos + match.start()))
        pos += len(code_line)

    edits = []
    used_body_lines = set()
    for name, guard, header_pos in candidates:
        body_stack: list = []
        body_pos = close + 1
        found = None
        for raw_line, code_line in zip(
            text[close + 1 : body_end].splitlines(keepends=True),
            masked[close + 1 : body_end].splitlines(keepends=True),
        ):
            code = code_line.rstrip("\r\n")
            line_end = body_pos + len(code_line)
            if _PP_BRANCH.match(code):
                _guard_stack_after(body_stack, code)
            elif (
                body_pos not in used_body_lines
                and _flat_guard(body_stack) == guard
                and "," not in code
            ):
                name_match = re.search(
                    r"\b" + re.escape(name) + r"\b((?:\s*\[[^\]]*\])*)\s*;\s*$",
                    code,
                )
                if name_match:
                    prefix = code[: name_match.start()].strip()
                    if re.match(r"^(?:input|output|inout)\b", prefix):
                        found = (
                            prefix,
                            name_match.group(1),
                            body_pos,
                            line_end,
                            raw_line,
                            len(code.rstrip()),
                        )
                        break
            body_pos = line_end
        if found is not None:
            prefix, unpacked, body_start, body_end_line, raw_line, code_end = found
            used_body_lines.add(body_start)
            edits.append((header_pos, len(name), prefix + " " + name + unpacked))
            tail = raw_line[code_end:].strip()
            if tail:
                indent = re.match(r"[ \t]*", raw_line).group(0)
                newline = "\r\n" if raw_line.endswith("\r\n") else "\n"
                body_replacement = indent + tail + newline
            else:
                body_replacement = ""
            edits.append((body_start, body_end_line - body_start, body_replacement))
    for pos, length, replacement in sorted(edits, reverse=True):
        text = text[:pos] + replacement + text[pos + length :]
    return text


def _guard_negation_choices(guard):
    """Disjoint disjuncts for NOT guard, without a raw negated conjunction."""
    choices = []
    prefix = []
    for macro, pol in guard:
        choices.append(tuple(prefix + [(macro, not pol)]))
        prefix.append((macro, pol))
    return choices


def _boundary_bridge_conds(my_guards, following_guards, trailing_my, emitted_leading):
    """Bridge cross-region direct predecessors not separated by a pin line."""
    from itertools import product

    conds = []
    seen_conds = set()
    n = len(my_guards)
    all_guards = my_guards + following_guards
    for fj, fg in enumerate(following_guards):
        if emitted_leading[fj]:
            continue
        j = n + fj
        for i in range(n):
            if trailing_my[i]:
                continue
            choices = []
            blocked = False
            for k in range(i + 1, j):
                alternatives = _guard_negation_choices(all_guards[k])
                if not alternatives:
                    blocked = True
                    break
                choices.append(alternatives)
            if blocked:
                continue
            for negated in product(*choices):
                cond = _combine_guards(my_guards[i], fg, *negated)
                key = frozenset(cond) if cond is not None else None
                if cond is not None and key not in seen_conds:
                    seen_conds.add(key)
                    conds.append(cond)
    return conds


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


def _repair_close_comma(text: str, from_offset: int, region_start: "int | None" = None) -> str:
    """verilog-repair-close-comma: delete a dangling ``,`` before the close
    paren of the paren level open at FROM_OFFSET (the module header's).
    The scan back skips directive lines, but only inside the region
    being repaired (at or after REGION_START): a guarded last port's
    boundary comma sits before its own `` `endif`` lines, and it is
    dangling in every branch — the port is last overall, so its
    trailing comma is never needed — so deleting it from the text is
    unconditionally correct.  Past the region start the scan stops at
    the first non-whitespace character: a directive line there closes
    a guard opened before the region (a hand-written `` `ifdef`` around
    an earlier port), and the comma before it is that port's separator
    to the following ports, not a dangling comma.  With REGION_START
    unset the scan never skips a directive line (the old behaviour)."""
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
    while True:
        while j >= 0 and masked[j] in " \t\n":
            j -= 1
        if j < 0:
            return text
        ls = masked.rfind("\n", 0, j) + 1
        if (
            masked[ls : j + 1].strip().startswith("`")
            and region_start is not None
            and ls >= region_start
        ):
            j = ls - 1  # the region's own guard closer: skip it
            continue
        break
    if masked[j] == ",":
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
    if v2k and msigs:
        text = _ansiify_following_bare_v2k(text, marker.end)
        masked = mask_comments(text)
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
        cur: tuple = ()
        if v2k:
            # Plan both hand-written and generated neighbours together.  The
            # preceding declaration's comma is repaired from the same plan as
            # this region; following declarations already have their own text,
            # so cross-region bridges account for their actual leading commas.
            # Unsupported following shapes retain the legacy boundary comma,
            # which _repair_close_comma removes when the header closes here.
            from .inst import guard_separator_plan

            _my_guards = [s.guard for s in msigs]
            _following, _combined_ok = _parse_following_v2k(text, insert_at)
            _following_guards = [g for g, _ in _following]
            _emit_leading = [hl for _, hl in _following]
            _all_guards = _my_guards + (_following_guards if _combined_ok else [])
            _forced_leading = [False] * len(msigs) + (
                _emit_leading if _combined_ok else []
            )
            _trailing_all, _leading_all, _bridges_all = guard_separator_plan(
                _all_guards, forced_leading=_forced_leading
            )
            _preceding = _parse_preceding_v2k(text, insert_at)
            if _preceding is not None and _preceding[3]:
                _preceding = None
            _offset = 0
            if _preceding is not None:
                _offset = 1
                _fixed_leading = (
                    [False, None]
                    + list(_leading_all[1:len(msigs)])
                    + (_emit_leading if _combined_ok else [])
                )
                _trailing_all, _leading_all, _bridges_all = guard_separator_plan(
                    [_preceding[0]] + _all_guards,
                    fixed_leading=_fixed_leading,
                )
                text, _shift = _set_preceding_v2k_comma(
                    text, _preceding, _trailing_all[0]
                )
                insert_at += _shift
            _end = _offset + len(msigs)
            _trailing = _trailing_all[_offset:_end]
            _leading = _leading_all[_offset:_end]
            _bridges = {
                j - _offset: v
                for j, v in _bridges_all.items()
                if _offset <= j < _end
            }
            if _combined_ok:
                _before_boundary = (
                    ([_preceding[0]] if _preceding is not None else []) + _my_guards
                )
                _boundary = _boundary_bridge_conds(
                    _before_boundary,
                    _following_guards,
                    _trailing_all[: len(_before_boundary)],
                    _emit_leading,
                )
                _last_comma = None
            else:
                _boundary = []
                _last_comma = True
        for idx, sig in enumerate(msigs):
            if v2k:
                for cond in _bridges.get(idx, []):
                    # Self-contained bridge: close any open guard,
                    # emit the comma under the bridge condition, close.
                    while cur:
                        rows.append(pad + "`endif")
                        cur = cur[:-1]
                    for macro, pol in cond:
                        rows.append(pad + ("`ifdef " if pol else "`ifndef ") + macro)
                    rows.append(pad + ",")
                    for _macro, _pol in cond:
                        rows.append(pad + "`endif")
            # Guard transitions (body and v2k regions alike): conditions
            # nest (one directive per condition, like the AUTOINST
            # emitter); a flat guard from an `elsif chain re-emits as
            # nested directives with the same conjunction.
            target = sig.guard
            while cur != target[: len(cur)]:
                rows.append(pad + "`endif")
                cur = cur[:-1]
            for macro, pol in target[len(cur):]:
                rows.append(pad + ("`ifdef " if pol else "`ifndef ") + macro)
            cur = target
            if v2k:
                if _last_comma is None:
                    comma = _trailing[idx]
                else:
                    comma = True if idx == len(msigs) - 1 else _trailing[idx]
                rows.append(
                    _emit_io_line(sig, max_len, keyword, indent, v2k, comments.get(sig.name, ""), allow_sv_types, wire_type, comma=comma, leading=_leading[idx])
                )
            else:
                rows.append(
                    _emit_io_line(sig, max_len, keyword, indent, v2k, comments.get(sig.name, ""), allow_sv_types, wire_type)
                )
        while cur:
            rows.append(pad + "`endif")
            cur = cur[:-1]
        if v2k:
            # Boundary bridges live inside the region (before the End
            # line) so killing the region removes them too.
            for cond in _boundary:
                for macro, pol in cond:
                    rows.append(pad + ("`ifdef " if pol else "`ifndef ") + macro)
                rows.append(pad + ",")
                for _macro, _pol in cond:
                    rows.append(pad + "`endif")
        rows.append(pad + _END_OF_AUTOMATICS)
        region = "\n".join(rows) + "\n"
        text = text[:insert_at] + region + text[insert_at:]
    if v2k:
        text = _repair_close_comma(text, insert_at + len(region), insert_at)
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


def auto_input(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, ctx: "AallContext | None" = None) -> list[str]:
    """Regenerate the /*AUTOINPUT*/ input declarations of a module.

    Declares an input port for every net feeding an input port of an
    /*autoinst*/ instance that is not declared or driven inside the module.
    MODULES maps instance module names to their parsed definitions, as in
    :func:`verilog_tooling.wire.auto_wire`.
    ALLOW_SV_TYPES=False (pure Verilog input) drops reg/logic inherited
    from a submodule port, falling back to the implicit net.
    CTX is the per-run AallContext (wire.AallContext) holding the shared per-span
    pin scan; None scans inline.
    """
    pin_scan_cache = ctx.pin_scan_cache if ctx is not None else None
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


def auto_output(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, ctx: "AallContext | None" = None) -> list[str]:
    """Regenerate the /*AUTOOUTPUT*/ output declarations of a module.

    Declares an output port for every net driven by an output port of an
    /*autoinst*/ instance that is not a port of this module and does not
    feed an instance input/inout.  ALLOW_SV_TYPES as in auto_input.
    CTX is the per-run AallContext (wire.AallContext) holding the shared per-span
    pin scan; None scans inline.
    """
    pin_scan_cache = ctx.pin_scan_cache if ctx is not None else None
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


def auto_inout(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, ctx: "AallContext | None" = None) -> list[str]:
    """Regenerate the /*AUTOINOUT*/ inout declarations of a module.

    Declares an inout port for every net connected to an inout port of an
    /*autoinst*/ instance that is not a port of this module and is not seen
    on an instance input/output port.  ALLOW_SV_TYPES as in auto_input.
    CTX is the per-run AallContext (wire.AallContext) holding the shared per-span
    pin scan; None scans inline.
    """
    pin_scan_cache = ctx.pin_scan_cache if ctx is not None else None
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


def auto_io(lines: Sequence[str], modules: Mapping[str, ModuleDef], allow_sv_types: bool = True, param_value: "bool | None" = None, ctx: "AallContext | None" = None) -> list[str]:
    """Run AUTOOUTPUT/AUTOINPUT/AUTOINOUT in verilog-auto order.

    v2k header regions affect earlier regions' boundary commas.  Iterate that
    order internally until the text is stable so one AIO call sees every later
    region expanded and still returns an idempotent result.
    """
    def _once(current):
        current = auto_output(current, modules, allow_sv_types, param_value, ctx)
        current = auto_input(current, modules, allow_sv_types, param_value, ctx)
        return auto_inout(current, modules, allow_sv_types, param_value, ctx)

    text = "\n".join(lines)
    masked = mask_comments(text)
    marker_count = sum(
        1
        for match in re.finditer(r"/\*\s*AUTO(?:INPUT|OUTPUT|INOUT)\b", text, re.I)
        if _in_paren(masked, match.start())
    )
    if not marker_count:
        return _once(lines)
    current = list(lines)
    for _attempt in range(marker_count + 1):
        updated = _once(current)
        if updated == current:
            return current
        current = updated
    return current


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
