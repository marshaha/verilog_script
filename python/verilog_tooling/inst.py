"""Python rewrite of automatic.vim's instance commands.

Covers the instance-handling family bound in ~/.vimrc:

- ``kill_auto_inst``          <- KillAutoInst  (collapse instance to a stub)
- ``auto_inst``               <- AutoInst      (engine for AIU stub expansion;
  the ``ait`` CLI verb is deprecated and delegates to ``eai``)
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
import bisect
import datetime
import os
import sys
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence, Union


_LOG_T0: float | None = None


_ARG_PORT_MARK = re.compile(r"/\*\s*\b(?:autoarg|AUTOARG)\b")
_ARG_PORT_CLOSE = re.compile(r"\);\s*$")
_ARG_PORT_NOT_LIST = re.compile(
    r";|\b(?:input|output|inout|wire|reg|logic|assign|always|module|endmodule)\b"
)


def auto_arg_port_names(lines: Sequence[str]) -> set[str]:
    """Names listed in /*autoarg*/ header port regions — the port-intent set
    shared by autoarg (dropped-name warning) and AUTOWIRE/autodef (a name on
    an instance inout pin is an inout PORT by intent, never a wire)."""
    names: set[str] = set()
    i, n = 0, len(lines)
    while i < n:
        if not _ARG_PORT_MARK.search(lines[i]) or _ARG_PORT_CLOSE.search(lines[i]):
            i += 1
            continue
        j = i + 1
        plausible = True
        collected: list[str] = []
        while j < n and not _ARG_PORT_CLOSE.search(lines[j]):
            if _ARG_PORT_NOT_LIST.search(lines[j]):
                plausible = False
                break
            collected.extend(re.findall(r"\w+", re.sub(r"//.*$", "", lines[j])))
            j += 1
        if plausible and j < n:
            names.update(collected)
            i = j + 1
        else:
            i += 1
    return names


def _log(msg: str) -> None:
    """Progress log on stderr with a per-step delta — visible in a terminal
    and streamed into vim :messages by the plugin's async job, so a large
    top visibly makes progress instead of looking hung.  Set
    VERILOG_TOOLING_QUIET=1 to silence."""
    global _LOG_T0
    if os.environ.get("VERILOG_TOOLING_QUIET"):
        return
    now = time.monotonic()
    dt = 0.0 if _LOG_T0 is None else now - _LOG_T0
    _LOG_T0 = now
    print(f"[verilog_tooling] {msg} (+{dt:.1f}s)", file=sys.stderr)

from .comments import mask_comments, strip_comments as _strip_c, strip_line_comments as _strip_lc
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
_IFACE_PORT_DECL = re.compile(r"^\s*(\w+)(?:\.(\w+))?\s+(\w+)\s*\)?\s*[,;]?\s*$")
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
    # declaration type info, preserved for AIO/AW/AREG emission:
    # ``output signed [1:0] x`` -> reg signed; ``input logic signed [15:0]``
    # -> input logic signed; ``input foo_t a`` -> input foo_t
    signed: bool = False
    net_type: str = ""  # wire | reg | logic | tri | wand | ...
    data_type: str = ""  # typedef name (foo_t), when the decl used one
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


def find_interfaces(libdirs: Sequence[str], read_includes: bool = True) -> dict[str, Path]:
    """Map interface names to the library file declaring them.  Every
    ``.v`` / ``.sv`` / ``.svh`` / ``.vh`` file in LIBDIRS is raw-regex
    scanned for ``interface <name>`` headers (thread-pooled) — header files
    are scanned directly, so an interface declared in an ``.svh`` is found
    WITHOUT expanding any includes.

    With READ_INCLUDES (default on; the buffer may opt out with
    ``verilog-auto-read-includes:nil``) the scan additionally follows
    `` `include`` edges through a basename index (built from the same pass,
    no directory re-statting) to catch interfaces hidden behind include
    files with unusual names."""
    from concurrent.futures import ThreadPoolExecutor

    from .libdirs import _find_include

    header_re = re.compile(r"^\s*interface\s+(\w+)", re.M)
    include_re = re.compile(r'`include\s+"([^"]+)"')
    paths: list[Path] = []
    for d in libdirs:
        for pat in ("*.v", "*.sv", "*.svh", "*.vh"):
            paths.extend(sorted(Path(d).glob(pat)))

    def scan(path: Path) -> "tuple[tuple[str, ...], tuple[str, ...]]":
        try:
            raw = path.read_text(errors="replace")
        except OSError:
            return (), ()
        return tuple(header_re.findall(raw)), tuple(include_re.findall(raw))

    found: dict[str, Path] = {}
    by_name: dict[str, Path] = {}

    def absorb(batch: list[Path], results: "list[tuple[tuple[str, ...], tuple[str, ...]]]") -> None:
        for p, (names, _incs) in zip(batch, results):
            for n in names:
                found.setdefault(n, p)
            by_name.setdefault(p.name, p)

    if not paths:
        return found
    with ThreadPoolExecutor(max_workers=min(8, len(paths))) as pool:
        results = list(pool.map(scan, paths))
        absorb(paths, results)
        if read_includes:
            # follow include edges to oddly-named include files; files
            # already scanned above need no rescan (their interface
            # declarations were already collected)
            scanned = {p.name for p in paths}
            queue = [n for (_n, incs) in results for n in incs]
            seen = set(queue)
            for _ in range(4):  # include-depth cap
                batch: list[Path] = []
                for name in queue:
                    p = by_name.get(name)
                    if p is None:
                        hit = _find_include(name)
                        p = Path(hit) if hit else None
                    if p is not None and p.name not in scanned:
                        scanned.add(p.name)
                        by_name.setdefault(p.name, p)
                        batch.append(p)
                if not batch:
                    break
                results = list(pool.map(scan, batch))
                absorb(batch, results)
                queue = [
                    n
                    for _p, (_names, incs) in zip(batch, results)
                    for n in incs
                    if n not in seen
                ]
                seen.update(queue)
    return found


