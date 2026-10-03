"""Python rewrite of verilog-mode's AUTOSENSE/AS and AUTORESET expansions.

Implements ``verilog-auto-sense`` / ``verilog-auto-sense-sigs``
(verilog-mode.el ~lines 14223/14241) and ``verilog-auto-reset``
(verilog-mode.el ~line 14336):

- ``always @(/*AUTOSENSE*/)`` (``/*AS*/`` for short): the sensitivity
  list is replaced with the signals the always block reads but does not
  itself drive, minus parameters, localparams, ``AUTO_CONSTANT``\\ s,
  `` `define``\\ s and multi-dimensional memories (which additionally
  trigger a ``/*memory or*/`` note).  Signals already present before the
  marker are kept; over-long lines wrap at ``fill-column`` (default 80).
- ``/*AUTORESET*/``: expands to a ``// Beginning of autoreset for
  uninitialized flops`` ... ``// End of automatics`` region assigning 0
  (1 for ``verilog-active-low-regexp`` signals) to every register
  assigned elsewhere in the always block but not manually reset between
  the previous begin/case/if and the marker.  ``<=`` is used when the
  signal has a non-blocking assignment in the block, else ``=``.

The always-block signal classification mirrors
``verilog-read-always-signals`` (verilog-mode.el ~line 10044): a
recursive-descent tokenizer distinguishing rvalue inputs, delayed
(``<=``) and immediate (``=``) outputs, and for-loop temporaries.
Width expressions mirror ``verilog-make-width-expression``
(~line 11471) and ``verilog-sig-tieoff`` (~line 9130).

Fidelity decisions vs verilog-mode:

- `` `define``/parameter names and values are collected through
  `` `include`` files (analysis only), like the rest of this package —
  deliberately stronger than verilog-mode, whose
  ``verilog-auto-read-includes`` defaults to nil.
- ``verilog-active-low-regexp`` defaults to nil (no active-low tieoffs),
  as in verilog-mode; set it file-locally (e.g. ``"_l$"``) to tie
  matching signals to 1.
- the ``<=``/``=`` column alignment seen in verilog-mode's test goldens
  comes from ``verilog-pretty-expr``, which the test harness runs after
  ``verilog-auto`` — it is not part of the expansion and is not
  reproduced here.

Configuration (file-local variables in the ``// Local Variables:``
section):

- ``verilog-auto-sense-include-inputs`` — non-nil keeps block outputs in
  the sense list (default nil).
- ``verilog-auto-sense-defines-constant`` — non-nil treats unknown
  `` `define``\\ s as constants (default nil).
- ``verilog-auto-reset-blocking-in-non`` — nil drops ``=``-assigned
  signals when the block also uses ``<=`` (default t).
- ``verilog-auto-reset-widths`` — nil uses plain ``0``, ``unbased`` uses
  ``'0``, otherwise (default) the zero carries the signal width
  (``32'h0``).
- ``verilog-assignment-delay`` — text emitted after ``<=`` (default "").
- ``verilog-active-low-regexp`` — signals tied to 1 (default nil).
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .comments import mask_comments


# ---------------------------------------------------------------------------
# markers

_SENSE_MARK = re.compile(r"/\*\s*(AUTOSENSE|AS)\s*\*/", re.IGNORECASE)
_RESET_MARK = re.compile(r"/\*\s*AUTORESET\s*\*/", re.IGNORECASE)
_RESET_HEADER = "// Beginning of autoreset for uninitialized flops"
_RESET_HEADER_RE = re.compile(
    r"^\s*// Beginning of autoreset for uninitialized flops\b"
)
_END_OF_AUTOMATICS = "// End of automatics"
_MEMORY_NOTE = " /*memory or*/ "

_WS = " \t\n\f\r\v"

_IDENT_RUN = re.compile(r"[a-zA-Z0-9$_.%`]+")
_SIG_START = re.compile(r"^[$`a-zA-Z_]")
_LITERAL_RE = re.compile(r"'[sS]?[hdxboHDBO]?[ \t]*[0-9a-fA-F_xzXZ?]+")
_STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')

# verilog-mode verilog-keywords + verilog-compiler-directives
_KEYWORDS = frozenset(
    """
    after alias always always_comb always_ff always_latch analog and assert
    assign assume automatic before begin bind bins binsof bit break buf bufif0
    bufif1 byte case casex casez cell chandle class clocking cmos config const
    constraint context continue cover covergroup coverpoint cross deassign
    default defparam design disable dist do edge else end endcase endclass
    endclocking endconfig endfunction endgenerate endgroup endinterface
    endmodule endpackage endprimitive endprogram endproperty endspecify
    endsequence endtable endtask enum event expect export extends extern final
    first_match for force foreach forever fork forkjoin function generate genvar
    highz0 highz1 if iff ifnone ignore_bins illegal_bins import incdir include
    initial inout input inside instance int integer interface intersect join
    join_any join_none large liblist library local localparam logic longint
    mailbox matches medium modport module nand negedge new nmos nor
    noshowcancelled not notif0 notif1 null or output package packed parameter
    pmos posedge primitive priority program property protected pull0 pull1
    pulldown pullup pulsestyle_onevent pulsestyle_ondetect pure rand randc
    randcase randsequence rcmos real realtime ref reg release repeat return
    rnmos rpmos rtran rtranif0 rtranif1 scalared semaphore sequence shortint
    shortreal showcancelled signed small solve specify specparam static string
    strong0 strong1 struct super supply0 supply1 table tagged task this
    throughout time timeprecision timeunit tran tranif0 tranif1 tri tri0 tri1
    triand trior trireg type typedef union unique unsigned use uwire var
    vectored virtual void wait wait_order wand weak0 weak1 while wildcard wire
    with within wor xnor xor accept_on checker endchecker eventually global
    implies let nexttime reject_on restrict s_always s_eventually s_nexttime
    s_until s_until_with strong sync_accept_on sync_reject_on unique0 until
    until_with untyped weak implements interconnect nettype soft connectmodule
    endconnectmodule
    `__FILE__ `__LINE `begin_keywords `celldefine `default_nettype `define
    `else `elsif `end_keywords `endcelldefine `endif `ifdef `ifndef `include
    `line `nounconnected_drive `pragma `resetall `timescale `unconnected_drive
    `undef `undefineall
    """.split()
)


# ---------------------------------------------------------------------------
# file-local configuration (Local Variables section)


def _local_str(lines: Sequence[str], name: str) -> str | None:
    """Value of a ``// {name}: value`` file-local variable, else None."""
    m = re.search(
        r"^\s*//\s*" + re.escape(name) + r"\s*:\s*(.*?)\s*$",
        "\n".join(lines),
        re.M,
    )
    return m.group(1) if m else None


