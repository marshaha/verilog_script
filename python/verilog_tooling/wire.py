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
  nil) exempts nets in ``{...}``/``(...)`` connections from the
  AUTOINPUT/AUTOOUTPUT candidacy sets; nil extracts their identifiers
  instead (see :func:`_expr_nets`).  AUTOWIRE/AUTOREG always extract them:
  a net driven through a concat IS driven (a fact, not a candidacy), so it
  is declared (AUTOWIRE, scalar width — the per-member split of the port
  width is unknowable) or excluded from AUTOREG;
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


# house default: exempt {...}/(...) connection nets from AUTOINPUT/
# AUTOOUTPUT candidacy; emacs verilog-auto-ignore-concat defaults to nil
# (extract their signals) — our users wrap signals in {} precisely to
# exempt them from AIO promotion (AUTOWIRE/AUTOREG still record the
# connection as the driven fact it is)
_IGNORE_CONCAT_DEFAULT = True
_IGNORE_CONCAT = _IGNORE_CONCAT_DEFAULT


def set_ignore_concat(value: bool) -> None:
    global _IGNORE_CONCAT
    _IGNORE_CONCAT = value


# NOTE: whether generated wire declarations may carry the variable
# types reg/logic inherited from a submodule port is an explicit
# ALLOW_SV_TYPES parameter threaded through auto_wire (default True
# preserves the submodule's type).  It is deliberately NOT a module
# global (see the note in inout.py) and is NOT threaded through
# auto_reg, whose reg declarations must stay reg.


def _local_bool(lines: Sequence[str], name: str, default: bool) -> bool:
    """A boolean file-local variable: ``// {name}: nil`` -> False,
    ``// {name}: t`` (or anything else) -> True, absent -> DEFAULT."""
    m = re.search(r"^\s*//\s*" + name + r"\s*:\s*(\w+)", "\n".join(lines), re.M)
    if not m:
        return default
    return m.group(1).lower() != "nil"


def parse_ignore_concat(lines: Sequence[str]) -> bool:
    """verilog-auto-ignore-concat file-local: non-nil exempts nets in
    {...}/(...) pin connections from AIO candidacy (AUTOWIRE/AUTOREG
    always extract them as driven facts); nil extracts their signals
    everywhere (emacs default)."""
    return _local_bool(lines, "verilog-auto-ignore-concat", _IGNORE_CONCAT_DEFAULT)


def parse_param_value(lines: Sequence[str]) -> bool:
    """verilog-auto-inst-param-value file-local (default nil)."""
    return _local_bool(lines, "verilog-auto-inst-param-value", False)


def parse_user_boundary(lines: Sequence[str]) -> bool:
    """verilog-autodef-user-boundary file-local (default t): non-nil treats
    declarations outside the /*autodef*/ region as user-owned — a
    conflicting inferred width/dimension is noted with a trailing
    ``//AD_CONFLICT: infer ...`` comment instead of rewriting the
    declaration; nil restores the in-place correction behaviour."""
    return _local_bool(lines, "verilog-autodef-user-boundary", True)


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
    unpacked_ranges: tuple = ()  # explicit unpacked ranges from /*[D].[R]*/
    # connection notes (tpl_arr_edge.v): merged with the indexes at emit
    multidim: tuple = ()  # leading selects of a packed-only port's
    # connection (verilog-mode multidim): unpacked dims rendered BEFORE the
    # packed dims, first driver's wins
    multi: bool = False  # driven/used by several instances (", ..." comment)
    merge_warn: bool = False  # non-numeric bus conflict (", Couldn't Merge")
    driver: object = None  # scan-tuple occurrence index: two records
    # from the same instance never merge, but two same-named
    # instances ARE different drivers (autoinst_rogoff.v)


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


def _same_driver(a: "InstNet", b: "InstNet") -> bool:
    """Same instance occurrence?  With occurrence indexes known, two
    same-named instances are different drivers; without them (legacy
    callers) fall back to the (inst, module) name pair."""
    if a.driver is not None and b.driver is not None:
        return a.driver == b.driver
    return (a.inst, a.module) == (b.inst, b.module)


