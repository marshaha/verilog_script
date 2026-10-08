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
_AUTO_MARK = re.compile(r"/\*\s*\bauto\w+\b", re.IGNORECASE)  # any AUTO marker comment
_INST_PIN = re.compile(r"^\s*\..*[^\\]\(.*\)\s*.*$")
_PORT_DECL = re.compile(r"^\s*(input|output|inout)\b")
_DEFINE_DECL = re.compile(r"^\s*(wire|reg|logic|integer|genvar)\b")
_WIDTH = re.compile(r"\[.*\]\s*[A-Za-z]")

_DEFINE_BONUS = {"wire": 3, "reg": 4, "integer": 0, "genvar": 1}
_INIT_ASSIGN_RE = re.compile(r"(?<![<>=!])=(?![=])")


def _top_level_comma(text: str) -> bool:
    """True when `text` holds a comma outside any []/() nesting (a
    multi-name declaration such as ``input clk, wen,``)."""
    depth = 0
    for c in text:
        if c in "[(":
            depth += 1
        elif c in "])":
            depth -= 1
        elif c == "," and depth == 0:
            return True
    return False


def _extract_width(text: str) -> str:
    """automatic.vim width extraction: ``[N:0]`` from ``[N:0] <letter>``."""
    if "[" not in text:
        return ""
    m = _WIDTH.search(text)
    if not m:
        return ""
    return re.sub(r"\]\s*[A-Za-z]", "]", m.group(0))