def _local_bool(lines: Sequence[str], name: str, default: bool) -> bool:
    """A boolean file-local variable: ``// {name}: nil`` -> False,
    ``// {name}: t`` (or anything else) -> True, absent -> DEFAULT."""
    v = _local_str(lines, name)
    if v is None:
        return default
    return v.strip().lower() != "nil"


def _unquote_elisp(s: str) -> str:
    """Strip one layer of elisp string quotes (``"#1 "`` -> ``#1 ``)."""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


def _parse_reset_widths(lines: Sequence[str]):
    """verilog-auto-reset-widths file-local: nil -> None, unbased ->
    'unbased', otherwise True (default)."""
    v = _local_str(lines, "verilog-auto-reset-widths")
    if v is None:
        return True
    v = v.strip().lower()
    if v == "nil":
        return None
    if v == "unbased":
        return "unbased"
    return True


def _parse_active_low(lines: Sequence[str]) -> str | None:
    """verilog-active-low-regexp file-local (default nil)."""
    v = _local_str(lines, "verilog-active-low-regexp")
    if v is None:
        return None
    v = _unquote_elisp(v)
    return v or None


# ---------------------------------------------------------------------------
# structural text (comments and strings blanked, same length)


def _comment_string_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) spans of ``//`` / ``/* */`` comments and ``"..."``
    string literals in TEXT."""
    spans: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            spans.append((i, j, "line"))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            spans.append((i, j, "block"))
            i = j
        elif text[i] == '"':
            j = _skip_dquote(text, i)
            spans.append((i, j, "string"))
            i = j
        else:
            i += 1
    return spans


def _find_markers(text: str, regex: "re.Pattern[str]") -> list[tuple[int, int]]:
    """(start, end) of REGEX matches not strictly inside a comment/string.

    The marker itself is a ``/* */`` comment, so a match is valid unless
    some other comment/string span strictly contains it (mirrors
    ``verilog-re-search-forward-quick`` skipping commented matches).
    """
    spans = _comment_string_spans(text)
    out: list[tuple[int, int]] = []
    for m in regex.finditer(text):
        s, e = m.start(), m.end()
        if any(
            ss <= s and se >= e and not (ss == s and se == e)
            for ss, se, _kind in spans
        ):
            continue
        out.append((s, e))
    return out


def _structural_text(text: str) -> str:
    """TEXT with ``//``/``/* */`` comments (mask_comments) and string
    literals blanked to spaces; positions stay valid for paren searches."""
    masked = mask_comments(text)
    return _STRING_RE.sub(lambda m: " " * len(m.group(0)), masked)


def _read_signals(masked: str, start: int, end: int) -> list[str]:
    """verilog-read-signals: every identifier-like token in
    [START, END), minus keywords, deduplicated (order kept)."""
    sigs: list[str] = []
    for m in _IDENT_RUN.finditer(masked, start, end):
        name = m.group(0)
        if name not in _KEYWORDS and name not in sigs:
            sigs.append(name)
    return sigs


def _find_open_paren(masked: str, pos: int) -> int | None:
    """Offset of the nearest ``(`` before POS (comment/string aware)."""
    j = masked.rfind("(", 0, pos)
    return j if j >= 0 else None


def _match_paren(masked: str, open_idx: int) -> int | None:
    """Offset of the ``)`` matching the ``(`` at OPEN_IDX, else None."""
    depth = 0
    for j in range(open_idx, len(masked)):
        ch = masked[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j
    return None


# ---------------------------------------------------------------------------
# kill (verilog-delete-auto for these two markers)


def _kill_sense_text(text: str) -> str:
    """Delete each AUTOSENSE/AS expansion: from after the marker to the
    matching ``)`` (verilog-delete-to-paren), keeping the marker."""
    structural = _structural_text(text)
    spans: list[tuple[int, int, int]] = []  # (open, mend, close)
    for mstart, mend in _find_markers(text, _SENSE_MARK):
        open_idx = _find_open_paren(structural, mstart)
        if open_idx is None:
            continue
        close = _match_paren(structural, open_idx)
        if close is None or close < mend:
            continue
        spans.append((open_idx, mend, close))
    out = text
    for _open, mend, close in sorted(spans, reverse=True):
        out = out[:mend] + out[close:]
    return out


def kill_auto_sense(lines: Sequence[str]) -> list[str]:
    """Delete the text generated after each ``/*AUTOSENSE*/`` / ``/*AS*/``
    marker (up to the closing paren), keeping the marker itself."""
    return _kill_sense_text("\n".join(lines)).split("\n")


def _kill_reset_region(lines: Sequence[str]) -> list[str]:
    """Delete every ``// Beginning of autoreset for uninitialized flops``
    ... ``// End of automatics`` region, keeping the marker line."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _RESET_HEADER_RE.match(lines[i]):
            j = i + 1
            while j < n and _END_OF_AUTOMATICS not in lines[j]:
                j += 1
            if j < n:
                i = j + 1  # drop the closer too
                continue
            # unterminated: keep (verilog-delete-autos-lined needs the End)
            out.append(lines[i])
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def kill_auto_reset(lines: Sequence[str]) -> list[str]:
    """Delete the ``/*AUTORESET*/`` generated region(s), keeping the
    ``/*AUTORESET*/`` marker line(s)."""
    return _kill_reset_region(list(lines))