def _note_net(nets: "dict[str, InstNet]", rec: InstNet) -> None:
    """Add REC to NETS; a second driver from a DIFFERENT instance marks the
    net multi (its comment gets ", ...", like verilog-mode's sig comment
    for a repeated signal) and merges the width per
    verilog-signals-combine-bus."""
    prev = nets.get(rec.name)
    if prev is None:
        nets[rec.name] = rec
        from ._trace import trace_sig, tracing

        if tracing(rec.name):
            trace_sig(
                rec.name,
                f"first-driven inst={rec.inst} module={rec.module} "
                f"width={rec.width} pdims={list(rec.packed_dims)}",
            )
    elif not _same_driver(prev, rec):
        from dataclasses import replace
        from .autodef import _eval_bound

        # Multiple drivers: keep the maximum width (max MSB, min LSB).
        # Evaluate MSB expressions (e.g. "224*1-1" -> 223) and take max.
        def _parse_range(r: str):
            # "223:128" -> (223, 128), "10:10" -> (10, 10)
            parts = r.split(":")
            if len(parts) != 2:
                return None
            msb = _eval_bound(parts[0].strip(), {})
            lsb = _eval_bound(parts[1].strip(), {})
            if isinstance(msb, int) and isinstance(lsb, int):
                return (msb, lsb)
            return None

        # verilog-signals-combine-bus (Emacs): numeric buses merge by
        # max highbit / min lowbit, but a non-numeric bus string wins
        # over any numeric merge (last bus string wins); a bus string
        # already set that differs from the next driver's bits adds
        # ", Couldn't Merge" — except numeric-then-string, which is
        # silent (autoinst_rogoff.v: [15:0] then [WIDTH-1:0] stays
        # [WIDTH-1:0] with no warning)
        def _bus_range(bus):
            if bus is None:
                return None
            if ":" in bus:
                return _parse_range(bus)
            v = _eval_bound(bus.strip(), {})
            return (v, v) if isinstance(v, int) else None

        prev_bus = prev.packed_dims[0] if prev.packed_dims else None
        rec_bus = rec.packed_dims[0] if rec.packed_dims else None
        prev_num = _bus_range(prev_bus)
        rec_num = _bus_range(rec_bus)
        prev_is_string = prev_bus is not None and prev_num is None
        rec_is_string = rec_bus is not None and rec_num is None
        new_width = prev.width
        new_pdims = prev.packed_dims
        buswarn = prev.merge_warn
        if rec_is_string:
            if prev_is_string and prev_bus != rec_bus:
                buswarn = True
            new_width = rec_bus.split(":")[0].strip()
            new_pdims = (rec_bus,) + tuple(prev.packed_dims[1:])
        elif rec_num is not None:
            if prev_is_string and prev_bus != rec_bus:
                buswarn = True
            elif prev_num is not None:
                hi = max(prev_num[0], rec_num[0])
                lo = min(prev_num[1], rec_num[1])
                new_width = str(hi)
                new_pdims = (f"{hi}:{lo}",) + tuple(prev.packed_dims[1:])
            elif prev_bus is None:
                new_width = str(rec_num[0])
                new_pdims = (
                    (f"{rec_num[0]}:{rec_num[1]}",) + tuple(prev.packed_dims[1:])
                )
            # prev is a bus string: it stays, the numeric merge is invisible
        elif prev_is_string:
            buswarn = True  # scalar driver after a bus string
        nets[rec.name] = replace(
            prev, multi=True, width=new_width, packed_dims=new_pdims,
            merge_warn=buswarn,
        )
        from ._trace import trace_sig

        trace_sig(prev.name, f"multi-driver inst={rec.inst} module={rec.module}")


def _split_top_exprs(masked: str) -> list[str]:
    """Split a parenthesized port list on depth-0 commas (comments already
    masked), stripped with empties and AUTOINST markers filtered.
    Position-independent: the result only depends on the instance's own
    text, so it can be cached across passes."""
    return [
        e for e in (p.strip() for p in _split_top_commas(masked))
        if e and "AUTOINST" not in e
    ]


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
    close_idx = emacs._matching_paren(text, open_idx)
    span = text[open_idx + 1 : close_idx]
    exprs = _split_top_exprs(emacs.mask_comments(span))
    _primitive_nets_from_exprs(exprs, module, inst, dir_list, nets, directions, simple_only)


def _primitive_nets_from_exprs(
    exprs: list[str], module: str, inst: str,
    dir_list: list[str],
    nets: dict,
    directions: "tuple[str, ...] | None",
    simple_only: bool,
) -> None:
    """_primitive_nets over pre-split expressions (see _split_top_exprs):
    the direction/simple filtering half, rerunnable on cached exprs."""
    import re
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


