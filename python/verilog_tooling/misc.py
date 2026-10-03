"""Python rewrite of verilog-mode's AUTOASCIIENUM / AUTOLOGIC / AUTOTIEOFF /
AUTOUNUSED / AUTOUNDEF / AUTOINSERTLISP / AUTOINSERTLAST expansions.

Implements ``verilog-auto-ascii-enum`` (verilog-mode.el ~line 14694),
``verilog-auto-logic`` (~13256, via ``verilog-auto-logic-setup`` ~13250),
``verilog-auto-tieoff`` (~14446, tieoff values via ``verilog-sig-tieoff``
~9128), ``verilog-auto-unused`` (~14602), ``verilog-auto-undef`` (~14535),
``verilog-auto-insert-lisp`` (~14143) and ``verilog-auto-insert-last``
(~14209):

- ``/*AUTOASCIIENUM("sig", "ascii_sig"[, "prefix"[, "onehot"]])*/`` decodes
  an enumerated state signal into an ASCII register: parameters/localparams
  tagged with ``// auto enum NAME`` / ``/* auto enum NAME */`` (``synopsys
  enum`` works too) become the case items; the signal named by the first
  argument must carry the same enum tag on its declaration.  Emits the
  ``// Beginning of automatic ASCII enum decoding`` region (ascii register
  declaration + ``always``/``case`` decoder).
- ``/*AUTOLOGIC*/`` is AUTOWIRE with ``logic`` declarations.  Like emacs it
  reuses the ``// Beginning of automatic wires`` region header and honors
  the ``verilog-auto-wire-type`` file-local variable.
- ``/*AUTOTIEOFF*/`` declares ``wire [w:0] o = <w>'h0;`` for module outputs
  that are neither declared as reg/wire nor driven by an /*autoinst*/
  submodule output/inout.  Signals matching the ``verilog-active-low-regexp``
  file-local tie to ``~<w>'h0`` (deasserted = 1); ``verilog-auto-reset-widths``
  selects the constant style.
- ``/*AUTOUNUSED*/`` is an *inline* marker (e.g. inside
  ``&{1'b0, /*AUTOUNUSED*/ 1'b0}``); it expands to the
  ``// Beginning of automatic unused inputs`` region listing the module's
  inputs/inouts that are not connected to any /*autoinst*/ instance
  input/inout pin.
- ``/*AUTOUNDEF*/`` (optional ``("regexp")``) emits `` `undef NAME`` for
  `` `define``s seen since the previous AUTOUNDEF marker (names already
  `` `undef``ed are suppressed), sorted by name.
- ``/*AUTOINSERTLISP(!shell-cmd)*/`` / ``/*AUTOINSERTLAST(!shell-cmd)*/``
  run the shell command and insert its stdout into the
  ``// Beginning of automatic insert lisp`` region.  Emacs evaluates lisp;
  Vim has no elisp, so the ``!`` shell form is this port's equivalent (cf.
  the ``(shell-command-to-string "echo //hello")`` example in the elisp
  docstring).  A marker whose content does not start with ``!`` warns and
  expands to nothing, mirroring ``verilog-delete-empty-auto-pair``.

Each expansion is idempotent: the corresponding ``kill_*`` removes the
previously generated region (the marker line is kept; for AUTOUNUSED the
inline marker line is kept and only the generated lines are dropped).

Fidelity decisions vs verilog-mode:

- Declaration name columns follow ``verilog-insert-one-definition``
  (``max(24, indent-pt+16)``); the ``// Decode of`` comment and the tieoff
  ``= value;`` sit at ``max(48, indent-pt+40)``.  The tests_ok goldens
  additionally went through ``verilog-pretty-declarations`` (the test
  harness's indent pass), which this project does not replicate — the same
  standing decision as :mod:`verilog_tooling.wire`.
- AUTOTIEOFF values follow ``verilog-sig-tieoff`` exactly: ``<w>'h0``
  (``<w>'sh0`` for signed, ``~``-prefixed for active-low), ``{W{1'b0}}``
  for non-numeric widths, ``0`` / ``'0`` under
  ``verilog-auto-reset-widths: nil`` / ``unbased``.
- AUTOUNDEF emits `` `undef`` at column 0 (preprocessor-directive
  convention).  Emacs's raw ``verilog-auto`` indents to indent-pt; its
  indent pass then moves directives to column 0, which is what the golden
  reflects.
- Kills are marker-scoped (the region following each marker), not global:
  AUTOLOGIC shares AUTOWIRE's ``// Beginning of automatic wires`` header,
  so a global kill would also drop a sibling /*AUTOWIRE*/ region.
- AUTOASCIIENUM's ``/* auto state_vector <sig> */`` comments are
  informational only (emacs never reads them); the decoded signal is the
  marker's first argument and must carry an enum tag.
- The enum/decl scanner does not understand typedef data types
  (``input foo_t x``); like ``verilog_tooling.autodef``'s own parsers it
  expects plain ``reg``/``wire``/``logic``/direction declarations.
- ``verilog-auto-ignore-concat`` does not affect AUTOUNUSED's "used"
  computation: emacs's ``verilog-read-sub-decls-expr`` always descends into
  ``{...}``/``(...)`` connections, so identifiers there are extracted
  unconditionally.
- Elisp regexps are matched case-insensitively (``verilog-case-fold`` is t
  by default); the ``?!`` inversion prefix of
  ``verilog-signals-not-matching-regexp`` is honored for the two
  ignore-regexps.

Configuration (file-local variables in the ``// Local Variables:`` section):

- ``verilog-auto-wire-type`` — e.g. ``"logic"``: AUTOLOGIC's declaration
  keyword (defaults to ``logic`` for /*AUTOLOGIC*/); also selects the
  AUTOASCIIENUM decode-register keyword (default ``reg``).
- ``verilog-active-low-regexp`` — signals tying to 1 under AUTOTIEOFF.
- ``verilog-auto-reset-widths`` — ``t`` (default) / ``nil`` / ``unbased``.
- ``verilog-auto-tieoff-declaration`` — ``"wire"`` (default) / ``"assign"``
  / another datatype for AUTOTIEOFF.
- ``verilog-auto-tieoff-ignore-regexp`` /
  ``verilog-auto-unused-ignore-regexp`` — signals skipped by AUTOTIEOFF /
  AUTOUNUSED.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from . import wire as _wire
from .autodef import Signal, get_all_paras
from .inst import ModuleDef, Port, VerilogBuffer

# ---------------------------------------------------------------------------
# markers & region headers (conventions follow verilog_tooling.wire)


_ASCIIENUM_MARK = re.compile(r"/\*\s*\b(autoasciienum|AUTOASCIIENUM)\b")
_LOGIC_MARK = re.compile(r"^\s*/\*\s*\b(autologic|AUTOLOGIC)\b\s*\*/")
_TIEOFF_MARK = re.compile(r"^\s*/\*\s*\b(autotieoff|AUTOTIEOFF)\b\s*\*/")
_UNUSED_MARK = re.compile(r"/\*\s*\b(autounused|AUTOUNUSED)\b")
_UNDEF_MARK = re.compile(r"/\*\s*\b(autoundef|AUTOUNDEF)\b")
_INSERTLISP_MARK = re.compile(r"/\*\s*\b(autoinsertlisp|AUTOINSERTLISP)\b\s*\(")
_INSERTLAST_MARK = re.compile(r"/\*\s*\b(autoinsertlast|AUTOINSERTLAST)\b\s*\(")

_ASCIIENUM_HEADER = "// Beginning of automatic ASCII enum decoding"
_ASCIIENUM_HEADER_RE = re.compile(r"^\s*// Beginning of automatic ASCII enum decoding\b")
_WIRES_HEADER_RE = re.compile(r"^\s*// Beginning of automatic wires\b")
_TIEOFF_HEADER = "// Beginning of automatic tieoffs (for this module's unterminated outputs)"
_TIEOFF_HEADER_RE = re.compile(r"^\s*// Beginning of automatic tieoffs\b")
_UNUSED_HEADER = "// Beginning of automatic unused inputs"
_UNUSED_HEADER_RE = re.compile(r"^\s*// Beginning of automatic unused inputs\b")
_UNDEF_HEADER = "// Beginning of automatic undefs"
_UNDEF_HEADER_RE = re.compile(r"^\s*// Beginning of automatic undefs\b")
_INSERT_HEADER = "// Beginning of automatic insert lisp"
_INSERT_HEADER_RE = re.compile(r"^\s*// Beginning of automatic insert lisp\b")

_END_OF_AUTOMATICS = _wire._END_OF_AUTOMATICS

# verilog-insert-one-definition / verilog-insert-definition geometry
def _name_col(indent: int) -> int:
    return max(24, indent + 16)


def _value_col(indent: int) -> int:
    return max(48, indent + 40)
_INDENT_LEVEL = 3  # verilog-indent-level default
_CASE_INDENT = 2  # verilog-case-indent default


# ---------------------------------------------------------------------------
# file-local configuration (Local Variables section)


def _local_str(lines: Sequence[str], name: str) -> str | None:
    """A string file-local variable: ``// {name}: "value"`` (quotes stripped)
    or ``// {name}: value``; None when absent."""
    m = re.search(
        r"^\s*//\s*" + re.escape(name) + r"\s*:\s*(\"[^\"]*\"|\S+)",
        "\n".join(lines),
        re.M,
    )
    if not m:
        return None
    val = m.group(1)
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        val = val[1:-1]
    return val