def interfaces_for(
    lines: Sequence[str],
    libdirs: Sequence[str],
    extra: Iterable[str] = (),
    found: "Mapping[str, Path] | None" = None,
) -> "set[str]":
    """Interface names for port parsing: library interfaces + user names +
    the buffer's ``verilog-align-typedef-words`` (verilog-mode feeds those
    words into its typedef/interface handling, e.g. ``svi.master m``).
    FOUND reuses an earlier find_interfaces scan when given."""
    from .libdirs import parse_align_typedef_words

    if found is None:
        found = find_interfaces(
            libdirs, read_includes=_read_includes_on(lines)
        )
    return (
        set(found)
        | set(extra)
        | set(parse_align_typedef_words(lines))
    )


_READ_INCLUDES_OFF_RE = re.compile(
    r"^\s*//\s*verilog-auto-read-includes\s*:\s*nil\b", re.M | re.IGNORECASE
)


def _read_includes_on(lines: Sequence[str]) -> bool:
    """Include read-through is on by default; the buffer's
    ``verilog-auto-read-includes:nil`` file-local opts out."""
    return not _READ_INCLUDES_OFF_RE.search("\n".join(lines))


def _port_continues(line: str) -> bool:
    """Whether LINE is an incomplete port declaration needing the next line:
    the (only) remaining content is a trailing `,` after a range."""
    t = _strip_lc(line).strip().rstrip(",").rstrip()
    return t.endswith("]")


_TYPEDEF_REGEXP: "re.Pattern[str] | None" = None
_UDT_HEAD_RE = re.compile(
    r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)?)"
    r"((?:\s*\[[^\]]*\])*)"
    r"(?:\s+(?:signed|unsigned))?"
    r"\s+([A-Za-z_]\w*)"
)


def set_typedef_regexp(regexp: str | None) -> None:
    """Set the verilog-typedef-regexp used when parsing port lines: a first
    word matching it is a TYPE (e.g. reqcmd_t), and the port name follows.
    Both Emacs (``\\(...\\)``/``\\|``) and Python (``(...)``/``|``) regexp
    dialects are accepted (auto-detected)."""
    global _TYPEDEF_REGEXP
    _TYPEDEF_REGEXP = re.compile(_auto_re_to_python(regexp)) if regexp else None


def _parse_port_line(line: str) -> Port | None:
    ports = _parse_port_line_multi(line)
    return ports[0] if ports else None


def _parse_port_line_multi(line: str) -> list[Port]:
    """Parse one declaration line into every port it declares: a
    comma-separated declaration (``output ia, ib, ic;``) contributes one
    Port per name, all sharing direction/type/packed range."""
    # /* ... */ is comment text, not declaration text (incomplete pairs and
    # line comments are handled by the caller before this point)
    line = re.sub(r"/\*.*?\*/", " ", line)
    m = _PORT_KEYWORD.match(line)
    if not m:
        return []
    rest = line[m.end() :]
    net_type = ""
    mnet = re.match(
        r"^(wire|reg|logic|tri0|tri1|trireg|tri|wand|wor|supply0|supply1)\b\s*", rest
    )
    if mnet:
        net_type = mnet.group(1) if mnet.group(1) != "wire" else ""  # wire is the default
        rest = rest[mnet.end() :]
    signed = False
    if re.match(r"^signed\b\s*", rest):
        signed = True
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
    rest = rest.rstrip().rstrip(";").rstrip(")").rstrip()
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in rest:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out: list[Port] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        nm = re.match(r"\w+", part)
        if not nm:
            continue
        name = nm.group(0)
        after = part[nm.end() :]
        data_type = ""
        # A user data type may precede the port name (``output
        # crash_dump_t crash_dump_o``, ``input pkg::foo_t x``), possibly
        # with a packed dimension between them (``input foo_t [3:0] x``):
        # the name is the last word and any ``[...]`` group is a packed
        # dimension of the port.  verilog-mode reads them this way even
        # without a verilog-typedef-regexp local; the explicit regexp
        # (when set) still applies below for cases it describes.  Inline
        # enum/struct/union heads keep the previous behaviour.
        um = _UDT_HEAD_RE.match(part)
        if um and um.group(1) not in ("enum", "struct", "union"):
            data_type, name = um.group(1), um.group(3)
            after = part[um.end() :]
            for dm in re.finditer(r"\[([^\]]*)\]", um.group(2) or ""):
                packed.append(re.sub(r"\s+", "", dm.group(1)))
        elif _TYPEDEF_REGEXP is not None and _TYPEDEF_REGEXP.search(name):
            # a user type (reqcmd_t): the port name is the next word, the
            # first word is its data type
            nm2 = re.match(r"\s*(\w+)", after)
            if not nm2:
                continue
            data_type, name = name, nm2.group(1)
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
        out.append(
            Port(
                name=name,
                direction=m.group(1),
                width=width,
                packed=tuple(packed),
                unpacked=tuple(unpacked),
                signed=signed,
                net_type=net_type,
                data_type=data_type,
            )
        )
    return out


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
    from .libdirs import expand_includes

    lines = _expand_ansi_header(expand_includes(list(lines)), interfaces)
    entries: list[Entry] = []
    seen_module = False
    have_port = False
    portlist_opened = False
    portlist_closed = False
    _hdr_depth = 0
    _param_next = False
    decl_open = False  # the previous declaration ended mid-list (trailing ,)
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
        line = re.sub(r"^\s*,", "", line)
        # the header's opening '(' may sit at the start of the first port
        # line itself (``(output [w-1:0] q,`` after a #(...) block)
        line = re.sub(r"^\s*\((?=\s*(?:input|output|inout)\b)", "", line)
        if re.match(r"^\s*module\b", line):
            seen_module = True
            if not mod_name:
                m = re.search(r"\bmodule\s+(\w+)", line)
                if m:
                    mod_name = m.group(1)
            # an ANSI port may start on the module line itself even in a
            # multi-line header (``module m (input a,\n output b);``): parse
            # the text after the port-list '(' as the first declaration
            # (the port list is the LAST '(' on the line — a #(...) block
            # closes before it)
            if "(" in line:
                tail = line[line.rindex("(") + 1 :]
                if _PORT_KEYWORD.match(tail):
                    portlist_opened = True  # port list starts on this line
                    _hdr_depth = line.count("(") - line.count(")")
                    line = tail
        if not seen_module:
            continue
        # the port list opens at the first depth-0 '(' that does not open a
        # #( ...) parameter block; Keeps (comments/directives) are port-list
        # material only from there until it closes, so a `ifdef FORMAL
        # guarding a localparam inside #(...) — or directives in the module
        # body — never leak into the connection list
        if not portlist_opened:
            for ch in line:
                if ch == "#" and _hdr_depth == 0:
                    _param_next = True
                elif ch == "(":
                    if _hdr_depth == 0 and not _param_next:
                        portlist_opened = True
                    _param_next = False
                    _hdr_depth += 1
                elif ch == ")":
                    _hdr_depth = max(0, _hdr_depth - 1)
        elif not portlist_closed:
            for ch in line:
                if ch == "(":
                    _hdr_depth += 1
                elif ch == ")":
                    _hdr_depth -= 1
                    if _hdr_depth <= 0:
                        portlist_closed = True
        if ");" in line and entries and not have_port:
            entries.clear()  # drop keep lines collected inside #( ... )
        if (
            _DIRECTIVE.match(line)
            or (_LINE_COMMENT.match(line) and not re.match(r"^\s*//\s*\{\{\{", line))
            or _BLANK.match(line)
        ):
            if portlist_closed or not (portlist_opened or have_port):
                continue  # outside the module's port list
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
            ports_here = _parse_port_line_multi(joined)
            if ports_here:
                entries.extend(ports_here)
                have_port = True
                # a join (j > idx) already swallowed the following lines;
                # only an in-place declaration can leave the list open
                decl_open = j == idx and _strip_lc(joined).rstrip().endswith(",")
            continue
        if ");" in line:
            decl_open = False  # the wrapped port list has closed
        mcont = re.match(r"^\s*([A-Za-z_][\w$]*(?:\s*,\s*[A-Za-z_][\w$]*)*)\s*,?\s*$", line)
        if (
            mcont
            and decl_open
            and entries
            and isinstance(entries[-1], Port)
            and _PORT_KEYWORD.match(line) is None
        ):
            # names continuing a multi-name declaration wrapped across
            # lines (`output wire a, b,` then a bare `c,` line): they
            # share the declaration's direction and width
            proto = entries[-1]
            for nm in mcont.group(1).split(","):
                nm = nm.strip()
                if nm:
                    entries.append(
                        Port(
                            name=nm,
                            direction=proto.direction,
                            width=proto.width,
                            packed=proto.packed,
                        )
                    )
            decl_open = _strip_lc(line).rstrip().endswith(",")
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