def _unwrap_parens(s: str) -> "str | None":
    """Inner text when S is fully wrapped in one balanced paren pair."""
    if not s.startswith("("):
        return None
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[1:-1].strip() if i == len(s) - 1 else None
    return None


def _expr_suffix_dims(
    stripped: str, net: str, npacked: int, nunpacked: int = 0
) -> tuple[str, ...]:
    """Packed dims carried by a plain ``net[G1][G2]...`` connection itself.

    verilog-read-sub-decls-expr: the connection's own bracket suffixes
    are the declaration evidence — the last group is the vector, earlier
    groups are multidim — NOT the submodule port's ranges.  This is what
    a template ``[]`` (expanded to the port's last dim) produces:
    ``b_twod[2:-1]`` declares ``[2:-1] b_twod``, ``c_twod[x][2:-1]``
    declares ``[x] [2:-1] c_twod`` (autoinst_array_braket.v).  Only a
    trailing RANGE group (with a colon) qualifies; a bare index
    (``net[i]``) is an element select and is left to the unpacked-dim
    logic.  MORE groups than the port has packed dims is the multidim
    case instead (autoinput_2d_gaspar.v ``in_momo[0][127:0]`` on a
    ``[127:0]`` port): the leading extra groups are unpacked element
    selects and must fall through to the mdim/unpacked logic — claiming
    them here double-counts the select (``[0] [0] [127:0]``).

    One exception (#54): a LONE numeric bare index on a port with
    exactly one packed dim and no unpacked dims is a bit-select, and
    verilog-mode records it as the sig's whole vector — ``net[3]``
    declares ``[3:3] net`` (verified in Emacs).  A symbolic index
    (``net[i]``) keeps the element-select reading, and with unpacked
    dims present the same text is an array-element select, so the
    exception stays off there too.
    """
    m = re.match(
        r"^\s*" + re.escape(net) + r"((?:\s*\[[^\]]+\])+)\s*$", stripped
    )
    if not m:
        return ()
    groups = re.findall(r"\[([^\]]+)\]", m.group(1))
    if not groups:
        return ()
    if ":" not in groups[-1]:
        if (
            len(groups) == 1
            and npacked == 1
            and nunpacked == 0
            and _clean_dim(groups[-1]).isdigit()
        ):
            g = _clean_dim(groups[-1])
            return (f"{g}:{g}",)
        return ()
    if len(groups) > npacked:
        return ()
    return tuple(_clean_dim(g) for g in groups)


def _span_module_name(span: Sequence[str]) -> "str | None":
    """The module name of a module_spans slice (it always starts at the
    ``module NAME`` line).  Stable across passes that only add declaration
    lines, so it keys the per-span pin-scan cache in _main_aall."""
    if not span:
        return None
    m = re.match(r"\s*module\s+(\w+)", span[0])
    return m.group(1) if m else None


def _cached_pin_scan(
    pin_scan_cache: "dict | None",
    span: Sequence[str],
    modules: Mapping[str, ModuleDef],
) -> "dict | None":
    """get-or-compute _inst_pin_scan for one module span, keyed by the
    span's module name.  Returns None when PIN_SCAN_CACHE is None (or the
    span has no module line) — the caller then scans inline."""
    if pin_scan_cache is None:
        return None
    name = _span_module_name(span)
    if name is None:
        return None
    scan = pin_scan_cache.get(name)
    if scan is None:
        scan = _inst_pin_scan(span, modules)
        pin_scan_cache[name] = scan
    return scan


