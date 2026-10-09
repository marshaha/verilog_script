"""Python rewrite of automatic.vim's AutoDefT (ADT) signal-inference command.

AutoDefT (the newer, tag-based AutoDef — NOT the legacy AutoDef/AD) infers
every undeclared signal of a module and emits the declarations after the
``/*autodef*/`` marker, in fixed sections:

- ``// Define io wire here``               ports without explicit wire/reg
                                           (undriven outputs become ``reg``,
                                           matching AUTOREG in the -a flow)
- ``// Define flip-flop registers here``    LHS of ``<=`` in clocked always
- ``// Define combination registers here``  LHS of ``=`` in other always
- ``// Define wires here``                  LHS of assign
- ``// Define inst wires here``             nets driven by instance outputs
- ``// Define integer here``                for-loop variables: ``integer``
                                            (always/function/task loops) or
                                            ``genvar`` (generate loops)
- ``// Unresolved define signals here``     identifiers whose width/type
                                            could not be inferred

Width inference (:func:`get_assign_side`) recognises ``M'b/h/d`` literals,
constant bit/part selects, ``==`` comparisons and plain-signal links; linked
signals are unioned (:func:`group_link_dict`) and widths propagate through
each group (:func:`update_define`).

Formatting follows automatic.vim exactly:

- the type field is padded to column 12 (``wire ``/``reg  `` + CalMargin);
- the name column is padded to max_len (floor 39), where each signal
  contributes ``5 + len(width) + 4`` (only when width != 'c0');
- sections appear in the fixed order above, each ending with
  ``// End of automatic define``.

Instance port widths come from a ``modules`` mapping ({name: ModuleDef},
like :mod:`verilog_tooling.inst`); the caller resolves the module files.

Beyond the Vim original, ADT also handles:

- instances without a ``/*autoinst*/`` marker (e.g. hand-written instances
  inside generate-for loops): when the module name resolves in ``modules``
  and the instance carries named ``.port(net)`` connections, its output
  nets become inst_wires (only named connections are accepted — a plain
  positional instantiation like ``bufif0 (o, i, en);`` is not, so gate
  primitives never produce false positives);
- SystemVerilog indexed part-select connections
  (``.port(net[EXPR +: W])`` / ``[EXPR -: W]``): the net width grows to
  cover the select over every loop iteration, symbolic when the loop bound
  is a parameter;
- concatenation LHS (``{a, b[..], ...} = ...``), single- or multi-line, in
  always blocks and assign statements: each member is classified
  independently through the plain LHS path (multidim/loop-index handling
  applies per member, a sliced member's width comes from its own select —
  never from the total width of an RHS literal), and nested concats
  recurse.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .inst import ModuleDef, Port, VerilogBuffer, _cal_margin, parse_module_ports
from .parser import discover_for_scopes

_AUTODEF_MARK = re.compile(r"^\s*/\*\s*\b(autodef|AUTODEF)\b")
_AUTODEF_MARK_FULL = re.compile(r"^\s*/\*\s*\b(autodef|AUTODEF)\b\*/")
_AUTOINST_MARK = re.compile(r"/\*\s*\bautoinst\b\s*\*/")
_AUTO_CMD = re.compile(r"/\*\s*\b(autoarg|autopara|autodef|autoinst|autofsm)\b")
_AUTOPARA = re.compile(r"/\*\s*\bautopara\b")
_PORT_LINE = re.compile(r"^\s*(input|output|inout)\b")
_DATA_LINE = re.compile(r"^\s*(wire|reg|logic|parameter|localparam|genvar|integer)\b")
_ALWAYS_SEQ = re.compile(r"^\s*always\s*@\s*\(\s*(posedge|negedge)\b")
_ALWAYS = re.compile(r"^\s*always\b")
_ALWAYS_OPEN = re.compile(r"^\s*always\s*@\s*\(\s*$")
_ASSIGN = re.compile(r"^\s*assign\b")
_ENDMODULE = re.compile(r"^\s*endmodule\b")
_BLOCK_BREAK = re.compile(
    r"^\s*(always|assign)\b|^\s*endmodule\b|/\*\s*\bautoinst\b\s*\*/"
    # declarations (and other construct starts) also end an always scan:
    # otherwise a reg/wire declared BETWEEN two always blocks is swallowed
    # by the previous block's scan and never registered as usrdef (→ the
    # signal is re-declared in the AD region: duplicate declaration)
    r"|^\s*(?:wire|reg|logic|integer|genvar|parameter|localparam|function|task"
    r"|property|endproperty|generate|endgenerate)\b"
)
_INST_PORT = re.compile(r"^\s*\.\w+\s*\(\s*\w+.*\)")
# net[EXPR +: W] / net[EXPR -: W]  (SystemVerilog indexed part-select)
_PART_SELECT = re.compile(r"\b(\w+)\s*\[([^\]]*?)\s*([+|-])\s*:\s*([^\]]*?)\s*\]\s*")
# genvar declarations / generate block labels (`begin : name`) — never signals
_GENERATE_LABEL = re.compile(r"\bbegin\s*:\s*(\w+)")
_GENVAR_DECL = re.compile(r"\bgenvar\s+(.+?);")

_MAX_LEN_FLOOR = 39  # automatic.vim s:autodef_max_len (AUTODEF only; AUTOWIRE uses dynamic)
_TYPE_FIELD = 12  # 'wire '/'reg  ' padded to 12 columns (CalMargin(12, 5))

# automatic.vim s:VlogKeyWords (port/data/calc/stru/other lists), extended
# to the full IEEE 1800 keyword set so SV constructs never leak into the
# unresolved set; membership is tested case-insensitively.
_KEYWORDS = frozenset(
    "input output inout "
    "wire reg logic parameter localparam genvar integer "
    "assign always "
    "module endmodule function endfunction task endtask generate endgenerate "
    "begin end case casex casez endcase default for if define ifdef ifndef "
    "elsif else endif celldefine endcelldefine "
    "posedge negedge timescale initial forever specify endspecify include or "
    "signed unsigned "
    # IEEE 1800 keywords beyond the Vim list
    "accept_on alias always_comb always_ff always_latch and assert assume "
    "automatic before bind bins binsof bit break buf bufif0 bufif1 byte cell "
    "chandle checker class clocking cmos config const constraint context "
    "continue cover covergroup coverpoint cross deassign defparam design "
    "disable dist do edge endchecker endclass endclocking endconfig endgroup "
    "endinterface endpackage endprimitive endprogram endproperty endsequence "
    "endtable enum event eventually expect export extends extern final "
    "first_match force foreach fork forkjoin global highz0 highz1 iff ifnone "
    "ignore_bins illegal_bins implements implies import incdir inside "
    "instance int interconnect interface intersect join join_any join_none "
    "large let liblist library local logic longint macromodule matches "
    "medium modport nand nettype new nexttime nmos nor noshowcancelled not "
    "notif0 notif1 null package packed pmos primitive priority program "
    "property protected pull0 pull1 pulldown pullup pulsestyle_ondetect "
    "pulsestyle_onevent pure rand randc randcase randsequence rcmos real "
    "realtime ref reject_on release repeat restrict return rnmos rpmos rtran "
    "rtranif0 rtranif1 s_always s_eventually s_nexttime s_until s_until_with "
    "scalared sequence shortint shortreal showcancelled small soft solve "
    "specparam static string strong strong0 strong1 struct super supply0 "
    "supply1 sync_accept_on sync_reject_on table tagged this throughout time "
    "timeprecision timeunit tran tranif0 tranif1 tri tri0 tri1 triand trior "
    "trireg type typedef union unique unique0 until until_with untyped use "
    "uwire var vectored virtual void wait wait_order wand weak weak0 weak1 "
    "while wildcard with within wor xnor xor".split()
)

_DEFINE_LINE = re.compile(r"^\s*`(define|ifdef|ifndef)\s*(\w+)")
_PARA_LINE = re.compile(r"^\s*(parameter|localparam)\s*(\w+)")

# verilog-auto-inst-param-value gate for declaration widths: when on, a
# generated width that still references a constant parameter of THIS module
# (e.g. one pulled in through an included param header) folds to its integer
# value — deliberately stronger than emacs, which leaves it symbolic.
_PARAM_VALUE = False


def set_param_value(enabled: bool) -> None:
    global _PARAM_VALUE
    _PARAM_VALUE = enabled


def _param_value_on() -> bool:
    return _PARAM_VALUE
_DIRECTIVE_LINE = re.compile(r"^\s*`(define|ifdef|ifndef|else|elseif|elsif|endif)\b")
_SIGNAL_TOKEN = re.compile(r"[`'.]?\w+")
# non-signal text pre-stripped before tokenising: string literals (their
# contents are never signals), system tasks/functions (``$display`` — the
# name goes but its arguments stay visible), and based numeric literals
# (``12'H0``, ``2'h0``, ``'B1``, optionally signed ``4'shF``)
_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')
_SYSTEM_TASK = re.compile(r"\$\w+")
_BASED_LITERAL = re.compile(r"\d*'[sS]?[hHdDbBoO][0-9a-fA-FxXzZ_?]+")


def _strip_line(line: str) -> str:
    """Drop trailing // comments and a leading comma (the ADT main-loop
    line prep); /*...*/ comments are kept — they may carry an auto marker.
    A full-line /*...*/ comment (or a trailing one after code) is removed
    so it cannot interfere with matching; a leading marker comment keeps
    any following text (``/*autopara*/ (A, B)`` must stay visible)."""
    if re.match(r"^\s*/\*", line):
        m = re.match(r"^\s*/\*.*?\*/", line)
        if m is not None and not re.match(r"\s*/\*\s*\b(?:autodef|autoinst)\b\s*\*/\s*$", line):
            line = line[m.end() :] if line[m.end() :].strip() else ""
    from .comments import strip_line_comments

    line = strip_line_comments(line)  # // cut; /* */ markers kept visible
    return re.sub(r"^\s*,", "", line)


def _strip_inline_comment(text: str) -> str:
    """The statement text with both comment styles removed: /* ... */ (even
    multi-line) and // to end of line (string/state aware)."""
    from .comments import strip_comments

    return strip_comments(text)


def _skip_comment_line(lines: Sequence[str], i: int) -> int:
    """automatic.vim s:SkipCommentLine(mode=0): index of the next line that
    is not comment-only: // comments and lines inside (or fully covered by)
    a /* ... */ pair are skipped; -1 at end of buffer.

    A line like ``code /* ... */`` is NOT skipped (it carries code); a
    line like ``/*marker*/ rest`` is returned too (the marker line itself
    carries the payload the callers dispatch on).  A /* ... */ pair may
    open mid-line; when it does, the rest of that line is comment.  The
    ``//`` cut is state-aware: a ``/*`` inside a line comment does not open
    a pair, a ``//`` inside a pair is not a line comment."""
    from .comments import strip_line_comments

    in_pair = False
    for j in range(i, len(lines)):
        line = lines[j]
        if in_pair:
            if "*/" in line:
                in_pair = False
                tail = line.split("*/", 1)[1]
                if tail.strip() and not re.match(r"^\s*(//|$)", tail):
                    return j  # code after the closing */
            continue
        text = strip_line_comments(line)
        if not text.strip():
            pass  # comment-only line
        elif "/*" in text:
            head, _, tail = text.partition("/*")
            if "*/" in tail:
                tail = tail.split("*/", 1)[1]
                if head.strip() or tail.strip():
                    return j  # code outside the comment
            elif head.strip():
                return j  # code before the opener
            else:
                in_pair = True
        else:
            return j
    return -1


def _skip_autodef_off(lines: Sequence[str], i: int) -> int:
    """Skip a ``/*autodef off*/ ... /*autodef on*/`` region starting at I."""
    if lines[i] != "/*autodef off*/":
        return i
    i += 1
    while i < len(lines) and lines[i] != "/*autodef on*/":
        i += 1
    return i + 1


def _norm_marker(s: str) -> str:
    """Whitespace-normalised section-marker text: generated marker lines like
    ``// End of automatic     define`` (old files have irregular spacing)
    must still match."""
    return re.sub(r"\s+", " ", s.strip())


def _ansi_header_lines(lines: Sequence[str]) -> frozenset[int]:
    """Line indexes inside a module header's port list (``module`` keyword
    line through the line carrying the matching ``);``).

    Ports declared there are complete ANSI declarations: a supplementary
    body ``wire``/``reg`` would be a duplicate declaration (IEEE 1800-2017
    23.2.2.2; Verilator: "Duplicate declaration of signal"), so autodef must
    not emit one for them.  Verilog-2001 body declarations (after the header
    ``);``) are NOT marked: ``input clk;`` in the body plus a ``wire clk;``
    companion is legal there, and the Vim semantics stay.  Parameter blocks
    ``#(...)`` sit inside the span too; they hold no io declarations."""
    from .comments import mask_comments

    text = mask_comments("\n".join(lines))
    text = _STRING_LITERAL.sub('""', text)
    marks: set[int] = set()
    depth = 0
    in_header = False
    for idx, line in enumerate(text.split("\n")):
        if not in_header:
            if re.match(r"^\s*(?:macro)?module\s", line):
                in_header = True
                depth = line.count("(") - line.count(")")
                marks.add(idx)
                if depth <= 0 and ";" in line:
                    in_header = False
            continue
        marks.add(idx)
        depth += line.count("(") - line.count(")")
        if depth <= 0 and ";" in line:
            in_header = False
    return frozenset(marks)


def _skip_section(lines: Sequence[str], i: int, start: str, end: str) -> int:
    """Skip a generated region from the START marker line through the first
    line containing END (both whitespace-tolerant)."""
    if i < len(lines) and _norm_marker(lines[i]) == _norm_marker(start):
        i += 1
        while i < len(lines) and _norm_marker(end) not in _norm_marker(lines[i]):
            i += 1
        i += 1
    return i


# ---------------------------------------------------------------------------
# data model (automatic.vim signal_dict value list)
#
# [width, type, has_defined, seq, line, name, io_dir, last_port]


@dataclass
class Signal:
    """One signal tracked by the ADT pipeline.

    ``width`` is 'c0' for a scalar, '' when unresolved, otherwise the msb
    text ('7', 'W-1').  ``type`` is one of 'io_wire', 'io_reg', 'usrdef',
    'freg', 'creg', 'wire', 'inst_wire', 'inst_in_wire', 'keep'.
    'inst_in_wire' is the weakest source: a net only seen on submodule
    input ports; it is upgraded to 'inst_wire' by any real driver.
    """

    width: str = ""
    type: str = ""
    has_defined: bool = False
    driven: bool = False  # io port driven by assign/always/subinstance
    packed_dims: tuple[str, ...] = ()  # multi-dim packed port: ("W-1:0", "3:0")
    signed: bool = False  # signed declaration (reg signed / input signed)
    net_type: str = ""  # logic | wire | ... from the submodule port
    data_type: str = ""  # typedef name (foo_t) from the submodule port
    seq: str = ""
    line: str = ""
    name: str = ""
    io_dir: str = ""
    last_port: bool = False
    dims: tuple[str, ...] = ()  # unpacked dims from for-loop indices, e.g. ("0:3",)
    # dims came from loop-var selects without any packed range; a later
    # packed-range side (sig[PIPELINE_LENGTH-1:0]) proves they were plain
    # bit-selects of a packed vector and clears them
    dims_select_only: bool = False
    line_idx: int = -1  # buffer line holding a usrdef declaration (-1: unknown)
    width_updated: bool = False  # a stale usrdef width was grown from its driver
    drop_line: bool = False  # a waived usrdef line re-derived identically by
    # instance evidence: the region re-emits it, the orphan line is dropped
    orphan: bool = False  # a kill-waived line sitting in the orphan slot
    # (directly after the /*autodef*/ marker) — eligible for absorb


def conn_net_name(rest: str) -> str | None:
    """First identifier in the connection text REST (everything after
    ``.port(``), scanning only up to the pin's own close paren or a ``//``
    comment — an empty ``.port()`` or a literal tie yields None (the word
    would otherwise be scraped out of a trailing ``// Templated`` note)."""
    out: list[str] = []
    depth = 1  # already inside .port(
    for k, ch in enumerate(rest):
        if ch == "/" and rest[k : k + 2] == "//":
            break
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    inner = re.sub(r"/\*.*?\*/", " ", "".join(out))
    if inner.lstrip()[:1].isdigit():
        return None  # sized literal tie (1'b0 …): no declarable net
    if re.match(r"\s*\w+\s*\.", inner):
        return None  # hierarchical/member reference (hi.ear.ial): not declarable here
    m = re.search(r"[a-zA-Z_]\w*", inner)
    return m.group(0) if m else None