def _local_inst_template_required(lines: Sequence[str]) -> bool:
    """verilog-auto-inst-template-required file-local (default nil): non-nil
    omits ports without a template from AUTOINST (instead of connecting
    them to a same-named net)."""
    m = re.search(r"^\s*//\s*verilog-auto-inst-template-required\s*:\s*(\w+)", "\n".join(lines), re.M)
    return bool(m) and m.group(1).lower() != "nil"


def _local_inst_vector(lines: Sequence[str]) -> str:
    """verilog-auto-inst-vector file-local (default t): t uses bus subscripts
    for default AUTOINST ports, nil skips them, unsigned uses them only for
    unsigned (non-signed) ports."""
    m = re.search(r"^\s*//\s*verilog-auto-inst-vector\s*:\s*(\w+)", "\n".join(lines), re.M)
    if m:
        v = m.group(1).lower()
        if v in ("nil", "unsigned"):
            return v
    return "t"


def default_connection(port: Port, inst_vector: str = "t") -> str:
    """The no-template instantiation connection: ``name.modport`` for an
    interface port declared with a modport, ``name`` for a plain interface
    port, ``name[width]`` for a vector, ``name`` for a scalar.

    INST_VECTOR is verilog-auto-inst-vector: 't' (default) always uses the
    subscript, 'nil' skips it, 'unsigned' uses it only for unsigned ports."""
    if port.is_interface:
        return f"{port.name}.{port.modport}" if port.modport else port.name
    use_vector = (
        inst_vector == "t"
        or (inst_vector == "unsigned" and not port.signed)
    )
    return port.name + (f"[{port.width}]" if (port.width and use_vector) else "")


def _paren_delta(text: str) -> int:
    """Net ``()``/``[]`` depth change across TEXT (comments stripped by
    the caller): positive means a connection value opened here has not
    yet closed on this line."""
    d = 0
    for ch in text:
        if ch in "([":                                     
            d += 1
        elif ch in ")]":
            d -= 1
    return d