# ---------------------------------------------------------------------------
# buffer scans: defines, declarations, parameters


@dataclass
class _Decl:
    """One declared signal: PACKED is the packed range text (``[MS:LS]``,
    "" when scalar), SIGNED the ``signed`` keyword, UNPACKED True when
    unpacked dims follow the name (a memory)."""

    packed: str = ""
    signed: bool = False
    unpacked: bool = False
    is_port: bool = False


@dataclass
class _ModuleInfo:
    decls: dict[str, _Decl] = field(default_factory=dict)
    params: set[str] = field(default_factory=set)  # parameter (gparams)
    consts: set[str] = field(default_factory=set)  # localparam, genvar
    auto_consts: set[str] = field(default_factory=set)  # AUTO_CONSTANT()


def _split_top_commas(text: str) -> list[str]:
    """Split TEXT on commas at bracket/paren/brace depth 0."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _split_statements(text: str) -> list[str]:
    """Split TEXT on ``;`` at bracket/paren/brace depth 0."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur)
    if tail.strip():
        parts.append(tail)
    return parts


_VAR_KW = (
    "input|output|inout|reg|wire|logic|bit|byte|shortint|int|longint|integer"
    "|time|real|realtime|shortreal|string|event|chandle|supply0|supply1"
    "|tri|tri0|tri1|triand|trior|trireg|uwire|wand|wor"
)
_DECL_RE = re.compile(r"^\s*(?P<kw>" + _VAR_KW + r")\b")
_TYPE_WORD_RE = re.compile(
    r"\s*(?:signed|unsigned|integer|int|bit|logic|reg|wire|real|realtime"
    r"|shortreal|string|time)\b\s*"
)


def _parse_var_decl(rest: str, is_port: bool, info: _ModuleInfo) -> None:
    """Parse one ``<type> [signed] [packed] name, ...`` declaration."""
    m = re.match(r"\s*(signed|unsigned)\b", rest)
    signed = bool(m and m.group(1) == "signed")
    if m:
        rest = rest[m.end() :]
    # skip a second type word: "output logic [7:0] q", "reg signed [3:0] r"
    m = _TYPE_WORD_RE.match(rest)
    if m:
        rest = rest[m.end() :]
        m2 = re.match(r"\s*(signed|unsigned)\b", rest)
        if m2:
            signed = m2.group(1) == "signed"
            rest = rest[m2.end() :]
    packed = ""
    while True:
        m = re.match(r"\s*(\[[^\[\]]*\])", rest)
        if not m:
            break
        if not packed:
            packed = m.group(1)
        rest = rest[m.end() :]
    for part in _split_top_commas(rest):
        part = part.split("=")[0].strip()
        m = re.match(r"([a-zA-Z_]\w*)", part)
        if not m or m.group(1) in _KEYWORDS:
            continue
        name = m.group(1)
        unpacked = bool(re.search(r"\[[^\[\]]*\]", part[m.end() :]))
        info.decls[name] = _Decl(
            packed=packed, signed=signed, unpacked=unpacked, is_port=is_port
        )


def _decl_text_after(masked: str, start: int) -> str:
    """Text from START until ``;`` or ``)`` at depth 0 (a parameter decl)."""
    depth = 0
    pos = start
    n = len(masked)
    while pos < n:
        ch = masked[pos]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        pos += 1
    return masked[start:pos]


def _parse_header_ports(header: str, info: _ModuleInfo) -> None:
    """Parse an ANSI-style module header port list.

    ``module m (input a, output logic [7:0] b, c)`` — a bare name like
    ``c`` inherits direction/type/packed-dims from the preceding port.
    Feeds the same :class:`_ModuleInfo` decls as body declarations.
    """
    packed = ""
    signed = False
    have_prefix = False
    for part in _split_top_commas(header):
        part = part.strip()
        if not part or part.startswith("`"):
            continue
        m = re.match(r"\s*(input|output|inout)\b", part)
        if m:
            rest = part[m.end() :]
            sm = re.match(r"\s*(signed|unsigned)\b", rest)
            signed = bool(sm and sm.group(1) == "signed")
            if sm:
                rest = rest[sm.end() :]
            tm = _TYPE_WORD_RE.match(rest)
            if tm:
                rest = rest[tm.end() :]
                sm2 = re.match(r"\s*(signed|unsigned)\b", rest)
                if sm2:
                    signed = sm2.group(1) == "signed"
                    rest = rest[sm2.end() :]
            pm = re.match(r"\s*(\[[^\[\]]*\])", rest)
            packed = pm.group(1) if pm else ""
            if pm:
                rest = rest[pm.end() :]
            have_prefix = True
            names_rest = rest
        elif not have_prefix:
            continue
        else:
            names_rest = part
        for sub in _split_top_commas(names_rest):
            sub = sub.split("=")[0].strip()
            nm = re.match(r"([a-zA-Z_]\w*)", sub)
            if not nm or nm.group(1) in _KEYWORDS:
                continue
            name = nm.group(1)
            unpacked = bool(re.search(r"\[[^\[\]]*\]", sub[nm.end() :]))
            info.decls[name] = _Decl(
                packed=packed, signed=signed, unpacked=unpacked, is_port=True
            )


