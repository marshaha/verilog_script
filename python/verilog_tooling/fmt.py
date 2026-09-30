"""Python rewrite of automatic.vim's formatting command family.

Covers the buffer-wide alignment commands bound in ~/.vimrc:

- ``auto_inst_format``   <- AutoInstFormat  (AIF, automatic.vim 3745-3903)
- ``auto_port_format``   <- AutoPortFormat  (APF, automatic.vim 7451-7526)
- ``auto_define_format`` <- AutoDefineFormat (ADF, automatic.vim 7341-7392)
- ``auto_define_len``    <- AutoDefineLen   (helper, automatic.vim 7394-7441)
- ``all_format``         <- AllFormat       (AF, automatic.vim 7750-7755)

Each command is a pure function (lines in -> lines out) and a
:class:`~verilog_tooling.inst.VerilogBuffer` method; the module-level
functions are thin wrappers, mirroring :mod:`verilog_tooling.inst`.
Importing this module attaches the three methods to ``VerilogBuffer``.

Intentional fixes over the Vim originals (output-identical otherwise):

- AIF pass 2 drops the stale-variable quirk: the Vim loop updates the
  maxima on every line from variables only refreshed on matching lines;
  here only matching lines contribute, which yields the same maxima.
- AIF pass 3 strips the full ``));`` for the ``);`` terminator (the Vim
  original leaves the ``;`` in the connection text).

Alignment uses :func:`verilog_tooling.inst._cal_margin` (the strict ``>``
variant of automatic.vim's CalMargin) and the 8-space ``t:vlog_inst_margin``.
"""

from __future__ import annotations

import re
from typing import Sequence

from .inst import VerilogBuffer, _cal_margin, block_comment_mask

_INST_MARGIN = " " * 8  # t:vlog_inst_margin

_LINE_COMMENT = re.compile(r"^\s*//")
_AUTOINST_MARK = re.compile(r"\(\s*/\*\b(?:autoinst|AUTOINST)\b\*/")
_INST_PIN = re.compile(r"^\s*\..*[^\\]\(.*\)\s*.*$")
_PORT_DECL = re.compile(r"^\s*(input|output|inout)\b")
_DEFINE_DECL = re.compile(r"^\s*(wire|reg|logic|integer|genvar)\b")
_WIDTH = re.compile(r"\[.*\]\s*[A-Za-z]")

_DEFINE_BONUS = {"wire": 3, "reg": 4, "integer": 0, "genvar": 1}


def _extract_width(text: str) -> str:
    """automatic.vim width extraction: ``[N:0]`` from ``[N:0] <letter>``."""
    if "[" not in text:
        return ""
    m = _WIDTH.search(text)
    if not m:
        return ""
    return re.sub(r"\]\s*[A-Za-z]", "]", m.group(0))


def _inst_pin_parts(line: str) -> tuple[str, str, str, str] | None:
    """Split a pin line into (port, connection, terminator, comment).

    Returns None when the line is not an instantiation pin connection.
    A line carrying a ``/*`` comment (instance headers like
    ``#(...)) inst (/*autoinst*/``, inline ``/* */`` notes) is never a
    pin — treating it as one would mangle the comment into the
    "connection".
    Terminator is ``')'`` for ``))``, ``');'`` for ``));``, ``''`` for a
    bare ``)`` and ``','`` otherwise.
    """
    if not _INST_PIN.match(line) or _LINE_COMMENT.match(line) or "/*" in line:
        return None
    cm = re.search(r"//.*", line)
    comment = cm.group(0) if cm else ""
    body = re.sub(r"//.*", "", line)
    body = re.sub(r"(^\s*\.\w+)\(", r"\1 (", body)
    pm = re.match(r"^\s*\.\S*", body)
    port = re.sub(r"[\s.]", "", pm.group(0))
    conn = re.sub(r"^\s*\.\S*\s*\(", "", body)
    if re.search(r"\)\s*\)\s*;\s*$", conn):
        terminator = ");"
        conn = re.sub(r"\)\s*\)\s*;\s*$", "", conn)
    elif re.search(r"\)\s*\)\s*$", conn):
        terminator = ")"
        conn = re.sub(r"\)\s*\)\s*$", "", conn)
    elif re.search(r"\)\s*$", conn):
        terminator = ""
        conn = re.sub(r"\)\s*$", "", conn)
    else:
        terminator = ","
        conn = re.sub(r"\)\s*,\s*$", "", conn)
    return port, re.sub(r"\s", "", conn), terminator, comment


def auto_define_len(lines: Sequence[str]) -> int:
    """Max width-bucket length over wire/reg/integer/genvar lines (0 if none).

    automatic.vim AutoDefineLen: ``len(width)`` plus a per-type bonus
    (wire +3, reg +4, integer +0, genvar +1, else +1).
    """
    max_len = 0
    for line in lines:
        m = _DEFINE_DECL.match(line)
        if not m:
            continue
        bonus = _DEFINE_BONUS.get(m.group(1), 1)
        max_len = max(max_len, len(_extract_width(line)) + bonus)
    return max_len


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached below)


