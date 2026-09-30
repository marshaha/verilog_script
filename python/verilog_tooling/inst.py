"""Python rewrite of automatic.vim's instance commands.

Covers the instance-handling family bound in ~/.vimrc:

- ``kill_auto_inst``          <- KillAutoInst  (collapse instance to a stub)
- ``auto_inst``               <- AutoInst      (AIT: full regenerate)
- ``auto_inst_update``        <- AutoInstUpdate      (AIU1: minimal diff update)
- ``auto_inst_update_order``  <- AutoInstUpdateOrder (AIU:  update + reorder)

On top of the Vim originals, ``auto_inst`` (and optionally the update
commands) apply verilog-mode AUTO_TEMPLATE rules found in the buffer —
including the regexp port patterns — via :mod:`verilog_tooling.template`.

The buffer-facing commands live on :class:`VerilogBuffer`; the module-level
functions of the same name are thin wrappers kept for backward
compatibility (and for the CLI below).

Formatting follows automatic.vim exactly:

- prefix/suffix alignment floors are 26 and 30+12 columns;
- a vector connection is ``name[width]`` with the declared packed range;
- the last module port ends with ``)  // dir``, others with ``), // dir``;
- ``input``/``inout`` are padded to 6 chars so the direction comments align.

Intentional deviations from the Vim originals (all documented in README):

- the autoinst marker match is case-insensitive (verilog-mode writes
  ``/*AUTOINST*/``; the Vim main commands only match lowercase);
- ``auto_inst_update`` terminates a newly added last port with ``)`` instead
  of emitting a dangling ``),`` before ``);`` (a Vim bug);
- ``AutoInstFormat`` (AIF) is ported in :mod:`verilog_tooling.fmt`, not here;
  ``auto_inst_update_order`` still keeps the original alignment of reused
  lines instead of re-aligning the buffer.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence, Union

from .comments import strip_comments as _strip_c, strip_line_comments as _strip_lc
from .libdirs import resolve_libdirs
from .template import (
    AutoTemplate,
    _auto_re_to_python,
    find_auto_templates,
    template_at_value,
    template_connection,
    template_for_module,
)

_AUTOINST_MARK = re.compile(r"/\*\s*\bautoinst\b", re.IGNORECASE)
_STUB_LINE = re.compile(r"/\*\s*\bautoinst\b\s*\*/\s*\)\s*;", re.IGNORECASE)
_LINE_COMMENT = re.compile(r"^\s*//")
_DIRECTIVE = re.compile(r"^\s*`(?:if|ifdef|ifndef|elsif|else|endif)\b")
_BLANK = re.compile(r"^\s*$")
_PORT_KEYWORD = re.compile(r"^\s*(input|output|inout)\b\s*")
_IFACE_PORT_DECL = re.compile(r"^\s*(\w+)(?:\.(\w+))?\s+(\w+)\s*[,;]?\s*$")
_IFACE_HEADER = re.compile(r"\binterface\s+(\w+)\s*(?:#\s*\(|\(|;)")
_BREAK_LINE = re.compile(r"^\s*(?:always|endmodule|wire)\b")
_PORT_NAME = re.compile(r"(\w+)\s*\(")
_INST_END = re.compile(r"\)\s*;")

_PREFIX_MAX_DEFAULT = 26
_SUFFIX_MAX_DEFAULT = 30 + 12


# ---------------------------------------------------------------------------
# module definition parsing (s:GetSeqIO / s:ExtendIoFromLine2)


@dataclass(frozen=True)
class Port:
    """One module port. ``width`` is the single packed-range text ('7:0',
    'W-1:0') for a simple vector, None for a scalar port (the Vim 'c0'
    sentinel). Multidimensional ports additionally carry ``packed`` (all
    packed ranges) and ``unpacked`` (array dimensions) so verilog-mode can
    emit its ``name/*[p].[u]*/`` connection comment."""

    name: str
    direction: str  # 'input' | 'output' | 'inout' | 'interface'
    width: str | None = None
    packed: tuple[str, ...] = ()
    unpacked: tuple[str, ...] = ()
    # SystemVerilog interface port (``cpu_bus.master bus``): ``iface`` is the
    # interface type, ``modport`` the optional modport.  The default
    # instantiation connection is ``name.modport`` (or just ``name``), the
    # verilog-mode convention of a same-named interface in the parent.
    is_interface: bool = False
    modport: str | None = None
    iface: str | None = None

    def __post_init__(self):
        # positional construction Port(name, dir, width) implies one packed dim
        if self.width is not None and not self.packed and not self.unpacked:
            object.__setattr__(self, "packed", (self.width,))

    @property
    def is_multidim(self) -> bool:
        return len(self.packed) > 1 or bool(self.unpacked)


@dataclass(frozen=True)
class Param:
    """One module parameter; ``value`` is the raw default-value text."""

    name: str
    value: str | None = None


@dataclass(frozen=True)
class Keep:
    """A `ifdef/comment/blank line kept verbatim inside the port list."""

    line: str


Entry = Union[Port, Keep]


@dataclass(frozen=True)
class ModuleDef:
    name: str
    entries: tuple[Entry, ...]
    params: tuple[Param, ...] = ()

    @property
    def ports(self) -> list[Port]:
        return [e for e in self.entries if isinstance(e, Port)]

    def port_names(self) -> list[str]:
        return [p.name for p in self.ports]

    def last_port_name(self) -> str | None:
        ports = self.ports
        return ports[-1].name if ports else None


# ---------------------------------------------------------------------------
# interface definition parsing (for interface-typed module ports)


@dataclass(frozen=True)
class InterfaceDef:
    """An ``interface name; ... endinterface`` block: the interface name and
    its ``modport`` names (modport signal lists are not needed for AUTOINST)."""

    name: str
    modports: tuple[str, ...] = ()


def parse_interface(lines: Iterable[str]) -> InterfaceDef:
    """Parse an interface definition into an :class:`InterfaceDef`.

    Raises ValueError when no ``interface <name>`` header is found."""
    text = "\n".join(lines)
    text = _strip_c(text)
    m = _IFACE_HEADER.search(text)
    if not m:
        raise ValueError("no interface declaration found")
    return InterfaceDef(name=m.group(1), modports=tuple(re.findall(r"\bmodport\s+(\w+)", text)))


def find_interfaces(libdirs: Sequence[str]) -> dict[str, Path]:
    """Map interface names to the library file declaring them: every ``.v`` /
    ``.sv`` file in LIBDIRS is scanned for ``interface <name>`` headers."""
    found: dict[str, Path] = {}
    for d in libdirs:
        for path in sorted(Path(d).glob("*.v")) + sorted(Path(d).glob("*.sv")):
            try:
                text = path.read_text()
            except OSError:
                continue
            for m in re.finditer(r"^\s*interface\s+(\w+)", text, re.M):
                found.setdefault(m.group(1), path)
    return found


def _port_continues(line: str) -> bool:
    """Whether LINE is an incomplete port declaration needing the next line:
    the (only) remaining content is a trailing `,` after a range."""
    t = _strip_lc(line).strip().rstrip(",").rstrip()
    return t.endswith("]")


_TYPEDEF_REGEXP: "re.Pattern[str] | None" = None


def set_typedef_regexp(regexp: str | None) -> None:
    """Set the verilog-typedef-regexp used when parsing port lines: a first
    word matching it is a TYPE (e.g. reqcmd_t), and the port name follows.
    Both Emacs (``\\(...\\)``/``\\|``) and Python (``(...)``/``|``) regexp
    dialects are accepted (auto-detected)."""
    global _TYPEDEF_REGEXP
    _TYPEDEF_REGEXP = re.compile(_auto_re_to_python(regexp)) if regexp else None


def _parse_port_line(line: str) -> Port | None:
    # /* ... */ is comment text, not declaration text (incomplete pairs and
    # line comments are handled by the caller before this point)
    line = re.sub(r"/\*.*?\*/", " ", line)
    m = _PORT_KEYWORD.match(line)
    if not m:
        return None
    rest = line[m.end() :]
    rest = re.sub(r"^wire\b\s*", "", rest)  # `wire [7:0]` and `wire[7:0]` alike
    rest = re.sub(r"^reg\b\s*", "", rest)
    rest = re.sub(r"^signed\b\s*", "", rest)
    # collect all packed dimensions:  [3:0][7:0] name ...
    packed: list[str] = []
    while True:
        wm = re.match(r"^\[([^\]]+)\]\s*", rest)
        if not wm:
            break
        # normalise the range: whitespace is insignificant inside [...]
        packed.append(re.sub(r"\s+", "", wm.group(1)))
        rest = rest[wm.end() :]
    nm = re.match(r"\w+", rest)
    if not nm:
        return None
    name = nm.group(0)
    after = rest[nm.end() :]
    if _TYPEDEF_REGEXP is not None and _TYPEDEF_REGEXP.search(name):
        # a user type (reqcmd_t): the port name is the next word
        nm2 = re.match(r"\s*(\w+)", after)
        if not nm2:
            return None
        name = nm2.group(1)
        after = after[nm2.end() :]
    # unpacked dimensions follow the name:  name [0:3][0:2] ;
    unpacked: list[str] = []
    while True:
        um = re.match(r"^\s*\[([^\]]+)\]", after)
        if not um:
            break
        unpacked.append(re.sub(r"\s+", "", um.group(1)))
        after = after[um.end() :]
    width = packed[-1] if len(packed) == 1 and not unpacked else (
        packed[-1] if len(packed) == 1 else None
    )
    # simple single-packed no-unpacked keeps width for name[width] form
    if len(packed) == 1 and not unpacked:
        width = packed[0]
    return Port(
        name=name,
        direction=m.group(1),
        width=width,
        packed=tuple(packed),
        unpacked=tuple(unpacked),
    )


def _expand_ansi_header(lines: list[str], interfaces: Iterable[str] | None = None) -> list[str]:
    """Expand an ANSI single-line module header's port list into one
    declaration per line, so ``module t(input clk, input [3:0] d, output q);``
    is parsed like a multi-line header. Multi-line headers and lines that are
    not the module header are passed through unchanged."""
    text = "\n".join(lines)
    m = re.search(r"\bmodule\s+\w+", text)
    if not m:
        return lines
    # locate the port-list '(' after an optional #( ... ) parameter block
    pos = m.end()
    if re.match(r"\s*#\s*\(", text[pos:]):
        ph = text.index("(", pos)
        j = _balanced_close(text, ph)
        if j < 0:
            return lines
        pos = j + 1
    pm = re.match(r"\s*\(", text[pos:])
    if not pm:
        return lines
    open_paren = pos + text[pos:].index("(")
    close_paren = _balanced_close(text, open_paren)
    if close_paren < 0:
        return lines
    inner = text[open_paren + 1 : close_paren]
    # expand only when ports are declared inline (a direction keyword or a
    # known interface type present AND the header body sits on a single
    # logical line)
    if not re.search(r"\b(input|output|inout)\b", inner) and not (
        interfaces
        and any(
            (im := _IFACE_PORT_DECL.match(part.strip())) and im.group(1) in interfaces
            for part in _split_top_commas(inner)
        )
    ):
        return lines
    if "\n" in inner.strip():
        return lines  # already a multi-line header
    ports = _split_ansi_ports(inner, interfaces)
    if not ports:
        return lines
    # rebuild: keep everything up to '(', inject one port per line, then the
    # ')' and the rest (split so `);` and following code stay on their own lines)
    head = text[: open_paren + 1]
    tail = text[close_paren:]
    tail_lines = tail.split("\n")
    first_tail = tail_lines[0]  # typically ");"
    rest_lines = tail_lines[1:]
    new_lines = head.split("\n") + ports + [first_tail] + rest_lines
    return new_lines


def _balanced_close(text: str, open_idx: int) -> int:
    """Index of the ')' matching the '(' at open_idx, or -1."""
    depth = 0
    j = open_idx
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _split_top_commas(s: str) -> list[str]:
    """Split S at top-level commas (ignoring parens/brackets)."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _split_ansi_ports(inner: str, interfaces: Iterable[str] | None = None) -> list[str]:
    """Split an ANSI port-list body into per-port declaration strings."""
    # a port decl may start a new direction mid-list; bare names after a decl
    # share the previous direction — we only split when a direction keyword or
    # a range introduces a port. Keep it simple: each comma part that contains
    # a direction keyword or a [...] range is a decl boundary; bare-name parts
    # are dropped (they belong to the previous decl, one-name-per-line rule).
    # Interface-typed parts (``cpu_bus.master bus``) are kept when the type is
    # a known interface name.
    out = []
    for p in _split_top_commas(inner):
        p = p.strip()
        if re.search(r"\b(input|output|inout)\b", p):
            out.append(p)
        elif interfaces:
            im = _IFACE_PORT_DECL.match(p)
            if im and im.group(1) in interfaces:
                out.append(p)
    return out


def parse_module_ports(
    lines: Iterable[str],
    name: str = "",
    *,
    with_params: bool = False,
    interfaces: Iterable[str] | None = None,
    typedef_regexp: str | None = None,
) -> ModuleDef:
    """Parse a module-definition file into an ordered port/keep list.

    One port per line (first signal name only), like the Vim original.
    ANSI single-line headers are expanded first.  With ``with_params=True``
    the module's ``parameter`` declarations are collected too (shared with
    the verilog-mode AUTOINSTPARAM expansion).  When INTERFACES names known
    SystemVerilog interfaces, declaration lines like ``cpu_bus.master bus``
    or ``cpu_bus misc`` parse as interface ports (direction ``'interface'``,
    ``is_interface=True``, optional ``modport``).
    """
    set_typedef_regexp(typedef_regexp)  # None clears it for this parse
    interfaces = set(interfaces) if interfaces else None
    lines = _expand_ansi_header(list(lines), interfaces)
    entries: list[Entry] = []
    seen_module = False
    have_port = False
    mod_name = name
    in_block_comment = False
    n_lines = len(lines)
    for idx in range(n_lines):
        raw = lines[idx]
        line = raw.rstrip("\n")
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if re.match(r"^\s*/\*", line) and "*/" not in line:
            in_block_comment = True
            continue
        if not _LINE_COMMENT.match(line):
            line = _strip_lc(line)
        line = re.sub(r"input\s*logic", "input ", line)
        line = re.sub(r"output\s*logic", "output ", line)
        line = re.sub(r"inout\s*logic", "inout ", line)
        line = re.sub(r"^\s*,", "", line)
        if re.match(r"^\s*module\b", line):
            seen_module = True
            if not mod_name:
                m = re.search(r"\bmodule\s+(\w+)", line)
                if m:
                    mod_name = m.group(1)
        if not seen_module:
            continue
        if ");" in line and entries and not have_port:
            entries.clear()  # drop keep lines collected inside #( ... )
        if (
            _DIRECTIVE.match(line)
            or (_LINE_COMMENT.match(line) and not re.match(r"^\s*//\s*\{\{\{", line))
            or _BLANK.match(line)
        ):
            if (
                _BLANK.match(line)
                and entries
                and isinstance(entries[-1], Keep)
                and _BLANK.match(entries[-1].line)
            ):
                continue  # collapse consecutive blank keep lines
            entries.append(Keep(line))
            continue
        if _PORT_KEYWORD.match(line):
            joined = line
            j = idx
            while _parse_port_line(joined) is None and _port_continues(joined):
                j += 1
                if j >= n_lines:
                    break
                nxt = _strip_lc(lines[j]).strip()
                if _BREAK_LINE.match(nxt) or re.search(r"\bautodef\b", nxt) or "/*" in nxt:
                    break  # a /*...*/ may carry a marker; never eat a statement
                if _port_continues(nxt) and _parse_port_line(nxt) is None:
                    break  # another decl will finish there: leave it to them
                joined += " " + nxt
            port = _parse_port_line(joined)
            if port:
                entries.append(port)
                have_port = True
            continue
        if interfaces:
            im = _IFACE_PORT_DECL.match(line)
            if im and im.group(1) in interfaces:
                entries.append(
                    Port(
                        name=im.group(3),
                        direction="interface",
                        is_interface=True,
                        modport=im.group(2),
                        iface=im.group(1),
                    )
                )
                have_port = True
                continue
        if _BREAK_LINE.match(line) or re.search(r"\bautodef\b", line) or _AUTOINST_MARK.search(line):
            break
    while entries and isinstance(entries[-1], Keep) and _BLANK.match(entries[-1].line):
        entries.pop()
    start = 0
    while start < len(entries) and isinstance(entries[start], Keep) and _BLANK.match(entries[start].line):
        start += 1
    moddef = ModuleDef(name=mod_name, entries=tuple(entries[start:]))
    if with_params:
        from .emacs import parse_module_params  # lazy: shares the param parser

        return ModuleDef(
            name=moddef.name, entries=moddef.entries, params=parse_module_params(lines)
        )
    return moddef


# ---------------------------------------------------------------------------
# formatting helpers (s:CalMargin / s:GetPortMaxLen)
#
# These stay at module level: format_connection is public API and shares
# _cal_margin/_padded_dir.


def get_port_max_len(
    entries: Iterable[Entry],
    prefix_max_len: int = _PREFIX_MAX_DEFAULT,
    suffix_max_len: int = _SUFFIX_MAX_DEFAULT,
    *,
    sort: bool = False,
) -> tuple[int, int]:
    for entry in entries:
        if not isinstance(entry, Port):
            continue
        conn = entry.name
        if entry.is_interface and entry.modport:
            # identity connection text: name.modport
            conn += f".{entry.modport}"
        elif entry.width and not sort:
            # identity connection text: name[width]  (without sort the
            # automatic.vim width rule name[width:0] adds len(width)+4)
            conn += f"[{entry.width}]"
        conn_len = len(conn)
        prefix_max_len = max(prefix_max_len, len(entry.name))
        suffix_max_len = max(suffix_max_len, conn_len)
    return prefix_max_len, suffix_max_len


def _cal_margin(max_len: int, cur_len: int) -> str:
    # automatic.vim s:CalMargin: pad to max_len+1 only when cur_len < max_len
    return " " * (max_len - cur_len + 1) if max_len > cur_len else ""


def _padded_dir(direction: str) -> str:
    return direction + " " if direction in ("input", "inout") else direction


def default_connection(port: Port) -> str:
    """The no-template instantiation connection: ``name.modport`` for an
    interface port declared with a modport, ``name`` for a plain interface
    port, ``name[width]`` for a vector, ``name`` for a scalar."""
    if port.is_interface:
        return f"{port.name}.{port.modport}" if port.modport else port.name
    return port.name + (f"[{port.width}]" if port.width else "")


def format_connection(
    port: Port,
    conn: str | None,
    prefix_max_len: int,
    suffix_max_len: int,
    last: bool,
) -> str:
    """One ``    .port(conn)  // dir`` line, automatic.vim alignment."""
    if conn is None:
        conn = default_connection(port)
    line = "    ." + port.name + _cal_margin(prefix_max_len, len(port.name)) + "(" + conn
    line += _cal_margin(suffix_max_len, len(conn))
    line += ")  // " if last else "), // "
    return line + _padded_dir(port.direction)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# VerilogBuffer: buffer lines + marker/instance resolution + the commands


def block_comment_mask(lines: Sequence[str]) -> list[bool]:
    """Per-line mask: True when the line STARTS inside a ``/* ... */`` block
    comment (e.g. every body line of an AUTO_TEMPLATE block).  Such lines are
    comment text, not code — formatters (AIF/APF/ADF,
    modify_emacs_inst_format) must leave them untouched, or template entries
    like ``.apb_\\(.*\\) (cfg_\\1[][])`` get mangled by pin-line rewriting."""
    mask: list[bool] = []
    in_comment = False
    for line in lines:
        mask.append(in_comment)
        i = 0
        while True:
            if in_comment:
                j = line.find("*/", i)
                if j < 0:
                    break
                in_comment = False
                i = j + 2
            else:
                lc = line.find("//", i)
                j = line.find("/*", i)
                if j < 0 or (0 <= lc < j):
                    break
                k = line.find("*/", j + 2)
                if k < 0:
                    in_comment = True
                    break
                i = k + 2
    return mask


class VerilogBuffer:
    """A Verilog buffer (list of lines) with /*autoinst*/ marker support.

    The transformation commands (:meth:`kill_auto_inst`, :meth:`auto_inst`,
    :meth:`auto_inst_update`, :meth:`auto_inst_update_order`,
    :meth:`modify_emacs_inst_format`) are pure: each returns a new
    :class:`VerilogBuffer` and leaves ``self`` untouched.
    """

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)
        self._stripped: list[str] | None = None

    def _stripped_lines(self) -> list[str]:
        """Lines with ``//`` comments removed, computed once (the instance
        resolvers scan the buffer prefix for every marker)."""
        if self._stripped is None:
            self._stripped = [_strip_lc(line) for line in self._lines]
        return self._stripped

    @property
    def lines(self) -> list[str]:
        """The current buffer lines."""
        return self._lines

    # ------------------------------------------------------------------
    # instance location (s:GetInstNameAdvance)

    def markers(self, keyword: str = "autoinst", *, ignore_case: bool = True) -> list[int]:
        """0-based indices of lines holding a /*KEYWORD*/ marker."""
        if keyword == "autoinst" and ignore_case:
            mark = _AUTOINST_MARK
        else:
            flags = re.IGNORECASE if ignore_case else 0
            mark = re.compile(r"/\*\s*\b" + re.escape(keyword) + r"\b", flags)
        return [
            i
            for i, line in enumerate(self._lines)
            if mark.search(line) and not _LINE_COMMENT.match(line)
        ]

    def resolve_instance(self, marker_idx: int) -> tuple[str, str]:
        """Return (module_name, instance_name) for the instance whose
        /*autoinst*/ marker sits on line MARKER_IDX (0-based)."""
        from . import emacs

        lines = self._stripped_lines()
        m = _AUTOINST_MARK.search(lines[marker_idx])
        if not m:
            raise ValueError(f"line {marker_idx + 1}: no autoinst marker")
        head = "\n".join(lines[:marker_idx] + [lines[marker_idx][: m.start()]])
        j = len(head)
        while j > 0 and head[j - 1] in " \t\n":
            j -= 1
        if j == 0 or head[j - 1] != "(":
            raise ValueError(f"() pair not-match in autoinst, line: {marker_idx + 1}")
        inst, k = emacs._prev_word(head, j - 1)
        if not inst:
            raise ValueError(f"cannot resolve instance name, line: {marker_idx + 1}")
        try:
            group = emacs._skip_group_back(head, k)
        except ValueError:
            raise ValueError(
                f"() pair not-match in autoinst, line: {marker_idx + 1}"
            ) from None
        if group is not None:
            # parameter override: the #( ... ) group before the instance name
            k = group[0]
            while k > 0 and head[k - 1] in " \t\n":
                k -= 1
            if k > 0 and head[k - 1] == "#":
                k -= 1
        module, _ = emacs._prev_word(head, k)
        if not module:
            raise ValueError(f"cannot resolve module name, line: {marker_idx + 1}")
        return module, inst

    def stub(self, marker_idx: int) -> bool:
        """Whether line MARKER_IDX holds a ``(/*autoinst*/);`` stub."""
        return _STUB_LINE.search(self._lines[marker_idx]) is not None

    # ------------------------------------------------------------------
    # internal helpers

    def _targets(self, which: "int | Sequence[int] | None") -> list[int]:
        """Marker line indices to process: all (None), the Nth (int), or a list."""
        markers = self.markers()
        if which is None:
            return markers
        if isinstance(which, int):
            try:
                return [markers[which]]
            except IndexError:
                raise ValueError(
                    f"No Instance found! (which={which}, {len(markers)} markers)"
                ) from None
        return [markers[i] for i in which if 0 <= i < len(markers)]

    @staticmethod
    def _lookup(modules: Mapping[str, ModuleDef], module: str) -> ModuleDef:
        try:
            return modules[module]
        except KeyError:
            raise KeyError(
                f"Has not found the instance: {module}'s module definition!"
            ) from None

    @staticmethod
    def _templated_or_identity(
        template: AutoTemplate | None, port: Port, at_value: str
    ) -> str:
        if template is not None:
            conn = template_connection(template, port.name, at_value, port.width)
            if conn is not None:
                return conn
        return default_connection(port)

    @staticmethod
    def _get_port_name(line: str) -> str:
        m = _PORT_NAME.search(line)
        return m.group(1) if m else ""

    @staticmethod
    def _emit_keep(entry: Keep) -> str:
        return "    " + entry.line if _LINE_COMMENT.match(entry.line) else entry.line

    @staticmethod
    def _fix_comma(raw: str, old_last: bool, new_last: bool) -> str:
        """Reuse an old connection line, fixing only the trailing comma."""
        if old_last == new_last:
            return raw
        m = re.search(r"//.*", raw)
        comments = m.group(0) if m else ""
        body = _strip_lc(raw)
        if new_last:
            body = re.sub(r"\)\s*,\s*$", ")", body)
        else:
            body = re.sub(r"\)\s*$", "),", body)
        return body + comments

    # ------------------------------------------------------------------
    # KillAutoInst

    def kill_auto_inst(self, which: int | None = None) -> VerilogBuffer:
        """Collapse expanded instances back to ``... inst(/*autoinst*/);`` stubs.

        WHICH selects a 0-based marker index; None processes every instance.
        """
        lines = self._lines
        target = set(self._targets(which))
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if i not in target:
                out.append(line)
                i += 1
                continue
            single_line = re.search(r"\)\s*;\s*(?://.*)?$", line) is not None
            out.append(re.sub(r"\*/.*$", "*/);", line))
            i += 1
            if single_line:
                continue
            while i < n and not re.search(r"\)\s*;\s*(?://.*)?$", lines[i]):
                if re.search(r"\bendmodule\b", lines[i]):
                    out.append("")
                    out.append(lines[i])
                    i += 1
                    break
                i += 1
            else:
                if i < n:
                    i += 1  # drop the closing ');' line
        return VerilogBuffer(out)

    # ------------------------------------------------------------------
    # AutoInst (AIT)

    def auto_inst(
        self,
        modules: Mapping[str, ModuleDef],
        *,
        which: int | None = None,
        templates: Sequence[AutoTemplate] | None = None,
        sort: bool = False,
    ) -> VerilogBuffer:
        """Regenerate instance connections from the module definition.

        Existing connections are discarded (KillAutoInst runs first).  With no
        AUTO_TEMPLATE the connection is the identity ``.port(port[width])``;
        when the buffer holds a matching AUTO_TEMPLATE its exact/regexp entries
        drive the connections (verilog-mode semantics).
        """
        killed = self.kill_auto_inst(which)
        lines = killed.lines
        target = set(killed._targets(which))
        out: list[str] = []
        for i, line in enumerate(lines):
            if i not in target:
                out.append(line)
                continue
            module, inst = killed.resolve_instance(i)
            moddef = self._lookup(modules, module)
            template = (
                template_for_module(list(templates), module, before_line=i) if templates else None
            )
            at_value = template_at_value(template, inst) if template else ""
            prefix_max_len, suffix_max_len = get_port_max_len(moddef.entries, sort=sort)
            opener = re.sub(r"\);\s*", "", line, count=1)
            if "--oneline" in line:
                parts = [opener]
                for entry in moddef.entries:
                    if not isinstance(entry, Port):
                        continue
                    conn = self._templated_or_identity(template, entry, at_value)
                    parts.append(f" .{entry.name}({conn}),")
                out.append(re.sub(r"\),\s*$", "));", "".join(parts)))
                continue
            out.append(opener)
            last_name = moddef.last_port_name()
            for entry in moddef.entries:
                if isinstance(entry, Keep):
                    out.append(self._emit_keep(entry))
                    continue
                conn = self._templated_or_identity(template, entry, at_value)
                out.append(
                    format_connection(
                        entry, conn, prefix_max_len, suffix_max_len, entry.name == last_name
                    )
                )
            out.append(");")
        return VerilogBuffer(out)

    # ------------------------------------------------------------------
    # AutoInstUpdate (AIU1)

    def auto_inst_update(
        self,
        modules: Mapping[str, ModuleDef],
        *,
        which: int | None = None,
        date: str | None = None,
        templates: Sequence[AutoTemplate] | None = None,
        sort: bool = False,
    ) -> VerilogBuffer:
        """Minimal-diff update: keep existing lines, append new ports before
        ``);`` (marked INST_NEW), comment out deleted ports (marked INST_DEL).

        Unlike the Vim original, a newly added last port is terminated with ``)``
        rather than leaving a dangling comma before ``);``.
        """
        date = date or _now()
        lines = self._lines
        target = set(self._targets(which))
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if i not in target:
                out.append(line)
                i += 1
                continue
            module, inst = self.resolve_instance(i)
            moddef = self._lookup(modules, module)
            template = (
                template_for_module(list(templates), module, before_line=i) if templates else None
            )
            at_value = template_at_value(template, inst) if template else ""
            prefix_max_len, suffix_max_len = get_port_max_len(moddef.entries, sort=sort)
            current = set(moddef.port_names())
            if self.stub(i):
                # stub instance: nothing to preserve; every port is new
                out.append(re.sub(r"\);\s*", "", line, count=1))
                last_name = moddef.last_port_name()
                for port in moddef.ports:
                    conn = self._templated_or_identity(template, port, at_value)
                    out.append(
                        format_connection(
                            port, conn, prefix_max_len, suffix_max_len, port.name == last_name
                        )
                        + f" // INST_NEW {date}"
                    )
                out.append(");")
                i += 1
                continue
            out.append(line)
            seen: set[str] = set()
            last_name = moddef.last_port_name()
            port_by_name = {p.name: p for p in moddef.ports}
            i += 1
            while True:
                if i >= n:
                    raise ValueError(f"instance at line {i + 1} not terminated by ');'")
                line = lines[i]
                if _INST_END.search(line):
                    for port in moddef.ports:
                        if port.name in seen:
                            continue
                        conn = self._templated_or_identity(template, port, at_value)
                        out.append(
                            format_connection(
                                port, conn, prefix_max_len, suffix_max_len, port.name == last_name
                            )
                            + f" // INST_NEW {date}"
                        )
                    out.append(line)
                    i += 1
                    break
                if _DIRECTIVE.match(line) or _LINE_COMMENT.match(line) or _BLANK.match(line):
                    out.append(line)
                else:
                    port = self._get_port_name(line)
                    if port and port in current:
                        seen.add(port)
                        # the template is authoritative for ports it declares;
                        # other manual connections are kept verbatim
                        tconn = (
                            template_connection(template, port, at_value, port_by_name[port].width)
                            if template
                            else None
                        )
                        if tconn is not None:
                            out.append(
                                format_connection(
                                    port_by_name[port],
                                    tconn,
                                    prefix_max_len,
                                    suffix_max_len,
                                    port == last_name,
                                )
                                + " // Templated"
                            )
                        else:
                            out.append(line)
                    elif port:
                        out.append(f"//{line} // INST_DEL: port {port} have deleted {date}")
                    else:
                        out.append(line)
                i += 1
        return VerilogBuffer(out)

    # ------------------------------------------------------------------
    # AutoInstUpdateOrder (AIU)

    def modify_emacs_inst_format(self, *, skip_sections: bool = False) -> VerilogBuffer:
        """Split verilog-mode's last-port ``.port(sig)); // comment`` into
        ``.port(sig)  // comment`` plus a standalone ``);`` line.  A bare
        ``.port(sig));`` (no trailing comment) is the target format itself
        (e.g. EAI expansion) and is left alone.  With ``skip_sections=True``
        lines inside ``// Outputs``/``// Inputs``/``// Interfaces`` sections
        (EAI-expanded instances) are left untouched (kept for callers that
        must preserve emacs formatting; AIF and AIU both pass False)."""
        pat = re.compile(r"^\s*\.\s*\w+.*\)\s*\)\s*;.*$")
        head = re.compile(r"^\s*.\w+.*\)\s*\)\s*;")
        section = re.compile(r"^\s*//\s*(Outputs|Inouts|Inputs|Interfaces|Parameters)\s*$")
        comment_mask = block_comment_mask(self._lines)
        out: list[str] = []
        in_section = False
        for li, line in enumerate(self._lines):
            if comment_mask[li]:
                out.append(line)
                continue
            if skip_sections and section.match(line):
                in_section = True
                out.append(line)
                continue
            if in_section:
                out.append(line)
                if re.search(r"\)\s*;?\s*$", line) and not re.match(r"^\s*\.", line):
                    in_section = False
                continue
            if pat.match(line) and not _LINE_COMMENT.match(line):
                pre = head.match(line).group(0)
                pre = re.sub(r"\)\s*\)\s*;", ")", pre)
                m = re.search(r"//.*", line)
                comments = m.group(0) if m else ""
                left = head.sub(");", line)
                left = _strip_lc(left)
                out.append(pre + "  " + comments)
                out.append(left)
            else:
                out.append(line)
        return VerilogBuffer(out)

    def auto_inst_update_order(
        self,
        modules: Mapping[str, ModuleDef],
        *,
        which: int | None = None,
        date: str | None = None,
        templates: Sequence[AutoTemplate] | None = None,
        sort: bool = False,
    ) -> VerilogBuffer:
        """Update like :meth:`auto_inst_update` but rewrite the instance in
        module port order, reusing each surviving connection line verbatim
        (so hand-edited ``.port(custom_sig)`` connections keep their text).

        Lines inside the instance body that carry no port connection (comments,
        blanks, directives) are dropped, as in the Vim original.  A stub
        ``(/*autoinst*/);`` is expanded first (the Vim "1st AI, please call AIT"
        path).
        """
        date = date or _now()
        buf = self.modify_emacs_inst_format()
        while True:
            stubs = [idx for idx in buf._targets(which) if buf.stub(idx)]
            if not stubs:
                break
            for stub_idx in stubs:
                marker_pos = buf.markers().index(stub_idx)
                buf = buf.auto_inst(modules, which=marker_pos, templates=templates, sort=sort)
        lines = buf.lines
        target = set(buf._targets(which))
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if i not in target:
                out.append(line)
                i += 1
                continue
            module, inst = buf.resolve_instance(i)
            moddef = self._lookup(modules, module)
            template = (
                template_for_module(list(templates), module, before_line=i) if templates else None
            )
            at_value = template_at_value(template, inst) if template else ""
            prefix_max_len, suffix_max_len = get_port_max_len(moddef.entries, sort=sort)
            out.append(line)
            old: dict[str, tuple[str, bool]] = {}  # port -> (raw line, was-last)
            i += 1
            while True:
                if i >= n:
                    raise ValueError("instance not terminated by ');'")
                line = lines[i]
                if _INST_END.search(line):
                    i += 1
                    break
                if _DIRECTIVE.match(line) or _LINE_COMMENT.match(line) or _BLANK.match(line):
                    i += 1
                    continue
                port = self._get_port_name(line)
                if port:
                    body = _strip_lc(line)
                    old[port] = (line, re.search(r"\)\s*,", body) is None)
                i += 1
            last_name = moddef.last_port_name()
            for entry in moddef.entries:
                if isinstance(entry, Keep):
                    out.append(self._emit_keep(entry))
                    continue
                new_last = entry.name == last_name
                if entry.name in old:
                    raw, old_last = old.pop(entry.name)
                    # the template is authoritative for ports it declares;
                    # other surviving manual lines are reused verbatim
                    tconn = (
                        template_connection(template, entry.name, at_value, entry.width)
                        if template
                        else None
                    )
                    if tconn is not None:
                        out.append(
                            format_connection(
                                entry, tconn, prefix_max_len, suffix_max_len, new_last
                            )
                            + " // Templated"
                        )
                    else:
                        out.append(self._fix_comma(raw, old_last, new_last))
                else:
                    conn = self._templated_or_identity(template, entry, at_value)
                    out.append(
                        format_connection(entry, conn, prefix_max_len, suffix_max_len, new_last)
                        + f" // INST_NEW {date}"
                    )
            for port, (raw, _) in old.items():
                out.append(f"//{raw} // INST_DEL: port {port} have deleted {date}")
            out.append(");")
        return VerilogBuffer(out)


# ---------------------------------------------------------------------------
# backward-compatible module-level wrappers (thin VerilogBuffer delegates)


def find_autoinst_markers(lines: Sequence[str]) -> list[int]:
    """0-based indices of lines holding an /*autoinst*/ marker."""
    return VerilogBuffer(lines).markers()


def resolve_instance(lines: Sequence[str], marker_idx: int) -> tuple[str, str]:
    """Return (module_name, instance_name) for the instance whose
    /*autoinst*/ marker sits on LINES[MARKER_IDX]."""
    return VerilogBuffer(lines).resolve_instance(marker_idx)


def kill_auto_inst(lines: Sequence[str], which: int | None = None) -> list[str]:
    """Collapse expanded instances back to ``... inst(/*autoinst*/);`` stubs.

    WHICH selects a 0-based marker index; None processes every instance.
    """
    return VerilogBuffer(lines).kill_auto_inst(which).lines


def auto_inst(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    *,
    which: int | None = None,
    templates: Sequence[AutoTemplate] | None = None,
    sort: bool = False,
) -> list[str]:
    """Regenerate instance connections from the module definition.

    Existing connections are discarded (KillAutoInst runs first).  With no
    AUTO_TEMPLATE the connection is the identity ``.port(port[width])``;
    when the buffer holds a matching AUTO_TEMPLATE its exact/regexp entries
    drive the connections (verilog-mode semantics).
    """
    return (
        VerilogBuffer(lines)
        .auto_inst(modules, which=which, templates=templates, sort=sort)
        .lines
    )


def auto_inst_update(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    *,
    which: int | None = None,
    date: str | None = None,
    templates: Sequence[AutoTemplate] | None = None,
    sort: bool = False,
) -> list[str]:
    """Minimal-diff update: keep existing lines, append new ports before
    ``);`` (marked INST_NEW), comment out deleted ports (marked INST_DEL).

    Unlike the Vim original, a newly added last port is terminated with ``)``
    rather than leaving a dangling comma before ``);``.
    """
    return (
        VerilogBuffer(lines)
        .auto_inst_update(modules, which=which, date=date, templates=templates, sort=sort)
        .lines
    )


def modify_emacs_inst_format(lines: Sequence[str]) -> list[str]:
    """Split verilog-mode's last-port ``.port(sig)); // comment`` into
    ``.port(sig)  // comment`` plus a standalone ``);`` line."""
    return VerilogBuffer(lines).modify_emacs_inst_format().lines


def auto_inst_update_order(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    *,
    which: int | None = None,
    date: str | None = None,
    templates: Sequence[AutoTemplate] | None = None,
    sort: bool = False,
) -> list[str]:
    """Update like :func:`auto_inst_update` but rewrite the instance in
    module port order, reusing each surviving connection line verbatim
    (so hand-edited ``.port(custom_sig)`` connections keep their text).

    Lines inside the instance body that carry no port connection (comments,
    blanks, directives) are dropped, as in the Vim original.  A stub
    ``(/*autoinst*/);`` is expanded first (the Vim "1st AI, please call AIT"
    path).
    """
    return (
        VerilogBuffer(lines)
        .auto_inst_update_order(modules, which=which, date=date, templates=templates, sort=sort)
        .lines
    )


# ---------------------------------------------------------------------------
# CLI (mirrors gen_empty.py so Vim can call it the same way)


def _scan_dir_once(path: str) -> frozenset:
    """File names directly inside PATH (one syscall batch instead of one
    stat per candidate).  Unreadable/missing dirs scan as empty — the same
    silent-skip semantics as the old per-candidate is_file() probes."""
    try:
        with os.scandir(path) as it:
            return frozenset(e.name for e in it if e.is_file())
    except OSError:
        return frozenset()


def _dir_listings(libdirs: Sequence[str]) -> dict[str, frozenset]:
    """{dir: file names} for every libdir, scanned in parallel — on a large
    SoC (dozens of -y dirs, possibly NFS) the serial stat-per-module-dir
    storm dominates the whole command.  Thread-safe: results are merged
    after the pool joins, keyed by dir."""
    unique = list(dict.fromkeys(libdirs))
    if len(unique) <= 2:
        return {d: _scan_dir_once(d) for d in unique}
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        scanned = list(pool.map(_scan_dir_once, unique))
    return dict(zip(unique, scanned))


def _resolve_module_files(
    module_names: Iterable[str],
    libdirs: Sequence[str],
    inst_files: Mapping[str, str] | None = None,
    vc_files: Sequence[str] | None = None,
    extensions: Sequence[str] | None = None,
    _dir_cache: "dict[str, frozenset] | None" = None,
) -> dict[str, Path]:
    """Resolve module name -> file.  Priority: explicit verilog-inst-file map,
    then vc-file entries (matched by stem), then <dir>/<module><ext> over
    EXTENSIONS (vc ``+libext+``, default ``.v``/``.sv``).

    Libdir probing uses a per-dir listing cache (_dir_cache may carry it
    across calls) instead of one stat() per (name, dir, ext) tuple."""
    files: dict[str, Path] = {}
    inst_files = inst_files or {}
    exts = list(extensions) if extensions else [".v", ".sv"]
    # index vc-file entries by stem
    vc_by_stem: dict[str, str] = {}
    for vc in vc_files or []:
        stem = Path(vc).stem
        vc_by_stem.setdefault(stem, vc)
    names = list(dict.fromkeys(module_names))
    for name in names:
        p = inst_files.get(name)
        if p and Path(p).is_file():
            files[name] = Path(p)
    for name in names:
        if name in files:
            continue
        p = vc_by_stem.get(name)
        if p and Path(p).is_file():
            files[name] = Path(p)
    remaining = [n for n in names if n not in files]
    if remaining:
        cache = _dir_cache if _dir_cache is not None else {}
        unscanned = [d for d in libdirs if d not in cache]
        if unscanned:
            cache.update(_dir_listings(unscanned))
        for name in remaining:
            for d in libdirs:
                listing = cache.get(d) or ()
                hit = next((e for e in exts if name + e in listing), None)
                if hit is not None:
                    files[name] = Path(d) / (name + hit)
                    break
    return files


def _cli_libdirs(args, lines: list[str]) -> list[str]:
    """Module search path for the CLI: ``-y`` dirs first, then the buffer
    file's own verilog-mode Local Variables (verilog-library-directories /
    ``-y`` in verilog-library-flags).  Falls back to ``.`` like before."""
    file_dir = os.path.dirname(
        os.path.abspath(getattr(args, "ref_file", None) or args.in_file)
    )
    return resolve_libdirs(lines, file_dir, extra_dirs=args.libdir) or ["."]


def _cli_resolve(args, lines: list[str]) -> tuple[list[str], dict[str, str], list[str], list[str]]:
    """Full module-resolution context for the CLI: (libdirs, inst_files,
    vc_entries, extensions).  libdirs = ``-y`` + Local-Variables dirs + vc
    ``-y``/``+incdir+`` dirs; inst_files = the verilog-inst-file map;
    vc_entries = module files from ``-f`` vc files; extensions = vc
    ``+libext+`` entries (default ``.v``/``.sv``)."""
    from .libdirs import parse_local_variables, read_vc_file, resolve_libdirs

    # Local-Variables relative paths resolve against the REAL source file's
    # directory (ref_file from the Vim front-end), not the temp buffer file.
    ref = getattr(args, "ref_file", None) or args.in_file
    file_dir = os.path.dirname(os.path.abspath(ref))
    lv = parse_local_variables(lines, file_dir)
    libdirs = resolve_libdirs(lines, file_dir, extra_dirs=args.libdir)
    vc_entries: list[str] = []
    extensions: list[str] = []
    for vc in lv["vc_files"]:
        if Path(vc).is_file():
            parsed = read_vc_file(vc)
            libdirs.extend(d for d in parsed["dirs"] if d not in libdirs)
            vc_entries.extend(parsed["files"])
            extensions.extend(e for e in parsed["extensions"] if e not in extensions)
    if not libdirs:
        libdirs = ["."]
    return libdirs, lv["inst_files"], vc_entries, extensions


def buffer_module_defs(text: str) -> dict[str, list[str]]:
    """Modules defined in TEXT itself, {name: definition lines} — the
    fallback when no library file provides a module (small multi-module
    files and self-contained tests)."""
    mods: dict[str, list[str]] = {}
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        m = re.match(r"\s*module\s+(\w+)", lines[i])
        if m:
            name = m.group(1)
            start = i
            while i < n and not re.match(r"\s*endmodule\b", lines[i]):
                i += 1
            mods.setdefault(name, lines[start : i + 1])
        i += 1
    return mods


def _module_lines(
    name: str, files: Mapping[str, Path], buffer_mods: Mapping[str, list[str]]
) -> list[str] | None:
    """A module's definition lines: its library file, else this buffer."""
    if name in files:
        return files[name].read_text().splitlines()
    return buffer_mods.get(name)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.inst",
        description="automatic.vim AIT/AIU/AIU1/kill rewrite + verilog-mode AUTOINST/AUTOINSTPARAM",
    )
    parser.add_argument(
        "command",
        choices=["ait", "aiu", "aiu1", "kill", "eai", "eap", "aif", "apf", "adf", "af", "aall"],
        help="ait/aiu/aiu1/kill: automatic.vim commands; "
        "eai: verilog-mode AUTOINST; eap: verilog-mode AUTOINSTPARAM; "
        "aif/apf/adf/af: automatic.vim format commands (buffer-local); "
        "aall: eap+eai+aio+aw+areg+adt+arg+af in one pass (shared module table)",
    )
    parser.add_argument("-i", "--in_file", required=True, help="buffer file with /*autoinst*/ markers")
    parser.add_argument(
        "--ref_file",
        default=None,
        help="real source file whose directory anchors Local-Variables relative paths "
        "(the Vim front-end passes the buffer's true file; defaults to in_file)",
    )
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
        help="user-known SystemVerilog interface type name (repeatable; "
        "supplements the interface names found by scanning the -y dirs)",
    )
    parser.add_argument(
        "--which",
        type=int,
        default=None,
        help="0-based instance index to process (default: all)",
    )
    parser.add_argument("--date", default=None, help="override the INST_NEW/INST_DEL timestamp")
    parser.add_argument(
        "--sort",
        action="store_true",
        help="eai/eap: sort pins within each section (verilog-auto-inst-sort)",
    )
    parser.add_argument(
        "--dot-name",
        action="store_true",
        help="eai: emit SystemVerilog .name shorthand when possible (verilog-auto-inst-dot-name)",
    )
    parser.add_argument(
        "--param-value",
        action="store_true",
        help="eai: substitute instance #(...) parameter values into port widths (verilog-auto-inst-param-value)",
    )
    parser.add_argument(
        "--star-expand",
        action="store_true",
        help="eai: expand SystemVerilog .* instances (verilog-auto-star-expand)",
    )
    parser.add_argument(
        "--star-save",
        action="store_true",
        help="eai: keep .* expansion with // Implicit .* tags (verilog-auto-star-save)",
    )
    return parser.parse_args(args_l)


def _read_module_srcs(files: Mapping[str, Path]) -> dict[str, list[str]]:
    """{module: source lines}, read in parallel (NFS-friendly); results are
    merged after the pool joins so nothing depends on thread timing."""
    items = sorted(files.items())
    if len(items) <= 2:
        return {n: p.read_text().splitlines() for n, p in items}
    from concurrent.futures import ThreadPoolExecutor

    def _read(item: "tuple[str, Path]") -> "tuple[str, list[str]]":
        name, path = item
        return name, path.read_text().splitlines()

    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        return dict(pool.map(_read, items))


def _which_resolvable(
    lines: list[str], keyword: str, resolved: set[str]
) -> "tuple[list[int], list[int]]":
    """Marker ordinals whose instance module resolved, for ``which=``.

    Returns (which, resolvable): WHICH is the resolvable list (the eai/eap
    missing-module case remaps "all" to an explicit ordinal list); an empty
    RESOLVABLE means the step should be skipped with a warning (the
    standalone commands raise SystemExit there)."""
    from . import emacs

    markers_all = emacs.find_auto_markers(lines, keyword)
    joined = "\n".join(lines)
    stacks = emacs._scan_parens_at(joined, [m.offset for m in markers_all])
    resolvable = []
    for mi, marker in enumerate(markers_all):
        try:
            stack = stacks[marker.offset]
            mod = emacs._resolve_instance_at(joined, stack[-1])[0]
        except (ValueError, IndexError):
            continue
        if mod in resolved:
            resolvable.append(mi)
    return resolvable, resolvable


def _eai_like_step(lines: list[str], keyword: str, resolved: set[str], include_star: bool = False) -> "tuple[list[int] | None, list[int], list[str]]":
    """(which, resolvable, missing) for one eai|eap step, mirroring the
    standalone _main_emacs command exactly: ``which=None`` (process every
    marker, including ones the probe cannot parse) while nothing is
    missing; an explicit resolvable-ordinal remap otherwise."""
    from . import emacs

    names = set(emacs.marker_modules(lines, keyword, include_star=include_star))
    missing = sorted(names - resolved)
    if not missing:
        return None, [], []
    which, resolvable = _which_resolvable(lines, keyword, resolved)
    return which, resolvable, missing


def _main_aall(text: str, lines: list[str], args) -> list[str]:
    """The AALL pipeline in ONE process, emacs verilog-batch-auto order:
    eap -> eai -> aio -> aw -> areg -> adt -> arg -> af.  Instance module files are
    resolved (dir-listing cached) and read (thread pool) exactly once — the
    seven separate CLI commands would repeat both per command."""
    from . import arg, autodef, emacs, fmt, inout, wire
    from .libdirs import parse_typedef_regexp

    names_emacs: set[str] = set()
    for kw in ("AUTOINST", "AUTOINSTPARAM"):
        try:
            names_emacs |= set(emacs.marker_modules(lines, kw, include_star=args.star_expand))
        except ValueError:
            pass
    # marker-less instances can only be resolved by name (autodef/aw path)
    names_wire = names_emacs | autodef._candidate_module_names(lines)

    libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
    files = _resolve_module_files(
        sorted(names_wire), libdirs, inst_files, vc_entries, extensions
    )
    buffer_mods = buffer_module_defs(text)
    srcs = _read_module_srcs(files)

    def src_of(name: str) -> "list[str] | None":
        return srcs.get(name) or buffer_mods.get(name)

    resolved = {n for n in names_wire if src_of(n) is not None}
    missing = sorted(names_emacs - resolved)
    if missing:
        print(
            f"warning: skipping {len(missing)} instance(s) with no module file: "
            f"{missing}",
            file=sys.stderr,
        )
    td_re = parse_typedef_regexp(lines)
    interfaces = set(find_interfaces(libdirs)) | set(args.interface)
    templates = find_auto_templates(text)

    # 1. EAP (AUTOINSTPARAM)
    which, resolvable, step_missing = _eai_like_step(
        lines, "AUTOINSTPARAM", resolved, include_star=args.star_expand
    )
    if which is None or resolvable:
        module_params = {
            n: emacs.parse_module_params(s) for n in names_emacs if (s := src_of(n))
        }
        lines = emacs.auto_param(
            lines, module_params, which=which, templates=templates, sort=args.sort
        )
    else:
        print(f"warning: AUTOINSTPARAM skipped, module file not found for: {step_missing}", file=sys.stderr)

    # 2. EAI (AUTOINST)
    which, resolvable, step_missing = _eai_like_step(
        lines, "AUTOINST", resolved, include_star=args.star_expand
    )
    if which is None or resolvable:
        modules = {
            n: parse_module_ports(s, with_params=True, interfaces=interfaces, typedef_regexp=td_re)
            for n in names_emacs
            if (s := src_of(n))
        }
        lines = emacs.auto_inst(
            lines,
            modules,
            which=which,
            templates=templates,
            sort=args.sort,
            dot_name=args.dot_name,
            param_value=args.param_value,
            star_expand=args.star_expand,
            star_save=args.star_save,
        )
    else:
        print(f"warning: AUTOINST skipped, module file not found for: {step_missing}", file=sys.stderr)

    # 3. AIO (AUTOOUTPUT/AUTOINPUT, emacs order) / 4. AW / 5. AREG /
    # 6. AD (autodef) share the plain port mapping; AIO runs first so the
    # new port declarations are visible to AW/AREG/ADT
    modules_w = {
        n: parse_module_ports(s, typedef_regexp=td_re, interfaces=interfaces)
        for n in names_wire
        if (s := src_of(n))
    }
    lines = inout.auto_output(lines, modules_w)
    lines = inout.auto_input(lines, modules_w)
    lines = wire.auto_wire(lines, modules_w)
    lines = wire.auto_reg(lines, modules_w)
    lines = autodef.auto_def_t(lines, modules_w)
    # 7. AR (autoarg) / 8. AF (all format) need no module table
    lines = arg.auto_arg(lines)
    lines = fmt.all_format(lines)
    return lines


def _main_emacs(command: str, text: str, lines: list[str], args) -> list[str]:
    from . import emacs

    keyword = "AUTOINST" if command == "eai" else "AUTOINSTPARAM"
    try:
        names = set(emacs.marker_modules(lines, keyword, include_star=args.star_expand))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
    files = _resolve_module_files(names, libdirs, inst_files, vc_entries, extensions)
    buffer_mods = buffer_module_defs(text)
    resolved = {n for n in names if n in files or n in buffer_mods}
    missing = sorted(names - resolved)
    which = args.which
    if missing:
        # skip instances whose module file is missing (process/library cells not
        # in the tree) instead of failing the whole command; warn so the user
        # sees them.  eai/eap take marker indices, so remap to resolvable ones.
        print(
            f"warning: skipping {len(missing)} instance(s) with no module file: "
            f"{missing}",
            file=sys.stderr,
        )
        resolvable = []
        markers_all = emacs.find_auto_markers(lines, keyword)
        joined = "\n".join(lines)
        stacks = emacs._scan_parens_at(joined, [m.offset for m in markers_all])
        for mi, marker in enumerate(markers_all):
            try:
                stack = stacks[marker.offset]
                mod = emacs._resolve_instance_at(joined, stack[-1])[0]
            except (ValueError, IndexError):
                continue
            if mod in files or mod in buffer_mods:
                resolvable.append(mi)
        if not resolvable:
            raise SystemExit(
                f"module file not found for: {missing} (searched {libdirs})"
            )
        which = resolvable if args.which is None else args.which
    templates = find_auto_templates(text)
    from .libdirs import parse_typedef_regexp

    td_re = parse_typedef_regexp(lines)
    if command == "eai":
        interfaces = set(find_interfaces(libdirs)) | set(args.interface)
        modules = {}
        for name in names:
            src = _module_lines(name, files, buffer_mods)
            if src is not None:
                modules[name] = parse_module_ports(
                    src, with_params=True, interfaces=interfaces, typedef_regexp=td_re
                )
        return emacs.auto_inst(
            lines,
            modules,
            which=which,
            templates=templates,
            sort=args.sort,
            dot_name=args.dot_name,
            param_value=args.param_value,
            star_expand=args.star_expand,
            star_save=args.star_save,
        )
    module_params = {}
    for name in names:
        src = _module_lines(name, files, buffer_mods)
        if src is not None:
            module_params[name] = emacs.parse_module_params(src)
    return emacs.auto_param(
        lines, module_params, which=which, templates=templates, sort=args.sort
    )


def main(argv=None) -> None:
    args = create_by_args(argv)
    text = Path(args.in_file).read_text()
    lines = text.splitlines()
    if args.command in ("eai", "aall") and not args.param_value:
        # the buffer's own Local Variables can switch param-value
        # substitution on, like emacs file-local variables
        if re.search(r"^\s*//\s*verilog-auto-inst-param-value\s*:\s*t\b", text, re.M):
            args.param_value = True
    if args.command == "aall":
        out = _main_aall(text, lines, args)
    elif args.command == "kill":
        out = kill_auto_inst(lines, args.which)
    elif args.command in ("aif", "apf", "adf", "af"):
        from . import fmt

        out = {
            "aif": fmt.auto_inst_format,
            "apf": fmt.auto_port_format,
            "adf": fmt.auto_define_format,
            "af": fmt.all_format,
        }[args.command](lines)
    elif args.command in ("eai", "eap"):
        out = _main_emacs(args.command, text, lines, args)
    else:
        try:
            target_lines = VerilogBuffer(lines)._targets(args.which)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        names = {resolve_instance(lines, idx)[0] for idx in target_lines}
        libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
        files = _resolve_module_files(names, libdirs, inst_files, vc_entries, extensions)
        missing = sorted(names - files.keys())
        which = args.which
        if missing:
            # skip instances whose module file is missing (process/library cells
            # not in the tree) instead of failing the whole command; warn.
            print(
                f"warning: skipping {len(missing)} instance(s) with no module file: "
                f"{missing}",
                file=sys.stderr,
            )
            # auto_inst/aiu/aiu1 take marker ORDINALS (not line numbers): remap
            # the resolvable target lines to marker ordinals — but only when the
            # user didn't pin a specific instance with --which.
            if which is None:
                marker_lines = VerilogBuffer(lines).markers()
                which = [
                    marker_lines.index(idx)
                    for idx in target_lines
                    if resolve_instance(lines, idx)[0] in files
                ]
            if not which:
                raise SystemExit(
                    f"module file not found for: {missing} (searched {libdirs})"
                )
        modules = {
            name: parse_module_ports(
                path.read_text().splitlines(),
                interfaces=set(find_interfaces(libdirs)) | set(args.interface),
            )
            for name, path in files.items()
        }
        templates = find_auto_templates(text)
        if args.command == "ait":
            out = auto_inst(lines, modules, which=which, templates=templates)
        elif args.command == "aiu":
            out = auto_inst_update_order(
                lines, modules, which=which, date=args.date, templates=templates
            )
        else:
            out = auto_inst_update(
                lines, modules, which=which, date=args.date, templates=templates
            )
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