@dataclass
class SignalTable:
    """Ordered name -> Signal map with the ADT update helpers."""

    signals: dict[str, Signal] = field(default_factory=dict)
    # parameter/localparam/`define names of the current module; a computed
    # width referencing anything else (a VARIABLE index like chn_sel_idx) is
    # not a constant msb and is discarded
    known: frozenset = frozenset()
    # names in /*autoarg*/ port lists: such a name on an instance inout pin
    # is an inout PORT by intent (autoarg declares it) — never an inst_wire
    port_names: frozenset = frozenset()
    # constant parameter values of this module (param-value on): instance
    # port widths naming them fold to integers
    consts: dict = field(default_factory=dict)

    def __contains__(self, name: str) -> bool:
        return name in self.signals

    def __getitem__(self, name: str) -> Signal:
        return self.signals[name]

    def __setitem__(self, name: str, sig: Signal) -> None:
        self.signals[name] = sig

    def get(self, name: str) -> Signal | None:
        return self.signals.get(name)

    def discard(self, name: str) -> None:
        self.signals.pop(name, None)

    def extend_io_from_line(self, line: str, seq: int, *, complete: bool = False) -> int:
        """automatic.vim s:ExtendIoFromLine: one ``input/output/inout`` line.
        Returns the next io sequence number.  COMPLETE marks declarations
        inside an AUTO region: they are complete by construction (no
        supplementary body wire/reg may be emitted for them — the emacs
        behavior; a net that only interconnects instances internally is
        AUTOWIRE's job, not a companion wire here)."""
        io_dir = re.match(r"\s*(\w+)", line).group(1)
        rest = re.sub(r"^\s*(input|output|inout)\s*", "", line)
        sig = Signal(width="c0", type="io_wire", io_dir=io_dir)
        if complete:
            # the declaration itself is complete (AUTO region or ANSI
            # module header): no body wire/reg may be emitted for the port
            sig.has_defined = True
        if rest.startswith("wire") and (len(rest) == 4 or not rest[4].isalnum()):
            sig.has_defined = True
            sig.net_type = "wire"
            rest = re.sub(r"^wire\s*", "", rest)
        if rest.startswith("logic") and (len(rest) == 5 or not rest[5].isalnum()):
            sig.has_defined = True
            sig.net_type = "logic"
            rest = re.sub(r"^logic\s*", "", rest)
        if rest.startswith("reg") and (len(rest) == 3 or not rest[3].isalnum()):
            sig.has_defined = True
            sig.net_type = "reg"
            sig.type = "io_reg"
            rest = re.sub(r"^reg\s*", "", rest)
        if re.match(r"^signed\b\s*", rest):
            sig.signed = True
            rest = re.sub(r"^signed\b\s*", "", rest)
        if rest.startswith("["):
            # Match packed dimensions: [31:0][7:0] etc.
            # First dim sets width; additional dims go to packed_dims.
            # Non-greedy to avoid swallowing unpacked dims like [0:7] after name.
            dims = []
            while rest.startswith("["):
                dm = re.match(r"^\[([^\]]+)\]", rest)
                if not dm:
                    break
                dims.append(dm.group(1).strip())
                rest = rest[dm.end():].lstrip()
                # Only continue if next is another [ (packed), not a name
                # Unpacked dims come AFTER the name, so stop here.
                # But we don't know where name starts yet; the loop will
                # naturally stop when rest doesn't start with [.
                # However, for [31:0][7:0] data_out, after [31:0], rest is
                # "[7:0] data_out" which starts with [, so we continue.
                # After [7:0], rest is "data_out" which doesn't start with [.
            if dims:
                # First dim: extract msb for width (e.g., "31:0" -> "31")
                m0 = re.match(r"^([^:]+):", dims[0])
                sig.width = m0.group(1).strip() if m0 else dims[0]
                if len(dims) > 1:
                    sig.packed_dims = tuple(dims[1:])
        # A user-defined type may precede the port name (``output
        # mst_req_t [Cfg.N-1:0] name``), packed dims between type and
        # name — same shape verilog-mode / inst._UDT_HEAD_RE reads.
        # Without this the TYPE word was registered as the port name and
        # the real port stayed "undeclared" (verilog-axi axi_xbar.sv ->
        # AUTOWIRE duplicated its own module output port).
        from .inst import _TYPEDEF_REGEXP as _inst_tre
        from .inst import _UDT_HEAD_RE

        names: list[str] = []
        entries = _split_top_commas(rest)
        for idx, entry in enumerate(entries):
            entry = entry.strip().strip(";").strip()
            if not entry:
                continue
            if idx == 0:
                um = _UDT_HEAD_RE.match(entry)
                if um and um.group(1) not in ("enum", "struct", "union"):
                    sig.data_type = um.group(1)
                    names.append(um.group(3))
                    for dm in re.finditer(r"\[([^\]]*)\]", um.group(2) or ""):
                        d = dm.group(1).strip()
                        if sig.width == "c0":
                            m0 = re.match(r"^([^:]+):", d)
                            sig.width = m0.group(1).strip() if m0 else d
                        else:
                            sig.packed_dims = sig.packed_dims + (d,)
                    continue
                wm = re.match(r"\w+", entry)
                if not wm:
                    return seq  # incomplete decl (nameless header line)
                first = wm.group(0)
                if _inst_tre is not None and _inst_tre.search(first):
                    nm2 = re.match(r"\s*(\w+)", entry[wm.end():])
                    if not nm2:
                        return seq
                    sig.data_type = first
                    names.append(nm2.group(1))
                else:
                    names.append(first)
            else:
                # later entries of a multi-name declare line share the
                # direction/type of the first; register each bare name
                wm = re.match(r"(\w+)", entry)
                if wm:
                    names.append(wm.group(1))
        if not names:
            return seq  # incomplete decl (nameless header line): skip
        name = names[0]
        sig.seq = f"{seq:05d}"  # s:Seq2String pads to 5 chars
        existing = self.signals.get(name)
        if existing is not None and existing.type == "usrdef":
            # a hand-written wire/reg declaration already exists for this net:
            # io_wire must not emit a duplicate `wire name;`, but the io record
            # still captures direction/width.  Mark it as defined and keep the
            # usrdef line as the canonical declaration.
            sig.has_defined = True
            sig.line = existing.line
        self.signals[name] = sig
        seq += 1
        for extra in names[1:]:
            esig = Signal(
                width=sig.width, type=sig.type, io_dir=io_dir,
                data_type=sig.data_type, net_type=sig.net_type,
                signed=sig.signed, packed_dims=sig.packed_dims,
            )
            esig.has_defined = sig.has_defined
            esig.seq = f"{seq:05d}"
            self.signals.setdefault(extra, esig)
            seq += 1
        return seq

    def extend_usrdef_from_line(
        self, line: str, seq: int, raw: str | None = None, line_idx: int = -1,
        orphan_idxs: "frozenset | None" = None,
    ) -> int:
        """automatic.vim s:ExtendUsrdefFromLine: a user ``wire``/``reg``/...
        declaration.  LINE is the parseable statement text (comments and
        extra lines already stripped/joined); RAW, when given, is the
        original buffer text kept verbatim in ``Signal.line`` — usrdef
        declarations are never regenerated (only a provably stale width is
        grown in place, see :meth:`_update_usrdef_width`).  LINE_IDX is the
        buffer index of RAW so a stale-width fix can be applied in place.
        ORPHAN_IDXS marks kill-waived lines (the old region's survivors)."""
        rest = re.sub(r"^\s*(wire|reg|logic|parameter|localparam|genvar|integer)\b\s*", "", line)
        rest = rest.lstrip()  # keyword-less (typedef) lines keep no indent
        from .inst import _TYPEDEF_REGEXP

        tm = re.match(r"^(\w+)\s+", rest)
        if tm and _TYPEDEF_REGEXP is not None and _TYPEDEF_REGEXP.search(tm.group(1)):
            # a typedef'd declaration (``reqcmd_t BReq;``): the first word is
            # the TYPE, the signal name follows
            rest = rest[tm.end() :]
        sig = Signal(width="c0", type="usrdef", line=raw if raw is not None else line)
        sig.line_idx = line_idx
        if orphan_idxs and line_idx in orphan_idxs:
            sig.orphan = True
        rest = re.sub(r"^signed\b\s*", "", rest)  # `wire signed [3:0]` alike
        if rest.startswith("["):
            # the FIRST bracket group is the packed range — a greedy match
            # would swallow multi-dim declarations whole
            # (wire [1:0] name [DIM-1:0];) and lose the name entirely
            m = re.match(r"^\[([^\]]*)\]\s*", rest)
            if m:
                sig.width = m.group(1).split(":")[0].strip()
                rest = rest[m.end() :]
                # further packed dims (reg [A:0][B:0] mem;): skip, the line
                # is kept verbatim anyway
                while rest.startswith("["):
                    m2 = re.match(r"^\[[^\]]*\]\s*", rest)
                    if not m2:
                        break
                    rest = rest[m2.end() :]
        m = re.match(r"\w+", rest)
        if not m:
            return seq
        name = m.group(0)
        from . import inst as _inst_mod

        _tre = _inst_mod._TYPEDEF_REGEXP
        if _tre is not None and _tre.search(name):
            # a typedef'd declaration (`reqcmd_t BReq;`): the name follows
            m2 = re.match(r"\s*(\w+)", rest[m.end() :])
            if not m2:
                return seq
            name = m2.group(1)
            name_end = m.end() + m2.end()
        else:
            um = _inst_mod._UDT_HEAD_RE.match(rest)
            if um is not None and um.group(1) not in (
                "enum",
                "struct",
                "union",
            ):
                # a typedef'd declaration with no verilog-typedef-regexp
                # local in scope (`axi_resp_t slv_resp_cut;`): the FIRST
                # word is the type even without the explicit regexp —
                # without this the type name was registered and the real
                # signal stayed "undeclared" (axi_demux.sv AUTOWIRE
                # duplicates).
                name = um.group(3)
                name_end = um.end()
            else:
                name_end = m.end()
        # Multi-name declarations (`logic a, b;`): every name is declared
        # by this line — registering only the first left the others to be
        # re-declared by AUTOWIRE/AUTODEF (pulp axi_demux.sv duplicates).
        extras: list[str] = []
        tail = rest[name_end:]
        # skip unpacked dims of the first name (`name [0:3], other;`)
        while True:
            dm = re.match(r"\s*\[", tail)
            if not dm:
                break
            close = tail.find("]", dm.end())
            if close < 0:
                break
            tail = tail[close + 1:]
        # an initialiser on the first name precedes the comma list
        # (`reg [2:0] a_reg = IDLE, a_next;`): drop `= INIT` (up to the
        # first top-level comma) so the later names still register —
        # missing them re-declares them here (verilog-axi duplicates)
        ltail = tail.lstrip()
        if ltail.startswith("="):
            depth, cut = 0, -1
            for pos, ch in enumerate(ltail):
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                elif ch == "," and depth == 0:
                    cut = pos
                    break
            ltail = ltail[cut:] if cut >= 0 else ""
        if ltail.startswith(","):
            for part in _split_top_commas(ltail[1:]):
                em = re.match(r"\s*(\w+)", part)
                if em:
                    extras.append(em.group(1))
        existing = self.signals.get(name)
        if existing is not None:
            # a hand-written wire/reg declaration for an io port: mark the io
            # record defined so io_wire does not emit a duplicate `wire name;`.
            if existing.type in ("io_wire", "io_reg"):
                existing.has_defined = True
            return seq
        sig.seq = f"{seq:05d}"
        self.signals[name] = sig
        seq += 1
        for extra in extras:
            if extra in self.signals:
                continue
            esig = Signal(width=sig.width, type="usrdef", line=sig.line)
            esig.seq = f"{seq:05d}"
            # line_idx stays -1: the physical line is printed once via
            # the first name's record; extras exist for name exclusion.
            self.signals[extra] = esig
            seq += 1
        return seq

    def _update_usrdef_width(self, sig: Signal, new_msb: str) -> None:
        """Grow a hand-written declaration whose width is provably STALE:
        the driver-derived NEW_MSB is strictly larger than the declared msb
        and both are numerically comparable (integers), or the declaration
        is scalar ('c0') and the driver is a vector.  A symbolic hand-written
        width (``W-1``, ``2*NUM0-1``) is never touched — it cannot be proven
        smaller.  A declaration ending with the comment ``//DT`` (don't
        touch) is never updated.  The declaration line itself is rewritten at
        emission time (only its range changes; formatting/comments preserved)."""
        if sig.type != "usrdef":
            return
        if re.search(r"//\s*DT\s*$", sig.line, re.IGNORECASE):
            return  # user said don't touch
        if not _usrdef_wider(new_msb, sig.width):
            return
        rewritten = _rewrite_usrdef_range(sig.line, new_msb)
        if rewritten is None:
            return  # declaration text is not a simple one-line wire/reg
        sig.line = rewritten
        sig.width = new_msb
        sig.width_updated = True

    def extend_from_side(self, side: "Side | None", stype: str) -> None:
        """automatic.vim s:ExtendFromSide: insert/update the LHS signal.

        Promotes io_wire to io_reg when assigned in an always block; width
        letters become 'W-1', numeric N becomes 'N-1', 1 becomes 'c0'.
        A multidim LHS additionally records its unpacked ``dims``."""
        if side is None:
            return
        name = side.name
        if side.width is None:
            # scalar index LHS (name[..]) or a link: only the type is stored
            width = ""
        elif side.elem_range is not None and not re.fullmatch(r"-?\d+", side.width or ""):
            # symbolic element range: side.width already IS the msb expression
            # (e.g. "2*NUM0-1"), don't shift it again.
            width = side.width
        elif re.search(r"[a-zA-Z]", side.width):
            width = side.width + "-1"
        else:
            n = int(side.width)
            width = "c0" if n == 1 else str(n - 1)
        if width not in ("", "c0") and self.known and not _width_syms_known(width, self.known):
            # a VARIABLE index (x[chn_sel_idx]) is not a constant msb: discard
            # the width so it cannot poison a good packed width from another
            # side (it stays unresolved via the post-pass when nothing else
            # evidences the width)
            width = ""
        sig = self.signals.get(name)
        if sig is None:
            sig = Signal(width=width, type=stype)
            if side.dims:
                sig.dims = side.dims
                sig.dims_select_only = side.elem_range is None
            self.signals[name] = sig
            return
        if sig.type == "usrdef" and side.dims and not sig.drop_line and sig.orphan:
            # a waived usrdef (kill's unregenerable waiver) re-derived
            # identically from the for-loop evidence: absorb it into the
            # region instead of shuttling between region and orphan slot
            udims = _usrdef_unpacked_dims(sig.line)
            if udims is not None and tuple(_clean_dim(d) for d in side.dims) == udims:
                sig.type = stype
                sig.dims = side.dims
                sig.dims_select_only = side.elem_range is None
                sig.drop_line = True
                if width:
                    sig.width = width
                return
        if sig.type == "inst_in_wire" and stype in ("freg", "creg", "wire"):
            # a real driver (always/assign) trumps the input-port hint: take
            # both the driver type and its width evidence
            sig.type = stype
            sig.width = width
        if (
            side.elem_range is not None
            and not side.dims
            and sig.dims_select_only
            and sig.type != "usrdef"
        ):
            # a packed-range side proves the earlier loop-var selects were
            # bit-selects of a packed vector, not unpacked dimensions
            sig.dims = ()
            sig.dims_select_only = False
            sig.width = ""  # let the packed width below take effect
        if side.width is not None and sig.type == "usrdef":
            # hand-written declaration whose driver is wider: grow it in place
            self._update_usrdef_width(sig, width)
        if side.width is not None and sig.width == "":
            sig.width = width
        elif side.width is not None and not side.dims and width not in ("", "c0"):
            # keep the widest packed evidence across plain/select sides
            # (x[0] then x[15] -> [15:0]); scalar ('c0') never downgrades
            if sig.width == "c0":
                sig.width = width
            elif width.isdigit() and sig.width.isdigit():
                if int(width) > int(sig.width):
                    sig.width = width
            elif not width.isdigit() and not sig.width.isdigit():
                if _sym_const_term(width) > _sym_const_term(sig.width):
                    sig.width = width
        elif side.dims and side.width is not None and sig.width not in ("", "c0"):
            # multidim: keep the widest element seen. Numeric compare when
            # possible; for symbolic msb expressions sharing the same base
            # (e.g. 2*NUM0-2 vs 2*NUM0-1) keep the one with the larger
            # constant term.  A scalar element from a bit-select side ('c0')
            # never outranks a known packed width.
            if width == "c0":
                pass
            elif width.isdigit() and sig.width.isdigit():
                if int(width) > int(sig.width):
                    sig.width = width
            elif not width.isdigit() and not sig.width.isdigit():
                if _sym_const_term(width) > _sym_const_term(sig.width):
                    sig.width = width
        if side.dims and sig.dims:
            # element writes at different indexes (val1[0][7:0], val1[1][7:0]):
            # the declared unpacked range must cover the widest index seen
            sig.dims = _merge_unpacked_dims(sig.dims, side.dims)
        if side.dims and not sig.dims:
            # loop-var selects on an already-packed vector are bit-selects,
            # not unpacked dims; true multidim sides carry a packed range too
            if not (side.elem_range is None and sig.width not in ("", "c0")):
                sig.dims = side.dims
                sig.dims_select_only = side.elem_range is None
        if sig.type == "io_wire" and stype in ("freg", "creg"):
            sig.type = "io_reg"
        if sig.type in ("io_wire", "io_reg") and stype in ("freg", "creg", "wire"):
            sig.driven = True

    def extend_inst_wire_from_line(
        self,
        line: str,
        inst_io: Mapping[str, Port],
        loop_bounds: Mapping[str, tuple[int, int]] | None = None,
        sym_hi: Mapping[str, str] | None = None,
        param_values: Mapping[str, str] | None = None,
    ) -> None:
        """automatic.vim s:ExtendInstWireFromLine: one ``.port(net)`` line of
        an autoinst body; INST_IO maps submodule port name -> Port.

        NET may be a plain identifier or an indexed part-select
        ``net[EXPR +: W]`` / ``net[EXPR -: W]``; for the part-select form the
        declared width grows to cover the select over the whole loop range
        (symbolic when the bound references a parameter)."""
        m = re.match(r"\w+", line)
        if not m:
            return
        port_name = m.group(0)
        rest = line[m.end() :]
        # only a `define/literal NET carries no declaration; a backtick
        # inside a bit-select (net[`MACRO-1:0]) is fine
        if rest.lstrip().startswith(("'", "`")):
            return
        net = conn_net_name(rest)
        if net is None or port_name not in inst_io:
            return
        port = inst_io[port_name]
        if port.direction == "interface":
            # an interface connection instantiates an interface — it is
            # neither a wire driver nor a plain input width hint
            return
        if port.direction == "inout" and net in self.port_names:
            # a port-list name on an inout pin: autoarg emits the inout
            # port declaration — record it as an inout port (no wire, no
            # unresolved), never an inst_wire
            if net not in self.signals:
                self.signals[net] = Signal(width="c0", type="io_inout", has_defined=True)
            return
        raw = port.width  # ModuleDef stores the 'msb:lsb' range
        port_width = "c0" if raw is None else raw.split(":")[0].strip()
        if param_values:
            # the instance's #(...) overrides make the submodule's own
            # parameter names resolvable at the parent (R_MASTER_NUM ->
            # R_MASTER_NUM_ITP); AUTOWIRE does the same substitution
            from .emacs import _apply_param_values

            port_width = _apply_param_values(port_width, param_values)
        if self.consts:
            # param-value on: a width still naming a constant parameter of
            # THIS module (e.g. from an included param header) folds to the
            # integer (FAB_PERIPH_INT_NUM-1 -> 7)
            from .emacs import _apply_param_values

            port_width = _apply_param_values(
                port_width, {k: str(v) for k, v in self.consts.items()}
            )
        if port.direction == "input":
            # a submodule INPUT port consumes the net — it is no driver, but
            # its range is a valid width fallback when nothing else declares
            # the net (an undriven net between two instances would otherwise
            # be flagged unresolved)
            self._record_inst_input_net(net, rest, port_width, port, param_values)
            return
        # a symbolic (parameter/expression) width is kept verbatim — the
        # declaration stays symbolic (wire [APB_BUS_ADDR_WIDTH-1:0]); blanking
        # it here would silently drop the net from emission entirely
        ps = _PART_SELECT.search(rest)
        if ps is not None:
            base_net = ps.group(1)
            if base_net != net:  # e.g. net[...] where net has been eaten by the regex
                net = base_net
            self._extend_inst_wire_partselect(
                port_name, net, ps.group(2), ps.group(3), ps.group(4),
                inst_io, loop_bounds or {}, sym_hi or {}, port_width,
            )
            return
        sig = self.signals.get(net)
        pdims = _conn_packed_dims(rest) or (
            tuple(_clean_dim(d) for d in port.packed) if len(port.packed) > 1 else ()
        )
        if param_values and pdims:
            from .emacs import _apply_param_values

            pdims = tuple(_clean_dim(_apply_param_values(d, param_values)) for d in pdims)
        if sig is not None:
            if pdims and _absorb_waived_usrdef(sig, pdims, "inst_wire"):
                return  # orphan absorbed into the region as inst_wire
            if sig.type == "inst_in_wire":
                sig.type = "inst_wire"  # a real driver trumps the input-port hint
                if port_width not in ("", "c0") and (
                    sig.width in ("", "c0") or _wider(port_width, sig.width)
                ):
                    sig.width = port_width
            if sig.type in ("io_wire", "io_reg"):
                sig.driven = True
            if pdims and not sig.packed_dims and sig.type in (
                "inst_wire", "inst_in_wire", "freg", "creg", "wire"
            ):
                # io/usrdef signals keep their own declaration's dims
                sig.packed_dims = pdims
            if sig.width == "":
                sig.width = port_width
            elif sig.type == "usrdef":
                # instance output port wider than the hand-written net
                self._update_usrdef_width(sig, port_width)
        else:
            self.signals[net] = Signal(width=port_width, type="inst_wire", packed_dims=pdims)

    def _record_inst_input_net(
        self,
        net: str,
        rest: str,
        port_width: str,
        port: "Port",
        param_values: Mapping[str, str] | None = None,
    ) -> None:
        """Weakest width source: a net connected to a submodule INPUT port.

        The width candidate is an explicit full-range select in the
        connection (``net[MSB:LSB]`` — template ``[]`` expansions carry the
        port's own, already param-value-substituted range here), else the
        input port's declared msb.  A bit-select connection (``net[3]``)
        says nothing about the net's width and is ignored.  Only fills empty
        widths (or provably widens an earlier hint) and never marks the net
        driven; any real driver (instance output, assign, always) trumps it.
        """
        if net[0].isdigit():
            return
        mrest = rest.lstrip()[len(net):] if rest.lstrip().startswith(net) else ""
        width = ""
        rm = re.match(r"\s*\[\s*([^:\[\]]+?)\s*:\s*([^\[\]]+?)\s*\]", mrest)
        if rm:
            width = rm.group(1).strip()  # explicit full range in the connection
        elif not re.match(r"\s*\[", mrest):
            width = port_width  # plain net: the input port's declared msb
        # else: a bit/part select — no usable width info
        pdims = _conn_packed_dims(rest) or (
            tuple(_clean_dim(d) for d in port.packed) if len(port.packed) > 1 else ()
        )
        if param_values:
            from .emacs import _apply_param_values

            if width:
                width = _apply_param_values(width, param_values)
            if pdims:
                pdims = tuple(_clean_dim(_apply_param_values(d, param_values)) for d in pdims)
        if not width and not pdims:
            return
        sig = self.signals.get(net)
        if sig is None:
            self.signals[net] = Signal(width=width, type="inst_in_wire", packed_dims=pdims)
        elif sig.type == "usrdef" and pdims:
            _absorb_waived_usrdef(sig, pdims, "inst_in_wire")
        elif sig.type == "inst_in_wire":
            if sig.width in ("", "c0") or (width and _wider(width, sig.width)):
                if width:
                    sig.width = width
            if pdims and not sig.packed_dims:
                sig.packed_dims = pdims  # only inst_in_wire reaches here
        elif sig.width == "" and sig.type in ("freg", "creg", "wire") and width:
            sig.width = width

    def _extend_inst_wire_partselect(
        self,
        port_name: str,
        net: str,
        expr: str,
        direction: str,
        width_sel: str,
        inst_io: Mapping[str, Port],
        loop_bounds: Mapping[str, tuple[int, int]],
        sym_hi: Mapping[str, str],
        port_width: str,
    ) -> None:
        """Record an inst_wire whose connection is ``net[EXPR +: W]`` /
        ``net[EXPR -: W]``. The net width is max(EXPR)+W (+: form) or
        max(EXPR)+1 (-: form), computed over the loop bounds; the msb stays
        symbolic when the bound references a parameter."""
        # width of the select: the port's own width unless an explicit W is given
        sel_w = width_sel.strip() if width_sel else ""
        if not sel_w and port_width != "c0":
            sel_w = port_width
        hi = _eval_index(expr.strip(), loop_bounds, sym_hi)
        if hi is None:
            # unresolvable index expression: fall back to the port width
            new_msb = port_width
        elif direction == "+":
            new_msb = _combine_width(hi, sel_w)
        else:  # '-:'
            new_msb = _combine_width(hi, "1")
        sig = self.signals.get(net)
        if sig is None:
            self.signals[net] = Signal(width=new_msb, type="inst_wire")
        elif sig.type == "usrdef":
            self._update_usrdef_width(sig, new_msb)
        else:
            if sig.type == "inst_in_wire":
                sig.type = "inst_wire"  # a real driver trumps the input-port hint
                if new_msb and (sig.width in ("", "c0") or _wider(new_msb, sig.width)):
                    sig.width = new_msb
            if sig.type in ("io_wire", "io_reg"):
                sig.driven = True
            if sig.width == "" or _wider(new_msb, sig.width):
                sig.width = new_msb