def _split_top_level_commas(text: str) -> list[str]:
    """Split TEXT at commas that sit at parenthesis/bracket depth 0 (a
    connection list like '.a(x), .b(y),'; commas inside .p({...}) or .p(f(a,b))
    stay with their fragment)."""
    frags, depth, cur = [], 0, []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            frags.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    frags.append("".join(cur))
    return frags


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
        if j == 0:
            raise ValueError(f"() pair not-match in autoinst, line: {marker_idx + 1}")
        if head[j - 1] == "(":
            open_paren = j - 1
        else:
            # the marker sits INSIDE the port list (manual connections
            # precede it): the innermost unclosed '(' at the marker is the
            # instance's open paren (connections in between are balanced)
            stacks = emacs._scan_parens_at(head + "\n", [len(head)])
            st = stacks[len(head)]
            if not st:
                raise ValueError(
                    f"() pair not-match in autoinst, line: {marker_idx + 1}"
                )
            open_paren = st[-1]
        inst, k = emacs._prev_word(head, open_paren)
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

    def _markers_at_head(self, which: int | None = None) -> "VerilogBuffer":
        # WHICH (a 0-based marker ordinal) limits the move to that instance:
        # a targeted update must not reformat instances it was not asked to touch
        """Move /*autoinst*/ markers that ride the last connection line
        (``.p(s) /*AUTOINST*/);``) to the canonical position right after
        the instance's opening paren.  The update loops consume
        connections only AFTER their marker, so a tail marker made them
        scan past the instance end (or crash on stale line numbers).
        Marker count and order are unchanged, keeping ordinal ``which``
        selection valid.  Buffers whose markers already sit at a head
        (only whitespace between the paren and the marker) are returned
        unchanged.
        """
        from . import emacs

        text = "\n".join(self._lines)
        mt = mask_comments(text)
        starts = []
        pos = 0
        for ln in self._lines:
            starts.append(pos)
            pos += len(ln) + 1
        edits: list[tuple[int, int, str]] = []
        for ordinal, midx in enumerate(self.markers()):
            if which is not None and ordinal != which:
                continue  # targeted update: other instances stay verbatim
            m = _AUTOINST_MARK.search(self._lines[midx])
            if not m:
                continue
            marker_off = starts[midx] + m.start()
            head_mt = mt[:marker_off]
            stacks = emacs._scan_parens_at(head_mt + "\n", [len(head_mt)])
            st = stacks[len(head_mt)]
            if not st:
                continue
            open_off = st[-1]
            if not mt[open_off + 1 : marker_off].strip():
                continue  # already head-positioned
            if not re.search(r"\w|\(", mt[open_off + 1 : marker_off]):
                continue  # instance body is empty between ( and the marker
            # _AUTOINST_MARK stops at the keyword; take the whole comment
            close = self._lines[midx].find("*/", m.start())
            if close < 0:
                continue
            mlen = close + 2 - m.start()
            marker_text = text[marker_off : marker_off + mlen]
            # if connection text shares the line after the marker's new
            # head position (``u(.a(a), ...``), split the line there so
            # each connection starts on its own line, as the loops expect
            line_end = text.find("\n", open_off)
            after = text[open_off + 1 : line_end if line_end >= 0 else len(text)]
            sep = "\n" if after.strip() else ""
            edits.append((open_off + 1, open_off + 1, marker_text + sep))
            edits.append((marker_off, marker_off + mlen, ""))
        if not edits:
            return self
        for s, e, rep in sorted(edits, reverse=True):
            text = text[:s] + rep + text[e:]
        return VerilogBuffer(text.split("\n"))

    def stub(self, marker_idx: int) -> bool:
        """Whether line MARKER_IDX holds a ``(/*autoinst*/);`` stub: the
        marker directly follows the instance's opening paren.  A marker
        riding the last connection line (``.p(s) /*AUTOINST*/);``) is a
        full instance, NOT a stub — regenerating it as one duplcates the
        whole connection list (and a hung stub scan walks past its end).
        """
        line = self._lines[marker_idx]
        if not _STUB_LINE.search(line):
            return False
        lines = self._stripped_lines()
        m = _AUTOINST_MARK.search(lines[marker_idx])
        if not m:
            return False
        head = "\n".join(lines[:marker_idx] + [lines[marker_idx][: m.start()]])
        j = len(head)
        while j > 0 and head[j - 1] in " \t\n":
            j -= 1
        return j > 0 and head[j - 1] == "("

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
        template: AutoTemplate | None, port: Port, at_value: str,
        template_required: bool = False, inst_vector: str = "t",
    ) -> str | None:
        if template is not None:
            conn = template_connection(template, port.name, at_value, port.width)
            if conn is not None:
                return conn
        if template_required:
            # verilog-auto-inst-template-required: omit non-templated ports
            return None
        return default_connection(port, inst_vector)

    @staticmethod
    def _get_port_name(line: str) -> str:
        # a leading .name covers both .name(...) and the SystemVerilog
        # shorthand .name, / .name) forms (pulp connects whole structs
        # that way); without it shorthand pins read as "no port" and the
        # updater re-emitted them as INST_NEW duplicates
        m = re.match(r"\s*\.(\w+)", line)
        if m:
            return m.group(1)
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
        # A marker riding the last connection line (or alone on a line
        # after the connections) points BACKWARD at its instance: the
        # forward drop below would leave the old connections standing
        # and the caller would append a second, duplicate list.  Move
        # such markers to the head first so the drop covers the span.
        buf = self._markers_at_head(which=which)
        lines = buf._lines
        target = set(buf._targets(which))
        template_required = _local_inst_template_required(lines)
        inst_vector = _local_inst_vector(lines)
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
        template_required = _local_inst_template_required(lines)
        inst_vector = _local_inst_vector(lines)
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
                    conn = self._templated_or_identity(
                        template, entry, at_value, template_required, inst_vector
                    )
                    if conn is None:
                        continue
                    parts.append(f" .{entry.name}({conn}),")
                out.append(re.sub(r"\),\s*$", "));", "".join(parts)))
                continue
            out.append(opener)
            # filter first: template-required may drop ports, so the last
            # EMITTED port (not the last defined port) gets the `)` terminator
            emitted: list[tuple[object, str]] = []
            for entry in moddef.entries:
                if isinstance(entry, Keep):
                    emitted.append((entry, ""))  # Keep lines pass through
                    continue
                conn = self._templated_or_identity(
                    template, entry, at_value, template_required, inst_vector
                )
                if conn is None:
                    continue
                emitted.append((entry, conn))
            last_idx = max(
                (k for k, (e, _) in enumerate(emitted) if isinstance(e, Port)),
                default=-1,
            )
            for k, (entry, conn) in enumerate(emitted):
                if isinstance(entry, Keep):
                    out.append(self._emit_keep(entry))
                    continue
                out.append(
                    format_connection(
                        entry, conn, prefix_max_len, suffix_max_len, k == last_idx
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
        # A marker riding the last connection line belongs at the head;
        # canonicalize first (idempotent; ordinal `which` stays valid).
        moved = self._markers_at_head(which=which)
        if moved._lines != self._lines:
            return moved.auto_inst_update(
                modules, which=which, date=date, templates=templates, sort=sort
            )
        # Normalize emacs-style last pins (.p(sig)); // comment) into a pin
        # line + standalone `);` first, like auto_inst_update_order does:
        # otherwise the update loop meets `);` on the last connection line,
        # treats that pin as unseen, and re-emits it (dup pin, unclosed
        # instance).  The normalization is idempotent, so recursing on the
        # normalized buffer cannot loop.  Ordinal `which` stays valid —
        # splitting inserts only non-marker lines.
        normalized = self.modify_emacs_inst_format(which=which)
        if normalized._lines != self._lines:
            return normalized.auto_inst_update(
                modules, which=which, date=date, templates=templates, sort=sort
            )
        lines = self._lines
        target = set(self._targets(which))
        template_required = _local_inst_template_required(lines)
        inst_vector = _local_inst_vector(lines)
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
                    conn = self._templated_or_identity(
                        template, port, at_value, template_required, inst_vector
                    )
                    if conn is None:
                        continue
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
                    new_lines = []
                    for port in moddef.ports:
                        if port.name in seen:
                            continue
                        conn = self._templated_or_identity(
                            template, port, at_value, template_required, inst_vector
                        )
                        if conn is None:
                            continue
                        new_lines.append(
                            format_connection(
                                port, conn, prefix_max_len, suffix_max_len, port.name == last_name
                            )
                            + f" // INST_NEW {date}"
                        )
                    if (
                        new_lines
                        and out
                        and out[-1].lstrip().startswith(".")
                        and not out[-1].rstrip().endswith(",")
                    ):
                        # the last kept connection needs its comma before
                        # the first appended pin (shorthand-kept lists end
                        # `.default_mst_port_i` with no trailing comma)
                        prev = out[-1].rstrip()
                        if "//" in prev:
                            head, tail = prev.split("//", 1)
                            out[-1] = head.rstrip() + ", //" + tail
                        else:
                            out[-1] = prev + ","
                    out.extend(new_lines)
                    out.append(line)
                    i += 1
                    break
                if _DIRECTIVE.match(line) or _LINE_COMMENT.match(line) or _BLANK.match(line):
                    out.append(line)
                else:
                    frags = [
                        f for f in _split_top_level_commas(_strip_lc(line)) if f.strip()
                    ]
                    port = self._get_port_name(line)
                    if port and port in current and len(frags) > 1:
                        # several connections share the line: register them
                        # all (the updater never rewrites kept lines, so a
                        # miss would re-emit them as duplicated INST_NEW)
                        for frag in frags:
                            fp = self._get_port_name(frag)
                            if fp and fp in current:
                                seen.add(fp)
                        out.append(line)
                    elif port and port in current:
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

    def modify_emacs_inst_format(
        self, *, skip_sections: bool = False, which: int | None = None
    ) -> VerilogBuffer:
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
        # WHICH (a 0-based marker ordinal) restricts the split to the
        # targeted instance's statement: a cursor-scoped update must not
        # reformat other instances' tails
        span_lines: "tuple[int, int] | None" = None
        if which is not None:
            ml = self.markers()
            if 0 <= which < len(ml):
                mt = mask_comments("\n".join(self._lines))
                moff = _marker_offsets(self._lines, ml)[which]
                try:
                    so, eo = _statement_span(mt, moff)
                    span_lines = (mt.count("\n", 0, so), mt.count("\n", 0, eo))
                except ValueError:
                    span_lines = None
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
            if span_lines is not None and not (span_lines[0] <= li <= span_lines[1]):
                out.append(line)  # outside the targeted instance: verbatim
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
        # tail markers first: until a tail marker leaves its line, the
        # format splitter cannot see the `.pin(sig));` shape it splits
        buf = self._markers_at_head(which=which).modify_emacs_inst_format(which=which)
        while True:
            # expand ONE stub, then rescan: auto_inst rewrites the buffer,
            # so every other stub's line number from the previous scan is
            # stale (the old loop crashed on `markers().index(stale)`)
            stubs = [idx for idx in buf._targets(which) if buf.stub(idx)]
            if not stubs:
                break
            marker_pos = buf.markers().index(stubs[0])
            buf = buf.auto_inst(modules, which=marker_pos, templates=templates, sort=sort)
        lines = buf.lines
        target = set(buf._targets(which))
        template_required = _local_inst_template_required(lines)
        inst_vector = _local_inst_vector(lines)
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
            pending: "str | None" = None  # port whose (first) line leaves parens open
            pending_depth = 0
            i += 1
            while True:
                if i >= n:
                    raise ValueError("instance not terminated by ');'")
                line = lines[i]
                if pending is not None:
                    # a hand-written connection may span several lines
                    # (``.data_i ({..., 22'b0, <newline> ...})``): the
                    # following lines are part of that connection — keep
                    # them, or the kept text ends mid-expression
                    raw, _ = old[pending]
                    old[pending] = (raw + "\n" + line, _)
                    pending_depth += _paren_delta(_strip_lc(line))
                    i += 1
                    if pending_depth <= 0:
                        raw, _ = old[pending]
                        old[pending] = (
                            raw,
                            re.search(r"\)\s*,\s*$", _strip_lc(raw)) is None,
                        )
                        pending = None
                    continue
                if _INST_END.search(line):
                    i += 1
                    break
                if _DIRECTIVE.match(line) or _LINE_COMMENT.match(line) or _BLANK.match(line):
                    i += 1
                    continue
                # a hand-written line may pack several connections
                # (`.a(x), .b(y),`); record each, or the updater re-emits
                # every later one as a duplicated INST_NEW
                frags = [f for f in _split_top_level_commas(_strip_lc(line)) if f.strip()]
                candidate: "str | None" = None
                if len(frags) > 1:
                    for fi, frag in enumerate(frags):
                        port = self._get_port_name(frag)
                        if port:
                            old[port] = (
                                frag.strip() + ("," if fi < len(frags) - 1 else ""),
                                fi == len(frags) - 1,
                            )
                            candidate = port
                else:
                    port = self._get_port_name(line)
                    if port:
                        body = _strip_lc(line)
                        old[port] = (line, re.search(r"\)\s*,", body) is None)
                        candidate = port
                if candidate is not None:
                    d = _paren_delta(_strip_lc(line))
                    if d > 0:
                        pending = candidate
                        pending_depth = d
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
                    conn = self._templated_or_identity(
                        template, entry, at_value, template_required, inst_vector
                    )
                    if conn is None:
                        continue
                    out.append(
                        format_connection(entry, conn, prefix_max_len, suffix_max_len, new_last)
                        + f" // INST_NEW {date}"
                    )
            for port, (raw, _) in old.items():
                # a deleted connection may span several lines: every
                # line must be commented out, not just the first
                if "\n" in raw:
                    first, rest = raw.split("\n", 1)
                    out.append(
                        f"//{first} // INST_DEL: port {port} have deleted {date}"
                    )
                    out.extend("//" + ln for ln in rest.split("\n"))
                else:
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


def _statement_span(mt: str, marker_off: int) -> tuple[int, int]:
    """(start, end) offsets of the instance statement containing the
    AUTO marker at MARKER_OFF in MASK_COMMENTS()-ed text: the module
    name through the ')' closing the port list.  Raises ValueError when
    the marker is not inside a resolvable instance statement."""
    from . import emacs

    st = emacs._scan_parens_at(mt + "\n", [marker_off])[marker_off]
    if not st:
        raise ValueError("marker outside any parenthesis group")
    inner = st[-1]
    # #( ... ) parameter-override group: '(' directly preceded by '#'
    k = inner
    while k > 0 and mt[k - 1] in " \t\n":
        k -= 1
    if k > 0 and mt[k - 1] == "#":
        start = emacs._prev_word(mt, k - 1)[1]
        close = emacs._matching_paren(mt, inner)
        nxt = mt.find("(", close + 1)
        end = emacs._matching_paren(mt, nxt) if nxt >= 0 else close
        return start, end
    # port list: the instance name precedes it, a #(...) group may
    # precede the instance name (mirrors VerilogBuffer.resolve_instance)
    _, kw = emacs._prev_word(mt, inner)
    group = emacs._skip_group_back(mt, kw)
    if group is not None:
        kk = group[0]
        while kk > 0 and mt[kk - 1] in " \t\n":
            kk -= 1
        if kk > 0 and mt[kk - 1] == "#":
            kk -= 1
        kw = kk
    start = emacs._prev_word(mt, kw)[1]
    return start, emacs._matching_paren(mt, inner)


def _line_to_ordinal(
    lines: Sequence[str], marker_offsets: Sequence[int], line_no: int
) -> int:
    """Ordinal (same numbering as --which) of the marker whose instance
    statement contains 1-based LINE_NO; -1 when no statement does.
    Marker-offsets that fail to resolve are skipped."""
    if line_no < 1 or line_no > len(lines):
        return -1
    mt = mask_comments("\n".join(lines))
    starts: list[int] = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    lo = starts[line_no - 1]
    hi = lo + len(lines[line_no - 1])
    for ordinal, moff in enumerate(marker_offsets):
        try:
            s, e = _statement_span(mt, moff)
        except ValueError:
            continue
        if s <= hi and e >= lo:
            return ordinal
    return -1


def _marker_offsets(lines: Sequence[str], marker_lines: Sequence[int]) -> list[int]:
    """Character offsets of the /*autoinst*/ markers on MARKER_LINES."""
    starts: list[int] = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    out = []
    for ml in marker_lines:
        m = _AUTOINST_MARK.search(lines[ml])
        out.append(starts[ml] + (m.start() if m else 0))
    return out


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
    libdirs = resolve_libdirs(lines, file_dir, extra_dirs=args.libdir) or ["."]
    from .libdirs import set_include_dirs

    set_include_dirs(libdirs)
    return libdirs


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
    from .libdirs import set_include_dirs

    set_include_dirs(libdirs)
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


def module_spans(lines: Sequence[str]) -> list[tuple[int, int]]:
    """(start, end) inclusive line range of every module in the buffer."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(lines)
    while i < n:
        if re.match(r"\s*module\s+\w+", lines[i]):
            start = i
            while i < n and not re.match(r"\s*endmodule\b", lines[i]):
                i += 1
            spans.append((start, i))
        i += 1
    return spans


def map_module_spans(
    lines: Sequence[str], mark_re: "re.Pattern[str]", fn
) -> list[str]:
    """Apply FN(span_lines) to every module span holding a MARK_RE match,
    splicing the results back (spans are processed bottom-up so earlier
    indices stay valid); all other lines pass through unchanged.

    Declaration-table commands (AIO/AW/AREG) must scope their exclusion
    sets to ONE module: a sibling module's ports/declarations in the same
    buffer are not visible here and must never suppress this module's
    candidates."""
    out = list(lines)
    for start, end in reversed(module_spans(lines)):
        span = out[start : end + 1]
        if not any(mark_re.search(ln) for ln in span):
            continue
        out[start : end + 1] = fn(span)
    return out


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
        description="automatic.vim AIU/AIU1/kill rewrite + verilog-mode AUTOINST/AUTOINSTPARAM (ait deprecated, delegates to eai)",
    )
    parser.add_argument(
        "command",
        choices=["ait", "aiu", "aiu1", "kill", "eai", "eap", "aif", "apf", "adf", "af", "aall", "ainj"],
        help="aiu/aiu1/kill: automatic.vim commands; "
        "ait: DEPRECATED, delegates to eai; "
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
        help="0-based index among /*AUTOINST*/ markers (default: all)",
    )
    parser.add_argument(
        "--line",
        type=int,
        default=None,
        help="1-based line number (editor cursor position): process only "
        "the instance whose statement contains that line; conflicts "
        "with --which",
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
    AUTOINSERTLISP -> AUTOINSTPARAM -> AUTOINST -> AUTOASCIIENUM ->
    AUTOINOUTMODPORT -> AUTOINOUTMODULE -> AUTOINOUTCOMP -> AUTOINOUTIN ->
    AUTOINOUTPARAM -> AUTOOUTPUT/AUTOINPUT/AUTOINOUT -> AUTOTIEOFF -> AUTOUNDEF
    -> AUTOASSIGNMODPORT -> AUTOLOGIC -> AUTOWIRE -> AUTOREG -> AUTOREGINPUT ->
    AUTOOUTPUTEVERY -> AUTOSENSE -> AUTORESET -> AUTOUNUSED -> AUTOARG ->
    AUTOINSERTLAST -> format.  Instance module files are resolved (dir-listing
    cached) and read (thread pool) exactly once — the separate CLI commands
    would repeat both per command."""
    from . import arg, autodef, emacs, fmt, inout, misc, sense, wire, xfer
    from .libdirs import parse_typedef_regexp

    # emacs verilog-delete-auto-buffer ("Clear existing autos else we'll be
    # screwed by existing ones"): drop last round's regions up front — left
    # in place, their declarations would poison this round's exclusions
    # (the regions are regenerated below)
    lines = wire.kill_auto_wire(lines)
    lines = wire.kill_auto_reg(lines)
    lines = autodef.kill_auto_def_t(lines)
    lines = misc.kill_auto_ascii_enum(lines)
    lines = misc.kill_auto_logic(lines)
    lines = misc.kill_auto_tieoff(lines)
    lines = misc.kill_auto_unused(lines)
    lines = misc.kill_auto_undef(lines)
    lines = misc.kill_auto_insert_lisp(lines)
    lines = misc.kill_auto_insert_last(lines)
    lines = xfer.kill_auto_inoutmodule(lines)  # also COMP/IN, same header
    lines = xfer.kill_auto_inoutparam(lines)
    lines = xfer.kill_auto_inoutmodport(lines)
    lines = xfer.kill_auto_assign_modport(lines)
    lines = xfer.kill_auto_outputevery(lines)
    lines = xfer.kill_auto_reginput(lines)
    lines = sense.kill_auto_sense(lines)
    lines = sense.kill_auto_reset(lines)
    text = "\n".join(lines)

    _log(f"aall: {Path(args.ref_file or args.in_file).name}: resolving instance modules ...")
    names_emacs: set[str] = set()
    for kw in ("AUTOINST", "AUTOINSTPARAM"):
        try:
            names_emacs |= set(emacs.marker_modules(lines, kw, include_star=args.star_expand))
        except ValueError:
            pass
    # marker-less instances can only be resolved by name (autodef/aw path);
    # AUTOINOUTMODULE/COMP/IN/PARAM markers name their source module in the
    # first quoted argument
    names_xfer: set[str] = set()
    for _kw in ("AUTOINOUTMODULE", "AUTOINOUTCOMP", "AUTOINOUTIN", "AUTOINOUTPARAM"):
        for _mk in xfer._find_markers(text, _kw):
            if _mk.args:
                names_xfer.add(_mk.args[0])
    names_wire = names_emacs | autodef._candidate_module_names(lines) | names_xfer

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
    _log(
        f"aall: {len(files)} module file(s) read"
        + (f", {len(missing)} unresolved (skipped)" if missing else "")
    )
    if missing:
        print(
            f"warning: skipping {len(missing)} instance(s) with no module file: "
            f"{missing}",
            file=sys.stderr,
        )
    td_re = parse_typedef_regexp(lines)
    _t0 = time.monotonic()
    iface_files = find_interfaces(  # one library scan, shared below
        libdirs, read_includes=_read_includes_on(lines)
    )
    interfaces = interfaces_for(lines, libdirs, args.interface, found=iface_files)
    templates = find_auto_templates(text)
    _log(f"aall: interface scan done ({len(iface_files)} interface(s), {time.monotonic() - _t0:.1f}s)")

    # Interface table for AUTOINOUTMODPORT / AUTOASSIGNMODPORT — buffer
    # definitions shadow same-named library interfaces (verilog-mode reads
    # the current buffer first)
    ifaces: dict = {}
    for iname, ipath in iface_files.items():
        try:
            ifaces[iname] = xfer.parse_interface_info(ipath.read_text().splitlines(), td_re)
        except (OSError, ValueError):
            pass
    ifaces.update(xfer.buffer_interfaces(lines, td_re))

    # 1. AUTOINSERTLISP (emacs runs it first)
    lines = misc.auto_insert_lisp(lines)
    _log("aall: AUTOINSERTLISP done")

    # 2. EAP (AUTOINSTPARAM)
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
        _log(f"aall: AUTOINSTPARAM done ({len(emacs.find_auto_markers(lines, 'AUTOINSTPARAM'))} instance(s))")
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
        _log(f"aall: AUTOINST done ({len(emacs.find_auto_markers(lines, 'AUTOINST'))} instance(s))")
    else:
        print(f"warning: AUTOINST skipped, module file not found for: {step_missing}", file=sys.stderr)

    # 3. AUTOINST done above; 4. AUTOASCIIENUM; 5-8. AUTOINOUTMODPORT/MODULE/
    # COMP/IN; 9. AUTOINOUTPARAM — all before AIO so their declarations are
    # visible to the wire/reg passes
    lines = misc.auto_ascii_enum(lines)
    _log("aall: AUTOASCIIENUM done")
    lines = xfer.auto_inoutmodport(lines, ifaces)
    _log("aall: AUTOINOUTMODPORT done")
    # 3. AIO (AUTOOUTPUT/AUTOINPUT/AUTOINOUT, emacs order) / 4. AW / 5. AREG /
    # 6. AD (autodef) share the plain port mapping; AIO runs first so the
    # new port declarations are visible to AW/AREG/ADT
    modules_w = {
        n: parse_module_ports(s, typedef_regexp=td_re, interfaces=interfaces)
        for n in names_wire
        if (s := src_of(n))
    }
    lines = xfer.auto_inoutmodule(lines, modules_w)
    lines = xfer.auto_inoutcomp(lines, modules_w)
    lines = xfer.auto_inoutin(lines, modules_w)
    _log("aall: AUTOINOUTMODULE/COMP/IN done")
    # AUTOINOUTPARAM copies the submodule's parameters: needs with_params
    if xfer._find_markers("\n".join(lines), "AUTOINOUTPARAM"):
        modules_p = {
            n: parse_module_ports(s, with_params=True, typedef_regexp=td_re, interfaces=interfaces)
            for n in names_wire
            if (s := src_of(n))
        }
    else:
        modules_p = modules_w
    lines = xfer.auto_inoutparam(lines, modules_p)
    _log("aall: AUTOINOUTPARAM done")
    lines = inout.auto_output(lines, modules_w)
    lines = inout.auto_input(lines, modules_w)
    lines = inout.auto_inout(lines, modules_w)
    _log("aall: AUTOOUTPUT/AUTOINPUT/AUTOINOUT done")
    # 10. AUTOTIEOFF; 11. AUTOUNDEF; 12. AUTOASSIGNMODPORT
    lines = misc.auto_tieoff(lines, modules_w)
    _log("aall: AUTOTIEOFF done")
    lines = misc.auto_undef(lines)
    _log("aall: AUTOUNDEF done")
    lines = xfer.auto_assign_modport(lines, ifaces)
    _log("aall: AUTOASSIGNMODPORT done")
    # 13. AUTOLOGIC; 14. AUTOWIRE; 15. AUTOREG; 16. AUTOREGINPUT;
    # 17. AUTOOUTPUTEVERY
    lines = misc.auto_logic(lines, modules_w)
    _log("aall: AUTOLOGIC done")
    lines = wire.auto_wire(lines, modules_w)
    lines = wire.auto_reg(lines, modules_w)
    lines = autodef.auto_def_t(lines, modules_w)
    _log("aall: AUTOWIRE/AUTOREG/autodef done")
    lines = xfer.auto_reginput(lines, modules_w)
    _log("aall: AUTOREGINPUT done")
    lines = xfer.auto_outputevery(lines)
    _log("aall: AUTOOUTPUTEVERY done")
    # 18. AUTOSENSE/AS; 19. AUTORESET; 20. AUTOUNUSED
    lines = sense.auto_sense(lines)
    _log("aall: AUTOSENSE done")
    lines = sense.auto_reset(lines)
    _log("aall: AUTORESET done")
    lines = misc.auto_unused(lines, modules_w)
    _log("aall: AUTOUNUSED done")
    # 21. AR (autoarg) / 22. AUTOINSERTLAST / 23. AF (all format).  AR uses
    # the module table only for the inout inference on direction-less
    # port-list names.
    lines = arg.auto_arg(lines, modules_w)
    _log("aall: autoarg done")
    lines = misc.auto_insert_last(lines)
    _log("aall: AUTOINSERTLAST done")
    lines = fmt.all_format(lines)
    _log("aall: format done")
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
        interfaces = interfaces_for(lines, libdirs, args.interface)
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
    _LINE_CMDS = ("ait", "aiu", "aiu1")
    if args.line is not None and args.which is not None:
        raise SystemExit("--line and --which are mutually exclusive")
    if args.line is not None and args.command not in _LINE_CMDS:
        raise SystemExit(
            "--line applies only to: " + "/".join(_LINE_CMDS)
        )
    if args.command in ("eai", "ait", "aall", "ainj") and not args.param_value:
        # the buffer's own Local Variables can switch param-value
        # substitution on, like emacs file-local variables
        if re.search(r"^\s*//\s*verilog-auto-inst-param-value\s*:\s*t\b", text, re.M):
            args.param_value = True
    if args.command == "aall":
        _t0 = time.monotonic()
        out = _main_aall(text, lines, args)
        _log(f"aall: done, {len(lines)} -> {len(out)} line(s), {time.monotonic() - _t0:.1f}s total")
    elif args.command == "ainj":
        # verilog-inject-auto + verilog-auto: insert AUTOARG/AS/AUTOINST
        # markers into legacy code, then run the full pipeline
        from . import inject as _inject
        _t0 = time.monotonic()
        lines = _inject.inject_auto(lines)
        text = "\n".join(lines)
        out = _main_aall(text, lines, args)
        _log(f"ainj: done, {time.monotonic() - _t0:.1f}s total")
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
    elif args.command in ("eai", "eap", "ait"):
        if args.line is not None:
            # --line is gated to ait/aiu/aiu1; ait is the only carrier here
            from . import emacs

            markers = emacs.find_auto_markers(lines, "AUTOINST")
            ordinal = _line_to_ordinal(
                lines, [mk.offset for mk in markers], args.line
            )
            if ordinal < 0:
                raise SystemExit(
                    f"no /*autoinst*/ instance contains line {args.line}"
                )
            args.which = ordinal
        if args.command == "ait":
            # deprecated: the Vim (automatic.vim) full regeneration used to
            # discard hand-written connections (.clk(rx_clk) -> .clk(clk));
            # ait now delegates to the emacs expansion, which keeps them.
            # The Vim engine (VerilogBuffer.auto_inst) stays for AIU's stub
            # expansion, but is no longer exposed as a verb of its own.
            print(
                "warning: ait is deprecated; delegating to eai "
                "(emacs-style AUTOINST expansion)",
                file=sys.stderr,
            )
        out = _main_emacs(
            "eai" if args.command == "ait" else args.command, text, lines, args
        )
    else:
        if args.line is not None:
            ml = VerilogBuffer(lines).markers()
            ordinal = _line_to_ordinal(
                lines, _marker_offsets(lines, ml), args.line
            )
            if ordinal < 0:
                raise SystemExit(
                    f"no /*autoinst*/ instance contains line {args.line}"
                )
            args.which = ordinal
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
                interfaces=interfaces_for(lines, libdirs, args.interface),
            )
            for name, path in files.items()
        }
        templates = find_auto_templates(text)
        if args.command == "aiu":
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