def _inst_pin_scan(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
) -> dict:
    """Phase 1 of _inst_driven_nets (the expensive part): locate every
    /*autoinst*/-marked instance and every marker-less instance whose
    module resolves in MODULES, and extract its raw pin data.

    The result holds position-independent values only — per-instance
    ``(pin, expr)`` pairs, per-instance parameter values, the const-symbol
    map and the concat setting — so it stays valid while later passes only
    ADD declaration lines (the AUTOLOGIC/AUTOWIRE/AUTOREG trio in aall) and
    can be cached per module span and shared across those passes.
    """
    from . import emacs
    from .autodef import _param_value_on, _const_symbols, _instance_headers

    const_map: dict[str, str] = {}
    if _param_value_on():
        const_map = {k: str(v) for k, v in _const_symbols(lines).items()}
    text = "\n".join(lines)
    text_masked = emacs.mask_comments(text)
    instances: list = []
    seen: set[int] = set()

    def _written_inst(open_idx: int, inst: str) -> str:
        # the instance word ends just before the pin-list paren (modulo
        # whitespace); reattach its backtick for the // From comment,
        # which verilog-mode writes with the name as written
        # (`` `SUBNAME_B `` — autoinst_moddefine.v)
        j = open_idx
        while j > 0 and text[j - 1] in " \t\n":
            j -= 1
        end = j
        while j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
            j -= 1
        if text[j:end] == inst and j > 0 and text[j - 1] == "`":
            return "`" + inst
        return inst

    def _add_named(module: str, inst: str, moddef: ModuleDef, open_idx: int) -> None:
        # a marked instance is also matched by _instance_headers below;
        # dedup on the instance's opening-paren position (both passes find
        # the same instance at the same open_idx) — _note_net is idempotent
        # for the same driver, so the result is unchanged.  NOT (module,
        # inst): two same-named instances (illegal, but the verilog-mode
        # suite has them — autoinst_regexp_inst.v) are distinct drivers.
        if open_idx in seen:
            return
        seen.add(open_idx)
        pin_exprs = list(emacs.inst_pin_connections(text, open_idx))
        # verilog-auto-inst-param-value gates ALL substitution in the
        # sub-decl widths (Emacs default nil): when on, only the
        # instance's own #(...) overrides replace — submodule defaults
        # stay symbolic (d.v experiment: flag t, no override, width
        # stays [WIDTH-1:0]); when off, nothing is substituted.
        # Per-instance, an AUTO_LISP setq before this instance
        # overrides the file-level flag (autoinst_rogoff.v).
        _pv_env = emacs.read_auto_lisp(text, open_idx)
        if "verilog-auto-inst-param-value" in _pv_env:
            _pv = bool(_pv_env["verilog-auto-inst-param-value"])
        else:
            _pv = _param_value_on()
        param_values = (
            emacs.read_inst_param_values(text, open_idx, text_masked)
            if _pv
            else {}
        )
        instances.append(("named", module, inst, moddef, (pin_exprs, param_values)))

    markers = emacs.find_auto_markers(lines, "autoinst")
    stacks = emacs._scan_parens_at(text, [m.offset for m in markers])
    for marker in markers:
        st = stacks[marker.offset]
        if not st:
            continue
        open_idx = st[-1]
        try:
            module, inst = emacs._resolve_instance_at(text, open_idx, text_masked)
            # lookup by the expanded name, but keep the as-written names
            # for the // From comment (verilog-mode writes `SUBNAME_B of
            # `SUBMOD_A.v, ticks included — autoinst_moddefine.v)
            moddef = modules.get(
                emacs.detick_name(module, emacs._buffer_defines(text))
            )
        except ValueError:
            continue

        if moddef is None:
            # Gate primitive (or/buf/etc.): parse positional ports.
            if module in emacs._GATE_PRIMITIVES and open_idx not in seen:
                seen.add(open_idx)
                close_idx = emacs._matching_paren(text, open_idx)
                exprs = _split_top_exprs(
                    emacs.mask_comments(text[open_idx + 1 : close_idx])
                )
                instances.append(
                    ("primitive", module, inst, emacs._GATE_PRIMITIVES[module], exprs)
                )
            continue
        _add_named(module, _written_inst(open_idx, inst), moddef, open_idx)
    # marker-less instances whose module name resolves in MODULES (the same
    # rule autodef documents)
    line_off = [0]
    for ln in lines:
        line_off.append(line_off[-1] + len(ln) + 1)
    for i, (module, inst, hdr_end) in _instance_headers(lines, modules).items():
        hl = i if hdr_end < 0 else hdr_end
        pos = lines[hl].find(inst)
        if pos < 0:
            continue
        rel = lines[hl].find("(", pos + len(inst))
        if rel < 0:
            continue
        _add_named(module, inst, modules[module], line_off[hl] + rel)
    return {
        "const_map": const_map,
        "ignore_concat": _IGNORE_CONCAT,
        "instances": instances,
    }