def _combine_width(hi: "int | str", w: str) -> str:
    """msb for a part-select covering max index HI with select width W:
    HI + W - 1, folded to an int when both are numeric."""
    if isinstance(hi, int):
        if re.fullmatch(r"-?\d+", w):
            n = hi + int(w) - 1
            return "c0" if n <= 0 else str(n)
        if w == "":
            return str(hi)
        # hi numeric, w symbolic: w-1+hi
        return f"{w}-1+{hi}" if hi else f"{w}-1"
    if w == "":
        return hi
    if re.fullmatch(r"-?\d+", w):
        return _combine_symbolic(hi, 1, int(w) - 1)
    # both symbolic: (hi) + (w) - 1, cancelling a matching -(w) tail in hi
    if hi.endswith(f"-{w}"):
        head = hi[: -len(w) - 1].rstrip()
        if head:
            return _merge_symbolic([head], -1)
    return _merge_to_width(hi, w)


def _merge_to_width(hi: str, w: str) -> str:
    """(hi) + (w) - 1 as a single simplified expression string, folding the
    constant terms of both sides."""
    total = -1
    parts: list[str] = []
    for expr in (hi, w):
        m = re.match(r"^(.*?)([+-]\d+)?$", expr)
        base, c = m.group(1).strip(), m.group(2)
        if base:
            parts.append(base)
        if c:
            total += int(c)
    body = "+".join(parts)
    if total == 0:
        return body
    sign = "+" if total > 0 else "-"
    return f"{body}{sign}{abs(total)}"


def _merge_unpacked_dims(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    """Element-wise union of two ``lo:hi`` unpacked-dimension tuples: equal
    entries pass through, numeric ranges widen to min(lo):max(hi), anything
    else keeps A's entry (symbolic extents are not comparable)."""
    if len(a) != len(b):
        return a
    out = []
    for x, y in zip(a, b):
        if x == y:
            out.append(x)
            continue
        mx = re.fullmatch(r"(-?\d+):(-?\d+)", x)
        my = re.fullmatch(r"(-?\d+):(-?\d+)", y)
        if mx and my:
            out.append(
                f"{min(int(mx.group(1)), int(my.group(1)))}"
                f":{max(int(mx.group(2)), int(my.group(2)))}"
            )
        else:
            out.append(x)
    return tuple(out)


def _wider(new_msb: str, old_msb: str) -> bool:
    """Whether NEW_MSB makes the declaration strictly wider than OLD_MSB."""
    if not new_msb or new_msb == "c0":
        return False
    if old_msb in ("", "c0"):
        return True
    if new_msb.isdigit() and old_msb.isdigit():
        return int(new_msb) > int(old_msb)
    if new_msb.isdigit() != old_msb.isdigit():
        return False  # cannot compare symbolic with numeric
    return _sym_const_term(new_msb) > _sym_const_term(old_msb)


# ---------------------------------------------------------------------------
# buffer pre-pass helpers (s:GetAllDefs / s:GetAllParas / s:GetAllSignals)


def _split_top_commas(text: str) -> list[str]:
    """Split TEXT at commas not nested inside (), [] or {}."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def get_all_defs(lines: Sequence[str]) -> set[str]:
    """Names from `` `define `` / `` `ifdef `` / `` `ifndef `` lines
    (`` `include`` files read through, analysis only)."""
    from .libdirs import expand_includes

    lines = expand_includes(lines)
    defs: set[str] = set()
    i = 0
    while i < len(lines):
        i = _skip_comment_line(lines, i)
        if i == -1:
            break
        m = _DEFINE_LINE.match(lines[i])
        if m:
            defs.add(m.group(2))
        elif _ENDMODULE.match(lines[i]):
            break
        i += 1
    return defs


def _autopara_names(line: str) -> list[str]:
    """Parameter names of a ``/*autopara*/ (A = 1, B, ...)`` line."""
    m = re.search(r"\(.*\)", line)
    if not m:
        return []
    inner = m.group(0).replace("(", "").replace(")", "")
    names = []
    for para in inner.split(","):
        para = re.sub(r"^\s*", "", para)
        para = re.sub(r"\s*=.*$", "", para)
        para = re.sub(r"\s*$", "", para)
        if para:
            names.append(para)
    return names


def get_all_paras(lines: Sequence[str]) -> set[str]:
    """``parameter``/``localparam`` names, plus names inside /*autopara*/
    regions; generated autopara sections are skipped.  `` `include`` files
    are read through (analysis only)."""
    from .libdirs import expand_includes

    lines = expand_includes(lines)
    paras: set[str] = set()
    i = 0
    while i < len(lines):
        i = _skip_section(lines, i, "// Define parameter here", "// End of automatic parameter")
        i = _skip_comment_line(lines, i)
        if i == -1:
            break
        line = lines[i]
        m = _PARA_LINE.match(line)
        if m:
            paras.add(m.group(2))
        elif _AUTOPARA.search(line):
            paras.update(_autopara_names(line))
        elif _ENDMODULE.match(line):
            break
        i += 1
    return paras


def get_all_signals(lines: Sequence[str], defs: set[str], paras: set[str]) -> set[str]:
    """Every identifier that is not a keyword, number, instance port
    (``.name``), `` `define`` use, string-literal text, system task, or
    known para/def: the unresolved set."""
    signals: set[str] = set()
    i = 0
    while i < len(lines):
        i = _skip_autodef_off(lines, i)
        if i >= len(lines):
            break
        i = _skip_section(lines, i, "// Define io wire here", "// End of automatic define")
        i = _skip_section(lines, i, "// Define parameter here", "// End of automatic parameter")
        i = _skip_comment_line(lines, i)
        if i == -1:
            break
        line = lines[i]
        i += 1
        if _AUTO_CMD.search(line):
            continue
        line = _strip_inline_comment(line)
        if _DIRECTIVE_LINE.match(line) or re.match(r"^\s*`timescale", line):
            continue
        if _ENDMODULE.match(line):
            break
        for m in _SIGNAL_TOKEN.finditer(
            _BASED_LITERAL.sub(" ", _SYSTEM_TASK.sub(" ", _STRING_LITERAL.sub(" ", line)))
        ):
            token = m.group(0)
            if "`" in token or "." in token:
                continue
            if re.search(r"'[hHdDbBoO]", token) or re.fullmatch(r"\d+", token):
                continue
            if re.fullmatch(r"'[01xXzZ]", token):
                continue  # SV unbased unsized literal ('0/'1/'x/'z)
            name = re.sub(r"^'", "", token)
            if re.fullmatch(r"\d+", name):
                continue  # pure numeric literal is never a signal
            if name.lower() in _KEYWORDS or name in paras or name in defs:
                continue
            signals.add(name)
    return signals


# ---------------------------------------------------------------------------
# RHS classification (s:GetAssignSide)


@dataclass(frozen=True)
class Side:
    """Classified assignment: ``width`` None + ``link`` set means the width
    must be inherited from the linked signals; width None + link None means
    an indexed LHS (``sig[..]``), only the type is recorded."""

    name: str
    width: str | None = None
    link: frozenset[str] | None = None
    # multidim LHS: unpacked dims (e.g. ("0:3",) from a for-loop index) and
    # the element packed range (e.g. "7:0") when both can be resolved.
    dims: tuple[str, ...] = ()
    elem_range: str | None = None


def _const_symbols(lines: Sequence[str]) -> dict[str, int]:
    """Constant integer symbols: `` `define NAME <int>`` and
    parameter/localparam ``NAME = <int>`` (module header and body, comments
    included — a commented-out localparam still tells the value).
    `` `include`` files are read through (analysis only), and values written
    as constant expressions of other symbols (``A+B+3``, ``$clog2(X)``,
    ``(X==1) ? 1 : $clog2(X)``) are folded to integers fixpoint-style."""
    from .libdirs import expand_includes

    text = "\n".join(expand_includes(lines))
    consts: dict[str, int] = {}
    for m in re.finditer(r"^\s*`define\s+(\w+)\s+(-?\d+)\b", text, re.M):
        consts[m.group(1)] = int(m.group(2))
    exprs: dict[str, str] = {}
    for m in re.finditer(
        r"\b(?:parameter|localparam)\s+(?:integer\s+|int\s+)?(\w+)\s*=\s*", text
    ):
        name = m.group(1)
        # the value ends at the next top-level , ; or ) — a greedy regex
        # would eat the rest of a single-line header (``= 8) (input clk``)
        depth = 0
        j = m.end()
        while j < len(text):
            ch = text[j]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif ch in ",;\n" and depth == 0:
                break
            j += 1
        value = text[m.end() : j].strip()
        if re.fullmatch(r"-?\d+", value):
            consts[name] = int(value)
        elif value:
            exprs[name] = value
    if exprs:
        from .emacs import _fold_numeric_expr

        known = {k: str(v) for k, v in consts.items()}
        for _ in range(8):
            progress = False
            # one combined pass per expression (hundreds of params make a
            # per-name substitution loop quadratic)
            pat = (
                re.compile(
                    r"\b("
                    + "|".join(sorted(map(re.escape, known), key=len, reverse=True))
                    + r")\b"
                )
                if known
                else None
            )
            for name, expr in list(exprs.items()):
                if pat:
                    expr = pat.sub(lambda m: f"({known[m.group(1)]})", expr)
                folded = _fold_numeric_expr(expr).strip()
                if re.fullmatch(r"-?\d+", folded):
                    consts[name] = int(folded)
                    known[name] = folded
                    del exprs[name]
                    progress = True
            if not progress:
                break
    return consts


def _loop_vars(lines: Sequence[str]) -> set[str]:
    """All for-loop variable names (never signals, regardless of bounds)."""
    return {lp.var for lp in discover_for_scopes(list(lines))}


def _generate_regions(lines: Sequence[str]) -> list[tuple[int, int]]:
    """1-based (start, end) line ranges of ``generate``/``endgenerate``
    regions; nested regions each get their own entry."""
    regions: list[tuple[int, int]] = []
    stack: list[int] = []
    for lineno, raw in enumerate(lines, start=1):
        line = re.sub(r"//.*$", "", raw)
        line = re.sub(r"/\*.*?\*/", " ", line)
        for m in re.finditer(r"\b(endgenerate|generate)\b", line):
            if m.group(1) == "generate":
                stack.append(lineno)
            elif stack:
                regions.append((stack.pop(), lineno))
    return regions


_INT_GENVAR_DECL = re.compile(r"\b(?:integer|genvar)\s+([^;]+);")


def _declared_loop_vars(lines: Sequence[str]) -> set[str]:
    """Names the user already declared with an ``integer``/``genvar``
    declaration (comma-separated lists included)."""
    names: set[str] = set()
    for raw in lines:
        m = _INT_GENVAR_DECL.search(re.sub(r"//.*$", "", raw))
        if m:
            for tok in m.group(1).split(","):
                tok = re.sub(r"=.*", "", tok).strip()
                if re.fullmatch(r"\w+", tok):
                    names.add(tok)
    return names


def _function_regions(lines: Sequence[str]) -> list[tuple[int, int]]:
    """1-based (start, end) line ranges of function/endfunction and
    task/endtask bodies."""
    regions: list[tuple[int, int]] = []
    stack: list[int] = []
    for lineno, raw in enumerate(lines, start=1):
        line = re.sub(r"//.*$", "", raw)
        line = re.sub(r"/\*.*?\*/", " ", line)
        for m in re.finditer(r"\b(endfunction|endtask|function|task)\b", line):
            kw = m.group(1)
            if kw in ("function", "task"):
                stack.append(lineno)
            elif stack:
                regions.append((stack.pop(), lineno))
    return regions


def _always_regions(lines: Sequence[str]) -> list[tuple[int, int]]:
    """1-based (start, end) line ranges of always blocks (header line through
    the matching ``end``, by begin/end depth)."""
    regions: list[tuple[int, int]] = []
    n = len(lines)
    i = 0
    while i < n:
        raw = re.sub(r"//.*$", "", lines[i])
        if re.search(r"\balways\b", raw):
            depth = 0
            started = False
            j = i
            while j < n:
                code = re.sub(r"//.*$", "", lines[j])
                code = re.sub(r"/\*.*?\*/", " ", code)
                depth += len(re.findall(r"\bbegin\b", code))
                depth -= len(re.findall(r"\bend\b", code))
                if re.search(r"\bbegin\b", code):
                    started = True
                if started and depth <= 0:
                    break
                if not started and j > i and ";" in code:
                    break  # single-statement always (no begin)
                j += 1
            regions.append((i + 1, j + 1))
            i = j + 1
        else:
            i += 1
    return regions


_FN_HEADER = re.compile(
    r"\b(?:function|task)\s+(?:automatic\s+)?(?:\[[^\]]*\]\s*)?(?:signed\s+)?([A-Za-z_]\w*)"
)


def _function_names(lines: Sequence[str]) -> set[str]:
    """Names of the buffer's function/task definitions themselves."""
    names: set[str] = set()
    for raw in lines:
        m = _FN_HEADER.search(re.sub(r"//.*$", "", raw))
        if m:
            names.add(m.group(1))
    return names