def parse_wire_type(lines: Sequence[str]) -> str | None:
    """verilog-auto-wire-type file-local (default nil), e.g. ``"logic"``."""
    return _local_str(lines, "verilog-auto-wire-type")


def _parse_reset_widths(lines: Sequence[str]) -> str:
    """verilog-auto-reset-widths file-local: 'widths' (t/default), 'zero'
    (nil) or 'unbased'."""
    val = _local_str(lines, "verilog-auto-reset-widths")
    if val is None:
        return "widths"
    low = val.lower().lstrip("'")
    if low == "nil":
        return "zero"
    if low == "unbased":
        return "unbased"
    return "widths"


def _ignored_by_regexp(name: str, regexp: str | None) -> bool:
    """verilog-signals-not-matching-regexp membership test: True when NAME is
    ignored.  Case-insensitive (verilog-case-fold); a ``?!`` prefix inverts."""
    if not regexp:
        return False
    if regexp.startswith("?!"):
        return not re.search(regexp[2:], name, re.IGNORECASE)
    return bool(re.search(regexp, name, re.IGNORECASE))


def _warn(msg: str) -> None:
    print(f"[verilog_tooling] warning: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# kill (marker-scoped: the region following each marker)


def _kill_region_after_marker(
    lines: Sequence[str], mark_re: "re.Pattern[str]", header_re: "re.Pattern[str]"
) -> list[str]:
    """Delete, after each MARK_RE line, the first HEADER_RE … ``// End of
    automatics`` region (the search stops at the next marker line).

    Marker-scoped, unlike :func:`verilog_tooling.wire._kill_region`: AUTOLOGIC
    shares AUTOWIRE's ``// Beginning of automatic wires`` header, so a global
    kill would also drop a sibling /*AUTOWIRE*/ region.
    """
    idxs = [i for i, ln in enumerate(lines) if mark_re.search(ln)]
    drop: set[int] = set()
    for k, mi in enumerate(idxs):
        limit = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        j = mi + 1
        while j < limit and not header_re.match(lines[j]):
            j += 1
        if j >= limit:
            continue
        e = j
        while e < limit and _END_OF_AUTOMATICS not in lines[e]:
            e += 1
        if e < limit:
            e += 1  # drop the closer too
        drop.update(range(j, e))
    return [ln for i, ln in enumerate(lines) if i not in drop]


def kill_auto_ascii_enum(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOASCIIENUM*/ region(s), keeping the marker line(s)."""
    return _kill_region_after_marker(lines, _ASCIIENUM_MARK, _ASCIIENUM_HEADER_RE)


def kill_auto_logic(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOLOGIC*/ region(s) (the shared ``automatic wires`` header),
    keeping the marker line(s); /*AUTOWIRE*/ regions are untouched."""
    return _kill_region_after_marker(lines, _LOGIC_MARK, _WIRES_HEADER_RE)


def kill_auto_tieoff(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOTIEOFF*/ region(s), keeping the marker line(s)."""
    return _kill_region_after_marker(lines, _TIEOFF_MARK, _TIEOFF_HEADER_RE)


def kill_auto_unused(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOUNUSED*/ region(s); the inline marker line is kept."""
    return _kill_region_after_marker(lines, _UNUSED_MARK, _UNUSED_HEADER_RE)


def kill_auto_undef(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOUNDEF*/ region(s), keeping the marker line(s)."""
    return _kill_region_after_marker(lines, _UNDEF_MARK, _UNDEF_HEADER_RE)


def kill_auto_insert_lisp(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOINSERTLISP*/ region(s), keeping the marker line(s)."""
    return _kill_region_after_marker(lines, _INSERTLISP_MARK, _INSERT_HEADER_RE)


def kill_auto_insert_last(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOINSERTLAST*/ region(s), keeping the marker line(s)."""
    return _kill_region_after_marker(lines, _INSERTLAST_MARK, _INSERT_HEADER_RE)


# ---------------------------------------------------------------------------
# shared parsing helpers


def _quoted_params(marker_line: str) -> list[str]:
    """verilog-read-auto-params: the double-quoted strings inside the marker
    parens (commas inside quotes do not split)."""
    m = re.search(r"\(", marker_line)
    if not m:
        return []
    return re.findall(r'"([^"]*)"', marker_line[m.end() :])


def _paren_content(line: str, open_idx: int) -> str | None:
    """Text between the paren at OPEN_IDX and its match (strings respected);
    None when unbalanced."""
    depth = 0
    in_str = False
    i = open_idx
    start: int | None = None
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return line[start:i] if start is not None else ""
        i += 1
    return None


def _split_statements(text: str) -> list[str]:
    """Split TEXT into ``;``-terminated statements.  Comments are kept (enum
    tags live in them); ``;`` inside ``//``, ``/* */`` or strings never
    splits."""
    stmts: list[str] = []
    cur: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            cur.append(ch)
            if ch == "\\" and i + 1 < n:
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            cur.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j == -1:
                cur.append(text[i:])
                i = n
            else:
                cur.append(text[i:j])
                i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                cur.append(text[i:])
                i = n
            else:
                cur.append(text[i : j + 2])
                i = j + 2
            continue
        cur.append(ch)
        if ch == ";":
            stmts.append("".join(cur))
            cur = []
        i += 1
    if "".join(cur).strip():
        stmts.append("".join(cur))
    return stmts


def _strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*", " ", re.sub(r"/\*.*?\*/", " ", text, flags=re.S))


_ENUM_TAG_RE = re.compile(r"(?:auto|synopsys)\s+enum\s+([A-Za-z0-9_]+)", re.IGNORECASE)
_DECL_KW_RE = re.compile(
    r"\s*(input|output|inout|reg|wire|logic|parameter|localparam)\b", re.IGNORECASE
)


@dataclass
class _Decl:
    """One declared signal: packed range BITS (``[2:0]`` text, None scalar),
    ENUM tag, IS_PARAM for parameter/localparam."""

    name: str
    bits: str | None
    enum: str | None
    is_param: bool


def _scan_decls(span: Sequence[str]) -> list[_Decl]:
    """input/output/inout/reg/wire/logic and parameter/localparam declarations
    of a module span, with enum tags and packed ranges.

    The enum tag is read from the declaration statement (emacs resets the tag
    at each new declaration keyword, so a tag only applies within its own
    statement — which is where the ``// auto enum`` / ``/* auto enum */``
    comments sit in practice).
    """
    decls: list[_Decl] = []
    for stmt in _split_statements("\n".join(span)):
        code = _strip_comments(stmt)
        # drop preprocessor directive lines (`ifdef/`endif/... around a
        # decl, e.g. inside an included parameter header)
        code = "\n".join(
            l for l in code.splitlines() if not l.lstrip().startswith("`")
        )
        km = _DECL_KW_RE.match(code)
        if not km:
            continue
        kind = km.group(1).lower()
        tag: str | None = None
        for tm in _ENUM_TAG_RE.finditer(stmt):
            tag = tm.group(1)  # last wins, as in the elisp scan
        rest = code[km.end() :]
        while True:  # net-type / signed prefixes: input logic signed [7:0] x
            nr = re.sub(r"^\s*(signed|unsigned|wire|reg|logic)\b", "", rest, flags=re.I)
            if nr == rest:
                break
            rest = nr
        bits: str | None = None
        bm = re.match(r"\s*(\[[^\]]+\])", rest)
        if bm:
            bits = bm.group(1)
            rest = rest[bm.end() :]
        is_param = kind in ("parameter", "localparam")
        for part in _wire._split_top_commas(rest):
            nm = re.match(r"\s*([A-Za-z_]\w*)", part)
            if nm:
                decls.append(_Decl(nm.group(1), bits, tag, is_param))
    return decls


def _width_expr(bits: str | None) -> str:
    """verilog-make-width-expression: element count of a packed range."""
    if not bits:
        return "1"
    inner = bits[1:-1].strip()
    m = re.fullmatch(r"(\d+)\s*:\s*(\d+)", inner)
    if m:
        return str(1 + abs(int(m.group(1)) - int(m.group(2))))
    m = re.fullmatch(r"([A-Za-z_]\w*)\s*-\s*1\s*:\s*0", inner)
    if m:
        return m.group(1)
    m = re.fullmatch(r"(.+?)\s*:\s*(.+)", inner)
    if m:
        msb, lsb = m.group(1).strip(), m.group(2).strip()
        if lsb == "0":
            return f"(1+({msb}))"
        return f"(1+({msb})-({lsb}))"
    return "1"


def _enum_ascii(name: str, elim: str | None) -> str:
    """verilog-enum-ascii: remove the ELIM regexp (case-insensitive), then
    downcase (all-upper state names become readable lower case)."""
    if elim:
        name = re.sub(elim, "", name, flags=re.IGNORECASE)
    return name.lower()


# ---------------------------------------------------------------------------
# AUTOASCIIENUM (verilog-auto-ascii-enum)


def auto_ascii_enum(lines: Sequence[str]) -> list[str]:
    """Regenerate the /*AUTOASCIIENUM*/ ASCII-decode regions of each module.

    ``/*AUTOASCIIENUM("sig", "ascii_sig"[, "prefix"[, "onehot"]])*/`` builds a
    register holding the ASCII decode of the enumerated signal ``sig``: the
    enum states are the parameter/localparam constants carrying the same
    ``auto enum``/``synopsys enum`` tag as ``sig``'s declaration.
    """
    lines = kill_auto_ascii_enum(lines)
    full = list(lines)
    from .inst import map_module_spans

    return map_module_spans(lines, _ASCIIENUM_MARK, lambda span: _aascii_single(span, full))


def _aascii_single(span: Sequence[str], full: Sequence[str]) -> list[str]:
    out: list[str] = []
    # enum states may ride in an `include'd header (verilog-auto-read-includes);
    # includes are always expanded for analysis here (project policy).
    # verilog-mode order: buffer-local states first (in declaration order),
    # then include-sourced states REVERSED (its include scan prepends) —
    # duplicate names decode once, first occurrence winning (peltan).
    from .libdirs import expand_includes

    buf_decls = _scan_decls(span)
    exp_decls = _scan_decls(expand_includes(span))
    buf_sigs = {(d.name, d.bits, d.enum) for d in buf_decls if d.is_param}
    inc_params = [d for d in exp_decls if d.is_param and (d.name, d.bits, d.enum) not in buf_sigs]
    inc_params.reverse()
    decls = buf_decls + inc_params
    for line in span:
        out.append(line)
        if not _ASCIIENUM_MARK.search(line):
            continue
        try:
            region = _aascii_region(line, decls, full)
        except ValueError as exc:
            _warn(f"AUTOASCIIENUM skipped: {exc}")
            continue
        out.extend(region)
    return out


def _aascii_region(
    marker_line: str, decls: Sequence[_Decl], full: Sequence[str]
) -> list[str]:
    params = _quoted_params(marker_line)
    if len(params) < 2:
        raise ValueError(f"expected 2-4 params, got: {marker_line.strip()}")
    undecode_name, ascii_name = params[0], params[1]
    elim = params[2] if len(params) > 2 and params[2] != "" else None
    onehot_flag = params[3] if len(params) > 3 else None

    cands = [d for d in decls if not d.is_param and d.name == undecode_name]
    sig = next((d for d in cands if d.enum), cands[0] if cands else None)
    if sig is None:
        raise ValueError(f"signal `{undecode_name}' not found in design")
    if not sig.enum:
        raise ValueError(f"signal `{undecode_name}' does not have an enum tag")
    consts = [d for d in decls if d.is_param and d.enum == sig.enum]
    if not consts:
        raise ValueError(f"no state definitions for `{sig.enum}'")
    # duplicate state names (a localparam repeated after an `include) decode
    # once — first occurrence wins, as in verilog-mode's assoc list
    seen_states: set[str] = set()
    consts = [c for c in consts if not (c.name in seen_states or seen_states.add(c.name))]

    # one-hot: forced by the 4th param, else auto-detected — width(sig) holds
    # exactly as many bits as there are states, and differs from the enum's
    # own width (verilog-auto-ascii-enum).
    sig_w = _width_expr(sig.bits)
    if consts[0].bits is None:
        width_mismatch = True
    else:
        width_mismatch = _width_expr(consts[0].bits) != sig_w
    onehot = bool(
        onehot_flag and re.search("onehot", onehot_flag, re.IGNORECASE)
    ) or (width_mismatch and str(len(consts)) == sig_w)

    enum_chars = max(len(c.name) for c in consts)
    asciis = [_enum_ascii(c.name, elim) for c in consts]
    ascii_chars = max(len(a) for a in asciis)

    indent = len(marker_line) - len(marker_line.lstrip())
    pad = " " * indent
    decl_type = parse_wire_type(full) or "reg"  # verilog-auto-wire-type
    region = [pad + _ASCIIENUM_HEADER]
    # verilog-insert-one-definition: name at max(24, indent+16)
    head = f"{pad}{decl_type} [{ascii_chars * 8 - 1}:0]"
    dline = head + " " * max(_name_col(indent) - len(head), 1) + ascii_name + ";"
    if _wire._wire_comment_enabled(full):
        dline += " " * max(_value_col(indent) - len(dline), 1)
        dline += f"// Decode of {undecode_name}"
    region.append(dline)
    region.append(pad + f"always @({undecode_name}) begin")
    i_case = indent + _INDENT_LEVEL
    region.append(" " * i_case + f"case ({{{undecode_name}}})")
    i_item = i_case + _CASE_INDENT
    state_w = (9 if onehot else 1) + max(8, enum_chars)
    for const, asc in zip(consts, asciis):
        label = f"({len(consts)}'b1<<{const.name}):" if onehot else f"{const.name}:"
        region.append(
            " " * i_item + "%-*s %s = \"%-*s\";" % (state_w, label, ascii_name, ascii_chars, asc)
        )
    errname = "%Error"[: min(6, ascii_chars)]
    region.append(
        " " * i_item + "%-*s %s = \"%-*s\";" % (state_w, "default:", ascii_name, ascii_chars, errname)
    )
    region.append(" " * i_case + "endcase")
    region.append(pad + "end")
    region.append(pad + _END_OF_AUTOMATICS)
    return region


# ---------------------------------------------------------------------------
# AUTOLOGIC (verilog-auto-logic via verilog-auto-logic-setup)


def _wire_like_single(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    full: Sequence[str],
    wire_type: str,
    mark_re: "re.Pattern[str]",
    header: str,
) -> list[str]:
    """verilog_tooling.wire._auto_wire_single generalized over the declaration
    WIRE_TYPE (``wire`` / ``logic``).

    Declares every net driven by an output/inout port of an /*autoinst*/
    instance that is not already declared (port, wire/reg/usrdef, parameter,
    or `define), exactly like AUTOWIRE.

    The declaration keyword follows ``verilog-insert-definition``: the
    submodule port's explicit type (``verilog-sig-type``) wins over
    ``verilog-auto-wire-type``.  :func:`verilog_tooling.autodef._emit_signal`
    implements exactly this selection for the ``"wire"`` keyword, so the
    desired head type is normalized into ``net_type`` and emission reuses
    ``"wire "``.
    """
    from .libdirs import parse_typedef_regexp
    from .inst import auto_arg_port_names, set_typedef_regexp
    from .autodef import (
        _const_symbols,
        _width_syms_known,
        get_all_defs,
        get_all_paras,
        set_param_value,
    )

    typedef_re = parse_typedef_regexp(full)
    set_typedef_regexp(typedef_re)
    _wire.set_ignore_concat(_wire.parse_ignore_concat(full))
    set_param_value(_wire.parse_param_value(full))
    driven = _wire._inst_driven_nets(lines, modules)
    if not driven:
        return list(lines)
    ports, usrdef, _ = _wire._module_tables(lines)
    declared = set(ports.signals) | set(usrdef.signals)
    declared |= get_all_defs(full) | get_all_paras(lines)
    port_names = auto_arg_port_names(lines)
    if port_names:
        declared |= {
            name
            for name, net in driven.items()
            if net.direction == "inout" and name in port_names
        }
    local_syms = set(get_all_paras(lines)) | set(_const_symbols(lines))
    sigs: list[Signal] = []
    comments: dict[str, str] = {}
    for name in sorted(driven):
        if name in declared:
            continue
        if typedef_re and re.search(typedef_re, name):
            continue
        net = driven[name]
        # unpacked dims: merged element indexes (multi-instance), else the
        # port's own unpacked decl — emitted symbolically like verilog-mode
        # (no visibility gate: AUTOLOGIC is AUTO-WIRE with logic type, and
        # verilog-auto-wire declares whatever the submodule port says)
        from .autodef import _merge_unpacked_indexes

        dims: tuple = ()
        if net.unpacked_idx:
            merged = _merge_unpacked_indexes(net.unpacked_idx, local_syms)
            dims = (merged,) if merged is not None else tuple(net.unpacked_dims)
        elif net.unpacked_dims:
            dims = tuple(net.unpacked_dims)
        sigs.append(
            Signal(
                width=net.width,
                type="inst_wire",
                name=name,
                packed_dims=net.packed_dims,
                dims=dims,
                signed=net.signed,
                # verilog-insert-definition: explicit port type wins,
                # else verilog-auto-wire-type
                net_type=net.data_type or net.net_type or wire_type,
                data_type="",
            )
        )
        direction = "To/From" if net.direction == "inout" else "From"
        comments[name] = (
            f"// {direction} {net.inst} of {net.module}.v"
            + (", ..." if net.multi else "")
        )
    if not _wire._wire_comment_enabled(full):
        comments = {}
    return _wire._regen(lines, mark_re, header, "wire ", sigs, comments)


def auto_logic(
    lines: Sequence[str], modules: Mapping[str, ModuleDef] | None = None
) -> list[str]:
    """Regenerate the /*AUTOLOGIC*/ ``logic`` declarations of each module.

    ``verilog-auto-logic`` is ``verilog-auto-wire`` with
    ``verilog-auto-wire-type`` set to ``"logic"`` (unless the file-local
    variable already sets it — cf. ``verilog-auto-logic-setup``); like emacs
    the region keeps the ``// Beginning of automatic wires`` header.
    """
    lines = kill_auto_logic(lines)
    full = list(lines)
    # verilog-auto-logic-setup: default to "logic" unless already set
    wire_type = parse_wire_type(full) or "logic"
    from .inst import map_module_spans

    return map_module_spans(
        lines,
        _LOGIC_MARK,
        lambda span: _wire_like_single(
            span, modules or {}, full, wire_type, _LOGIC_MARK, _wire._WIRE_HEADER
        ),
    )


def _span_ports(span: Sequence[str]) -> "dict[str, Port]":
    """Complete port table of a module span.

    :func:`verilog_tooling.wire._module_tables` reduces a multi-name port
    line (``input da, db, dc, dd;``) to its first name; the full
    :func:`verilog_tooling.inst.parse_module_ports` table is complete, so it
    is used here for AUTOTIEOFF/AUTOUNUSED candidacy.
    """
    from .inst import parse_module_ports

    try:
        md = parse_module_ports(span)
    except Exception:  # noqa: BLE001 — fall back to the partial table
        return {}
    return {
        p.name: p for p in md.ports if p.direction in ("input", "output", "inout")
    }


def _port_signal(p: "Port") -> Signal:
    """A :class:`verilog_tooling.autodef.Signal` view of an inst Port."""
    msb = "c0"
    if p.width:
        m = re.match(r"\s*([^:\]]+)", p.width)
        if m:
            msb = m.group(1).strip()
    return Signal(
        width=msb,
        type="io_wire",
        name=p.name,
        io_dir=p.direction,
        signed=p.signed,
        net_type=p.net_type,
    )


# ---------------------------------------------------------------------------
# AUTOTIEOFF (verilog-auto-tieoff, values via verilog-sig-tieoff)


def _tieoff_width_expr(msb: str) -> str:
    """verilog-make-width-expression on a port msb (lsb presumed 0)."""
    if msb in ("", "c0"):
        return "1"
    if re.fullmatch(r"\d+", msb):
        return str(int(msb) + 1)
    m = re.fullmatch(r"([A-Za-z_]\w*)-1", msb)
    if m:
        return m.group(1)
    return f"(1+({msb}))"


def _tieoff_value(
    name: str, sig: Signal, active_low_re: str | None, reset_widths: str
) -> str:
    """verilog-sig-tieoff: deasserted value for SIG.  Active-low signals
    (``verilog-active-low-regexp``) tie to 1, expressed as ``~<w>'h0``."""
    prefix = "~" if (active_low_re and re.search(active_low_re, name, re.IGNORECASE)) else ""
    if reset_widths == "zero":  # verilog-auto-reset-widths: nil
        return prefix + "0"
    if reset_widths == "unbased":  # verilog-auto-reset-widths: unbased
        return prefix + "'0"
    width = _tieoff_width_expr(sig.width)
    if re.fullmatch(r"\d+", width):
        return f"{prefix}{width}'{'s' if sig.signed else ''}h0"
    return f"{prefix}{{{width}{{1'b0}}}}"


def auto_tieoff(
    lines: Sequence[str], modules: Mapping[str, ModuleDef] | None = None
) -> list[str]:
    """Regenerate the /*AUTOTIEOFF*/ tieoff declarations of each module.

    Finds all outputs of the module; any output not otherwise declared as a
    register or wire, not assigned, not a parameter, and not driven by an
    /*autoinst*/ submodule output/inout (nor an interface port) gets a
    tieoff to deasserted.  ``verilog-auto-tieoff-declaration`` selects the
    declaration form (``"wire"`` — the default — ``"assign"``, or another
    datatype); ``verilog-auto-tieoff-ignore-regexp`` skips signals.
    """
    lines = kill_auto_tieoff(lines)
    full = list(lines)
    from .inst import map_module_spans

    return map_module_spans(
        lines, _TIEOFF_MARK, lambda span: _tieoff_single(span, modules or {}, full)
    )


def _tieoff_single(
    span: Sequence[str], modules: Mapping[str, ModuleDef], full: Sequence[str]
) -> list[str]:
    ignore_re = _local_str(full, "verilog-auto-tieoff-ignore-regexp")
    active_low_re = _local_str(full, "verilog-active-low-regexp")
    reset_widths = _parse_reset_widths(full)
    declaration = _local_str(full, "verilog-auto-tieoff-declaration") or "wire"

    ports, usrdef, assigns = _wire._module_tables(span)
    port_table = _span_ports(span)
    # verilog-subdecls-get-outputs/inouts/interfaced: nets driven by
    # /*autoinst*/ instance outputs, inouts and interface ports
    driven = set(
        _wire._inst_driven_nets(span, modules, directions=("output", "inout", "interface"))
    )
    excluded = set(usrdef.signals) | assigns | driven | get_all_paras(span)
    infos = {name: _port_signal(p) for name, p in port_table.items()}
    names = sorted(
        name
        for name, p in port_table.items()
        if p.direction == "output"
        and name not in excluded
        and not _ignored_by_regexp(name, ignore_re)
    )
    out: list[str] = []
    for line in span:
        out.append(line)
        if _TIEOFF_MARK.match(line) and names:
            indent = len(line) - len(line.lstrip())
            pad = " " * indent
            out.append(pad + _TIEOFF_HEADER)
            out.extend(
                _tieoff_decl_lines(
                    names, infos, indent, declaration, active_low_re, reset_widths
                )
            )
            out.append(pad + _END_OF_AUTOMATICS)
    return out


def _tieoff_decl_lines(
    names: Sequence[str],
    signals: Mapping[str, Signal],
    indent: int,
    declaration: str,
    active_low_re: str | None,
    reset_widths: str,
) -> list[str]:
    """One ``<decl> <name> = <tieoff>;`` line per signal.

    The declaration follows ``verilog-insert-one-definition`` (name at
    ``max(24, indent+16)``); ``= value;`` sits at ``max(48, indent+40)``.
    """
    pad = " " * indent
    name_col = _name_col(indent)
    eq_col = _value_col(indent)
    out: list[str] = []
    for name in names:
        sig = signals[name]
        if declaration == "assign":
            line = f"{pad}assign {name}"
        else:
            head = pad + declaration
            if sig.signed:
                head += " signed"
            if sig.width not in ("", "c0"):
                head += f" [{sig.width}:0]"
            line = head + " " * max(name_col - len(head), 1) + name
        tie = _tieoff_value(name, sig, active_low_re, reset_widths)
        line += " " * max(eq_col - len(line), 1) + f"= {tie};"
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# AUTOUNUSED (verilog-auto-unused) — inline expansion


def auto_unused(
    lines: Sequence[str], modules: Mapping[str, ModuleDef] | None = None
) -> list[str]:
    """Regenerate the /*AUTOUNUSED*/ unused-input regions of each module.

    Lists the module's inputs/inouts that are not connected to any /*autoinst*/
    instance input/inout pin (identifiers inside ``{...}``/``(...)``
    connections count as used, as in ``verilog-read-sub-decls-expr``),
    filtered by ``verilog-auto-unused-ignore-regexp``.  The marker is inline
    (e.g. ``&{1'b0, /*AUTOUNUSED*/ 1'b0}``); generated lines sit at the
    column of the marker's ``/*``, each name followed by a comma.
    """
    lines = kill_auto_unused(lines)
    full = list(lines)
    from .inst import map_module_spans

    return map_module_spans(
        lines, _UNUSED_MARK, lambda span: _unused_single(span, modules or {}, full)
    )


def _unused_single(
    span: Sequence[str], modules: Mapping[str, ModuleDef], full: Sequence[str]
) -> list[str]:
    # "used" = nets on instance input/inout pins.  verilog-read-sub-decls-expr
    # always descends into {...}/(...) — force the extraction on regardless
    # of verilog-auto-ignore-concat.
    prev = _wire._IGNORE_CONCAT
    _wire.set_ignore_concat(False)
    try:
        used = set(_wire._inst_driven_nets(span, modules, directions=("input", "inout")))
    finally:
        _wire.set_ignore_concat(prev)
    ports, _, _ = _wire._module_tables(span)
    port_table = _span_ports(span)
    # supplement the partial _module_tables port list (multi-name lines) —
    # names already present keep the _module_tables Signal
    io_names = {
        name: sig.io_dir for name, sig in ports.signals.items()
    }
    for name, p in port_table.items():
        io_names.setdefault(name, p.direction)
    ignore_re = _local_str(full, "verilog-auto-unused-ignore-regexp")
    names = sorted(
        name
        for name, io_dir in io_names.items()
        if io_dir in ("input", "inout")
        and name not in used
        and not _ignored_by_regexp(name, ignore_re)
    )
    out: list[str] = []
    for line in span:
        out.append(line)
        m = _UNUSED_MARK.search(line)
        if m and names:
            pad = " " * m.start()  # indent-pt: column of the marker's /*
            out.append(pad + _UNUSED_HEADER)
            out.extend(pad + name + "," for name in names)
            out.append(pad + _END_OF_AUTOMATICS)
    return out


# ---------------------------------------------------------------------------
# AUTOUNDEF (verilog-auto-undef)


_DEFINE_RE = re.compile(r"`(define|undef)\s*([A-Za-z_][A-Za-z_0-9]*)")


def _collect_undefs(
    lines: Sequence[str], start: int, end: int, regexp: str | None
) -> list[str]:
    """`` `define`` names in LINES[start:end], minus those later `` `undef``ed
    (the elisp scans comments too — ``verilog-re-search-forward-quick`` does
    not skip them).  Sorted, as ``verilog-auto-undef`` emits them."""
    defs: list[str] = []
    for line in lines[start:end]:
        for m in _DEFINE_RE.finditer(line):
            kind, name = m.group(1), m.group(2)
            if kind == "define":
                if (regexp is None or re.search(regexp, name, re.IGNORECASE)) and (
                    name not in defs
                ):
                    defs.append(name)
            elif name in defs:
                defs.remove(name)
    return sorted(defs)


def auto_undef(lines: Sequence[str]) -> list[str]:
    """Regenerate the /*AUTOUNDEF*/ regions of the file.

    Each marker collects the `` `define``s seen since the previous AUTOUNDEF
    marker (or the start of the file); a `` `undef`` suppresses its name.
    With ``/*AUTOUNDEF("regexp")*/`` only matching names are undefed.  The
    `` `undef`` lines go at column 0 (preprocessor-directive convention;
    emacs's indent pass moves them there from indent-pt).
    """
    lines = kill_auto_undef(lines)
    idxs = [i for i, ln in enumerate(lines) if _UNDEF_MARK.search(ln)]
    plans: list[tuple[int, int, list[str]]] = []
    for k, idx in enumerate(idxs):
        prev = idxs[k - 1] if k else -1
        params = _quoted_params(lines[idx])
        regexp = params[0] if params else None
        defs = _collect_undefs(lines, prev + 1, idx, regexp)
        if defs:
            indent = len(lines[idx]) - len(lines[idx].lstrip())
            plans.append((idx, indent, defs))
    out = list(lines)
    for idx, indent, defs in reversed(plans):  # bottom-up: indices stay valid
        pad = " " * indent
        region = [pad + _UNDEF_HEADER]
        region.extend("`undef " + name for name in defs)
        region.append(pad + _END_OF_AUTOMATICS)
        out[idx + 1 : idx + 1] = region
    return out


# ---------------------------------------------------------------------------
# AUTOINSERTLISP / AUTOINSERTLAST (verilog-auto-insert-lisp/-last)
#
# Port design: Vim has no elisp to eval.  When the marker content starts with
# ``!``, the rest is run as a shell command and its stdout is inserted into
# the region — the equivalent of the elisp docstring's
# ``/*AUTOINSERTLISP(insert (shell-command-to-string "echo //hello"))*/``
# example.  Anything else warns and expands to nothing
# (``verilog-delete-empty-auto-pair``).


def _run_shell(cmd: str) -> list[str]:
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=120
        )
    except Exception as exc:  # noqa: BLE001 — report and continue
        _warn(f"AUTOINSERTLISP shell failed: {cmd}: {exc}")
        return []
    if proc.returncode != 0:
        _warn(f"AUTOINSERTLISP shell exited {proc.returncode}: {cmd}")
    return proc.stdout.splitlines()


def _auto_insert(lines: Sequence[str], mark_re: "re.Pattern[str]", kind: str) -> list[str]:
    out: list[str] = []
    for line in lines:
        out.append(line)
        m = mark_re.search(line)
        if not m:
            continue
        indent = len(line) - len(line.lstrip())
        pad = " " * indent
        content = _paren_content(line, m.end() - 1)
        inserted: list[str] = []
        if content is None:
            _warn(f"{kind} marker without (...): no expansion")
        elif content.startswith("!"):
            inserted = _run_shell(content[1:].strip())
        else:
            _warn(
                f"{kind}: only the '!shell-cmd' form is supported "
                f"(got: {content.strip()[:40]}); no expansion"
            )
        if inserted:
            # emacs inserts at point (column 0); the command owns indentation
            out.append(pad + _INSERT_HEADER)
            out.extend(inserted)
            out.append(pad + _END_OF_AUTOMATICS)
        # else: verilog-delete-empty-auto-pair — emit nothing
    return out


def auto_insert_lisp(lines: Sequence[str]) -> list[str]:
    """Expand /*AUTOINSERTLISP(!cmd)*/ markers: run CMD, insert its stdout.

    The lisp is evaluated *before* other AUTOs in emacs; ordering within a
    pipeline is the caller's job (cf. ``verilog-auto``).
    """
    lines = kill_auto_insert_lisp(lines)
    return _auto_insert(lines, _INSERTLISP_MARK, "AUTOINSERTLISP")


def auto_insert_last(lines: Sequence[str]) -> list[str]:
    """Expand /*AUTOINSERTLAST(!cmd)*/ markers: identical to
    :func:`auto_insert_lisp`, but runs *after* other AUTOs in emacs."""
    lines = kill_auto_insert_last(lines)
    return _auto_insert(lines, _INSERTLAST_MARK, "AUTOINSERTLAST")


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.wire)


def _vb_auto_ascii_enum(self: VerilogBuffer) -> VerilogBuffer:
    """Regenerate the /*AUTOASCIIENUM*/ regions of the buffer."""
    return VerilogBuffer(auto_ascii_enum(self._lines))


def _vb_auto_logic(self: VerilogBuffer, modules: Mapping[str, ModuleDef] | None = None) -> VerilogBuffer:
    """Regenerate the /*AUTOLOGIC*/ logic declarations of the buffer."""
    return VerilogBuffer(auto_logic(self._lines, modules))


def _vb_auto_tieoff(self: VerilogBuffer, modules: Mapping[str, ModuleDef] | None = None) -> VerilogBuffer:
    """Regenerate the /*AUTOTIEOFF*/ tieoff declarations of the buffer."""
    return VerilogBuffer(auto_tieoff(self._lines, modules))


def _vb_auto_unused(self: VerilogBuffer, modules: Mapping[str, ModuleDef] | None = None) -> VerilogBuffer:
    """Regenerate the /*AUTOUNUSED*/ unused-input regions of the buffer."""
    return VerilogBuffer(auto_unused(self._lines, modules))


def _vb_auto_undef(self: VerilogBuffer) -> VerilogBuffer:
    """Regenerate the /*AUTOUNDEF*/ regions of the buffer."""
    return VerilogBuffer(auto_undef(self._lines))


def _vb_auto_insert_lisp(self: VerilogBuffer) -> VerilogBuffer:
    """Expand /*AUTOINSERTLISP*/ markers of the buffer."""
    return VerilogBuffer(auto_insert_lisp(self._lines))


def _vb_auto_insert_last(self: VerilogBuffer) -> VerilogBuffer:
    """Expand /*AUTOINSERTLAST*/ markers of the buffer."""
    return VerilogBuffer(auto_insert_last(self._lines))


VerilogBuffer.auto_ascii_enum = _vb_auto_ascii_enum
VerilogBuffer.auto_logic = _vb_auto_logic
VerilogBuffer.auto_tieoff = _vb_auto_tieoff
VerilogBuffer.auto_unused = _vb_auto_unused
VerilogBuffer.auto_undef = _vb_auto_undef
VerilogBuffer.auto_insert_lisp = _vb_auto_insert_lisp
VerilogBuffer.auto_insert_last = _vb_auto_insert_last


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.wire)


def _resolve_modules(lines: Sequence[str], args) -> dict[str, ModuleDef]:
    """name -> ModuleDef for the /*autoinst*/ instance modules in LINES."""
    from .inst import (
        VerilogBuffer,
        _cli_resolve,
        _module_lines,
        _resolve_module_files,
        buffer_module_defs,
        find_interfaces,
        parse_module_ports,
    )
    from .libdirs import parse_typedef_regexp

    buf = VerilogBuffer(lines)
    names: set[str] = set()
    for idx in buf.markers():
        try:
            names.add(buf.resolve_instance(idx)[0])
        except ValueError:
            pass
    modules: dict[str, ModuleDef] = {}
    if not names:
        return modules
    libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
    files = _resolve_module_files(names, libdirs, inst_files, vc_entries, extensions)
    buffer_mods = buffer_module_defs("\n".join(lines))
    td_re = parse_typedef_regexp(lines)
    interfaces = set(find_interfaces(libdirs)) | set(args.interface)
    for name in names:
        src = _module_lines(name, files, buffer_mods)
        if src is not None:
            modules[name] = parse_module_ports(
                src, typedef_regexp=td_re, interfaces=interfaces
            )
    return modules


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.misc",
        description="verilog-mode AUTOASCIIENUM/AUTOLOGIC/AUTOTIEOFF/AUTOUNUSED/"
        "AUTOUNDEF/AUTOINSERTLISP/AUTOINSERTLAST rewrite",
    )
    parser.add_argument(
        "command",
        choices=[
            "aascii",
            "kill-aascii",
            "alogic",
            "kill-alogic",
            "atieoff",
            "kill-atieoff",
            "aunused",
            "kill-aunused",
            "aundef",
            "kill-aundef",
            "ainsertlisp",
            "kill-ainsertlisp",
            "ainsertlast",
            "kill-ainsertlast",
        ],
        help="aascii: AUTOASCIIENUM; alogic: AUTOLOGIC; atieoff: AUTOTIEOFF; "
        "aunused: AUTOUNUSED; aundef: AUTOUNDEF; ainsertlisp/ainsertlast: "
        "AUTOINSERTLISP/AUTOINSERTLAST; kill-*: delete the region",
    )
    parser.add_argument("-i", "--in_file", required=True, help="buffer file with /*AUTO...*/ markers")
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
    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    cmd = args.command
    if cmd == "kill-aascii":
        out = kill_auto_ascii_enum(lines)
    elif cmd == "kill-alogic":
        out = kill_auto_logic(lines)
    elif cmd == "kill-atieoff":
        out = kill_auto_tieoff(lines)
    elif cmd == "kill-aunused":
        out = kill_auto_unused(lines)
    elif cmd == "kill-aundef":
        out = kill_auto_undef(lines)
    elif cmd == "kill-ainsertlisp":
        out = kill_auto_insert_lisp(lines)
    elif cmd == "kill-ainsertlast":
        out = kill_auto_insert_last(lines)
    elif cmd == "aascii":
        out = auto_ascii_enum(lines)
    elif cmd == "aundef":
        out = auto_undef(lines)
    elif cmd == "ainsertlisp":
        out = auto_insert_lisp(lines)
    elif cmd == "ainsertlast":
        out = auto_insert_last(lines)
    else:  # alogic | atieoff | aunused — need instance module defs
        modules = _resolve_modules(lines, args)
        if cmd == "alogic":
            out = auto_logic(lines, modules)
        elif cmd == "atieoff":
            out = auto_tieoff(lines, modules)
        else:
            out = auto_unused(lines, modules)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