def _process_named_instance(
    nets: dict[str, InstNet],
    module: str,
    inst: str,
    moddef: ModuleDef,
    pin_exprs: list[tuple[str, str]],
    param_values: dict,
    directions: "tuple[str, ...] | None",
    simple_only: bool,
    concat_ok: bool,
    const_map: dict[str, str],
    ignore_concat: bool,
    driver: object = None,
) -> None:
    """Phase 2 of _inst_driven_nets for one named-port instance: the
    direction/simple/concat filtering and width computation over cached
    pin data (cheap — rerunnable per pass)."""
    from . import emacs

    if directions is None:
        # the AUTOWIRE driver set: outputs and inouts — an interface
        # port connection is an interface instance, never a wire driver
        inst_io = {
            p.name: p for p in moddef.ports if p.direction in ("output", "inout")
        }
    else:
        inst_io = {p.name: p for p in moddef.ports if p.direction in directions}
    for pin, expr in pin_exprs:
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
        # A fully paren-wrapped plain net — (sig), ((sig[3])) — is
        # the net itself: verilog-read-sub-decls-expr strips
        # redundant parens before classifying the connection, and
        # the {} exemption treats (...) alike.  Unwrap so the
        # plain-net path (port width, suffix dims) applies; under
        # verilog-auto-ignore-concat the candidacy scan skips it
        # exactly like a {...} connection, while fact callers
        # (concat_ok) still record the net — with the port's width.
        unwrapped = stripped
        while unwrapped.startswith("("):
            inner = _unwrap_parens(unwrapped)
            if inner is None or not _ELEM_NET.match(inner):
                break
            unwrapped = inner
        if unwrapped != stripped:
            if ignore_concat and not concat_ok:
                continue
            stripped = unwrapped
        if stripped.startswith("{"):
            # verilog-auto-ignore-concat: skip {…} (default) or extract.
            # CONCAT_OK is fact recording (AW/AREG "driven" sets, AIO
            # "is this net wired internally anywhere"): there the
            # identifiers in a {...} expression always count as
            # connected, regardless of the candidacy exemption.
            if not ignore_concat or concat_ok:
                for net, ewidth in _expr_nets(stripped):
                    if param_values:
                        ewidth = emacs._apply_param_values(ewidth, param_values)
                    if const_map:
                        ewidth = emacs._apply_param_values(ewidth, const_map)
                    _note_net(nets, InstNet(net, ewidth, inst, module, driver=driver))
            continue
        if stripped.startswith("("):
            # Parenthesized expression: not a concat, always extract.
            # e.g. ((signal_e3)) -> signal_e3
            for net, ewidth in _expr_nets(stripped):
                if param_values:
                    ewidth = emacs._apply_param_values(ewidth, param_values)
                if const_map:
                    ewidth = emacs._apply_param_values(ewidth, const_map)
                _note_net(nets, InstNet(net, ewidth, inst, module, driver=driver))
            continue
        # Issue #1526: SV cast type'(expr) — extract inner expr for the
        # simple-net check, so t'(x) counts as signal x for AIO candidacy.
        # Fast path: only run the cast regex when there's a quote (casts
        # and literals are rare; the regex backtracks on every pin).
        if "'" in stripped:
            cast_m = re.match(r"^[a-zA-Z_]\w*'\s*\((.*)\)$", stripped)
            check_expr = cast_m.group(1).strip() if cast_m else stripped
        else:
            check_expr = stripped
        if simple_only and not _ELEM_NET.match(check_expr):
            continue
        net = conn_net_name(check_expr)
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
        # packed ranges — including a single packed range: width is
        # msb-only, so a non-zero lsb ([1:-1], [8:1]) would be lost
        # if the single dim were left to the width fallback ([1:0]).
        # A plain net connection's own bracket suffixes are stronger
        # evidence still (verilog-read-sub-decls-expr): a template []
        # already consumed the port's leading dims, so the suffixes
        # ARE the net's dims (autoinst_array_braket.v).  But when the
        # connection carries a /*...*/ dims note, the note wins: for
        # the [].[@] / []. [range] forms the executable suffix is an
        # element select, and the note holds the real packed dims.
        pdims = (
            (() if "/*" in expr else _expr_suffix_dims(stripped, net, len(port.packed), len(port.unpacked)))
            or _conn_packed_dims(expr)
            or tuple(_clean_dim(d) for d in port.packed)
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
        from ._trace import trace_sig, tracing

        if tracing(net):
            trace_sig(
                net,
                f"conn-dims pin={pin} pdims={list(pdims)} width={width} "
                f"inst={inst} module={module}",
            )
        # Apply param substitution to data_type (e.g. OUT_t -> upper_t
        # via #(.OUT_t(upper_t)) when verilog-auto-inst-param-value is on)
        data_type = port.data_type
        if data_type and param_values:
            data_type = emacs._apply_param_values(data_type, param_values)
        if data_type and const_map:
            data_type = emacs._apply_param_values(data_type, const_map)
        _note_net(
            nets,
            InstNet(
                net, width, inst, module, pdims, port.unpacked,
                port.direction, port.signed, port.net_type, data_type,
                driver=driver,
            ),
        )
        # unpacked-array element index: the note's ``.[idx]`` part or the
        # connection's own select when the port is unpacked (both render
        # after the name, verilog-mode memory); a packed-only port whose
        # connection carries a leading select (name[U][packed...]) takes
        # it as an unpacked multidim, rendered before the packed dims.
        uidx = ""
        mdim = ""
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
        else:
            em = _ELEM_NET.match(stripped)
            if em:
                ranges = re.findall(r"\[([^\]]+)\]", em.group(2))
                npacked = len(port.packed)
                if npacked and len(ranges) == npacked + 1:
                    # name[U][packed...]: the leading select is an
                    # unpacked dimension.  The first driver's wins, as
                    # in verilog-signals-combine-bus (verified:
                    # autoinput_2d_gaspar.v -> input [0] [127:0]).
                    mdim = ranges[0].strip()
        if uidx:
            rec = nets[net]
            if uidx not in rec.unpacked_idx:
                from dataclasses import replace

                nets[net] = replace(
                    rec, unpacked_idx=tuple(rec.unpacked_idx) + (uidx,)
                )
        # explicit unpacked RANGES from the connection note
        # (/*[15:0].[-2:-1]*/ from a `net[].[-2:-1]` template): the
        # dot-part brackets carrying a colon are range evidence for
        # the declaration merge (tpl_arr_edge.v)
        from .autodef import _CONN_DIM_NOTE

        nm = _CONN_DIM_NOTE.search(expr)
        if nm:
            rev = tuple(
                d.strip()
                for d in re.findall(r"\[([^\]]+)\]", nm.group(2))
                if ":" in d
            )
            if rev:
                rec = nets[net]
                new = tuple(x for x in rev if x not in rec.unpacked_ranges)
                if new:
                    from dataclasses import replace

                    nets[net] = replace(
                        rec, unpacked_ranges=tuple(rec.unpacked_ranges) + new
                    )
        if mdim:
            rec = nets[net]
            if not rec.multidim:
                from dataclasses import replace

                nets[net] = replace(rec, multidim=(mdim,))


def _inst_driven_nets(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    directions: "tuple[str, ...] | None" = None,
    simple_only: bool = False,
    concat_ok: bool = False,
    *,
    _pin_scan: "dict | None" = None,
) -> dict[str, InstNet]:
    """net -> InstNet for every net connected to an output/inout port of an
    /*autoinst*/ instance — or a marker-less instance whose module name
    resolves in MODULES — (first driver wins).

    DIRECTIONS restricts the port directions considered (default: outputs
    and inouts — the AUTOWIRE driver set; interface ports are never wire
    drivers).  SIMPLE_ONLY keeps only connections that are a single bare
    net (``AUTOINPUT``/``AUTOOUTPUT`` candidates — a concat or expression
    cannot be re-declared).

    A {...} or (...) connection is skipped when the ignore-concat setting is
    on (``verilog-auto-ignore-concat`` file-local, default on) and
    CONCAT_OK is false; otherwise its identifiers are extracted
    (verilog-read-sub-decls-expr).  CONCAT_OK is on for every caller that
    records a FACT (AUTOWIRE/AUTOREG "driven" sets, AIO connection
    bookkeeping); it stays off only for AIO candidacy, where the
    ignore-concat exemption is the user-visible strategy.

    The pin list is parsed with the same full-text machinery as EAI
    (:func:`emacs.inst_pin_connections` on the paren-stack instance
    resolution): pins before the marker, multi-pin lines, multi-line
    expressions and ``.pin`` shorthand all count.

    The port width is passed through the instance's ``#(...)`` parameter
    overrides (e.g. R_USER_WIDTH -> AR_INFO_WIDTH) so the declared wire
    names LOCAL symbols wherever the mapping provides them.  With
    ``verilog-auto-inst-param-value`` on, a width still naming a constant
    parameter of THIS module folds to its integer value.

    The expensive per-instance pin extraction lives in
    :func:`_inst_pin_scan`; pass its result as ``_pin_scan`` to skip it
    (the AUTOLOGIC/AUTOWIRE/AUTOREG trio in aall shares one scan per
    module span via :func:`_cached_pin_scan`)."""
    # _PIN_SCAN: a cached _inst_pin_scan() result for this span (see
    # _cached_pin_scan).  The expensive per-instance pin extraction is then
    # skipped; only the cheap direction/simple/concat filtering reruns.
    scan = _pin_scan if _pin_scan is not None else _inst_pin_scan(lines, modules)
    const_map = scan["const_map"]
    ignore_concat = scan["ignore_concat"]
    nets: dict[str, InstNet] = {}
    for _di, (kind, module, inst, moddef_or_dirs, payload) in enumerate(scan["instances"]):
        if kind == "primitive":
            _primitive_nets_from_exprs(
                payload, module, inst, moddef_or_dirs, nets, directions, simple_only
            )
        else:
            pin_exprs, param_values = payload
            _process_named_instance(
                nets, module, inst, moddef_or_dirs, pin_exprs, param_values,
                directions, simple_only, concat_ok, const_map, ignore_concat,
                driver=_di,
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


def _emit_decl(sig: Signal, max_len: int, keyword: str, indent: int, comment: str = "", allow_sv_types: bool = True) -> str:
    line = " " * indent + _emit_signal(sig, max_len, keyword, emacs_dims=True, allow_sv_types=allow_sv_types)
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
    allow_sv_types: bool = True,
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
                    out.append(_emit_decl(sig, max_len, keyword, indent, comments.get(sig.name, ""), allow_sv_types))
                emitted = True
            out.append(pad + _END_OF_AUTOMATICS)
    return out


# ---------------------------------------------------------------------------
# AUTOWIRE (verilog-auto-wire)


def auto_wire(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    pin_scan_cache: "dict | None" = None,
    allow_sv_types: bool = True,
    param_value: "bool | None" = None,
) -> list[str]:
    """Regenerate the /*AUTOWIRE*/ wire declarations of a module.

    Declares every net driven by an output/inout port of an /*autoinst*/
    instance that is not already declared (port, wire/reg/usrdef, parameter,
    or `define).  MODULES maps instance module names to their parsed
    definitions, as in :func:`verilog_tooling.inst.auto_inst`.
    PIN_SCAN_CACHE shares the per-span instance pin scan with the
    AUTOLOGIC/AUTOREG passes (see _cached_pin_scan); None scans inline.
    ALLOW_SV_TYPES=False (pure Verilog input) drops reg/logic inherited
    from a submodule port, falling back to the implicit net.
    """
    lines = kill_auto_wire(lines)
    full = list(lines)
    from .inst import map_module_spans

    return map_module_spans(
        lines,
        _AUTOWIRE_MARK_FULL,
        lambda span: _auto_wire_single(
            span, modules, full, pin_scan_cache, allow_sv_types, param_value
        ),
    )


def _auto_wire_single(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    full: Sequence[str],
    pin_scan_cache: "dict | None" = None,
    allow_sv_types: bool = True,
    param_value: "bool | None" = None,
) -> list[str]:
    """AUTOWIRE for ONE module span (FULL is the whole buffer, for the
    compilation-unit `define set)."""
    from .libdirs import parse_typedef_regexp
    from .inst import set_typedef_regexp

    typedef_re = parse_typedef_regexp(full)
    set_typedef_regexp(typedef_re)
    set_ignore_concat(parse_ignore_concat(full))
    from .autodef import set_param_value

    set_param_value(
        param_value if param_value is not None else parse_param_value(full)
    )
    driven = _inst_driven_nets(
        lines, modules, concat_ok=True,
        _pin_scan=_cached_pin_scan(pin_scan_cache, lines, modules),
    )
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
    from ._trace import trace_sig, tracing

    for name in sorted(driven):
        if name in declared:
            if tracing(name):
                u = usrdef.signals.get(name)
                trace_sig(
                    name,
                    f"skip-declared "
                    + (
                        f"decl_width={u.width} decl_pdims={list(u.packed_dims)} "
                        f"line={u.line.strip()!r}"
                        if u is not None
                        else "decl=port/param/define"
                    ),
                )
            continue
        if typedef_re and re.search(typedef_re, name):
            continue  # a typedef, not a net (verilog-typedef-regexp)
        net = driven[name]
        # unpacked array: merge the element indexes and explicit ranges
        # from all instances into one dim (abc[0]+abc[2] -> [0:2],
        # [-2:-1]+[0:2] -> [-2:2]); a whole-array connection of an
        # unpacked port keeps the port's own dims
        dims: tuple = ()
        merge_conflict = False
        if net.unpacked_idx or net.unpacked_ranges:
            from .autodef import _merge_unpacked_evidence

            merged, merge_conflict = _merge_unpacked_evidence(
                net.unpacked_idx, net.unpacked_ranges
            )
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
                multidim=net.multidim,
            )
        )
        if tracing(name):
            trace_sig(
                name,
                f"emit-decl width={net.width} packed_dims={list(net.packed_dims)} "
                f"dims={list(dims)} signed={net.signed}",
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
            + (", Couldn't Merge" if (merge_conflict or net.merge_warn) else "")
        )
    if not _wire_comment_enabled(full):
        comments = {}
    return _regen(lines, _AUTOWIRE_MARK_FULL, _WIRE_HEADER, "wire ", sigs, comments, allow_sv_types)