def _only_in_regions(
    name: str, lines: Sequence[str], regions: Sequence[tuple[int, int]]
) -> bool:
    """True when every whole-word occurrence of NAME sits inside REGIONS
    (1-based line ranges) — a function/task-local identifier by usage."""
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    hits = [
        ln
        for ln, raw in enumerate(lines, start=1)
        if pat.search(re.sub(r"//.*$", "", raw))
    ]
    return bool(hits) and all(
        any(s <= ln <= e for s, e in regions) for ln in hits
    )


def _loop_var_decls(lines: Sequence[str]) -> list[tuple[str, str]]:
    """(name, kind) for each for-loop variable that needs a declaration:
    kind is ``genvar`` only for a PURE generate-for (header inside a
    generate region but not inside any always block); a procedural loop —
    anywhere, including inside a generate region — gets ``integer``.  Names
    already hand-declared with ``integer``/``genvar`` are excluded; each
    remaining name appears once, in loop source order."""
    regions = _generate_regions(lines)
    always_regions = _always_regions(lines)
    function_regions = _function_regions(lines)
    declared = _declared_loop_vars(lines)
    loops = discover_for_scopes(list(lines))
    inline = {lp.var for lp in loops if lp.inline_type}
    kinds: dict[str, str] = {}
    order: list[str] = []
    for lp in loops:
        if lp.var in declared or lp.var in inline:
            continue
        if any(s <= lp.scope[0] <= e for s, e in function_regions):
            continue  # function/task-local: no module-level declaration
        if lp.var not in kinds:
            kinds[lp.var] = "integer"
            order.append(lp.var)
        in_always = any(s <= lp.scope[0] <= e for s, e in always_regions)
        in_generate = any(s <= lp.scope[0] <= e for s, e in regions)
        if in_generate and not in_always:
            kinds[lp.var] = "genvar"  # pure generate-for is a hard constraint
    # a for header may declare several variables: for (i=0, j=0; ...) —
    # discover_for_scopes only tracks the first; the rest still need decls
    for lineno, raw in enumerate(lines, start=1):
        if any(s <= lineno <= e for s, e in function_regions):
            continue
        fm = re.search(r"\bfor\s*\(\s*([^;]+);", re.sub(r"//.*$", "", raw))
        if not fm:
            continue
        for vm in re.finditer(r"([A-Za-z_]\w*)\s*=", fm.group(1)):
            v = vm.group(1)
            if v not in kinds and v not in declared and v not in inline:
                kinds[v] = "integer"
                order.append(v)
    return [(v, kinds[v]) for v in order]