def _inst_pin_parts(line: str) -> tuple[str, str, str, str, str] | None:
    """Split a pin line into (port, connection, terminator, comment,
    header_tail).

    Returns None when the line is not an instantiation pin connection.
    A line carrying an AUTO marker comment is an instance HEADER, not a
    pin — except the last-param line of an AUTOINSTPARAM expansion
    (``#(...)) inst (/*autoinst*/``): its pin part is aligned like its
    siblings and the instance header (``inst (/*autoinst*/``) is returned
    separately as header_tail, to be emitted on its own line.
    Terminator is ``')'`` for ``))``, ``');'`` for ``));``, ``''`` for a
    bare ``)`` and ``','`` otherwise.
    """
    if not _INST_PIN.match(line) or _LINE_COMMENT.match(line):
        return None
    header_tail = ""
    if _AUTO_MARK.search(line):
        # try the `#(...)) inst (/*autoinst*/` last-param shape: find the
        # pin connection's balanced close, then require `)` + instance
        # name + marker; anything else with a marker is left untouched
        pm = re.match(r"^\s*\.\w+\s*\(", line)
        if not pm:
            return None
        depth = 0
        j = pm.end() - 1
        while j < len(line):
            if line[j] == "(":
                depth += 1
            elif line[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(line):
            return None
        tm = re.match(r"^\s*\)\s*(\w+\s*\(\s*/\*.*)$", line[j + 1 :])
        if not tm:
            return None
        header_tail = tm.group(1)  # `inst (/*autoinst*/...`
        line = line[: j + 1]  # parse the pin part normally below
    cm = re.search(r"//.*", line)
    comment = cm.group(0) if cm else ""
    body = re.sub(r"//.*", "", line)
    # A connection value that continues on following lines (net paren
    # depth ends POSITIVE, e.g. a multi-line ?: in a #(...) parameter
    # override) is not a complete pin connection: emitting
    # `.NAME (value-so-far),` truncates it and strands the remaining
    # lines as syntax errors.  Leave the line untouched.  Negative
    # depth is fine: the line closes an enclosing group, as in the
    # parameter override terminator `.W(16))`.
    depth_src = re.sub(r'"(?:[^"\\]|\\.)*"', '""', body)
    depth_src = re.sub(r"/\*.*?\*/", "", depth_src)
    depth = 0
    for ch in depth_src:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    if depth > 0:
        return None
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
    return port, re.sub(r"\s", "", conn), terminator, comment, header_tail


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
        port, conn, _, _, _ = parts
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
        port, conn, terminator, comment, header_tail = parts
        pin_line = (
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
        if header_tail:
            # the `#(` close stays with the last param; the instance header
            # gets its own line at the instance opener's indent
            final.append(pin_line + ")")
            indent = ""
            for prev in reversed(final):
                hm = re.match(r"^(\s*)\w+\s*#\s*\(", prev)
                if hm:
                    indent = hm.group(1)
                    break
            final.append(indent + header_tail)
        else:
            final.append(pin_line)
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
        if re.search(r"\)\s*;?\s*$", body):
            terminator = ");"
        else:
            # the token before the terminator may be `]` when the port
            # carries an unpacked dimension (``arr_i [2],``): an
            # end-anchored ``\w+`` misses it and drops the punctuation.
            tm = re.search(r"[\w\]]\s*([;|,])\s*$", body)
            terminator = tm.group(1) if tm else ""
        width = _extract_width(body)
        body_ns = re.sub(r"\s*$", "", body)
        body_ns = re.sub(r"\)\s*;$", "", body_ns)
        body_ns = re.sub(r"[;|,]$", "", body_ns)
        # A multi-name declaration (``input clk, wen,``) names several
        # ports on one line; the single-name rebuild below would keep
        # only the last one.  Leave such lines untouched.
        if _top_level_comma(body_ns):
            out.append(line)
            continue
        # A port declaration with an initializer (``output logic [W-1:0]
        # cx = START_X,``) is not a plain declaration: the name
        # extraction below assumes the name is the last token and would
        # rebuild the port as ``START_X``, dropping the real name and
        # the default value.  Leave it untouched (ADF already guards
        # the same shape for wire/reg declarations).
        if _INIT_ASSIGN_RE.search(body):
            out.append(line)
            continue
        # an optional reg/wire/logic after the direction must be kept
        # (output reg [DW-1:0] Q — dropping it turns the port into a wire)
        dir_m = re.match(r"\s*(?:input|output|inout)\s*", body_ns)
        rest_dir = body_ns[dir_m.end() :] if dir_m else body_ns
        reg_m = re.match(r"(reg|wire|logic)\s+", rest_dir)
        kw = (reg_m.group(1) + " ") if reg_m else ""
        if not kw:
            # a user data type (or signed/unsigned) may sit where the net
            # type would: ``input ibex_mubi_t fetch_enable_i``.  Dropping
            # it silently turns the port into an implicit 1-bit net.
            tm = re.match(
                r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)?)\s+(?:\[[^\]]*\]\s*)*\w+\s*$",
                rest_dir,
            )
            if tm:
                kw = tm.group(1) + " "
        # the name is the identifier at the end of the declaration,
        # optionally followed by unpacked dimensions (``arr_i [2]``):
        # the dims are kept with the name — end-anchored ``\w+\s*$``
        # alone loses the name entirely when a dim is present.
        nm = re.search(r"(\w+)((?:\s*\[[^\]]*\])*)\s*$", body_ns)
        if nm:
            port_name = nm.group(1) + re.sub(r"\s+", " ", nm.group(2))
        else:
            nm2 = re.search(r"\w+\s*$", body_ns)
            port_name = nm2.group(0).strip() if nm2 else ""
        space_max = 20 + max_len + (1 if m.group(1) == "output" else 2)
        out.append(
            m.group(1)
            + (" " if kw else "")
            + kw
            + width
            + _cal_margin(space_max, len(kw) + len(width) + (1 if kw else 0))
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
        # A declaration carrying an initializer (net declaration
        # assignment, e.g. ``wire x = a && b;``) or one whose statement
        # continues on a following line (no ';' here) is not a plain
        # declaration: the name extraction below assumes ``name;`` and
        # would silently drop the rest of the statement.  Leave it.
        if ";" not in line or _INIT_ASSIGN_RE.search(line.split(";", 1)[0]):
            out.append(line)
            continue
        port_type = m.group(1)
        cm = re.search(r";.*$", line)
        port_comment = cm.group(0) if cm else ""
        width = _extract_width(line)
        stripped = re.sub(r"^\s*", "", line)
        # The name follows the keyword, an optional ``signed`` and the
        # packed range groups.  Anchor there: the old
        # ``(?:\s+|\]\s*)[A-Za-z].*;`` search matched the first identifier
        # INSIDE a width expression (``[(CL_S_COUNT > 0? CL_S_COUNT-1:
        # 0):0]`` from verilog-axi) and emitted it as the name.
        rest = stripped[len(port_type):]
        while True:
            rest = rest.lstrip()
            if re.match(r"signed\b", rest):
                rest = rest[len("signed"):]
                continue
            if rest.startswith("["):
                depth = 0
                k = 0
                closed = False
                while k < len(rest):
                    if rest[k] == "[":
                        depth += 1
                    elif rest[k] == "]":
                        depth -= 1
                        if depth == 0:
                            closed = True
                            k += 1
                            break
                    k += 1
                if not closed:  # unbalanced range: not a plain decl
                    rest = ""
                    break
                rest = rest[k:]
                continue
            break
        if not re.match(r"[A-Za-z_]", rest):
            out.append(line)
            continue
        name = rest.split(";", 1)[0]
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