# ---------------------------------------------------------------------------
# AUTOREG (verilog-auto-reg)


def auto_reg(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef] | None = None,
    pin_scan_cache: "dict | None" = None,
) -> list[str]:
    """Regenerate the /*AUTOREG*/ reg declarations of a module.

    Declares every output port of the current module that carries no
    wire/reg keyword and is not otherwise declared, assign-driven, or driven
    by an instance output/inout.  MODULES (optional) is used for the
    instance-driven exclusion, as in :func:`auto_wire`.
    PIN_SCAN_CACHE shares the per-span instance pin scan with the
    AUTOLOGIC/AUTOWIRE passes (see _cached_pin_scan); None scans inline.
    """
    lines = kill_auto_reg(lines)
    full = list(lines)
    from .inst import map_module_spans

    return map_module_spans(
        lines,
        _AUTOREG_MARK_FULL,
        lambda span: _auto_reg_single(span, modules, full, pin_scan_cache)
    )


def _auto_reg_single(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef] | None,
    full: Sequence[str],
    pin_scan_cache: "dict | None" = None,
) -> list[str]:
    """AUTOREG for ONE module span (FULL for the compilation-unit `define set)."""
    set_ignore_concat(parse_ignore_concat(full))
    from .autodef import set_param_value

    set_param_value(parse_param_value(full))
    ports, usrdef, assigns = _module_tables(lines)
    driven = set(
        _inst_driven_nets(
            lines, modules or {}, concat_ok=True,
            _pin_scan=_cached_pin_scan(pin_scan_cache, lines, modules or {}),
        )
    )
    excluded = set(usrdef.signals) | assigns | driven
    excluded |= get_all_defs(full) | get_all_paras(lines)
    from ._trace import trace_sig, tracing

    sigs = []
    for name, sig in ports.signals.items():
        if sig.io_dir != "output" or sig.has_defined:
            continue
        if name in excluded:
            if tracing(name):
                why = (
                    "usrdef" if name in usrdef.signals
                    else "driven" if name in driven
                    else "assign" if name in assigns
                    else "param/define"
                )
                trace_sig(name, f"skip-declared cmd=areg why={why}")
            continue
        sigs.append(
            Signal(
                width=sig.width, type="io_reg", name=name, signed=sig.signed,
                net_type=sig.net_type, data_type=sig.data_type,
            )
        )
        trace_sig(name, f"emit-decl cmd=areg width={sig.width}")
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
    # Dialect anchor: --ref_file is the real source file (see inout.main).
    allow_sv_types = Path(args.ref_file or args.in_file).suffix.lower() != ".v"
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
        out = auto_wire(lines, modules, allow_sv_types=allow_sv_types, param_value=getattr(args, "param_value", None)) if args.command == "aw" else auto_reg(lines, modules)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