def _scan_module_decls(masked_span: str, orig_span: str) -> _ModuleInfo:
    """Declarations of one module span: var/port decls, parameter /
    localparam / genvar names, ``AUTO_CONSTANT()`` names.

    MASKED_SPAN has comments blanked (so ``//`` can't hide a ``;``);
    AUTO_CONSTANTs live inside ``/* */`` and are read from ORIG_SPAN.
    """
    info = _ModuleInfo()
    # ANSI header ports: module m [#(...)] (input a, output [7:0] b, c)
    hm = re.match(r"\s*module\s+[a-zA-Z_][\w$]*", masked_span)
    if hm:
        pos = hm.end()
        rest = masked_span[pos : pos + 1]
        if rest == "#":  # skip parameter port list
            pp = masked_span.find("(", pos)
            pe = _match_paren(masked_span, pp) if pp >= 0 else None
            if pe is not None:
                pos = pe + 1
        pp = masked_span.find("(", pos)
        # the port list must open before any ';' (else it's the body)
        semi = masked_span.find(";", pos)
        if pp >= 0 and (semi < 0 or pp < semi):
            pe = _match_paren(masked_span, pp)
            if pe is not None:
                _parse_header_ports(masked_span[pp + 1 : pe], info)
    for stmt in _split_statements(masked_span):
        m = _DECL_RE.match(stmt)
        if m:
            _parse_var_decl(
                stmt[m.end() :], m.group("kw") in ("input", "output", "inout"), info
            )
    for m in re.finditer(r"(?<![\w$])(parameter|localparam)\b", masked_span):
        text = _decl_text_after(masked_span, m.end())
        target = info.params if m.group(1) == "parameter" else info.consts
        for pm in re.finditer(r"([a-zA-Z_]\w*)\s*=", text):
            target.add(pm.group(1))
    for m in re.finditer(r"(?<![\w$])genvar\b", masked_span):
        text = _decl_text_after(masked_span, m.end())
        for pm in re.finditer(r"[a-zA-Z_]\w*", text):
            if pm.group(0) not in _KEYWORDS:
                info.consts.add(pm.group(0))
    for m in re.finditer(
        r"/\*\s*AUTO_CONSTANT\s*\((.*?)\)\s*\*/", orig_span, re.S | re.I
    ):
        for nm in _IDENT_RUN.finditer(m.group(1)):
            if nm.group(0) not in _KEYWORDS:
                info.auto_consts.add(nm.group(0))
    return info


def _collect_defines(lines: Sequence[str]) -> dict[str, str]:
    """`` `define NAME value`` -> value, from the buffer and `` `include``\\ d
    files (analysis only).  Mirrors ``verilog-read-defines``: defines start
    at the beginning of a line; an empty value means ``1``."""
    from .libdirs import expand_includes

    masked = mask_comments("\n".join(expand_includes(lines)))
    values: dict[str, str] = {}
    for m in re.finditer(
        r"^\s*`define\s+([a-zA-Z0-9_$]+)\s+(.*?)\s*$", masked, re.M
    ):
        val = re.sub(r"\s*/[/*].*$", "", m.group(2)).strip()
        values[m.group(1)] = val if val else "1"
    return values


def _scan_include_consts(lines: Sequence[str]) -> set[str]:
    """parameter / localparam / AUTO_CONSTANT names from the buffer and
    `` `include``\\ d files (analysis only).

    Mirrors the parameter-reading half of ``verilog-read-defines`` (which
    feeds ``verilog-signals-not-params``): a parameter declared in an
    include file counts as a constant for AUTOSENSE.
    """
    from .libdirs import expand_includes

    # expand_includes only matches quoted `include "f"; verilog-mode also
    # accepts the quoteless form (`include f), so normalize first.
    fixed = [
        re.sub(r"`include\s+([^\s\"`]+)", r'`include "\1"', ln)
        for ln in lines
    ]
    expanded = "\n".join(expand_includes(fixed))
    info = _scan_module_decls(_structural_text(expanded), expanded)
    return info.params | info.consts | info.auto_consts


def _module_span_at(text: str, pos: int) -> tuple[int, int] | None:
    """(start, end) offsets of the ``module`` ... ``endmodule`` span
    containing POS, on comment-blanked TEXT; None when outside a module."""
    start = None
    for m in re.finditer(r"(?m)^\s*module\b", text):
        if m.start() <= pos:
            start = m.start()
        else:
            break
    if start is None:
        return None
    m = re.search(r"(?m)^\s*endmodule\b", text[start:])
    end = len(text) if m is None else start + m.end()
    if not (start <= pos <= end):
        return None
    return (start, end)


# ---------------------------------------------------------------------------
# always-block signal classification
# (verilog-read-always-signals / -recurse, verilog-mode.el ~line 10044)


def _is_number(sym: str) -> bool:
    """verilog-is-number: non-nil when SYMBOL is number-like."""
    return bool(
        re.match(r"^[0-9 \t:]+$", sym)
        or re.match(r"^[-]*[0-9]+$", sym)
        or re.match(r"^[0-9 \t]+'s?[hdxbo][0-9a-fA-F_xz? \t]*$", sym)
    )