def _auto_inst_format(self: VerilogBuffer) -> VerilogBuffer:
    """AIF: explode one-liner instances, then re-align every pin line
    (EAI-sectioned instances included — their pins/parens are reformatted
    to AIF style like any other instance)."""
    lines = self.modify_emacs_inst_format().lines
    comment_mask = block_comment_mask(lines)

    # pass 1: explode ``... (/*autoinst*/ .a(x), .b(y));`` one-liners
    out: list[str] = []
    for li, line in enumerate(lines):
        if comment_mask[li] or not _AUTOINST_MARK.search(line) or _LINE_COMMENT.match(line):
            out.append(line)
            continue
        m = re.search(r"\b(?:autoinst|AUTOINST)\b\*/", line)
        out.append(line[: m.end()])
        rest = line[m.end() :]
        end_flag = False
        if re.search(r"\)\s*;", rest):
            end_flag = True
            rest = re.sub(r"\)\s*;", "", rest)
        dm = re.search(r"\..*$", rest)
        rest = dm.group(0) if dm else ""
        rest = re.sub(r"//.*", "", rest)
        rest = re.sub(r"\s*", "", rest)
        tokens = rest.replace(",", " ").split()
        for token in tokens:
            out.append(token + ",")
        if end_flag and tokens:
            out[-1] = re.sub(r"\),$", ") ", out[-1])
            out.append(_INST_MARGIN + ");")

    # pass 2: measure prefix/suffix maxima over pin lines
    comment_mask = block_comment_mask(out)
    prefix_max_len = 0
    suffix_max_len = 0
    for li, line in enumerate(out):
        if comment_mask[li]:
            continue
        parts = _inst_pin_parts(line)
        if parts is None:
            continue
        port, conn, _, _ = parts
        prefix_max_len = max(prefix_max_len, len(port))
        suffix_max_len = max(suffix_max_len, len(conn))
    prefix_max_len += 2
    suffix_max_len = 58 if suffix_max_len < 58 else suffix_max_len + 8

    # pass 3: re-emit aligned pin lines
    final: list[str] = []
    for li, line in enumerate(out):
        if comment_mask[li]:
            final.append(line)
            continue
        parts = _inst_pin_parts(line)
        if parts is None:
            final.append(line)
            continue
        port, conn, terminator, comment = parts
        final.append(
            _INST_MARGIN
            + "."
            + port
            + _cal_margin(prefix_max_len, len(port))
            + "("
            + conn
            + _cal_margin(suffix_max_len, len(conn))
            + ")"
            + terminator
            + comment
        )
    return VerilogBuffer(final)


def _auto_port_format(self: VerilogBuffer) -> VerilogBuffer:
    """APF: align input/output/inout port declarations."""
    max_len = auto_define_len(self._lines)
    comment_mask = block_comment_mask(self._lines)
    out: list[str] = []
    for li, line in enumerate(self._lines):
        m = None if comment_mask[li] else _PORT_DECL.match(line)
        if not m:
            out.append(line)
            continue
        line = re.sub(r"\)\s*;", ");", line)
        cm = re.search(r"//.*", line)
        comment = cm.group(0) if cm else ""
        # a trailing /* ... */ comment is comment text too — leaving it in
        # the body makes the name extraction fail (empty port name)
        if not comment:
            bcm = re.search(r"/\*.*?\*/\s*$", line)
            if bcm:
                comment = bcm.group(0).strip()
        body = re.sub(r"//.*", "", line)
        body = re.sub(r"/\*.*?\*/", " ", body)
        if re.search(r"\)\s*$", body):
            terminator = ");"
        else:
            tm = re.search(r"\w+\s*([;|,])\s*$", body)
            terminator = tm.group(1) if tm else ""
        width = _extract_width(body)
        body_ns = re.sub(r"\s*$", "", body)
        body_ns = re.sub(r"\)\s*;$", "", body_ns)
        body_ns = re.sub(r"[;|,]$", "", body_ns)
        # an optional reg/wire/logic after the direction must be kept
        # (output reg [DW-1:0] Q — dropping it turns the port into a wire)
        dir_m = re.match(r"\s*(?:input|output|inout)\s*", body_ns)
        rest_dir = body_ns[dir_m.end() :] if dir_m else body_ns
        reg_m = re.match(r"(reg|wire|logic)\s+", rest_dir)
        reg_kw = (reg_m.group(1) + " ") if reg_m else ""
        nm = re.search(r"\w+\s*$", body_ns)
        port_name = nm.group(0).strip() if nm else ""
        space_max = 20 + max_len + (1 if m.group(1) == "output" else 2)
        out.append(
            m.group(1)
            + (" " if reg_kw else "")
            + reg_kw
            + width
            + _cal_margin(space_max, len(reg_kw) + len(width) + (1 if reg_kw else 0))
            + port_name
            + terminator
            + comment
        )
    return VerilogBuffer(out)