def _eval_bound(expr: str, consts: Mapping[str, int], symbolic: bool = False):
    """Resolve a loop-bound expression. With symbolic=False returns an int
    (or None). With symbolic=True returns a string where parameter/`define
    names are kept symbolic and only literal arithmetic is folded, so the
    generated dimension survives parameter overrides at instantiation."""
    expr = expr.strip()
    if symbolic:
        # fold pure-integer subexpressions but keep named constants symbolic;
        # `define keeps its backtick so it stays a valid macro reference
        if re.fullmatch(r"-?\d+", expr):
            return expr
        return re.sub(r"\s+", "", expr)
    expr = re.sub(r"`(\w+)", r"\1", expr)  # `define -> name
    for name, val in consts.items():
        expr = re.sub(r"\b" + re.escape(name) + r"\b", str(val), expr)
    if re.fullmatch(r"-?[\d\s+*/%()+-]+", expr):
        try:
            return int(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307
        except Exception:  # noqa: BLE001
            return None
    return None


def _loop_ranges(
    lines: Sequence[str], consts: "Mapping[str, int] | None" = None
) -> dict[str, str]:
    """for-loop variable -> unpacked range "lo:hi" derived from the loop
    bounds. Literal bounds are folded to integers; bounds mentioning a
    ``parameter``/`` `define`` are kept symbolic (``0:NUM0-1``) so the
    declared dimension stays correct when an instance overrides the value.
    CONSTS overrides the constant-symbol table (a module span's caller
    passes the whole-buffer table so pre-module `` `define``s resolve)."""
    if consts is None:
        consts = _const_symbols(lines)
    ranges: dict[str, str] = {}
    for lp in discover_for_scopes(list(lines)):
        init = _eval_bound(lp.init, consts)
        m = re.match(rf"{re.escape(lp.var)}\s*(<=?|>=?)\s*(.+?)\s*$", lp.cond)
        if init is None or not m:
            continue
        op, rhs = m.group(1), m.group(2)
        # keep the bound symbolic if it references a parameter/`define name;
        # only fold when it is purely numeric (so instance overrides survive)
        rhs_clean = re.sub(r"`(\w+)", r"\1", rhs).strip()
        symbolic_names = set(consts)
        is_numeric = not any(
            re.search(r"\b" + re.escape(nm) + r"\b", rhs_clean) for nm in symbolic_names
        )
        # a bound naming another loop variable (j <= i) inherits that
        # variable's range edge: a sound over-approximation for a
        # declaration bound (loops are visited outer-first)
        dep = None
        if re.fullmatch(r"[A-Za-z_]\w*", rhs_clean) and rhs_clean in ranges:
            dep = ranges[rhs_clean].split(":", 1)
        if op in ("<", "<="):
            lo = str(init)
            b = _eval_bound(rhs, consts)
            if is_numeric and b is not None:
                hi = str(b - 1) if op == "<" else str(b)
            elif not is_numeric:
                sym = _eval_bound(rhs, consts, symbolic=True)
                hi = f"{sym}-1" if op == "<" else sym
            elif dep is not None:
                hi = dep[1]
            else:
                continue  # bound references a non-constant (e.g. an input)
        else:  # descending: var > E / var >= E
            hi = str(init)
            b = _eval_bound(rhs, consts)
            if is_numeric and b is not None:
                lo = str(b + 1) if op == ">" else str(b)
            elif not is_numeric:
                sym = _eval_bound(rhs, consts, symbolic=True)
                lo = f"{sym}+1" if op == ">" else sym
            elif dep is not None:
                lo = dep[0]
            else:
                continue
        # sanity: for purely numeric ranges, require hi >= lo
        if re.fullmatch(r"-?\d+", lo) and re.fullmatch(r"-?\d+", hi):
            if int(hi) < int(lo):
                continue
        new = f"{lo}:{hi}"
        old = ranges.get(lp.var)
        if old is not None and old != new:
            # the same variable heading several loops at different
            # bounds: the declaration must cover the widest extent
            on = re.fullmatch(r"(-?\d+):(-?\d+)", old)
            nn = re.fullmatch(r"(-?\d+):(-?\d+)", new)
            if on and nn:
                new = (
                    f"{min(int(on.group(1)), int(nn.group(1)))}"
                    f":{max(int(on.group(2)), int(nn.group(2)))}"
                )
            else:
                new = old  # symbolic extents do not merge; keep the first
        ranges[lp.var] = new
    return ranges


def _loop_sym_hi(
    lines: Sequence[str], consts: "Mapping[str, int] | None" = None
) -> dict[str, str]:
    """for-loop variable -> symbolic hi expression (e.g. ``NUM0-1``), only for
    loops whose bound references a parameter/`define (stays symbolic)."""
    out = {}
    for var, rng in _loop_ranges(lines, consts).items():
        hi = rng.split(":", 1)[1]
        if not re.fullmatch(r"-?\d+", hi):
            out[var] = hi
    return out


def _loop_bounds(
    lines: Sequence[str], consts: "Mapping[str, int] | None" = None
) -> dict[str, tuple[int, int]]:
    """for-loop variable -> numeric (lo, hi) inclusive bounds.  CONSTS
    overrides the constant-symbol table (see :func:`_loop_ranges`)."""
    if consts is None:
        consts = _const_symbols(lines)
    bounds: dict[str, tuple[int, int]] = {}
    for lp in discover_for_scopes(list(lines)):
        init = _eval_bound(lp.init, consts)
        if init is None:
            continue
        m = re.match(rf"{re.escape(lp.var)}\s*(<=?|>=?)\s*(.+?)\s*$", lp.cond)
        if not m:
            continue
        bound = _eval_bound(m.group(2), consts)
        if bound is None:
            # bound naming another loop variable (j <= i): that
            # variable's range edge stands in for the bound
            rhs_name = m.group(2).strip()
            donor = (
                bounds.get(rhs_name)
                if re.fullmatch(r"[A-Za-z_]\w*", rhs_name)
                else None
            )
            if donor is None:
                continue
            bound = donor[1] if op in ("<", "<=") else donor[0]
        op = m.group(1)
        if op == "<":
            hi = bound - 1
        elif op == "<=":
            hi = bound
        elif op == ">":
            hi = bound + 1
        else:
            hi = bound
        if op in ("<", "<="):
            lo, hi = init, hi
        else:
            lo, hi = hi, init
        if hi >= lo:
            if lp.var in bounds:
                olo, ohi = bounds[lp.var]
                bounds[lp.var] = (min(olo, lo), max(ohi, hi))
            else:
                bounds[lp.var] = (lo, hi)
    return bounds


def _eval_index(
    expr: str,
    bounds: Mapping[str, tuple[int, int]],
    sym_bounds: Mapping[str, str] | None = None,
) -> "int | str | None":
    """Max value of a bit-select index expression over the loop bounds.

    Handles constants and multi-variable linear forms (``2*j + k + 1``).
    Numeric loop bounds fold to an int; a term whose variable has a symbolic
    bound (parameter/`define) stays symbolic — ``j*2+k+1`` with
    ``j:[0:NUM0-1]``, ``k:[0:NUM1-1]`` -> ``2*NUM0+NUM1-2``. Returns int for
    fully numeric, str for symbolic, None when unresolvable."""
    expr = expr.strip()
    if re.fullmatch(r"-?\d+", expr):
        return int(expr)
    terms = re.findall(r"([+-]?\s*\w+(?:\s*\*\s*\w+)?|[+-]?\s*\d+)", expr)
    if not terms or "".join(t.replace(" ", "") for t in terms) != expr.replace(" ", ""):
        return None
    sym_bounds = sym_bounds or {}
    sym_parts: list[str] = []
    const_total = 0
    for term in terms:
        term = term.replace(" ", "")
        if re.fullmatch(r"[+-]?\d+", term):
            const_total += int(term)
            continue
        # VAR, VAR*K or K*VAR — VAR is the loop var, the other side a coef
        m = re.fullmatch(r"([+-]?)(\w+?)(?:\*(\w+))?$", term)
        if not m:
            return None
        sign = -1 if m.group(1) == "-" else 1
        a, b = m.group(2), m.group(3)
        if b is None:
            var, coef_txt = a, ""
        elif b.isdigit():
            var, coef_txt = a, b  # j*2
        elif a in bounds or a in sym_bounds:
            var, coef_txt = a, b  # j*DW: loop var times a symbolic coef
        else:
            var, coef_txt = b, a  # 32*gv_i / BITS*gv_i
        if var in sym_bounds:
            # symbolic hi: (hi_expr) contributes coef * hi_expr
            hi_sym = sym_bounds[var]
            if coef_txt and not coef_txt.isdigit():
                # symbolic coefficient (BITS_PER_DITHER*gv_i): keep both symbolic
                if sign < 0:
                    coef_txt = f"-{coef_txt}"
                sym_parts.append(_symbolic_coef(hi_sym, coef_txt))
            else:
                coef = int(coef_txt) if coef_txt else 1
                sym_parts.append(_combine_symbolic(hi_sym, sign * coef, 0))
        elif var in bounds and (not coef_txt or coef_txt.isdigit()):
            lo, hi = bounds[var]
            coef = int(coef_txt) if coef_txt else 1
            const_total += sign * coef * (hi if sign * coef > 0 else lo)
        elif var in bounds and coef_txt:
            # numeric loop bound times a symbolic coefficient (i*DW with
            # i in 0..3): the product stays symbolic (DW*3)
            lo, hi = bounds[var]
            v = hi if sign > 0 else lo
            if v == 0:
                pass
            elif v > 0:
                sym_parts.append(
                    f"{coef_txt}*{v}" if sign > 0 else f"-{coef_txt}*{v}"
                )
            else:
                return None
        else:
            return None
    if not sym_parts:
        return const_total
    # merge symbolic parts and the accumulated constant
    return _merge_symbolic(sym_parts, const_total)


def _symbolic_coef(hi_sym: str, coef: str, sign: int = 1) -> str:
    """(hi_sym) times a symbolic coefficient, simplified:
    _symbolic_coef('DITHER_NUM-1', 'BITS') -> 'BITS*DITHER_NUM-BITS'.
    COEF may carry a leading '-' (negative sign)."""
    m = re.match(r"^(.*?)([+-]\s*\d+)?$", hi_sym.strip())
    base = m.group(1).strip()
    const = int(m.group(2).replace(" ", "")) if m.group(2) else 0
    neg = coef.startswith("-")
    coef = coef.lstrip("-")
    if base.isdigit():
        return f"{(const + int(base)) * (-1 if neg else 1)}*{coef}"
    out = f"{coef}*{base}"
    if const:
        c = abs(const)
        term = f"{c}*{coef}" if c != 1 else coef
        out += ("-" if (const < 0) != neg else "+") + term
    return out


def _combine_symbolic(hi_sym: str, k: int, off: int) -> str:
    """(hi_sym)*k + off as a simplified expression string.

    hi_sym is a loop hi like ``NUM0-1`` or ``9``. Fold the constant part:
    (NUM0-1)*2+1 -> 2*NUM0-1; (9)*2+1 -> 19."""
    m = re.match(r"^(.*?)([+-]\s*\d+)?$", hi_sym)
    base, const = m.group(1), m.group(2)
    base = base.strip()
    c = 0
    if const:
        c = int(const.replace(" ", ""))
    if re.fullmatch(r"-?\d+", base):
        return str(int(base) * k + c * k + off)
    coef = f"{k}*" if k != 1 else ""
    total = c * k + off
    if total == 0:
        return f"{coef}{base}"
    sign = "+" if total > 0 else "-"
    return f"{coef}{base}{sign}{abs(total)}"


def _merge_symbolic(sym_parts: list[str], const: int) -> str:
    """Combine several symbolic hi contributions plus a constant offset into
    one simplified expression, e.g. [2*NUM0-2, NUM1-1] + 1 -> 2*NUM0+NUM1-2."""
    terms: list[str] = []
    total_const = const
    for part in sym_parts:
        m = re.match(r"^(.*?)([+-]\d+)?$", part)
        base, c = m.group(1), m.group(2)
        terms.append(base)
        if c:
            total_const += int(c)
    body = "+".join(t for t in terms if t)
    if total_const == 0:
        return body
    sign = "+" if total_const > 0 else "-"
    return f"{body}{sign}{abs(total_const)}"

def _classify_lhs(
    lhs: str,
    loop_ranges: Mapping[str, str],
    loop_bounds: Mapping[str, tuple[int, int]] | None = None,
    sym_hi: Mapping[str, str] | None = None,
) -> tuple[str, tuple[str, ...], str | None]:
    """Split an LHS like ``mem[ch][7:0]`` into (base, unpacked_dims,
    elem_packed_range). A bracket group equal to a loop variable becomes an
    unpacked dim; a trailing constant ``[M:N]``/``[N]`` becomes the element
    packed range. A loop-variable bit-select expression (``j*2``) resolves to
    a packed ``[0:max]`` range via the loop bounds (symbolic when the bound
    references a parameter). Unresolvable groups make elem_range None."""
    m = re.match(r"\s*(\w+)\s*((?:\[[^\]]*\]\s*)*)", lhs)
    if not m:
        return lhs.strip(), (), None
    base = m.group(1)
    groups = re.findall(r"\[([^\]]*)\]", m.group(2))
    if len(groups) > 1:
        # multi-index LHS with a non-constant, non-loop index (e.g. a 2D
        # packed array element mem[0][addr_w]): such a group is
        # unclassifiable (packed-dim select vs unpacked) — drop it from
        # consideration instead of fabricating dims/widths from it
        keep = []
        for g0 in groups:
            g0s = g0.strip()
            if (
                g0s in loop_ranges
                or re.fullmatch(r"-?\d+", g0s)
                or re.fullmatch(r"-?\d+\s*:\s*-?\d+", g0s)
                or (":" in g0s and not re.search(r"\+:|-:", g0s))
            ):
                keep.append(g0)
                continue
            if _eval_index(g0s, loop_bounds or {}, sym_hi or {}) is not None:
                keep.append(g0)
        if not keep:
            return base, (), None
        groups = keep
    bounds = loop_bounds or {}
    sym_hi = sym_hi or {}
    dims: list[str] = []
    elem: str | None = None
    for gi, g in enumerate(groups):
        g = g.strip()
        last = gi == len(groups) - 1
        if g in loop_ranges:
            dims.append(loop_ranges[g])  # plain loop var -> unpacked dim
        elif re.fullmatch(r"-?\d+\s*:\s*-?\d+", g):
            elem = re.sub(r"\s+", "", g)  # constant packed range [M:N]
        elif re.fullmatch(r"-?\d+", g):
            # constant bit-index [N]: an unpacked dim when more indices follow
            # (val3[i][3][...] -> [0:3]); a trailing [N] is a bit-select whose
            # index evidences the packed msb (x[15] = ... -> [15:0]); the
            # widest evidence wins on merge
            if not last:
                dims.append(f"0:{int(g)}")
            else:
                elem = f"{int(g)}:0"
        else:
            if ":" in g and "?" not in g and not re.search(r"\+:|-:", g):
                # symbolic packed range, e.g. [APB_BUS_ADDR_WIDTH-1:0]: keep
                # verbatim (whitespace-normalised); get_assign_side takes the
                # msb expression as the width
                elem = re.sub(r"\s+", "", g)
                continue
            pm = re.fullmatch(r"(.+?)\s*([+-])\s*:\s*(.+)", g)
            if pm:
                # indexed part-select [BASE +: W] / [BASE -: W]: the span
                # tops out at max(BASE)+W-1 (+:) or max(BASE) (-:) over
                # the loop bounds — the same formula
                # _record_inst_part_select uses for instance connections.
                hi = _eval_index(pm.group(1), bounds, sym_hi)
                if hi is None:
                    elem = None  # base not resolvable: no fabricated width
                elif pm.group(2) == "+":
                    msb = _combine_width(hi, pm.group(3).strip())
                    elem = "0:0" if msb == "c0" else f"{msb}:0"
                else:
                    elem = f"{hi}:0"
                continue
            # a loop-variable expression used as a bit select: j*2, j*2+1, j+1
            hi = _eval_index(g, bounds, sym_hi)
            if isinstance(hi, str):
                elem = f"{hi}:0"  # symbolic packed range, e.g. 2*NUM0-1:0
            elif hi is not None and hi >= 0:
                elem = f"{hi}:0"  # packed bit range covering all iterations
            elif ":" not in g and not set(re.findall(r"[a-zA-Z_]\w*", g)) & (
                set(loop_ranges) | set(bounds) | set(sym_hi)
            ):
                # no loop variable and no part-select colon: a plain symbolic
                # index [EXPR] evidences the packed msb of a vector
                # (x[PIPELINE_LENGTH-1] = ...)
                elem = re.sub(r"\s+", "", g) + ":0"
            else:
                elem = None  # unresolvable variable select
    return base, tuple(dims), elem


def _sym_const_term(expr: str) -> int:
    """Constant term of a symbolic msb expression ('2*NUM0-1' -> -1), used to
    pick the wider of two same-base symbolic widths."""
    m = re.search(r"([+-]\s*\d+)\s*$", expr)
    return int(m.group(1).replace(" ", "")) if m else 0


def _linked(right: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", right))


def get_assign_side(
    lhs: str,
    rhs: str,
    loop_ranges: Mapping[str, str] | None = None,
    loop_bounds: Mapping[str, tuple[int, int]] | None = None,
    sym_hi: Mapping[str, str] | None = None,
) -> Side | None:
    """automatic.vim s:GetAssignSide.  LHS/RHS are the text around the
    assignment operator (operator already removed). Multidim LHS indices are
    resolved against ``loop_ranges`` (for-loop variable -> range),
    ``loop_bounds`` (numeric bounds) and ``sym_hi`` (symbolic hi expressions,
    for parameterised bounds)."""
    loop_ranges = loop_ranges or {}
    if re.search(r"\[.*\]", lhs):
        base, dims, elem = _classify_lhs(lhs, loop_ranges, loop_bounds, sym_hi)
        if elem is not None and ":" in elem:
            parts = elem.split(":")
            if len(parts) != 2:
                # obfuscated/garbled source (e.g. VCS-encrypted models):
                # not a range select — declare width unknown, never crash
                return Side(base, None, dims=dims, elem_range=elem)
            hi_s, lo_s = parts
            if re.fullmatch(r"-?\d+", hi_s) and re.fullmatch(r"-?\d+", lo_s):
                hi, lo = int(hi_s), int(lo_s)
                width = str(1 + hi - lo) if hi >= lo else None
            else:
                # symbolic range "EXPR:0": keep the msb expression EXPR as-is
                # (extend_from_side treats it as the final msb, no -1).
                width = hi_s  # e.g. "2*NUM0-1"
        elif elem is not None:
            width = "1"
        elif dims:
            width = "1"  # unpacked-element LHS: scalar element (c0 after extend)
        else:
            width = None
        return Side(base, width, dims=dims, elem_range=elem)
    m = re.search(r"\w+\s*$", lhs)
    if not m:
        return None
    left = m.group(0).rstrip()
    if re.search(r"\W", left):
        return None
    right = re.sub(r"#`?\w+(\.\w+)?\s", "", rhs)  # strip #delay / #0.1
    right = right.strip()
    m = re.match(r"(`?\w+|\d+)'[bhd]", right)  # M'bN / M'hN / M'dN literal
    if m:
        return Side(left, m.group(1))
    if re.match(r"^~?\w+\[\s*\d+\s*\]\s*;$", right):  # sig[N];
        return Side(left, "1")
    m = re.match(r"^~?\w+\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*;$", right)  # sig[M:N];
    if m:
        high, low = int(m.group(1)), int(m.group(2))
        if high >= low:
            return Side(left, str(1 + high - low))
        return None  # M < N: Vim leaves the branch empty, no width at all
    # multidim RHS element/slice: sig[..][M:N] — last group is the packed slice
    m = re.match(r"^~?(\w+)((?:\[[^\]]*\])+)\s*;$", right)
    if m:
        groups = re.findall(r"\[([^\]]*)\]", m.group(2))
        last = groups[-1].strip()
        if re.fullmatch(r"-?\d+\s*:\s*-?\d+", last):
            hi, lo = (int(x) for x in re.split(r"\s*:\s*", last))
            return Side(left, str(1 + hi - lo) if hi >= lo else "1")
        pm = re.search(r"[+-]:\s*(\d+|[A-Za-z_][\w$]*)\s*$", last)
        if pm:
            # indexed part-select sig[base +: W]: the select is W wide
            return Side(left, pm.group(1))
        return Side(left, "1")  # element pick: scalar
    if re.match(r"^~?\w+\s*;$", right):  # plain signal (or ~signal)
        return Side(left, link=frozenset([re.search(r"\w+", right).group(0)]))
    if re.match(r"^~?\w+(\s+[&|^]\s+~?\w+)+\s*;$", right):  # sig0 & sig1 ...
        return Side(left, link=_linked(right))
    if re.match(r"^\(?\s*\w+\s*==\s*\w+\s*\)?\s*;$", right):  # sig0 == sig1
        return Side(left, "1")
    m = re.match(r"^~?\w+\s*\?\s*(\w+)\s*:\s*(\w+)\s*;$", right)  # sel ? a : b
    if m:
        s0, s1 = m.group(1), m.group(2)
        if re.search(r"[a-zA-Z]", s0) and re.search(r"[a-zA-Z]", s1):
            return Side(left, link=frozenset([s0, s1]))
        return Side(left, link=frozenset())
    return Side(left, link=frozenset())


# ---------------------------------------------------------------------------
# link graph (s:UpdateLinkDict / s:GroupLinkDict / s:UpdateDefine)


def update_link_dict(
    link_dict: dict[str, set[str]], paras: set[str], key: str, rhs_signals
) -> None:
    """Merge RHS signal names into link_dict[key], dropping parameters
    and numeric constants.  A constant is not a signal: letting ``0``
    join (every ``assign x = 0;`` tie-off links its LHS to it) bridges
    all such assignments in the file into one link group, and
    update_define then smears the widest member's width across
    unrelated hand-written declarations."""
    bucket = link_dict.setdefault(key, set())
    for name in rhs_signals:
        if name not in paras and not re.fullmatch(r"\d[\d_]*", name):
            bucket.add(name)


def group_link_dict(link_dict: dict[str, set[str]]) -> dict[str, set[str]]:
    """Transitive closure: merge groups connected through a shared member or
    through a member that is itself a key (union-find)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key, members in link_dict.items():
        find(key)
        for member in members:
            parent[find(member)] = find(key)
    groups: dict[str, set[str]] = {}
    for name in parent:
        groups.setdefault(find(name), set()).add(name)
    return groups


def update_define(
    unresolved: dict[str, str], link_dict: Mapping[str, set[str]], signals: SignalTable
) -> None:
    """Propagate width inside each link group (from the first member with a
    non-empty width to the empty-width members), then prune UNRESOLVED:
    io_wire/io_reg/usrdef/inst_wire always, freg/creg/wire only when their
    width is resolved.  Group members are the link_dict keys, as in Vim."""
    for key, members in link_dict.items():
        group = {key, *members}
        # resolve the group's driving width: prefer a NON-usrdef member (a real
        # driver, not a hand-written declaration) and the numerically widest,
        # so a stale usrdef width never wins over its driver's width.  An
        # inst_in_wire hint (from a submodule input port) is never evidence —
        # it must not win, and it must not block the group's resolved width.
        width = ""
        best = -1
        for name in group:
            sig = signals.get(name)
            if sig is None or sig.width == "" or sig.type in ("usrdef", "inst_in_wire"):
                continue
            if re.fullmatch(r"-?\d+", sig.width):
                if int(sig.width) > best:
                    best = int(sig.width)
                    width = sig.width
            elif not width:
                width = sig.width
        if not width:  # fall back: any non-empty width (e.g. only usrdef)
            for name in group:
                sig = signals.get(name)
                if sig is not None and sig.width != "" and sig.type != "inst_in_wire":
                    width = sig.width
                    break
        if width:
            for name in group:
                sig = signals.get(name)
                if sig is not None and (sig.width == "" or sig.type == "inst_in_wire"):
                    sig.width = width
            # a hand-written declaration in the group whose width is stale
            # (the group's resolved width is larger) is grown in place.
            for name in group:
                sig = signals.get(name)
                if sig is not None and sig.type == "usrdef":
                    signals._update_usrdef_width(sig, width)
    for name, sig in signals.signals.items():
        if sig.type in ("io_wire", "io_reg", "io_inout", "usrdef", "inst_wire"):
            unresolved.pop(name, None)
        elif sig.type in ("freg", "creg", "wire"):
            # these are always declared now (empty width defaults to scalar in
            # div_signals), so they never belong in unresolved
            unresolved.pop(name, None)
        elif sig.type == "inst_in_wire":
            # the input-port hint only earns a declaration when it carries a
            # width; without one the net stays unresolved
            if sig.width:
                unresolved.pop(name, None)
            else:
                unresolved[name] = "only seen on submodule input port(s), width unknown"


def _width_unknown_syms(width: str, known: set[str]) -> list[str]:
    """Identifiers in a width expression that do NOT resolve locally (not a
    parameter/localparam of THIS module, not a `` `define``, not a ``$``
    system function, not a number)."""
    w = re.sub(r"\d*'[bhdBHD][0-9a-fA-FxXzZ_?]+", "", width)  # sized literals
    out: list[str] = []
    for tok in re.findall(r"`?\w+", w):
        if tok[0].isdigit() or tok.startswith("$") or tok.startswith("`"):
            continue
        if tok not in known and tok not in out:
            out.append(tok)
    return out


def _merge_unpacked_indexes(idxs: Sequence[str], known: set[str]) -> str | None:
    """Merge element indexes from several instances into one unpacked range:
    ``{0, 2}`` → ``0:2``, ``{1, 1}`` → ``1:1``, ``{IDX}`` → ``IDX``.  Bare
    identifiers are trusted even when not locally defined (a template wrote
    them deliberately — verilog-mode keeps them symbolic); a
    symbolic/numeric mix or several distinct symbols is unmergeable (None)."""
    nums: list[int] = []
    syms: set[str] = set()
    for x in idxs:
        x = x.strip()
        if re.fullmatch(r"\d+", x):
            nums.append(int(x))
        elif re.fullmatch(r"[a-zA-Z_]\w*", x):
            syms.add(x)
        else:
            return None
    if syms:
        if len(syms) == 1 and not nums:
            return syms.pop()
        return None
    if not nums:
        return None
    return f"{min(nums)}:{max(nums)}"


def _width_syms_known(width: str, known: set[str]) -> bool:
    """Every identifier in a width expression must resolve locally: a
    parameter/localparam of THIS module, a `` `define`` (macros are global —
    they may come from an included header, so backticked names always
    pass), a ``$`` system function, or a number.  A submodule's own
    parameter names (e.g. IS_SIGNED from the port's range) are not visible
    at the parent."""
    return not _width_unknown_syms(width, known)


# ---------------------------------------------------------------------------
# bucketing (s:DivSignals)


@dataclass
class Divided:
    io_wire: list[Signal]  # has_defined == 0 ports, declaration order
    ff_reg: list[Signal]
    comb_reg: list[Signal]
    wire: list[Signal]
    inst_wire: list[Signal]
    max_len: int


def div_signals(signals: SignalTable) -> Divided:
    """Bucket signals for emission and compute the name-column width."""
    div = Divided([], [], [], [], [], _MAX_LEN_FLOOR)
    for name, sig in signals.signals.items():
        if sig.type in ("io_wire", "io_reg"):
            # multi-dim io port (greedy width holds "A:0][B-1" or
            # "A:0] [B-1"): the port declaration itself is already the
            # complete net declaration — a supplementary body wire would be
            # redundant, and kill's unregenerable waiver would shuttle it
            # out of the region on the next run
            # Multi-dim check: either width contains ][ (legacy) or packed_dims is set
            is_multidim = bool(sig.packed_dims) or re.search(r"\]\s*\[", sig.width)
            if not sig.has_defined and not is_multidim:
                sig.name = name
                div.io_wire.append(sig)
        elif sig.type == "freg":
            if sig.width == "":
                sig.width = "c0"  # no width source: default to scalar (1 bit)
            sig.name = name
            div.ff_reg.append(sig)
        elif sig.type == "creg":
            if sig.width == "":
                sig.width = "c0"
            sig.name = name
            div.comb_reg.append(sig)
        elif sig.type == "wire":
            if sig.width == "":
                sig.width = "c0"
            sig.name = name
            div.wire.append(sig)
        elif sig.type in ("inst_wire", "inst_in_wire"):
            if sig.width != "" or sig.packed_dims:
                sig.name = name
                div.inst_wire.append(sig)
        if sig.type != "usrdef" and (sig.width != "" or sig.packed_dims):
            div.max_len = max(div.max_len, _sig_decl_len(sig))
    div.io_wire.sort(key=lambda s: s.seq)
    for bucket in (div.ff_reg, div.comb_reg, div.wire, div.inst_wire):
        bucket.sort(key=lambda s: s.name)
    return div


def _usrdef_wider(new_msb: str, old_msb: str) -> bool:
    """True when a driver-derived width provably outgrows a hand-written one:
    both numeric and new > old, or the declaration is scalar ('c0') and the
    driver is a vector. Symbolic widths are never provably smaller -> False."""
    if not new_msb or new_msb == "c0":
        return False  # driver gives no growth
    if not re.fullmatch(r"-?\d+", new_msb):
        return False  # symbolic driver width: cannot prove
    if old_msb in ("", "c0"):
        return True  # scalar declaration gains a vector width
    if not re.fullmatch(r"-?\d+", old_msb):
        return False  # symbolic declared width: never touched
    return int(new_msb) > int(old_msb)


def _rewrite_usrdef_range(line: str, new_msb: str) -> str | None:
    """Rewrite ONLY the range of a one-line ``wire``/``reg`` declaration to
    ``[new_msb:0]``, preserving keyword, name, ``;`` and trailing comment.
    Returns the rewritten line, or None if the line is not a simple decl.
    The name must be the first identifier after the keyword, an optional
    ``signed`` and any packed ranges — a lazy ``.*?`` between keyword and
    name once slid across ``= 1'`` in ``reg r = 1'b0;`` declarations and
    rewrote them as declarations of a phantom signal named ``b0``."""
    m = re.match(
        r"^(?P<indent>\s*)(?P<kw>wire|reg|logic)\b"
        r"(?P<bw>(?:\s*signed\b)?(?:\s*\[[^\]]*\])*)\s*"
        r"(?P<name>[A-Za-z_]\w*)"
        r"(?P<tail>\s*(?:\[[^\]]*\]\s*)*;.*)$",
        line,
    )
    if not m:
        return None
    indent, kw, name, tail = (
        m.group("indent"),
        m.group("kw"),
        m.group("name"),
        m.group("tail"),
    )
    # tail keeps the name's unpacked dims (if any) and everything from ';' on
    if re.search(r"\[\s*-?\d+\s*:\s*-?\d+\s*\]", line):
        # replace the first packed range
        return re.sub(
            r"\[\s*-?\d+\s*:\s*-?\d+\s*\]", f"[{new_msb}:0]", line, count=1
        )
    # scalar declaration: insert the range right after the keyword
    return f"{indent}{kw} [{new_msb}:0] {name}{tail}"


def _emit_signal(sig: Signal, max_len: int, keyword: str, *, emacs_dims: bool = False) -> str:
    # head: the leading keyword — a typedef or an explicit net type from the
    # submodule port REPLACES a bare ``wire`` (``foo_t x;``, ``logic signed
    # [15:0] x;``) but follows a direction keyword (``input foo_t a``,
    # ``input logic signed [15:0] x``)
    head = keyword
    if sig.data_type and keyword.strip() == "wire":
        head = sig.data_type
    elif sig.net_type and keyword.strip() == "wire":
        head = sig.net_type
    # Type keyword padded: CalMargin(12, len) -> 12 - len + 1 spaces,
    # so the field is 13 wide (automatic.vim arithmetic)
    line = head + " " * (12 - len(head) + 1)
    if sig.data_type:
        if keyword.strip() != "wire":
            line += sig.data_type
    else:
        if sig.net_type and keyword.strip() != "wire":
            line += sig.net_type + " "
        if sig.signed:
            line += "signed "
        if sig.packed_dims:
            if emacs_dims:
                # verilog-insert-one-definition: dims verbatim, one space
                # before the LAST range ([A-1:0][B-1:0] [C-1:0])
                line += "".join(f"[{d}]" for d in sig.packed_dims[:-1])
                line += (" " if len(sig.packed_dims) > 1 else "") + f"[{sig.packed_dims[-1]}]"
            else:
                # multi-dim packed port: keep the original dimensions
                # verbatim — exactly what the submodule port declares
                line += "".join(f"[{d}]" for d in sig.packed_dims)
        elif sig.width != "c0":
            line += f"[{sig.width}:0]"
    line += _cal_margin(max_len, len(line)) or " "
    line += sig.name
    if sig.dims:
        line += " " + ("" if emacs_dims else " ").join(f"[{d}]" for d in sig.dims)
    return line + ";"


def _sig_decl_len(sig: Signal, *, emacs_dims: bool = False) -> int:
    """Column contribution of one declaration: ``5 + len(width) + 4`` for a
    vector ('reg  '/'wire ' is 5 chars; '[width:0]' adds 4), 5 for scalar;
    multi-dim packed dims add their bracketed text; net type / signed /
    typedef type add their text (a replacement head swaps the 5-char wire)."""
    if sig.data_type:
        return max(5, len(sig.data_type))
    extra = (len(sig.net_type) + 1 if sig.net_type else 0) + (7 if sig.signed else 0)
    if sig.packed_dims:
        return 5 + extra + sum(len(d) + 2 for d in sig.packed_dims) + (
            1 if emacs_dims and len(sig.packed_dims) > 1 else 0
        )
    return 5 + extra if sig.width == "c0" else 5 + extra + len(sig.width) + 4


_CONN_DIM_COMMENT = re.compile(r"/\*\s*((?:\[[^\]]+\])+)\s*\*/")
_REDUNDANT_PARENS = re.compile(r"\((\w+)\)")


def _normalize_dim(dim: str) -> str:
    """Cosmetic: strip redundant parens around a lone symbol so all dims
    read alike — ``(W)-1:0`` and ``W-1:0`` become ``W-1:0``."""
    return _REDUNDANT_PARENS.sub(r"\1", dim.strip())


def _clean_dim(dim: str) -> str:
    """Full dim cleanup: strip redundant parens, then fold pure-numeric
    expressions (``12-1`` -> ``11``, ``2*16-1`` -> ``31``) the way
    verilog-mode writes notes/declarations; anything with an identifier
    stays symbolic."""
    from .emacs import _fold_numeric_expr

    return _fold_numeric_expr(_normalize_dim(dim))


def _conn_packed_dims(text: str) -> tuple[str, ...]:
    """Packed dims from an EAI multidim connection note ``net/*[D1][D2]*/``:
    the dims the template expansion already param-value-substituted, so they
    name parent-visible symbols."""
    m = _CONN_DIM_COMMENT.search(text)
    if not m:
        return ()
    return tuple(_clean_dim(d) for d in re.findall(r"\[([^\]]+)\]", m.group(1)))


# declarations re-extraction cannot reproduce are "unregenerable" — the
# kill waiver keeps them verbatim (packed-dim selects and unpacked indices
# are indistinguishable in assignments)
_UNREGENERABLE = re.compile(
    r"^\s*(?:wire|reg|logic)\s*(?:\[[^\]]+\]\s*){2,}\w+\s*;"
    r"|^\s*(?:wire|reg|logic)\s*(?:\[[^\]]+\]\s*)*\w+\s*(?:\[[^\]]+\]\s*)+;"
)


def _usrdef_packed_dims(line: str) -> "tuple[str, ...] | None":
    """The packed-dimension prefix of a waive-shaped usrdef declaration
    (``wire[2:0][10:0] x;`` -> ("2:0", "10:0")), normalized like the
    instance-evidence dims; None for non-matching lines."""
    if not _UNREGENERABLE.match(line):
        return None
    m = re.match(r"^\s*(?:wire|reg|logic)\s*((?:\[[^\]]+\]\s*)+)", line)
    if not m:
        return None
    dims = tuple(_clean_dim(d) for d in re.findall(r"\[([^\]]+)\]", m.group(1)))
    return dims if len(dims) > 1 else None


def _usrdef_unpacked_dims(line: str) -> "tuple[str, ...] | None":
    """The after-name (unpacked) dims of a waive-shaped usrdef declaration
    (``reg rlast_r[0:N];`` -> ("0:N",)); None for non-matching lines."""
    if not _UNREGENERABLE.match(line):
        return None
    m = re.match(
        r"^\s*(?:wire|reg|logic)\s*(?:\[[^\]]+\]\s*)*\w+\s*((?:\[[^\]]+\]\s*)+)\w*;", line
    )
    if not m:
        return None
    return tuple(_clean_dim(d) for d in re.findall(r"\[([^\]]+)\]", m.group(1)))


def _absorb_waived_usrdef(sig: Signal, pdims: tuple, new_type: str) -> bool:
    """A waived orphan (kill's unregenerable waiver left it right after the
    /*autodef*/ marker) whose declaration the current evidence re-derives
    IDENTICALLY: convert it to the derived signal type so the region
    re-emits it and the orphan line is dropped — otherwise the line shuttles
    between the region and the orphan slot on alternating runs.  Only orphan
    lines are eligible: a hand-written multi-dim declaration anywhere else
    keeps its usrdef status verbatim."""
    if sig.type != "usrdef" or not sig.orphan or not pdims or sig.drop_line:
        return False
    dims = _usrdef_packed_dims(sig.line)
    if dims is None or tuple(_clean_dim(d) for d in pdims) != dims:
        return False
    sig.type = new_type
    sig.packed_dims = tuple(_clean_dim(d) for d in pdims)
    sig.drop_line = True
    return True


def kill_auto_def_t(lines: Sequence[str]) -> list[str]:
    """Delete the generated region (``// Define io wire here`` through
    ``// End of automatic define``), keeping the /*autodef*/ marker line.
    Also recognises the legacy AutoDef (AD) section header
    ``// Define flip-flop registers here`` used by older generated regions.

    Declarations that re-extraction cannot reproduce are kept verbatim (same
    spirit as a ``//DT`` waiver):
    - two or more packed dimensions (``reg [A:0][B:0] mem;``)
    - any unpacked dimension after the name (``reg [W:0] mem [0:N][0:M];``)
      — packed-dim selects and unpacked indices are indistinguishable in
      assignments, so these declarations are not regenerable."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _AUTODEF_MARK.match(line):
            out.append(line)
        elif _norm_marker(line) in (
            _norm_marker("// Define io wire here"),
            _norm_marker("// Define flip-flop registers here"),
        ):
            i += 1
            while i < len(lines) and _norm_marker(lines[i]) != _norm_marker(
                "// End of automatic define"
            ):
                if _UNREGENERABLE.match(lines[i]):
                    out.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # drop the "// End of automatic define" line too
            continue
        else:
            out.append(line)
        i += 1
    return out


def _waived_orphan_lines(lines: Sequence[str]) -> frozenset:
    """Line indices of kill-waived multi-dim declarations: waive-shaped
    lines sitting directly after an /*autodef*/ marker — the old region's
    surviving lines, which kill leaves in place.  Only these are eligible
    for absorb; a hand-written declaration elsewhere is never touched."""
    out: set[int] = set()
    for i, ln in enumerate(lines):
        if not _AUTODEF_MARK.match(ln):
            continue
        j = i + 1
        while j < len(lines) and _UNREGENERABLE.match(lines[j]):
            out.add(j)
            j += 1
    return frozenset(out)


# ---------------------------------------------------------------------------
# AutoDefT


def _split_assign(line: str, op: str) -> tuple[str, str] | None:
    """(lhs, rhs) around the first OP ('<=' or '='); None when absent.
    LHS may carry several bracket groups (``mem[i][j*2]``); an LHS with an
    arithmetic suffix (``val[i]+1`` — not valid Verilog) is rejected so it
    never produces a bogus declaration like ``reg 1;``."""
    if op == "<=":
        m = re.search(r"\w+\s*((?:\[[^\]]*\]\s*)*)<=", line)
        if not m:
            return None
        lhs = re.sub(r"<=", "", m.group(0))
        rhs = re.sub(r"<=", "", line[line.index("<=") :])
    else:
        m = re.search(r"\w+\s*((?:\[[^\]]*\]\s*)*)=(?![=])", line)
        if not m:
            return None
        if re.search(r"[a-zA-Z0-9_'`]\s*$", line[: m.start()]):
            return None  # the match is a constant/keyword tail, not a base name
        lhs = m.group(0).replace("=", "")
        rhs = re.sub(r"=", "", line[line.index("=") :], count=1)
    rhs = _strip_inline_comment(rhs)
    # reject an LHS with arithmetic junk outside the brackets
    # (``val[i]+1`` is not valid Verilog); brackets' contents stay untouched.
    base_end = re.match(r"\s*\w+", lhs).end()
    tail = lhs[base_end:]
    tail_no_brackets = re.sub(r"\[[^\]]*\]", "", tail)
    if re.search(r"[^\w\s]", tail_no_brackets):
        return None
    # also reject when the match is actually a constant after an operator
    # (``... +1 =`` matched the ``1`` as if it were a signal)
    if re.fullmatch(r"\d+", lhs.strip()):
        return None
    return lhs, rhs


# ---------------------------------------------------------------------------
# concatenation LHS ({a, b[..], ...} = ...), single- or multi-line


def _split_concat_members(body: str) -> list[str] | None:
    """Top-level comma split of a concat body, ``[]``/``{}``/``()`` depth
    aware.  None when the text is not exactly one balanced ``{...}`` group
    with optional whitespace around it."""
    text = body.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    inner = text[1:-1]
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in inner:
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
            if depth < 0:
                return None
        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if not part:
                return None
            parts.append(part)
            buf = []
        else:
            buf.append(ch)
    if depth != 0:
        return None
    part = "".join(buf).strip()
    if not part:
        return None
    parts.append(part)
    return parts


def _split_concat_assign(line: str, op: str) -> tuple[str, str] | None:
    """Split a concatenation assignment ``{a, b} <op> rhs`` into the outer
    braces text and the RHS.  OP is '<=' or '='.  The outer braces are
    balanced first (``<``/``>`` count as depth so comparisons inside a
    member cannot fake a close); the operator must then directly follow the
    matching '}' — a bare '=' is accepted only when it is not part of
    <=/==/>=/!=.  The scan starts at the first '{' so for-loop syntax can
    never be treated as an assignment here."""
    start = line.index("{")
    depth = 0
    close = -1
    for p in range(start, len(line)):
        ch = line[p]
        if ch in "{[<":
            depth += 1
        elif ch in "}]>":
            depth -= 1
        if depth == 0 and ch == "}":
            close = p
            break
    if close < 0:
        return None  # braces never balance: not a complete concat LHS
    p = close + 1
    while p < len(line) and line[p].isspace():
        p += 1
    if op == "<=":
        if line[p : p + 2] != "<=":
            return None
        p += 1
    else:
        # a bare '=' only; <=/==/>=/!= and a missing operator are rejected
        if p >= len(line) or line[p] != "=" or line[p - 1] in "<>=!":
            return None
        if p + 1 < len(line) and line[p + 1] == "=":
            return None
    rhs = line[p + 1 :]
    if not rhs.strip():
        return None
    return line[start : close + 1], rhs


def _lhs_member_sides(
    lhs: str,
    rhs: str,
    loop_ranges: Mapping[str, str] | None = None,
    loop_bounds: Mapping[str, tuple[int, int]] | None = None,
    sym_hi: Mapping[str, str] | None = None,
) -> list[Side]:
    """Classify one concat member through :func:`get_assign_side`; nested
    concats recurse.  An unsliced member may NOT take the RHS literal's
    total width (``{p, q} = 4'h0`` leaves p and q scalar): a plain signal
    RHS keeps its link, any other RHS form degrades to a scalar (width 1).
    A member with an explicit packed slice keeps the width of its own
    select (``a[3:0]`` in ``{a[3:0], b} = 12'h0`` is 4 bits, not 12)."""
    if lhs.lstrip().startswith("{"):
        return get_concat_assign_sides(lhs, rhs, loop_ranges, loop_bounds, sym_hi)
    side = get_assign_side(lhs, rhs, loop_ranges, loop_bounds, sym_hi)
    if side is None:
        return []
    if side.link is not None and not side.link:
        return [Side(side.name, "1", None, side.dims, side.elem_range)]
    if side.width is not None and side.elem_range is None:
        # an unsliced member never inherits the RHS literal's total width
        return [Side(side.name, "1", None, side.dims, side.elem_range)]
    return [side]


def get_concat_assign_sides(
    lhs: str,
    rhs: str,
    loop_ranges: Mapping[str, str] | None = None,
    loop_bounds: Mapping[str, tuple[int, int]] | None = None,
    sym_hi: Mapping[str, str] | None = None,
) -> list[Side]:
    """Classify every member of a concatenation LHS.  LHS is the outer
    ``{...}`` text, RHS the text after the operator.  Each member goes
    through :func:`get_assign_side` so multidim/loop-var handling applies
    per member; a sliced member's width comes from its own select, never
    from the total width of an RHS literal like ``2'b0``, and an unsliced
    member stays scalar unless it can link to a plain signal.  Nested
    concats recurse.  When the RHS is a concat of the same arity, each
    member is classified against its matching RHS element."""
    parts = _split_concat_members(lhs)
    if parts is None:
        return []
    rhs_members = _split_concat_members(rhs.strip().rstrip(";").strip())
    rhs_linkable = rhs_members is not None and len(rhs_members) == len(parts)
    sides: list[Side] = []
    for idx, member in enumerate(parts):
        member_rhs = rhs if not rhs_linkable else rhs_members[idx].strip() + ";"
        sides.extend(
            _lhs_member_sides(member, member_rhs, loop_ranges, loop_bounds, sym_hi)
        )
    return sides


def _join_statement(lines: Sequence[str], i: int, op: str) -> tuple[str, int]:
    """Join a logical assignment statement across lines.  When the text from
    ``i`` onward has an unbalanced ``{`` (multi-line concat LHS) or the OP
    has not appeared yet, following lines are appended with their //
    comments stripped until braces balance and OP is present.  Returns
    (joined_text, next_line_index)."""
    text = re.sub(r"//.*$", "", lines[i])
    j = i
    # statement ends at the first ';' once the operator was seen (the ';'
    # separators of a for(...) header precede it and must not stop the join)
    has_op = op in text
    if has_op and ";" in text[text.index(op) :]:
        return text, i + 1
    while j + 1 < len(lines):
        if has_op and text.count("{") <= text.count("}"):
            break
        if _BLOCK_BREAK.search(lines[j + 1]):
            break
        j += 1
        text += " " + re.sub(r"//.*$", "", lines[j])
        if not has_op:
            has_op = op in text
        if has_op and ";" in text[text.index(op) :]:
            break
    return text, j + 1


def _strip_condition(line: str) -> str:
    line = re.sub(r"\bif\s*\(.*\)\s+", "", line)
    return re.sub(r"\belse\s+", "", line)


def _scan_always_block(
    lines: Sequence[str],
    start: int,
    trigger: str,
    stype: str,
    signals: SignalTable,
    link_dict: dict[str, set[str]],
    paras: set[str],
    loop_ranges: Mapping[str, str] | None = None,
    loop_bounds: Mapping[str, tuple[int, int]] | None = None,
    sym_hi: Mapping[str, str] | None = None,
) -> int:
    """Consume an always block starting after its header line; per matching
    line record the LHS signal (freg for ``<=``, creg for ``= ... ;``)."""
    i = start
    while i < len(lines) and not _BLOCK_BREAK.search(lines[i]):
        j = _skip_comment_line(lines, i)
        if j == -1:
            return len(lines)
        i = j
        line = lines[i]
        if _BLOCK_BREAK.search(line):
            break
        # strip trailing comments so comment text can never fake an
        # assignment operator ('; /* ... */' or '// a = b' tails)
        code = _strip_inline_comment(line)
        if not code.strip():
            i += 1
            continue
        # a concat LHS may span lines: its opening '{' line carries no ';',
        # so it must be joined before the plain-line assignment gate below
        if "{" in code:
            joined, next_i = _join_statement(lines, i, trigger)
            concat_sides = _split_concat_assign(_strip_inline_comment(joined), trigger)
            if concat_sides is not None:
                for side in get_concat_assign_sides(
                    *concat_sides, loop_ranges, loop_bounds, sym_hi
                ):
                    signals.extend_from_side(side, stype)
                    if side.link is not None:
                        update_link_dict(link_dict, paras, side.name, side.link)
                i = next_i
                continue
            if next_i != i + 1:
                i = next_i  # unbalanced/junk multi-line text: not an assignment
                continue
        if re.search(r".*<=.*", code) if trigger == "<=" else re.search(r".*=.*;", code):
            # automatic.vim strips if(...)/else only in the <= (freg) block;
            # doing it for '=' lines would eat '(a == b)' via 'if\s*(.*)'
            if trigger == "<=":
                code = _strip_condition(code)
            sides = _split_assign(code, trigger)
            if sides:
                side = get_assign_side(*sides, loop_ranges, loop_bounds, sym_hi)
                signals.extend_from_side(side, stype)
                if side is not None and side.link is not None:
                    update_link_dict(link_dict, paras, side.name, side.link)
        i += 1
    return i


def _scan_inst_body_text(
    lines: Sequence[str],
    start: int,
    inst_io: Mapping[str, Port],
    signals: SignalTable,
    loop_bounds: Mapping[str, tuple[int, int]] | None = None,
    sym_hi: Mapping[str, str] | None = None,
    param_values: Mapping[str, str] | None = None,
) -> int:
    """Consume an instance whose pin list opens on the header line
    (``sub u_sub (.clk(clk), .dout(w));``), possibly spanning lines.

    The pin list is located by paren balance from the header's pin
    ``(``, split at top-level commas, and each named pin is recorded
    exactly like a line of the line-oriented body scan.  Positional
    connections stay unrecorded, as in the line-oriented path.
    Returns the line index after the instance's closing line.
    """
    from .inst import _split_top_commas

    loop_bounds = loop_bounds or {}
    sym_hi = sym_hi or {}
    m = re.match(
        r"^\s*\w+\s+(?:#\s*\(.*?\)\s*)?(\w+)\s*\(", re.sub(r"//.*$", "", lines[start])
    )
    if not m:
        return start + 1
    text = "\n".join(lines[start:])
    open_idx = m.end() - 1
    depth = 0
    close_idx = -1
    for k in range(open_idx, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                close_idx = k
                break
    if close_idx < 0:
        return start + 1
    inner = text[open_idx + 1 : close_idx]
    for part in _split_top_commas(inner):
        pm = re.match(r"\s*\.(\w+)\s*\((.*)\)\s*$", part, re.S)
        if pm:
            signals.extend_inst_wire_from_line(
                pm.group(1) + " " + pm.group(2) + ")",
                inst_io,
                loop_bounds,
                sym_hi,
                param_values,
            )
    return start + text[:close_idx].count("\n") + 1


def _scan_inst_body(
    lines: Sequence[str],
    start: int,
    inst_io: Mapping[str, Port],
    signals: SignalTable,
    loop_bounds: Mapping[str, tuple[int, int]] | None = None,
    sym_hi: Mapping[str, str] | None = None,
    param_values: Mapping[str, str] | None = None,
) -> int:
    """Consume an instance body (after the marker / header line) up to ``);``,
    recording output-port nets as inst_wire (and input-port nets as the
    inst_in_wire width fallback).  A pin sitting on the SAME line as the
    closing ``));`` — the last pin of every emacs-style expansion — is
    processed before the body is considered closed."""
    loop_bounds = loop_bounds or {}
    sym_hi = sym_hi or {}
    i = start
    while i < len(lines):
        j = _skip_comment_line(lines, i)
        if j == -1:
            return len(lines)
        i = j
        line = lines[i]
        if _INST_PORT.match(line):
            m = re.match(r"^\s*\.(\w+)\s*\((.*)", line)
            signals.extend_inst_wire_from_line(
                m.group(1) + " " + m.group(2), inst_io, loop_bounds, sym_hi, param_values
            )
        if re.search(r"\);\s*$", line) or ");" in line:
            i += 1
            break
        i += 1
    return i


def _emit_sections(
    marker_line: str,
    div: Divided,
    unresolved: dict[str, str],
    loop_decls: Sequence[tuple[str, str]] = (),
) -> list[str]:
    out = [marker_line, "// Define io wire here"]
    for sig in div.io_wire:
        # An undriven OUTPUT is declared reg (matching AUTOREG in the -a
        # flow): the user will drive it from an always block next, and a
        # wire would make that illegal.  assign/always/instance-driven
        # outputs and all inputs/inouts stay wire.
        kw = (
            "reg  "
            if sig.type == "io_reg" or (sig.io_dir == "output" and not sig.driven)
            else "wire "
        )
        out.append(_emit_signal(sig, div.max_len, kw))
    out.append("// Define flip-flop registers here")
    for sig in div.ff_reg:
        out.append(_emit_signal(sig, div.max_len, "reg  "))
    out.append("// Define combination registers here")
    for sig in div.comb_reg:
        out.append(_emit_signal(sig, div.max_len, "reg  "))
    out.append("// Define wires here")
    for sig in div.wire:
        out.append(_emit_signal(sig, div.max_len, "wire "))
    out.append("// Define inst wires here")
    for sig in div.inst_wire:
        out.append(_emit_signal(sig, div.max_len, "wire "))
    out.append("// Define integer here")
    for name, kind in loop_decls:
        if kind == "genvar":
            out.append(f"genvar {name};")
        else:
            # same alignment as wire/reg: keyword padded to column 12, the
            # name at the max_len column
            out.append(_emit_signal(Signal(width="c0", name=name), div.max_len, "integer"))
    out.append("// Unresolved define signals here")
    for name in sorted(unresolved):
        # emit as a comment, not `unresolved x;` (which is illegal Verilog and
        # breaks compilation; the Vim original had this bug). Unresolved
        # signals may be declared elsewhere — a comment flags them for manual
        # review without producing duplicate or invalid declarations.  The
        # trailing reason tells the user WHY no declaration was generated.
        out.append(f"// unresolved: {name} // {unresolved[name]}")
    out.append("// End of automatic define")
    return out


def _candidate_module_names(lines: Sequence[str]) -> set[str]:
    """Names that could be instantiated modules in LINES: the first token of
    any ``name [#(...)] instname (`` header that is not a keyword. Used by
    the CLI to gather module files before the real scan."""
    names: set[str] = set()
    for line in lines:
        raw = re.sub(r"//.*$", "", line)
        m = re.match(r"^\s*(\w+)\s+(#\s*\(.*\)\s*)?(\w+)\s*\(", raw)
        if m and m.group(1) not in _KEYWORDS:
            names.add(m.group(1))
            continue
        m = re.match(r"^\s*(\w+)\s+#\s*\(\s*$", raw)
        if m and m.group(1) not in _KEYWORDS:
            names.add(m.group(1))
    return names


def _instance_headers(
    lines: Sequence[str], modules: Mapping[str, ModuleDef]
) -> "dict[int, tuple[str, str, int]]":
    """Marker-less instantiation headers whose module name is a key of
    MODULES.

    Returns {line_index: (module_name, instance_name, header_end_index)}
    where header_end_index is the index of the line whose text ends with
    ``(`` for a multi-line parameter override; ``-1`` when the body opens on
    the same line. Handles ``module inst (``, ``module #(...) inst (`` and
    multi-line ``#( ... )`` parameter overrides. Conservative: a candidate
    must match ``\\s*module\\s*(#\\s*\\(...\\))?\\s*inst\\s*\\(\\s*$`` and
    module must be in MODULES; anything else is ignored."""
    out: dict[int, tuple[str, str, int]] = {}
    i = 0
    n = len(lines)
    while i < n:
        raw = re.sub(r"//.*$", "", lines[i])
        m = re.match(r"^\s*(\w+)\s*(#\s*\()\s*$", raw)
        if m and m.group(1) in modules:
            # module #(   <- parameter override spans lines; find its close,
            # then the ``inst_name (`` line that follows
            depth = raw.count("(") - raw.count(")")
            j = i + 1
            while j < n and depth > 0:
                depth += _bal(lines[j])
                if depth <= 0:
                    break
                j += 1
            # j is the line where the #( ... ) closes; the instance name
            # opens the body on the next line
            if j < n:
                nxt = re.sub(r"//.*$", "", lines[j + 1]) if j + 1 < n else ""
                m2 = re.match(r"^\s*(\w+)\s*\(\s*$", nxt)
                if m2 and m2.group(1) not in _KEYWORDS:
                    out[i] = (m.group(1), m2.group(1), j + 1)
                    i = j + 1
                    continue
        m = re.match(r"^\s*(\w+)\s+(#\s*\(.*\)\s*)?(\w+)\s*\(\s*$", raw)
        if m and m.group(1) in modules and m.group(3) not in _KEYWORDS:
            out[i] = (m.group(1), m.group(3), -1)
            i += 1
            continue
        # body opens on the header line itself: `mod inst (.clk(clk), ...);`
        # (header_end -2 marks "scan the header line's own text")
        m = re.match(r"^\s*(\w+)\s+(#\s*\(.*?\)\s*)?(\w+)\s*\((?!\s*$)", raw)
        if m and m.group(1) in modules and m.group(3) not in _KEYWORDS:
            out[i] = (m.group(1), m.group(3), -2)
        i += 1
    return out


def _bal(line: str) -> int:
    """Net ``(`` minus ``)`` count of a line, ignoring // comments."""
    t = re.sub(r"//.*$", "", line)
    return t.count("(") - t.count(")")


def _structure_names(
    lines: Sequence[str], modules: Mapping[str, ModuleDef]
) -> set[str]:
    """Identifiers that are never signals: genvar declarations, generate
    block labels (``begin : label``), instantiation module/instance names
    (``/*autoinst*/``-marked ones via :class:`VerilogBuffer`, marker-less
    ones whose module resolves in MODULES via :func:`_instance_headers`)."""
    names: set[str] = set()
    for raw in lines:
        line = re.sub(r"//.*$", "", raw)
        m = _GENVAR_DECL.search(line)
        if m:
            for tok in m.group(1).split(","):
                tok = re.sub(r"=.*", "", tok).strip()
                if re.fullmatch(r"\w+", tok):
                    names.add(tok)
        m = _GENERATE_LABEL.search(line)
        if m:
            names.add(m.group(1))
    buf = VerilogBuffer(list(lines))
    for idx in buf.markers():
        try:
            mod, inst = buf.resolve_instance(idx)
        except ValueError:
            continue
        names.add(mod)
        names.add(inst)
    for mod, inst, _ in _instance_headers(lines, modules).values():
        names.add(mod)
        names.add(inst)
    names.update(_markerless_instance_names(lines))
    return names


# marker-less instantiation header shapes (module unresolvable or not):
# ``mod #(...) inst (`` and ``mod inst (`` — the two identifier-plus-pin-list
# forms an instantiation statement can take
_MLI_HEAD = re.compile(
    r"^[ \t]*([A-Za-z_]\w*)[ \t]*(?:(#[ \t]*\()|([A-Za-z_]\w*)[ \t]*(?:\[[^\]]*\][ \t]*)?\()",
    re.M,
)
_MLI_INST = re.compile(r"\s*([A-Za-z_]\w*)[ \t]*(?:\[[^\]]*\][ \t]*)?\(")


def _markerless_instance_names(lines: Sequence[str]) -> set[str]:
    """Module and instance names of marker-less instantiation-shaped
    statements, whether or not the module resolves.

    ``mod inst (`` and ``mod #(...) inst (`` at statement start are
    instantiations (or SystemVerilog interface instances, same shape):
    neither the module/type name nor the instance name is a signal, so
    they must never land in the unresolved set.  `` `include`` files are
    read through (analysis only) — an interface-instance include
    (``axi_bus u_bus (...);``) declares those names for the whole module.
    Conservative: the first word must not be a keyword and a pin-list
    ``(`` must follow the instance name, so declarations
    (``mytype myvar;``) and plain calls (``foo(bar);``) never match.
    ``module``/``function``/``task`` headers are excluded via _KEYWORDS."""
    from .comments import mask_comments
    from .emacs import _balanced
    from .libdirs import expand_includes

    text = mask_comments("\n".join(expand_includes(list(lines))))
    names: set[str] = set()
    for m in _MLI_HEAD.finditer(text):
        mod = m.group(1)
        if mod in _KEYWORDS:
            continue
        if m.group(3) is not None:
            # ``mod inst (`` — both words on the matched line
            inst = m.group(3)
            if inst not in _KEYWORDS:
                names.add(mod)
                names.add(inst)
            continue
        # ``mod #( ... ) inst (``: skip the balanced parameter block, the
        # instance name opens the pin list right after it
        open_idx = m.end() - 1
        try:
            _inner, end = _balanced(text, open_idx)
        except ValueError:
            continue
        im = _MLI_INST.match(text, end)
        if im and im.group(1) not in _KEYWORDS:
            names.add(mod)
            names.add(im.group(1))
    return names


_UDT_DECL_LINE = re.compile(
    r"^\s*(?P<type>[A-Za-z_][\w]*(?:::[A-Za-z_]\w*)*)\s+"
    r"(?:\[[^\]]*\]\s*)?"
    r"[A-Za-z_]\w*(?:\s*\[[^\]]*\])?"
    r"(?:\s*,\s*[A-Za-z_]\w*(?:\s*\[[^\]]*\])?)*"
    r"\s*(?:=.*)?;\s*$"
)

_UDT_SHAPE_KEYWORDS = frozenset({
    "always", "always_comb", "always_ff", "always_latch", "assign",
    "automatic", "begin", "bit", "byte", "case", "clocking", "const",
    "else", "end", "endcase", "endclass", "endmodule", "enum", "for",
    "function", "generate", "genvar", "if", "import", "initial",
    "input", "inout", "integer", "interface", "localparam", "logic",
    "modport", "module", "output", "package", "parameter", "property",
    "reg", "return", "shortint", "shortreal", "signed", "static",
    "string", "struct", "task", "time", "typedef", "union", "unsigned",
    "var", "void", "while", "wire",
})


def is_udt_decl_line(line: str) -> bool:
    """True when LINE is a ``<type-name> <var> [, <var>]*;`` declaration.

    With no ``verilog-typedef-regexp`` in the buffer, recognition is
    structural: the first word is a type-position identifier and the
    line is a plain name list closed by ``;``.  Statements that are not
    declarations are kept out by the keyword blocklist, the no-parens
    rule (rules out instantiations and calls) and the ``;`` terminator.
    """
    m = _UDT_DECL_LINE.match(line)
    if not m or "(" in line or ")" in line:
        return False
    return m.group("type").rsplit("::", 1)[-1] not in _UDT_SHAPE_KEYWORDS


def _is_typedef_decl(line: str) -> bool:
    """A ``reqcmd_t BReq;``-style declaration: the first word matches the
    buffer's verilog-typedef-regexp (a TYPE); the second word is the signal.

    With no typedef regexp configured, fall back to the structural
    shape check of :func:`is_udt_decl_line`."""
    from .inst import _TYPEDEF_REGEXP

    if _TYPEDEF_REGEXP is not None:
        m = re.match(r"^\s*(\w+)\s+\w+", line)
        if m and _TYPEDEF_REGEXP.search(m.group(1)):
            return True
    return is_udt_decl_line(line)


def _comma_separated_header(expanded: list[str], original: Sequence[str]) -> list[str]:
    """Re-comma the ANSI header _expand_ansi_header split apart.

    _expand_ansi_header rewrites a single-line ANSI port list into one
    comma-less port per line so the header parses like a multi-line
    one.  autoarg rebuilds the header afterwards, but autodef passes
    the expanded form through to its output, emitting

        module top (
        input clk
        input [7:0] din
        );

    -- a syntax error.  When (and only when) the original header was
    the single-line form the expansion fires on, put the commas back:
    every expanded port line but the last gets its trailing comma,
    yielding the ordinary multi-line header.  Headers the user
    already wrote across lines are returned untouched (the expansion
    passes them through, and their commas are already present).
    """
    from .inst import _balanced_close

    text = "\n".join(original)
    m = re.search(r"\bmodule\s+\w+", text)
    if not m:
        return expanded
    pos = m.end()
    if re.match(r"\s*#\s*\(", text[pos:]):
        j = _balanced_close(text, text.index("(", pos))
        if j < 0:
            return expanded
        pos = j + 1
    if not re.match(r"\s*\(", text[pos:]):
        return expanded
    open_paren = pos + text[pos:].index("(")
    close_paren = _balanced_close(text, open_paren)
    if close_paren < 0:
        return expanded
    inner = text[open_paren + 1 : close_paren]
    if "\n" in inner.strip():
        return expanded  # no single-line port list: expansion did not fire
    # _expand_ansi_header (called without interfaces) only fires when the
    # single-line list holds real port declarations.  A marker-only list
    # (`module a(/*autoarg*/);`) is also "single-line": without this gate
    # the walk below never meets a `)`-led line and commas every line to
    # end of file (comments, body declarations, even `reg ack;` -> `; ,`).
    if not re.search(r"\b(?:input|output|inout)\b", inner):
        return expanded
    head_lines = text[: open_paren + 1].count("\n") + 1
    out = list(expanded)
    i = head_lines
    ports = []
    closed = False
    while i < len(out):
        if out[i].lstrip().startswith(")"):
            closed = True
            break
        if out[i].strip():
            ports.append(i)
        i += 1
    if not closed:
        return expanded  # expanded header lost its close: touch nothing
    for k in ports[:-1]:
        head, sep, tail = out[k].partition("//")
        if head.strip() and not head.rstrip().endswith(","):
            # a port line with a trailing // comment gets its comma
            # before the comment; a pure comment line is left alone
            out[k] = head.rstrip() + "," + (" " + sep + tail if sep else "")
    return out


def auto_def_t(lines: Sequence[str], modules: Mapping[str, ModuleDef] | None = None) -> list[str]:
    """Regenerate the /*autodef*/ declarations of every marked module.

    MODULES maps instance module names to their parsed definitions (for
    inst_wire port-width lookup); it may be empty/None when the buffer has
    no /*autoinst*/ markers.  User ``wire``/``reg`` declarations stay in
    place and are never moved or deleted.

    The buffer may hold several modules (a submodule defined above the
    instantiating one): each /*autodef*/ marker is regenerated from its
    OWN module's text only — a single buffer-wide scan would stop at the
    first ``endmodule`` and leave every later module's region empty (and
    would leak the sibling module's ports/signals into the tables)."""
    modules = modules or {}
    from .inst import set_typedef_regexp
    from .libdirs import parse_typedef_regexp
    from .wire import parse_param_value

    set_typedef_regexp(parse_typedef_regexp(lines))
    set_param_value(parse_param_value(lines))
    lines = kill_auto_def_t(lines)
    from .inst import _expand_ansi_header, map_module_spans, module_spans

    # expand each module's single-line ANSI header in place (the shared
    # helper rewrites only the FIRST header of the text it is given, so it
    # must run once per module span)
    expanded = list(lines)
    for start, end in reversed(module_spans(expanded)):
        span = expanded[start : end + 1]
        expanded[start : end + 1] = _comma_separated_header(
            _expand_ansi_header(list(span)), span
        )
    lines = expanded
    return map_module_spans(
        lines,
        _AUTODEF_MARK_FULL,
        lambda span: _auto_def_t_single(span, modules, lines),
    )


def _auto_def_t_single(
    lines: Sequence[str], modules: Mapping[str, ModuleDef], full: Sequence[str]
) -> list[str]:
    """ADT for ONE module span (FULL is the whole post-kill buffer).

    `` `define``/constant scans use FULL (macros and named constants are a
    compilation-unit resource — a `` `define``/`` `include`` above the
    module line still applies); everything else is scoped to the span."""
    alldefs = get_all_defs(full) | get_all_defs(lines)
    allparas = get_all_paras(full) | get_all_paras(lines)
    consts = _const_symbols(full)
    loop_ranges = _loop_ranges(lines, consts)
    loop_bounds = _loop_bounds(lines, consts)
    sym_hi = _loop_sym_hi(lines, consts)
    loop_decls = _loop_var_decls(lines)
    unresolved: dict[str, str] = {
        name: "no driver or declaration found"
        for name in get_all_signals(lines, alldefs, allparas)
    }
    from .inst import buffer_module_defs

    for excluded in (
        _loop_vars(lines),  # for-loop variables are never signals
        set(_const_symbols(full)),  # named constants are not signals
        _structure_names(lines, modules),  # genvar/labels/instances
        set(buffer_module_defs("\n".join(full))),  # the module's own name
    ):
        for name in excluded:
            unresolved.pop(name, None)
    fn_regions = _function_regions(lines)
    if fn_regions:
        # function/task scope: the callable's own name, plus every
        # identifier whose occurrences all live inside a function/task
        # body (its ports, locals and loop variables), are not module
        # signals and never unresolved at module level
        fn_names = _function_names(lines)
        for name in list(unresolved):
            if name in fn_names or _only_in_regions(name, lines, fn_regions):
                unresolved.pop(name, None)
    inst_headers = _instance_headers(lines, modules)
    orphan_idxs = _waived_orphan_lines(lines)

    # the #(...) overrides of every instance, keyed by marker line (and by
    # header line for marker-less instances): lets port widths referencing
    # the submodule's own parameters resolve to parent-visible names
    from . import emacs as _emacs

    _text = "\n".join(lines)
    _markers = _emacs.find_auto_markers(lines, "autoinst")
    _stacks = _emacs._scan_parens_at(_text, [m.offset for m in _markers])
    param_by_line: dict[int, dict[str, str]] = {}
    for _mk in _markers:
        _st = _stacks[_mk.offset]
        if _st:
            param_by_line[_text.count("\n", 0, _mk.offset)] = _emacs.read_inst_param_values(
                _text, _st[-1]
            )
    _line_off = [0]
    for _l in lines:
        _line_off.append(_line_off[-1] + len(_l) + 1)

    signals = SignalTable()
    signals.known = frozenset(set(allparas) | set(_const_symbols(full)))
    if _param_value_on():
        signals.consts = _const_symbols(full)
    from .inst import auto_arg_port_names

    signals.port_names = frozenset(auto_arg_port_names(lines))
    link_dict: dict[str, set[str]] = {}
    io_seq = 0
    usr_seq = 0
    i = 0
    n = len(lines)
    in_auto_region = False
    header_lines = _ansi_header_lines(lines)
    while i < n:
        # track AUTO-generated regions (AUTOWIRE/AUTOREG/AUTOINPUT/
        # AUTOOUTPUT): an io port declared there is complete by construction
        # (no supplementary body wire/reg may be emitted for it — a bare
        # AUTOINPUT v2k `input foo,` would otherwise get a duplicate
        # `wire foo;`), while the region's wire/reg declarations are read
        # like any other usrdef declaration (AUTOWIRE runs before ADT in the
        # pipeline; missing them would duplicate the wires)
        if re.match(r"^\s*// Beginning of automatic\b", lines[i]):
            in_auto_region = True
            i += 1
            continue
        if in_auto_region and "// End of automatics" in lines[i]:
            in_auto_region = False
            i += 1
            continue
        i = _skip_autodef_off(lines, i)
        if i >= n:
            break
        j = _skip_comment_line(lines, i)
        if j == -1:
            break
        if not in_auto_region:
            # the comment batch-skip may have jumped over a region header
            # hiding in the comment span (marker line + `// Beginning ...`
            # are both comment-only) — resume region tracking at the header
            region_start = next(
                (
                    k
                    for k in range(i, j)
                    if re.match(r"^\s*// Beginning of automatic\b", lines[k])
                ),
                None,
            )
            if region_start is not None:
                in_auto_region = True
        i = j
        if any(s <= i + 1 <= e for s, e in fn_regions):
            # function/task bodies are a separate scope: their ports,
            # locals and loop variables are not module signals
            i += 1
            continue
        line = _strip_line(lines[i])
        if _PORT_LINE.match(line) or _DATA_LINE.match(line) or _is_typedef_decl(line):
            # a declaration may span lines (name on the next line, etc.) and
            # carry /* ... */ comments: parse the joined, comment-stripped
            # statement but keep the original text so usrdef lines stay
            # verbatim on output.  Only join when the current line clearly
            # ended mid-declaration (after a packed range, nothing left but
            # a trailing ',') and the next line names the port; otherwise a
            # header port (no ';') must not consume its sibling declarations.
            stmt = _strip_inline_comment(line)
            k = i
            t = stmt.strip().rstrip(",").rstrip()
            # join only while the statement still has no name: a header port
            # (no ';' and no name yet) continues; a complete multi-name decl
            # line ending in ',' already carries its first name and must not
            # eat its sibling declarations.  Exception: a DATA declaration
            # whose line ends with ',' is an unfinished multi-name decl
            # (names continue on the next line: `wire a,\n b;`) — join it,
            # or the continuation names stay unregistered and are
            # re-declared (zipcpu duplicates); port lines keep the old
            # behaviour (each header port line is its own declaration)
            while (
                t.endswith("]")
                or (
                    _DATA_LINE.match(stmt)
                    and not _PORT_LINE.match(stmt)
                    and _strip_inline_comment(stmt).rstrip().endswith(",")
                )
            ) and ";" not in stmt and k + 1 < n:
                nxt = _strip_inline_comment(lines[k + 1])
                nxt_t = nxt.strip().rstrip(",").rstrip()
                if not nxt_t:
                    k += 1
                    stmt += " "
                    continue
                if (
                    re.match(r"^\s*\)", nxt)
                    or re.match(r"^\s*module\b", nxt)
                    or re.match(r"^\s*`", nxt)  # preprocessor line: never join over it
                    or _BLOCK_BREAK.search(nxt)
                    or _AUTO_CMD.search(nxt)
                    or _DATA_LINE.match(nxt)
                    or _PORT_LINE.match(nxt)
                ):
                    break  # a comment may carry a marker; never eat a statement
                k += 1
                stmt += " " + lines[k]
            stmt = _strip_inline_comment(stmt)
            if _PORT_LINE.match(stmt):
                io_seq = signals.extend_io_from_line(
                    stmt, io_seq, complete=in_auto_region or i in header_lines
                )
            elif _DATA_LINE.match(stmt) or _is_typedef_decl(stmt):
                if ";" in _strip_inline_comment(lines[k]):
                    usr_line = lines[k]  # the declaration ends on this line
                    anchor = k
                else:
                    # partial join (statement still unterminated): anchor at
                    # the FIRST line so emission never stamps the first
                    # line's text over a later continuation line
                    usr_line = line
                    anchor = i
                usr_seq = signals.extend_usrdef_from_line(stmt, usr_seq, usr_line, anchor, orphan_idxs)
            i = k + 1
            continue
        elif _ALWAYS_OPEN.match(line):
            # multi-line sensitivity list: always @( ... ): consume lines
            # through the ')' and classify the joined header
            header = line
            j = i
            while ")" not in header:
                j += 1
                if j >= n:
                    break
                header += " " + _strip_line(lines[j])
                if _BLOCK_BREAK.search(_strip_inline_comment(lines[j])):
                    break
            if re.search(r"\(\s*(posedge|negedge)\b", header):
                i = _scan_always_block(lines, j + 1, "<=", "freg", signals, link_dict, allparas, loop_ranges, loop_bounds, sym_hi)
            else:
                i = _scan_always_block(lines, j + 1, "=", "creg", signals, link_dict, allparas, loop_ranges, loop_bounds, sym_hi)
            continue
        elif _ALWAYS_SEQ.match(line):
            i = _scan_always_block(lines, i + 1, "<=", "freg", signals, link_dict, allparas, loop_ranges, loop_bounds, sym_hi)
            continue
        elif _ALWAYS.match(line):
            i = _scan_always_block(lines, i + 1, "=", "creg", signals, link_dict, allparas, loop_ranges, loop_bounds, sym_hi)
            continue
        elif _ASSIGN.match(line):
            joined, next_i = _join_statement(lines, i, "=")
            line = re.sub(r"^\s*assign\s*", "", joined)
            line = re.sub(r"^\s*#`?\w+\s*", "", line)
            if line.lstrip().startswith("{"):
                sides = _split_concat_assign(line, "=")
                if sides is not None:
                    for side in get_concat_assign_sides(
                        *sides, loop_ranges, loop_bounds, sym_hi
                    ):
                        signals.extend_from_side(side, "wire")
                        if side.link is not None:
                            update_link_dict(link_dict, allparas, side.name, side.link)
                    i = next_i
                    continue
            elif next_i != i + 1:
                # the statement spans lines with the concat on the RHS
                # (``assign x = {a,\n b};``): the joined text is still a
                # valid assignment — classify it instead of dropping it
                sides = _split_assign(line, "=")
                if sides:
                    side = get_assign_side(*sides, loop_ranges, loop_bounds, sym_hi)
                    signals.extend_from_side(side, "wire")
                    if side is not None and side.link is not None:
                        update_link_dict(link_dict, allparas, side.name, side.link)
                i = next_i
                continue
            sides = _split_assign(line, "=")
            if sides:
                side = get_assign_side(*sides, loop_ranges, loop_bounds, sym_hi)
                signals.extend_from_side(side, "wire")
                if side is not None and side.link is not None:
                    update_link_dict(link_dict, allparas, side.name, side.link)
        elif _AUTOINST_MARK.search(line):
            module = ""
            try:
                module, _ = VerilogBuffer(lines).resolve_instance(i)
            except ValueError:
                m = re.match(r"\s*(\w+)", line)
                module = m.group(1) if m else ""
            moddef = modules.get(module)
            if moddef is not None:
                inst_io = {p.name: p for p in moddef.ports}
                i = _scan_inst_body(
                    lines, i + 1, inst_io, signals, loop_bounds, sym_hi,
                    _emacs.effective_param_values(moddef, param_by_line.get(i)),
                )
                continue
        elif i in inst_headers:
            # marker-less instantiation (module resolved in MODULES): scan
            # its named .port(net) connections for output/inout nets too
            module, _inst, hdr_end = inst_headers[i]
            moddef = modules[module]
            inst_io = {p.name: p for p in moddef.ports}
            if hdr_end == -2:
                # pins begin on the header line itself (single-line
                # instance): scan the balanced pin-list text, with the
                # same default+override parameter bindings as the
                # line-oriented path
                try:
                    pin_off = _line_off[i] + lines[i].index(
                        "(", lines[i].index(_inst) + len(_inst)
                    )
                    pvals = _emacs.effective_param_values(
                        moddef, _emacs.read_inst_param_values(_text, pin_off)
                    )
                except ValueError:
                    pvals = _emacs.param_default_values(moddef)
                i = _scan_inst_body_text(
                    lines, i, inst_io, signals, loop_bounds, sym_hi, pvals
                )
                continue
            start = (hdr_end + 1) if hdr_end >= 0 else (i + 1)
            # the pin-list open paren: last '(' of the header's final line
            hl = hdr_end if hdr_end >= 0 else i
            pin_off = _line_off[hl] + lines[hl].rfind("(")
            pvals = _emacs.effective_param_values(
                moddef,
                _emacs.read_inst_param_values(_text, pin_off) if pin_off >= 0 else {},
            )
            i = _scan_inst_body(lines, start, inst_io, signals, loop_bounds, sym_hi, pvals)
            continue
        elif _ENDMODULE.match(line):
            break
        i += 1

    groups = group_link_dict(link_dict)
    for para in allparas:
        signals.discard(para)
    for var in _loop_vars(lines):
        signals.discard(var)  # loop variables are never declared signals
    update_define(unresolved, groups, signals)
    # any signal whose symbolic width references identifiers not visible in
    # THIS module (a submodule's own parameter names, or — worse — another
    # SIGNAL mistaken for a constant msb like reg[chn_sel_idx:0]) would not
    # compile: mark it unresolved instead of emitting a broken declaration
    known_syms = set(allparas) | set(_const_symbols(full))
    for name, sig in list(signals.signals.items()):
        if sig.type == "usrdef":
            continue
        if sig.type == "inst_wire":
            # driven by an instance output/inout pin: its width IS the
            # driver's dimension (submodule parameters/`defines already
            # substituted as far as they resolve).  Declare with that
            # dimension even when the remainder still names symbols the
            # parent does not define — dropping the net would lose the
            # one authoritative width it has.
            continue
        if sig.packed_dims:
            dims_text = "".join(f"[{d}]" for d in sig.packed_dims)
            misses = sorted(
                {s for d in sig.packed_dims for s in _width_unknown_syms(d, known_syms)}
            )
            if misses:
                del signals.signals[name]
                unresolved[name] = (
                    f"packed dims '{dims_text}' reference symbol(s) not visible "
                    f"in this module: {', '.join(misses)}"
                )
            continue
        if sig.width in ("", "c0"):
            continue
        misses = _width_unknown_syms(sig.width, known_syms)
        if misses:
            del signals.signals[name]
            unresolved[name] = (
                f"width '{sig.width}' references symbol(s) not visible "
                f"in this module: {', '.join(misses)}"
            )
    div = div_signals(signals)

    # index usrdef declarations by their buffer line so an in-place width fix
    # (a provably stale hand-written width, grown from its driver) replaces
    # the original line at emission time.
    usrdef_lines: dict[int, Signal] = {}
    for sig in signals.signals.values():
        if sig.type == "usrdef" and sig.line_idx >= 0:
            usrdef_lines[sig.line_idx] = sig
    # waived orphan lines the instance evidence re-derived identically:
    # they are emitted inside the region instead
    drop_idxs = {s.line_idx for s in signals.signals.values() if s.drop_line and s.line_idx >= 0}

    out: list[str] = []
    for idx, line in enumerate(lines):
        if _AUTODEF_MARK_FULL.match(line):
            out.extend(_emit_sections(line, div, unresolved, loop_decls))
        elif idx in usrdef_lines:
            out.append(usrdef_lines[idx].line)  # possibly width-updated decl
        elif idx in drop_idxs:
            continue
        else:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.inst: Vim calls it the same way)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.autodef",
        description="automatic.vim AutoDefT (ADT) rewrite",
    )
    parser.add_argument("command", choices=["adt", "kill"], help="adt: AutoDefT; kill: KillAutoDefT")
    parser.add_argument("-i", "--in_file", required=True, help="buffer file with /*autodef*/ markers")
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="field-debug decision log on stderr (same as VERILOG_TOOLING_DEBUG=1)",
    )
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    from .inst import _resolve_module_files, _cli_resolve, buffer_module_defs, _module_lines, find_interfaces

    args = create_by_args(argv)
    if getattr(args, "debug", False):
        import os

        os.environ["VERILOG_TOOLING_DEBUG"] = "1"
    from .libdirs import _dbg

    import os

    _dbg(
        f"{args.command}: cwd={os.getcwd()} in={args.in_file} "
        f"ref={args.ref_file or args.in_file} out={args.out_file} libdir={args.libdir}"
    )
    lines = Path(args.in_file).read_text().splitlines()
    if args.command == "kill":
        out = kill_auto_def_t(lines)
    else:
        buf = VerilogBuffer(lines)
        modules = {}
        names = set()
        for idx in buf.markers():
            try:
                names.add(buf.resolve_instance(idx)[0])
            except ValueError:
                pass
        # marker-less instances can only be resolved by name; add every
        # candidate header (including multi-line ``module #(`` forms)
        names |= _candidate_module_names(lines)
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
        out = auto_def_t(lines, modules)
    from .libdirs import _dbg

    _n_unres = sum(1 for ln in out if ln.lstrip().startswith("// unresolved:"))
    _dbg(f"{args.command}: unresolved entries in output: {_n_unres}")
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