def _skip_dquote(text: str, pos: int) -> int:
    """Offset just past the string literal opened at POS."""
    j = pos + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        j += 1
    return n


class _AlwaysScan:
    """Recursive-descent port of ``verilog-read-always-signals-recurse``.

    Tokenizes the always block at ``self.pos`` and classifies signals:

    - ``sigs_in``: read in rvalue position (sensitivity candidates);
    - ``sigs_out_d`` / ``sigs_out_i``: assigned with ``<=`` / ``=``;
    - ``sigs_temp``: for-loop variables.

    ``defines`` maps `` `define`` names to values for
    ``verilog-symbol-detick-denumber``; ``defines_constant`` mirrors
    ``verilog-auto-sense-defines-constant``.
    """

    def __init__(
        self, text: str, defines: dict[str, str], defines_constant: bool
    ) -> None:
        self.text = text
        self.n = len(text)
        self.defines = defines
        self.defines_constant = defines_constant
        self.pos = 0
        self.sigs_in: list[str] = []
        self.sigs_out_d: list[str] = []
        self.sigs_out_i: list[str] = []
        self.sigs_out_unk: list[str] = []  # shared across recursion, per elisp
        self.sigs_temp: list[str] = []

    # -- helpers ---------------------------------------------------------

    def _target(self, name: str) -> list[str]:
        return {
            "in": self.sigs_in,
            "out_d": self.sigs_out_d,
            "out_i": self.sigs_out_i,
            "out_unk": self.sigs_out_unk,
            "temp": self.sigs_temp,
        }[name]

    def _detick_denumber(self, sym: str) -> str | None:
        """verilog-symbol-detick-denumber: substitute known `` `define``
        values (dropping ``[non-numeric]`` selects), drop numbers."""
        if sym.startswith("`"):
            val = self.defines.get(sym[1:])
            if val is None:
                sym = "0" if self.defines_constant else sym
            else:
                sym = re.sub(r"\[[^0-9: \t]+\]", "", val)
        if _is_number(sym):
            return None
        return sym

    # -- the recursion ----------------------------------------------------

    def scan(
        self, exit_keywd: str | None, rvalue: bool, temp_next: bool
    ) -> None:
        """Parse until EXIT_KEYWD (or the top-level end), updating POS."""
        text, n = self.text, self.n
        semi_rvalue = exit_keywd == "endcase"
        ignore_next = False
        got_sig: str | None = None
        got_list = "out_unk"
        end_else_check = False
        last_keywd: str | None = None
        sig_tolk = False
        pos = self.pos
        gotend = False

        def flush_got() -> None:
            nonlocal got_sig
            if got_sig is not None:
                self._target(got_list).append(got_sig)
                got_sig = None

        while pos < n and not gotend:
            if text.startswith("//", pos):
                nl = text.find("\n", pos + 2)
                pos = n if nl < 0 else nl + 1
                continue
            if text.startswith("/*", pos):
                j = text.find("*/", pos + 2)
                pos = n if j < 0 else j + 2
                continue
            if text.startswith("(*", pos):
                # attribute: advance past "(*" but not the first "*"
                j = text.find("*)", pos + 1)
                pos = n if j < 0 else j + 2
                continue
            m = _IDENT_RUN.match(text, pos)
            keywd = m.group(0) if m else text[pos]
            sig_last_tolk = sig_tolk
            sig_tolk = False

            if keywd == '"':
                pos = _skip_dquote(text, pos)
            elif end_else_check and keywd == "else":
                # no forward movement; re-read as a keyword next iteration
                end_else_check = False
            elif end_else_check and text[pos] not in " \t\n\f":
                gotend = True
            elif (
                exit_keywd is not None
                and keywd == exit_keywd
                and not text.startswith("::", pos)
            ):
                gotend = True
                pos += len(keywd)
            elif (
                exit_keywd == "'}"
                and keywd == "}"
                and not text.startswith("::", pos)
            ):
                gotend = True
                pos += 1
            elif keywd == ";":
                ignore_next = False
                rvalue = semi_rvalue
                if exit_keywd is None:
                    end_else_check = True
                pos += 1
            elif keywd == "'":
                lm = _LITERAL_RE.match(text, pos)
                if lm:
                    pos = lm.end()
                elif text.startswith("'{", pos):
                    pos += 2
                    self.pos = pos
                    self.scan("'}", True, False)
                    pos = self.pos
                else:
                    pos += 1
            elif keywd == ":":
                if text.startswith("::", pos):
                    pos += 1  # another forward-char below
                elif exit_keywd == "endcase":
                    # case x: y=z; statement next
                    ignore_next = False
                    rvalue = False
                elif exit_keywd in ("?", "]", "'}"):
                    pass  # x?y:z / [x:y] / pattern rvalue
                elif got_sig is not None:
                    # label: statement — the label is dropped, not recorded
                    ignore_next = False
                    rvalue = semi_rvalue
                    got_sig = None
                elif not rvalue:
                    # begin label
                    ignore_next = True
                    rvalue = False
                pos += 1
            elif keywd == "=":
                flush_got()
                if not rvalue:
                    if pos > 0 and text[pos - 1] == "<":
                        self.sigs_out_d.extend(self.sigs_out_unk)
                    else:
                        self.sigs_out_i.extend(self.sigs_out_unk)
                    self.sigs_out_unk = []
                ignore_next = False
                rvalue = True
                pos += 1
            elif keywd == "?":
                pos += 1
                self.pos = pos
                self.scan(":", rvalue, False)
                pos = self.pos
            elif keywd == "[":
                pos += 1
                self.pos = pos
                self.scan("]", True, False)
                pos = self.pos
            elif keywd == "(":
                pos += 1
                if sig_last_tolk:
                    got_sig = None  # function call; zap last signal
                self.pos = pos
                if last_keywd == "for":
                    # loop vars are lvalues but assumed temporaries
                    self.scan(";", False, True)
                    self.scan(";", True, False)
                    self.scan(")", False, False)
                else:
                    self.scan(")", True, False)
                pos = self.pos
            elif keywd == "begin":
                pos += len(keywd)
                while pos < n and (text[pos].isalnum() or text[pos] == "_"):
                    pos += 1  # skip-syntax-forward "w_"
                self.pos = pos
                self.scan("end", False, False)
                pos = self.pos
                ignore_next = False
                rvalue = semi_rvalue
                if exit_keywd is None:
                    end_else_check = True
            elif keywd in ("case", "casex", "casez", "randcase"):
                pos += len(keywd)
                while pos < n and (text[pos].isalnum() or text[pos] == "_"):
                    pos += 1
                self.pos = pos
                self.scan("endcase", True, False)
                pos = self.pos
                ignore_next = False
                rvalue = semi_rvalue
                if exit_keywd is None:
                    gotend = True  # top level begin/end
            elif _SIG_START.match(keywd):
                if keywd in ("`ifdef", "`ifndef", "`elsif"):
                    ignore_next = True
                elif (
                    ignore_next
                    or keywd in _KEYWORDS
                    or keywd.startswith("$")
                ):
                    ignore_next = False
                else:
                    deticked = self._detick_denumber(keywd)
                    flush_got()
                    if temp_next:
                        got_list = "temp"
                    elif rvalue:
                        got_list = "in"
                    else:
                        got_list = "out_unk"
                    if deticked is None or deticked in self._target(got_list):
                        got_sig = None
                    else:
                        got_sig = deticked
                    temp_next = False
                    sig_tolk = True
                im = _IDENT_RUN.match(text, pos)
                pos = im.end() if im else pos + 1
            else:
                pos += 1
            last_keywd = keywd
            while pos < n and text[pos] in _WS:
                pos += 1  # skip-syntax-forward " "
        flush_got()
        self.pos = pos