def _auto_define_format(self: VerilogBuffer) -> VerilogBuffer:
    """ADF: align wire/reg/integer/genvar declarations."""
    max_len = auto_define_len(self._lines)
    comment_mask = block_comment_mask(self._lines)
    out: list[str] = []
    for li, line in enumerate(self._lines):
        m = None if comment_mask[li] else _DEFINE_DECL.match(line)
        if not m:
            out.append(line)
            continue
        port_type = m.group(1)
        cm = re.search(r";.*$", line)
        port_comment = cm.group(0) if cm else ""
        width = _extract_width(line)
        stripped = re.sub(r"^\s*", "", line)
        nm = re.search(r"(?:\s+|\]\s*)[A-Za-z].*;", stripped)
        name = nm.group(0) if nm else ""
        name = re.sub(r"//.*$", "", name)
        name = re.sub(r"\s", "", name)
        name = re.sub(r"^]", "", name)
        name = re.sub(r";", "", name)
        space_max = 20 + max_len + _DEFINE_BONUS.get(port_type, 1)
        out.append(
            port_type
            + width
            + _cal_margin(space_max, len(width))
            + name
            + port_comment
        )
    return VerilogBuffer(out)


VerilogBuffer.auto_inst_format = _auto_inst_format
VerilogBuffer.auto_port_format = _auto_port_format
VerilogBuffer.auto_define_format = _auto_define_format


# ---------------------------------------------------------------------------
# parameter / localparam declaration alignment


_PARAM_DECL = re.compile(
    r"^(\s*)(parameter|localparam)\s+(signed\s+)?((?:\[[^\]]*\]\s*)*)(\w+)\s*=\s*(.*?)\s*$"
)


def _auto_param_decl_format(self: VerilogBuffer) -> VerilogBuffer:
    """Align ``parameter``/``localparam`` declarations within each run of
    consecutive declarations (blank and comment lines do not break a run):
    names share one column and the ``=`` signs line up — ``localparam``
    being one character longer than ``parameter`` is absorbed by the
    keyword padding.  The value text after ``=`` is kept verbatim."""
    lines = self._lines
    n = len(lines)

    def decl(line: str):
        return _PARAM_DECL.match(line)

    def filler(line: str) -> bool:
        t = line.strip()
        return not t or t.startswith("//")

    out = list(lines)
    i = 0
    while i < n:
        if not decl(lines[i]):
            i += 1
            continue
        j = i
        last = i
        while j < n and (decl(lines[j]) or filler(lines[j])):
            if decl(lines[j]):
                last = j
            j += 1
        run = [k for k in range(i, last + 1) if decl(lines[k])]
        kw_max = max(len(decl(lines[k]).group(2)) for k in run)
        name_max = max(
            len(
                (decl(lines[k]).group(3) or "")
                + decl(lines[k]).group(4)
                + decl(lines[k]).group(5)
            )
            for k in run
        )
        for k in run:
            m = decl(lines[k])
            indent, kw, signed, rng, name, value = m.groups()
            field = (signed or "") + rng + name
            out[k] = (
                indent
                + kw
                + " " * (kw_max + 2 - len(kw))
                + field
                + " " * (name_max - len(field))
                + " = "
                + value
            )
        i = last + 1
    return VerilogBuffer(out)


VerilogBuffer._auto_param_decl_format = _auto_param_decl_format


# ---------------------------------------------------------------------------
# module-level wrappers (thin VerilogBuffer delegates)


def auto_inst_format(lines: Sequence[str]) -> list[str]:
    """AIF: explode one-liner instances, then re-align every pin line."""
    return VerilogBuffer(lines).auto_inst_format().lines


def auto_port_format(lines: Sequence[str]) -> list[str]:
    """APF: align input/output/inout port declarations."""
    return VerilogBuffer(lines).auto_port_format().lines


def auto_define_format(lines: Sequence[str]) -> list[str]:
    """ADF: align wire/reg/integer/genvar declarations."""
    return VerilogBuffer(lines).auto_define_format().lines


def auto_param_decl_format(lines: Sequence[str]) -> list[str]:
    """Align parameter/localparam declarations (name and ``=`` columns)."""
    return VerilogBuffer(lines)._auto_param_decl_format().lines


def all_format(lines: Sequence[str]) -> list[str]:
    """AF: AutoPortFormat then AutoDefineFormat then parameter/localparam
    declarations then AutoInstFormat."""
    lines = auto_port_format(lines)
    lines = auto_define_format(lines)
    lines = auto_param_decl_format(lines)
    return auto_inst_format(lines)