def _scan_always(
    text: str, defines: dict[str, str], defines_constant: bool, start: int
) -> _AlwaysScan:
    """verilog-read-always-signals at START: leftover lvalues count as
    immediate outputs."""
    sc = _AlwaysScan(text, defines, defines_constant)
    sc.pos = start
    sc.scan(None, False, False)
    sc.sigs_out_i.extend(sc.sigs_out_unk)
    sc.sigs_out_unk = []
    return sc


def _dedup(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for nm in names:
        if nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def _not_in(in_list: list[str], not_lists: list[list[str]]) -> list[str]:
    """verilog-signals-not-in: IN_LIST minus every NOT_LIST, deduplicated."""
    excluded: set[str] = set()
    for lst in not_lists:
        excluded.update(lst)
    return _dedup([nm for nm in in_list if nm not in excluded])


def _not_in_struct(in_list: list[str], not_list: list[str]) -> list[str]:
    """verilog-signals-not-in-struct: also drops ``a.b.c`` when ``a`` or
    ``a.b`` is excluded; deduplicated."""
    excluded = set(not_list)
    out: list[str] = []
    seen: set[str] = set()
    for nm in in_list:
        if nm in excluded or nm in seen:
            continue
        ok = True
        parts = nm.split(".")
        for i in range(1, len(parts)):
            if ".".join(parts[:i]) in excluded:
                ok = False
                break
        if ok:
            seen.add(nm)
            out.append(nm)
    return out


# ---------------------------------------------------------------------------
# width / tieoff (verilog-make-width-expression, verilog-sig-tieoff)


def _width_expr(packed: str) -> str | None:
    """verilog-make-width-expression: expression for the length of PACKED
    (``[MS:LS]``); ``"1"`` for scalars, None when not expressible."""
    if not packed:
        return "1"
    inner = packed.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    else:
        return None
    m = re.match(r"^\s*([0-9]+)\s*:\s*([0-9]+)\s*$", inner)
    if m:
        return str(1 + abs(int(m.group(1)) - int(m.group(2))))
    m = re.match(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*-\s*1\s*:\s*0\s*$", inner)
    if m:
        return m.group(1)  # [PARAM-1:0] is just PARAM
    m = re.match(r"^(.*)\s*:\s*(.*?)\s*$", inner, re.S)
    if m:
        msb, lsb = m.group(1).strip(), m.group(2).strip()
        expr = "(1+(" + msb + ")"
        if lsb != "0":
            expr += "-(" + lsb + ")"
        return expr + ")"
    return None


def _tieoff(
    name: str,
    width_expr: str | None,
    signed: bool,
    widths_mode,
    active_low_re: str | None,
) -> str:
    """verilog-sig-tieoff: deasserted value for NAME (``~``-prefixed when
    it matches ``verilog-active-low-regexp``)."""
    prefix = (
        "~"
        if active_low_re and re.search(active_low_re, name, re.IGNORECASE)
        else ""
    )
    if widths_mode is None:
        return prefix + "0"
    if widths_mode == "unbased":
        return prefix + "'0"
    if width_expr is None:
        return prefix + "'0/*NOWIDTH*/"
    if re.match(r"^[0-9]+$", width_expr):
        return prefix + width_expr + ("'sh0" if signed else "'h0")
    return prefix + "{" + width_expr + "{1'b0}}"


# ---------------------------------------------------------------------------
# AUTOSENSE (verilog-auto-sense)


def _sense_not_first(masked: str, mstart: int, open_paren: int) -> bool:
    """Mirror the elisp ``not-first`` logic: True when the identifier
    before the marker is not already ``or`` (so the first inserted signal
    needs a `` or `` separator).  Skips `` `endif`` lines like the elisp."""
    pos = mstart
    while True:
        last = None
        # NB: finditer offsets are absolute even with pos/endpos args
        for mm in _IDENT_RUN.finditer(masked, open_paren, pos):
            last = mm
        if last is None:
            return False
        ws = last.start() - 1
        while ws >= open_paren and masked[ws] in _WS:
            ws -= 1
        ws += 1
        if ws < open_paren:
            return True
        if re.match(r"\s*`endif\b", masked[ws:pos]):
            pos = last.start()
            continue
        return not bool(re.match(r"\s*or\b", masked[ws:pos], re.IGNORECASE))


@dataclass
class _SenseCtx:
    defines: dict[str, str]
    defines_constant: bool
    include_inputs: bool
    fill_column: int
    inc_consts: set[str] = field(default_factory=set)


def _expand_sense_at(
    out: str,
    structural: str,
    mstart: int,
    mend: int,
    ctx: _SenseCtx,
) -> str:
    """Expand one AUTOSENSE/AS marker (point is after the marker)."""
    open_idx = _find_open_paren(structural, mstart)
    if open_idx is None:
        return out
    line_start = out.rfind("\n", 0, open_idx) + 1
    indent_pt = open_idx - line_start + 1  # column just after '('
    presense = _read_signals(structural, open_idx + 1, mstart)
    close_idx = out.find(")", mend)  # search-forward ")", like the elisp
    if close_idx < 0:
        return out
    span = _module_span_at(structural, mstart)
    info = (
        _scan_module_decls(structural[span[0] : span[1]], out[span[0] : span[1]])
        if span
        else _ModuleInfo()
    )
    sc = _scan_always(out, ctx.defines, ctx.defines_constant, close_idx + 1)

    # verilog-auto-sense-sigs
    const_names = (
        info.consts | info.auto_consts | info.params | set(ctx.defines)
        | ctx.inc_consts
    )
    exclusions = [sc.sigs_temp, presense]
    if not ctx.include_inputs:
        exclusions.insert(0, sc.sigs_out_d + sc.sigs_out_i)
    sig_list = _not_in(sc.sigs_in, exclusions)
    sig_list = [nm for nm in sig_list if nm not in const_names]
    # memories are excluded, with a /*memory or*/ note
    mem_names = {
        nm for nm, d in info.decls.items() if d.unpacked and not d.is_port
    }
    before = len(sig_list)
    sig_list = _not_in(sig_list, [sorted(mem_names)])
    memory_note = len(sig_list) != before
    sig_list.sort()  # verilog-signals-sort-compare (string<)

    ins = ""
    col = mend - (out.rfind("\n", 0, mend) + 1)  # current-column
    if memory_note:
        ins += _MEMORY_NOTE
        col += len(_MEMORY_NOTE)
    not_first = bool(presense) and _sense_not_first(structural, mstart, open_idx)
    for name in sig_list:
        if 4 + col + len(name) > ctx.fill_column:  # +4 for width of " or "
            ins += "\n" + " " * indent_pt
            col = indent_pt
            if not_first:
                ins += "or "
                col += 3
        elif not_first:
            ins += " or "
            col += 4
        ins += name
        col += len(name)
        not_first = True
    return out[:mend] + ins + out[mend:]


def auto_sense(lines: Sequence[str], *, fill_column: int = 80) -> list[str]:
    """Regenerate every ``/*AUTOSENSE*/`` / ``/*AS*/`` sensitivity list.

    Idempotent: previous expansions are deleted first
    (:func:`kill_auto_sense`).
    """
    lines = list(lines)
    ctx = _SenseCtx(
        defines=_collect_defines(lines),
        defines_constant=_local_bool(
            lines, "verilog-auto-sense-defines-constant", False
        ),
        include_inputs=_local_bool(
            lines, "verilog-auto-sense-include-inputs", False
        ),
        fill_column=fill_column,
        inc_consts=_scan_include_consts(lines),
    )
    out = _kill_sense_text("\n".join(lines))
    # later markers first, so earlier offsets stay valid
    markers = _find_markers(out, _SENSE_MARK)
    for mstart, mend in reversed(markers):
        structural = _structural_text(out)
        out = _expand_sense_at(out, structural, mstart, mend, ctx)
    return out.split("\n")


# ---------------------------------------------------------------------------
# AUTORESET (verilog-auto-reset)


@dataclass
class _ResetCtx:
    defines: dict[str, str]
    defines_constant: bool
    blocking_in_non: bool
    widths_mode: object
    assign_delay: str
    active_low_re: str | None


_RESET_BACK_RE = r"@|\b(begin|if|casex?|casez|always(_latch|_ff|_comb)?)\b"
_RESET_ALW_RE = r"@|\b(always(_latch|_ff|_comb)?)\b"


def _expand_reset_at(
    out: str,
    structural: str,
    mstart: int,
    mend: int,
    ctx: _ResetCtx,
) -> str:
    """Expand one ``/*AUTORESET*/`` marker (point is after the marker)."""
    pre = None
    for m in re.finditer(_RESET_BACK_RE, structural[:mstart]):
        pre = m
    prereset = _read_signals(
        structural, pre.start() if pre else 0, mstart
    )
    alw = None
    for m in re.finditer(_RESET_ALW_RE, structural[:mstart]):
        alw = m
    if alw is None:
        return out
    span = _module_span_at(structural, mstart)
    info = (
        _scan_module_decls(structural[span[0] : span[1]], out[span[0] : span[1]])
        if span
        else _ModuleInfo()
    )
    sc = _scan_always(out, ctx.defines, ctx.defines_constant, alw.start())

    candidates = list(sc.sigs_out_d)
    if not sc.sigs_out_d or ctx.blocking_in_non:
        candidates += sc.sigs_out_i
    sig_list = _not_in_struct(candidates, sc.sigs_temp + prereset)
    sig_list.sort()  # verilog-signals-sort-compare (string<)
    if not sig_list:
        return out

    line_start = out.rfind("\n", 0, mstart) + 1
    line_end = out.find("\n", mstart)
    if line_end < 0:
        line_end = len(out)
    line = out[line_start:line_end]
    indent = line[: len(line) - len(line.lstrip())]  # current-indentation
    out_d = set(sc.sigs_out_d)
    parts = ["\n", indent + _RESET_HEADER + "\n"]
    for name in sig_list:
        decl = info.decls.get(name)
        width_expr = _width_expr(decl.packed) if decl else _width_expr("")
        signed = decl.signed if decl else False
        op = " <= " + ctx.assign_delay if name in out_d else " = "
        parts.append(
            indent
            + name
            + op
            + _tieoff(name, width_expr, signed, ctx.widths_mode, ctx.active_low_re)
            + ";\n"
        )
    parts.append(indent + _END_OF_AUTOMATICS)
    return out[:mend] + "".join(parts) + out[mend:]


def auto_reset(lines: Sequence[str]) -> list[str]:
    """Regenerate every ``/*AUTORESET*/`` flop-reset region.

    Idempotent: previous regions are deleted first
    (:func:`kill_auto_reset`).
    """
    lines = list(lines)
    ctx = _ResetCtx(
        defines=_collect_defines(lines),
        defines_constant=_local_bool(
            lines, "verilog-auto-sense-defines-constant", False
        ),
        blocking_in_non=_local_bool(
            lines, "verilog-auto-reset-blocking-in-non", True
        ),
        widths_mode=_parse_reset_widths(lines),
        assign_delay=_unquote_elisp(_local_str(lines, "verilog-assignment-delay") or ""),
        active_low_re=_parse_active_low(lines),
    )
    out = "\n".join(kill_auto_reset(lines))
    markers = _find_markers(out, _RESET_MARK)
    for mstart, mend in reversed(markers):
        structural = _structural_text(out)
        out = _expand_reset_at(out, structural, mstart, mend, ctx)
    return out.split("\n")


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.wire)


def _vb_auto_sense(self, *, fill_column: int = 80):  # type: ignore[no-untyped-def]
    """Regenerate the /*AUTOSENSE*///*AS*/ sensitivity lists of the buffer."""
    from .inst import VerilogBuffer

    return VerilogBuffer(auto_sense(self._lines, fill_column=fill_column))


def _vb_kill_auto_sense(self):  # type: ignore[no-untyped-def]
    """Delete AUTOSENSE/AS expansions, keeping the markers."""
    from .inst import VerilogBuffer

    return VerilogBuffer(kill_auto_sense(self._lines))


def _vb_auto_reset(self):  # type: ignore[no-untyped-def]
    """Regenerate the /*AUTORESET*/ flop-reset regions of the buffer."""
    from .inst import VerilogBuffer

    return VerilogBuffer(auto_reset(self._lines))


def _vb_kill_auto_reset(self):  # type: ignore[no-untyped-def]
    """Delete AUTORESET regions, keeping the markers."""
    from .inst import VerilogBuffer

    return VerilogBuffer(kill_auto_reset(self._lines))


def _attach_buffer_methods() -> None:
    from .inst import VerilogBuffer

    VerilogBuffer.auto_sense = _vb_auto_sense
    VerilogBuffer.kill_auto_sense = _vb_kill_auto_sense
    VerilogBuffer.auto_reset = _vb_auto_reset
    VerilogBuffer.kill_auto_reset = _vb_kill_auto_reset


_attach_buffer_methods()


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.wire)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.sense",
        description="verilog-mode AUTOSENSE/AS and AUTORESET rewrite",
    )
    parser.add_argument(
        "command",
        choices=["asense", "areset", "kill-sense", "kill-reset"],
        help="asense: AUTOSENSE/AS; areset: AUTORESET; kill-sense/kill-reset: delete the region",
    )
    parser.add_argument(
        "-i", "--in_file", required=True,
        help="buffer file with /*AUTOSENSE*///*AUTORESET*/ markers",
    )
    parser.add_argument(
        "-o", "--out_file", required=True, help="output file"
    )
    parser.add_argument(
        "-y",
        "--libdir",
        action="append",
        default=[],
        help="`include search dir (repeatable; default: input file's dir)",
    )
    parser.add_argument(
        "--fill-column",
        type=int,
        default=70,
        help="wrap AUTOSENSE lines past this column (default: 80, emacs fill-column)",
    )
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    from .libdirs import set_include_dirs

    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    file_dir = os.path.dirname(os.path.abspath(args.in_file))
    set_include_dirs([file_dir] + list(args.libdir or []))
    if args.command == "kill-sense":
        out = kill_auto_sense(lines)
    elif args.command == "kill-reset":
        out = kill_auto_reset(lines)
    elif args.command == "asense":
        out = auto_sense(lines, fill_column=args.fill_column)
    else:
        out = auto_reset(lines)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
