"""Python rewrite of verilog-mode's "copy I/O from elsewhere" AUTO family.

Implements ``verilog-auto-inout-module``, ``verilog-auto-inout-comp``,
``verilog-auto-inout-in``, ``verilog-auto-inout-param``,
``verilog-auto-inout-modport``, ``verilog-auto-assign-modport``,
``verilog-auto-output-every`` and ``verilog-auto-reg-input``:

- ``/*AUTOINOUTMODULE("Mod"[,"re"[,"dir-re"[,"not-re"]]])*/`` copies the
  input/output/inout/interface port declarations of another module into
  this one — for null templates and shell modules.  Ports already declared
  in this module (per direction) are not redeclared.
- ``/*AUTOINOUTCOMP(...)`` — inputs become outputs and vice versa (bench
  shells); ``/*AUTOINOUTIN(...)`` — everything becomes an input
  (monitors).  Same region header as AUTOINOUTMODULE.
- ``/*AUTOINOUTPARAM("Mod"[,"re"])`` — copies ``parameter`` declarations
  (valueless, SystemVerilog 2009 style).
- ``/*AUTOINOUTMODPORT("If","mp-re"[,"re"[,"prefix"]])`` — port declarations
  for an interface modport's signals (clocking blocks expanded).
- ``/*AUTOASSIGNMODPORT("If","mp-re","inst"[,"re"[,"prefix"]])`` — the
  ``assign`` statements wiring those signals to an interface instance.
- ``/*AUTOOUTPUTEVERY[("re")]`` — every non-port signal of the module
  becomes an ``output``.
- ``/*AUTOREGINPUT*/`` — ``reg`` for undeclared nets feeding input/inout
  ports of /*autoinst*/ instances (top-level test shells).

Each ``auto_*`` is idempotent: it kills its own region first, then expands.
Each ``kill_*`` deletes only its own region, keeping the marker line.

Fidelity decisions vs verilog-mode (in addition to the conventions shared
with :mod:`verilog_tooling.wire` / :mod:`verilog_tooling.inout`):

- declaration lines use verilog-mode's ``verilog-insert-one-definition``
  columns exactly: the name goes at ``max(24, indent+16)`` (a single space
  is added when the type text already reached the column) and a trailing
  comment at ``max(48, indent+40)`` — byte-identical to emacs after
  untabify;
- markers are expanded in buffer order, each seeing the declarations
  emitted by earlier markers (like verilog-mode's modi cache); the text is
  spliced back-to-front so offsets stay valid;
- a ``// Beginning of automatic ...`` header with no following
  ``// End of automatics`` is left alone by ``kill_*`` (the stale-region
  artifact emacs itself leaves behind, cf. verilog-mode's
  tests/autoinoutcomp.v);
- the modport regexp is anchored ``^...$``
  (``verilog-modi-modport-lookup``);
- the direction regexp matches ``"<dir> <signed> <multidim><bits>"`` exactly
  like ``verilog-signals-matching-dir-re`` — the data type is NOT part of
  the match string (the elisp docstring's "output logic" example
  notwithstanding);
- interface *variable* declarations never contribute a data type
  (``verilog-read-decls`` only records typedefs there, and only when
  ``verilog-typedef-regexp`` is set); submodule *port* types follow the
  ``verilog-signals-edit-wire-reg`` rule (``wire``/``reg`` dropped,
  ``logic`` and typedefs kept);
- AUTOOUTPUTEVERY does not turn `define names into outputs (emitting
  ``output `FOO;`` is never valid Verilog).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from .autodef import Signal, get_all_defs, get_all_paras
from .comments import mask_comments
from .inout import _in_paren, _repair_close_comma, _repair_open_comma
from .inst import ModuleDef, Port, VerilogBuffer, parse_module_ports
from .template import _auto_re_to_python
from .wire import (
    _END_OF_AUTOMATICS,
    _inst_driven_nets,
    _module_tables,
    _wire_comment_enabled,
)

# ---------------------------------------------------------------------------
# markers & region headers

_INOUTMODULE_MARK = re.compile(r"/\*\s*autoinoutmodule\b", re.IGNORECASE)
_INOUTCOMP_MARK = re.compile(r"/\*\s*autoinoutcomp\b", re.IGNORECASE)
_INOUTIN_MARK = re.compile(r"/\*\s*autoinoutin\b", re.IGNORECASE)
_INOUTPARAM_MARK = re.compile(r"/\*\s*autoinoutparam\b", re.IGNORECASE)
_INOUTMODPORT_MARK = re.compile(r"/\*\s*autoinoutmodport\b", re.IGNORECASE)
_ASSIGNMODPORT_MARK = re.compile(r"/\*\s*autoassignmodport\b", re.IGNORECASE)
_OUTPUTEVERY_MARK = re.compile(r"/\*\s*autooutputevery\b", re.IGNORECASE)
_REGINPUT_MARK = re.compile(r"/\*\s*autoreginput\b", re.IGNORECASE)

_INOUTMODULE_HEADER = "// Beginning of automatic in/out/inouts (from specific module)"
_INOUTPARAM_HEADER = "// Beginning of automatic parameters (from specific module)"
_INOUTMODPORT_HEADER = "// Beginning of automatic in/out/inouts (from modport)"
_ASSIGNMODPORT_HEADER = "// Beginning of automatic assignments from modport"
_OUTPUTEVERY_HEADER = "// Beginning of automatic outputs (every signal)"
_REGINPUT_HEADER = (
    "// Beginning of automatic reg inputs (for undeclared instantiated-module inputs)"
)

_INOUTMODULE_HEADER_RE = re.compile(
    r"^\s*// Beginning of automatic in/out/inouts \(from specific module\)"
)
_INOUTPARAM_HEADER_RE = re.compile(
    r"^\s*// Beginning of automatic parameters \(from specific module\)"
)
_INOUTMODPORT_HEADER_RE = re.compile(
    r"^\s*// Beginning of automatic in/out/inouts \(from modport\)"
)
_ASSIGNMODPORT_HEADER_RE = re.compile(
    r"^\s*// Beginning of automatic assignments from modport"
)
_OUTPUTEVERY_HEADER_RE = re.compile(
    r"^\s*// Beginning of automatic outputs \(every signal\)"
)
_REGINPUT_HEADER_RE = re.compile(r"^\s*// Beginning of automatic reg inputs\b")

_ANY_BEGIN_RE = re.compile(r"^\s*// Beginning of automatic\b")


@dataclass(frozen=True)
class _Marker:
    """A /*AUTO...*/ marker with its quoted string arguments."""

    offset: int  # text offset of '/*'
    end: int  # text offset just past '*/'
    args: tuple[str, ...]


def _xfer_marker_re(keyword: str) -> "re.Pattern[str]":
    # like verilog-read-auto-params: ("p1" , "p2") or ("p1" "p2") —
    # quoted strings, comma optional
    return re.compile(
        r"/\*\s*" + keyword + r"\b\s*(?:\(\s*((?:\"(?:[^\"\\]|\\.)*\"\s*,?\s*)*)\))?\s*\*/",
        re.IGNORECASE,
    )


def _unescape_arg(arg: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(arg):
        if arg[i] == "\\" and i + 1 < len(arg):
            out.append(arg[i + 1])
            i += 2
        else:
            out.append(arg[i])
            i += 1
    return "".join(out)


def _find_markers(text: str, keyword: str) -> list[_Marker]:
    """All /*KEYWORD(...)*/ markers with every quoted argument, skipping
    ones inside // comments."""
    masked = mask_comments(text, block=False)
    out: list[_Marker] = []
    for m in _xfer_marker_re(keyword).finditer(text):
        if masked[m.start()] == " ":
            continue  # inside a // comment
        args = tuple(
            _unescape_arg(a)
            for a in re.findall(r"\"((?:[^\"\\]|\\.)*)\"", m.group(1) or "")
        )
        out.append(_Marker(offset=m.start(), end=m.end(), args=args))
    return out


# ---------------------------------------------------------------------------
# kill (only header -> // End of automatics, and only when the closer exists)


def _kill_region(lines: Sequence[str], header_re: "re.Pattern[str]") -> list[str]:
    """Delete the generated region from the HEADER_RE line through the first
    ``// End of automatics``.  A header with no closer is left alone (it is
    emacs's own stale-region artifact, not a live region); the scan also
    stops at any other ``// Beginning of automatic`` header."""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if header_re.match(lines[i]):
            j = i + 1
            while (
                j < n
                and _END_OF_AUTOMATICS not in lines[j]
                and not _ANY_BEGIN_RE.match(lines[j])
            ):
                j += 1
            if j < n and _END_OF_AUTOMATICS in lines[j]:
                i = j + 1
                continue
            out.append(lines[i])
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def kill_auto_inoutmodule(lines: Sequence[str]) -> list[str]:
    """Delete /*AUTOINOUTMODULE*/ (and COMP/IN — same header) regions."""
    return _kill_region(lines, _INOUTMODULE_HEADER_RE)


kill_auto_inoutcomp = kill_auto_inoutmodule
kill_auto_inoutin = kill_auto_inoutmodule


def _kill_region_after_marker(text: str, marker: _Marker, header: str) -> str:
    """Delete the generated region (HEADER .. ``// End of automatics``) that
    follows MARKER's line, leaving other markers' regions alone.

    This mirrors emacs: each ``verilog-auto-inout-*`` deletes only the region
    after its own marker.  A header with no closer is left in place (emacs's
    own stale-region artifact).
    """
    nl = text.find("\n", marker.end)
    if nl == -1:
        return text
    lines = text.split("\n")
    # line index of the marker's line
    mline = text.count("\n", 0, marker.end)
    i = mline + 1
    # the header should be the first non-blank line after the marker
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or header not in lines[i]:
        return text
    j = i + 1
    while j < len(lines) and _END_OF_AUTOMATICS not in lines[j]:
        # stop at another region's header — don't eat a sibling region
        if _ANY_BEGIN_RE.match(lines[j]):
            return text
        j += 1
    if j >= len(lines):
        return text  # no closer: stale header, leave it
    del lines[i : j + 1]
    return "\n".join(lines)


def kill_auto_inoutparam(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOINOUTPARAM*/ region, keeping the marker."""
    return _kill_region(lines, _INOUTPARAM_HEADER_RE)


def kill_auto_inoutmodport(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOINOUTMODPORT*/ region, keeping the marker."""
    return _kill_region(lines, _INOUTMODPORT_HEADER_RE)


def kill_auto_assign_modport(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOASSIGNMODPORT*/ region, keeping the marker."""
    return _kill_region(lines, _ASSIGNMODPORT_HEADER_RE)


def kill_auto_outputevery(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOOUTPUTEVERY*/ region, keeping the marker."""
    return _kill_region(lines, _OUTPUTEVERY_HEADER_RE)


def kill_auto_reginput(lines: Sequence[str]) -> list[str]:
    """Delete the /*AUTOREGINPUT*/ region, keeping the marker."""
    return _kill_region(lines, _REGINPUT_HEADER_RE)


# ---------------------------------------------------------------------------
# signal helpers


def _xfer_signal(
    name: str,
    packed: Sequence[str],
    signed: bool = False,
    net_type: str = "",
    data_type: str = "",
) -> Signal:
    """autodef Signal for a copied declaration: PACKED holds every packed
    range text in declaration order (``("3:0", "7:0")``) — ranges are
    re-emitted verbatim (``[3:1]`` stays ``[3:1]``), like verilog-mode."""
    width = "c0"
    if packed:
        width = packed[0].split(":")[0].strip()
    return Signal(
        width=width, type="xfer", name=name, packed_dims=tuple(packed),
        signed=signed, net_type=net_type, data_type=data_type,
    )


def _type_text(keyword: str, sig: Signal) -> str:
    """The ``type`` text verilog-insert-one-definition inserts: the direction
    keyword plus data type, signed and packed ranges, e.g. ``"output"``,
    ``"output logic [7:0]"``, ``"input [3:0] [7:0]"``, ``"my_svi.master"``."""
    text = keyword
    if sig.data_type:
        text += " " + sig.data_type
    elif sig.net_type:
        text += " " + sig.net_type
    if sig.signed:
        text += " signed"
    if sig.packed_dims:
        for d in sig.packed_dims:
            text += f" [{d}]"
    elif sig.width not in ("c0", ""):
        text += f" [{sig.width}:0]"
    return text


def _emit_def_line(
    indent: int, type_text: str, name: str, v2k: bool, comment: str = ""
) -> str:
    """One declaration line with verilog-insert-one-definition columns: the
    name goes at ``max(24, indent+16)`` (one space is added when the type
    text already reached the column), a trailing comment at
    ``max(48, indent+40)``.  V2K uses ``,`` instead of ``;``."""
    name_col = max(24, indent + 16)
    line = " " * indent + type_text
    if len(line) < name_col:
        line += " " * (name_col - len(line))
    elif not line.endswith(" "):
        # indent-to did nothing (type already at/past the column)
        line += " "
    line += name + ("," if v2k else ";")
    if comment:
        comment_col = max(48, indent + 40)
        if len(line) < comment_col:
            line += " " * (comment_col - len(line))
        line += comment
    return line


def _port_signal(port: Port) -> Signal:
    """Signal for a submodule :class:`Port`."""
    return _xfer_signal(
        port.name, port.packed, port.signed, port.net_type, port.data_type
    )


def _drop_wire_reg(sig: Signal) -> Signal:
    """verilog-signals-edit-wire-reg: blank wire/reg data types (``output
    reg`` -> ``output``; ``output logic`` and typedefs survive)."""
    if sig.net_type in ("wire", "reg"):
        sig.net_type = ""
    if sig.data_type in ("wire", "reg"):
        sig.data_type = ""
    return sig


def _filter_name(sigs: list[Signal], regexp: str | None) -> list[Signal]:
    """verilog-signals-matching-regexp (``?!`` inverts, case-insensitive)."""
    from . import emacs

    return emacs._filter_regexp(sigs, regexp)


def _filter_not(sigs: list[Signal], not_re: str | None) -> list[Signal]:
    """verilog-signals-not-matching-regexp."""
    if not not_re:
        return list(sigs)
    if not_re.startswith("?!"):
        # elisp double negation: not-matching with ?! -> matching
        return _filter_name(sigs, not_re[2:])
    rx = re.compile(_auto_re_to_python(not_re), re.IGNORECASE)
    return [s for s in sigs if not rx.search(s.name)]


def _dir_match_str(direction: str, sig: Signal) -> str:
    """The string ``verilog-signals-matching-dir-re`` matches against:
    ``"<dir> <signed> <multidim><bits>"`` (bits is the last packed range)."""
    bits = ""
    multidim = ""
    if sig.packed_dims:
        bits = f"[{sig.packed_dims[-1]}]"
        multidim = "".join(f"[{d}]" for d in sig.packed_dims[:-1])
    elif sig.width != "c0":
        bits = f"[{sig.width}:0]"
    signed = "signed" if sig.signed else ""
    return f"{direction} {signed} {multidim}{bits}"


def _filter_dir(sigs: list[Signal], direction: str, dir_re: str | None) -> list[Signal]:
    """verilog-signals-matching-dir-re (no ``?!`` handling in elisp)."""
    if not dir_re:
        return list(sigs)
    rx = re.compile(_auto_re_to_python(dir_re), re.IGNORECASE)
    return [s for s in sigs if rx.search(_dir_match_str(direction, s))]


def _warn(msg: str) -> None:
    print(f"[verilog_tooling] xfer: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# region splicing (shared by every marker family)


def _splice_region(
    text: str,
    marker: _Marker,
    header: str,
    decls: Sequence[tuple[str, Signal, str]],
    v2k: "bool | None" = None,
) -> str:
    """Splice one generated region after MARKER's line.  DECLS is
    ``(keyword, signal, comment)`` per declaration line; V2K (marker inside
    the module header parens) uses comma style with verilog-mode's open/close
    comma repairs.  Empty DECLS still runs the repairs, like verilog-mode.
    V2K may be forced (AUTOREGINPUT is always Verilog-1995 style)."""
    masked = mask_comments(text)
    if v2k is None:
        v2k = _in_paren(masked, marker.offset)
    shift = 0
    if v2k:
        text, masked, shift = _repair_open_comma(text, masked, marker.offset)
    offset = marker.offset + shift
    end = marker.end + shift
    line_start = text.rfind("\n", 0, offset) + 1
    indent = len(re.match(r"[ \t]*", text[line_start:]).group(0))
    nl = text.find("\n", end)
    if v2k:
        # the header's close paren may sit on the marker line itself
        # (``/*AUTO...*/);``): break the line after the marker so the region
        # stays INSIDE the header
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
    if decls:
        pad = " " * indent
        rows = [pad + header]
        for keyword, sig, comment in decls:
            rows.append(
                _emit_def_line(indent, _type_text(keyword, sig),
                               sig.name, v2k, comment))
        rows.append(pad + _END_OF_AUTOMATICS)
        region = "\n".join(rows) + "\n"
        text = text[:insert_at] + region + text[insert_at:]
    if v2k:
        text = _repair_close_comma(text, insert_at + len(region))
    return text

# ---------------------------------------------------------------------------
# interface parsing (for AUTOINOUTMODPORT / AUTOASSIGNMODPORT)
#
# verilog_tooling.inst.parse_interface only extracts modport names; the
# modport features need each modport's signals (with clocking-block
# expansion) and each signal's declaration (type/width), so the interface
# gets its own small parser here.


@dataclass(frozen=True)
class IfaceVar:
    """One interface variable declaration."""

    name: str
    packed: tuple[str, ...] = ()  # packed range texts, e.g. ("7:0",)
    signed: bool = False
    data_type: str = ""  # typedef name — only when verilog-typedef-regexp matched


@dataclass(frozen=True)
class ModportDef:
    """A modport (or clocking block): signal names per direction plus, for a
    modport, the clocking blocks it references."""

    name: str
    clockings: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    inouts: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterfaceInfo:
    """The parts of an interface definition the modport AUTOs need."""

    name: str
    header_ports: tuple[str, ...] = ()  # (input logic clk) header names
    vars: tuple[IfaceVar, ...] = ()
    modports: tuple[ModportDef, ...] = ()
    clockings: tuple[ModportDef, ...] = ()


_IFACE_RE = re.compile(r"\binterface\s+(\w+)")
_ENDIFACE_RE = re.compile(r"\bendinterface\b")
_CLOCKING_RE = re.compile(r"^\s*clocking\s+(\w+)\b")
_ENDCLOCKING_RE = re.compile(r"^\s*endclocking\b")
_MODPORT_HEAD_RE = re.compile(r"^\s*modport\s+(\w+)\s*\(")
_DIR_ITEM_RE = re.compile(r"^\s*(input|output|inout)\b\s*(.*?)\s*$")
_IFACE_VAR_RE = re.compile(
    r"^\s*([a-zA-Z_]\w*)\s+(signed\s+)?((?:\[[^\]]+\]\s*)+)?"
    r"([a-zA-Z_]\w*(?:\s*,\s*[a-zA-Z_]\w*)*)\s*;\s*$"
)
_IFACE_DECL_KW = frozenset(
    "modport clocking endclocking function task generate endgenerate "
    "initial always assign assert property sequence cover".split()
)


def buffer_interface_defs(text: str) -> dict[str, list[str]]:
    """{name: block lines} for every ``interface ... endinterface`` block in
    TEXT (the buffer fallback of verilog-modi-lookup)."""
    lines = text.splitlines()
    masked = mask_comments(text).splitlines()
    out: dict[str, list[str]] = {}
    i, n = 0, len(lines)
    while i < n:
        m = _IFACE_RE.search(masked[i])
        if m:
            name = m.group(1)
            start = i
            i += 1
            while i < n and not _ENDIFACE_RE.search(masked[i]):
                i += 1
            out.setdefault(name, lines[start : i + 1])
        i += 1
    return out


def _split_top(text: str, delim: str) -> list[str]:
    """Split TEXT on DELIM at paren/bracket/brace depth 0."""
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == delim and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _header_port_names(text: str) -> list[str]:
    """Signal names of the interface's ``(...)`` header port list."""
    m = re.search(r"\binterface\s+\w+\s*(?:#\s*\([^;]*\)\s*)?\(", text)
    if not m:
        return []
    depth = 0
    j = m.end() - 1
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    inner = text[m.end() : j]
    names: list[str] = []
    for part in _split_top(inner, ","):
        part = part.strip()
        if re.match(r"(input|output|inout)\b", part):
            words = re.findall(r"[a-zA-Z_]\w*", part)
            if words:
                names.append(words[-1])
    return names


def _parse_modport_items(inner: str, name: str) -> ModportDef:
    """Parse a modport's ``(...)`` item list into a :class:`ModportDef`."""
    clockings: list[str] = []
    buckets: dict[str, list[str]] = {"input": [], "output": [], "inout": []}
    for item in _split_top(inner, ","):
        item = item.strip()
        if not item:
            continue
        mc = re.match(r"^clocking\s+(\w+)$", item)
        if mc:
            clockings.append(mc.group(1))
            continue
        if re.match(r"^import\b", item):
            continue  # a method import, not a signal
        md = _DIR_ITEM_RE.match(item)
        if md:
            names = [w for w in re.findall(r"[a-zA-Z_]\w*", md.group(2))]
            buckets[md.group(1)].extend(names)
    return ModportDef(
        name,
        tuple(clockings),
        tuple(buckets["input"]),
        tuple(buckets["output"]),
        tuple(buckets["inout"]),
    )


def buffer_interfaces(
    lines: Sequence[str], typedef_re: "str | None" = None
) -> "dict[str, InterfaceInfo]":
    """Every ``interface`` block defined in the buffer itself, name →
    :class:`InterfaceInfo`.  Buffer definitions shadow same-named library
    interfaces (verilog-mode reads the current buffer first)."""
    found: dict[str, InterfaceInfo] = {}
    n = len(lines)
    i = 0
    while i < n:
        if re.match(r"^\s*interface\s+\w+", lines[i]):
            j = i + 1
            while j < n and not re.match(r"^\s*endinterface\b", lines[j]):
                j += 1
            try:
                info = parse_interface_info(lines[i : j + 1], typedef_re)
                found[info.name] = info
            except ValueError:
                pass
            i = j + 1
        else:
            i += 1
    return found


def parse_interface_info(
    lines: Sequence[str], typedef_re: "str | None" = None
) -> InterfaceInfo:
    """Parse an interface definition into an :class:`InterfaceInfo`.

    Raises ValueError when no ``interface <name>`` header is found.
    TYPEDEF_RE (verilog-typedef-regexp) selects which variable type words
    are kept as data types — like verilog-read-decls, plain ``logic`` /
    ``wire`` / ... never survive on variables."""
    text = "\n".join(lines)
    masked = mask_comments(text)
    m = _IFACE_RE.search(masked)
    if not m:
        raise ValueError("no interface declaration found")
    name = m.group(1)
    mlines = masked.splitlines()
    # block extent (masked search, original lines kept)
    start = next(i for i, l in enumerate(mlines) if _IFACE_RE.search(l))
    end = next(
        (i for i in range(start + 1, len(mlines)) if _ENDIFACE_RE.search(mlines[i])),
        len(mlines) - 1,
    )
    header_ports = _header_port_names(text)
    tdx = re.compile(typedef_re) if typedef_re else None

    vars_: list[IfaceVar] = []
    modports: list[ModportDef] = []
    clockings: list[ModportDef] = []
    i = start + 1
    cur_clk: str | None = None
    clk_buckets: dict[str, list[str]] | None = None
    while i < end:
        line = mlines[i]
        s = line.strip()
        if cur_clk is not None:
            if _ENDCLOCKING_RE.match(line):
                assert clk_buckets is not None
                clockings.append(
                    ModportDef(
                        cur_clk, (), tuple(clk_buckets["input"]),
                        tuple(clk_buckets["output"]), tuple(clk_buckets["inout"]),
                    )
                )
                cur_clk = None
                clk_buckets = None
            else:
                dm = _DIR_ITEM_RE.match(line)
                if dm and clk_buckets is not None:
                    names = re.findall(r"[a-zA-Z_]\w*", dm.group(2).split(";")[0])
                    clk_buckets[dm.group(1)].extend(names)
            i += 1
            continue
        mc = _CLOCKING_RE.match(line)
        if mc:
            cur_clk = mc.group(1)
            clk_buckets = {"input": [], "output": [], "inout": []}
            i += 1
            continue
        mm = _MODPORT_HEAD_RE.match(line)
        if mm:
            mp_name = mm.group(1)
            # accumulate until the parens balance (multi-line modports)
            buf = line[mm.end() - 1 :]
            depth = buf.count("(") - buf.count(")")
            j = i + 1
            while depth > 0 and j < end:
                buf += " " + mlines[j]
                depth += buf.count("(") - buf.count(")")
                j += 1
            inner = buf[1 : buf.rfind(")")]
            modports.append(_parse_modport_items(inner, mp_name))
            i = j
            continue
        vm = _IFACE_VAR_RE.match(line)
        if vm and vm.group(1) not in _IFACE_DECL_KW:
            type_word, signed, brackets, namelist = (
                vm.group(1), bool(vm.group(2)), vm.group(3) or "", vm.group(4)
            )
            packed = tuple(b.strip()[1:-1] for b in re.findall(r"\[[^\]]+\]", brackets))
            data_type = type_word if (tdx and tdx.search(type_word)) else ""
            for vname in re.findall(r"[a-zA-Z_]\w*", namelist):
                vars_.append(
                    IfaceVar(vname, packed, signed=bool(signed), data_type=data_type)
                )
        i += 1
    return InterfaceInfo(name, tuple(header_ports), tuple(vars_), tuple(modports), tuple(clockings))


def _modport_lookup(
    info: InterfaceInfo, modport_re: str
) -> tuple[list[str], list[str], list[str]]:
    """(inputs, outputs, inouts) of every modport matching ``^re$``
    (``verilog-modi-modport-lookup``), with referenced clocking blocks
    expanded recursively."""
    rx = re.compile("^" + _auto_re_to_python(modport_re) + "$", re.IGNORECASE)
    clk_by_name = {c.name: c for c in info.clockings}
    inputs: list[str] = []
    outputs: list[str] = []
    inouts: list[str] = []

    def collect(mp: ModportDef, seen: set[str]) -> None:
        for cname in mp.clockings:
            if cname in seen:
                continue
            seen.add(cname)
            clk = clk_by_name.get(cname)
            if clk is not None:
                collect(clk, seen)
        inputs.extend(mp.inputs)
        outputs.extend(mp.outputs)
        inouts.extend(mp.inouts)

    for mp in list(info.modports) + list(info.clockings):
        if rx.match(mp.name):
            collect(mp, set())

    def dedup(xs: list[str]) -> list[str]:
        return list(dict.fromkeys(xs))

    return dedup(inputs), dedup(outputs), dedup(inouts)


def _modport_var_signals(
    info: InterfaceInfo, names: Sequence[str]
) -> list[Signal]:
    """Resolve modport signal NAMES against the interface variable
    declarations (``verilog-signals-in``); header ports and undeclared names
    are dropped.  Variable data types never survive (see module docstring)."""
    header = set(info.header_ports)
    var_by_name = {v.name: v for v in info.vars}
    out: list[Signal] = []
    for n in names:
        if n in header:
            # "If a signal is part of the interface header and in both a
            # modport and the interface itself, it will not be listed."
            continue
        v = var_by_name.get(n)
        if v is None:
            continue
        out.append(
            _xfer_signal(v.name, v.packed, v.signed, data_type=v.data_type)
        )
    return out

# ---------------------------------------------------------------------------
# shared per-module declared sets


def _declared_io(span: Sequence[str]) -> "tuple[set[str], set[str], set[str], set[str]]":
    """(inputs, outputs, inouts, interface-ports) declared in this module
    span — the per-direction exclusion sets of AUTOINOUTMODULE."""
    ports, _, _ = _module_tables(span)
    ins = {n for n, s in ports.signals.items() if s.io_dir == "input"}
    outs = {n for n, s in ports.signals.items() if s.io_dir == "output"}
    ios = {n for n, s in ports.signals.items() if s.io_dir == "inout"}
    ifs = set(
        re.findall(
            r"^\s*[a-zA-Z_]\w*\.[a-zA-Z_]\w*\s+([a-zA-Z_]\w*)\s*[,;]",
            "\n".join(span),
            re.M,
        )
    )
    return ins, outs, ios, ifs


# ---------------------------------------------------------------------------
# AUTOINOUTMODULE / AUTOINOUTCOMP / AUTOINOUTIN (verilog-auto-inout-module)


def _inoutmodule_decls(
    label: str,
    marker: _Marker,
    modules: Mapping[str, ModuleDef],
    declared: "tuple[set[str], set[str], set[str], set[str]]",
    emitted: dict[str, set[str]],
    complement: bool,
    all_in: bool,
) -> list[tuple[str, str, Signal]]:
    """(direction, keyword, signal) for one marker, in verilog-mode's
    output→inout→input→interface order (never sorted).  EMITTED accumulates
    per-direction names for later markers (the modi-cache equivalent)."""
    args = marker.args
    if not args:
        _warn(f"{label}: missing module name argument, skipped")
        return []
    moddef = modules.get(args[0])
    if moddef is None:
        return []  # warned at resolution time
    regexp = args[1] if len(args) > 1 else ""
    dir_re = args[2] if len(args) > 2 else ""
    not_re = args[3] if len(args) > 3 else ""
    mod_ins, mod_outs, mod_ios, mod_ifs = declared

    def bucket(want: str) -> list[Port]:
        return [p for p in moddef.ports if p.direction == want]

    if all_in:
        i_src = bucket("input") + bucket("inout") + bucket("output")
        o_src: list[Port] = []
        io_src: list[Port] = []
    elif complement:
        i_src, o_src, io_src = bucket("output"), bucket("input"), bucket("inout")
    else:
        i_src, o_src, io_src = bucket("input"), bucket("output"), bucket("inout")
    if_src = bucket("interface")

    def filt(
        src: list[Port], direction: str, excluded: set[str]
    ) -> list[tuple[Port, Signal]]:
        pairs = [(p, _port_signal(p)) for p in src if p.name not in excluded]
        sigs = [s for _, s in pairs]
        sigs = _filter_name(sigs, regexp)
        sigs = _filter_dir(sigs, direction, dir_re)
        sigs = _filter_not(sigs, not_re)
        keep = {id(s) for s in sigs}
        return [(p, s) for p, s in pairs if id(s) in keep]

    decls: list[tuple[str, str, Signal]] = []
    for direction, src, base in (
        ("output", o_src, mod_outs),
        ("inout", io_src, mod_ios),
        ("input", i_src, mod_ins),
    ):
        for p, sig in filt(src, direction, base | emitted[direction]):
            decls.append((direction, direction, _drop_wire_reg(sig)))
            emitted[direction].add(p.name)
    for p, sig in filt(if_src, "interface", mod_ifs | emitted["interface"]):
        keyword = p.iface + ("." + p.modport if p.modport else "") if p.iface else "interface"
        decls.append(("interface", keyword, sig))
        emitted["interface"].add(p.name)
    return decls


def _auto_inout_x(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    keyword: str,
    mark_re: "re.Pattern[str]",
    label: str,
    complement: bool = False,
    all_in: bool = False,
) -> list[str]:
    # NOTE: no global kill here — AUTOINOUTMODULE/COMP/IN share one header,
    # so a global kill would delete sibling markers' regions.  Each marker's
    # old region is deleted marker-scoped in _single (like emacs).
    from .inst import map_module_spans

    def _single(span: Sequence[str]) -> list[str]:
        text = "\n".join(span)
        markers = _find_markers(text, keyword)
        # Kill each marker's old region FIRST (marker-scoped), so the
        # previously generated declarations are not mistaken for
        # user-declared signals when computing `declared` below.
        # Reversed: later markers first, offsets of earlier ones stay valid.
        for mk in reversed(markers):
            text = _kill_region_after_marker(text, mk, _INOUTMODULE_HEADER)
        # Re-find markers and rebuild plans on the cleaned text (offsets
        # shifted by the kills above).
        markers = _find_markers(text, keyword)
        declared = _declared_io(text.split("\n"))
        emitted = {"input": set(), "output": set(), "inout": set(), "interface": set()}
        plans: list[tuple[_Marker, list[tuple[str, str, Signal]]]] = []
        for mk in markers:
            plans.append(
                (mk, _inoutmodule_decls(label, mk, modules, declared, emitted, complement, all_in))
            )
        # reversed: later markers first, so earlier markers' offsets stay
        # valid for _splice_region.
        for mk, decls in reversed(plans):
            text = _splice_region(
                text, mk, _INOUTMODULE_HEADER,
                [(kw, s, "") for _, kw, s in decls],
            )
        return text.split("\n")

    return map_module_spans(lines, mark_re, _single)


def auto_inoutmodule(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate /*AUTOINOUTMODULE("Mod"[,"re"[,"dir-re"[,"not-re"]]])*/.

    Copies the submodule's port declarations into this module (shell/null
    template); ports already declared here are not redeclared.  MODULES maps
    module names to parsed definitions (with_params not needed here).
    """
    return _auto_inout_x(lines, modules, "AUTOINOUTMODULE", _INOUTMODULE_MARK, "AUTOINOUTMODULE")


def auto_inoutcomp(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate /*AUTOINOUTCOMP(...)*/ — inputs become outputs and vice
    versa (bench shells)."""
    return _auto_inout_x(lines, modules, "AUTOINOUTCOMP", _INOUTCOMP_MARK, "AUTOINOUTCOMP",
                         complement=True)


def auto_inoutin(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate /*AUTOINOUTIN(...)*/ — every copied port becomes an input
    (monitor shells)."""
    return _auto_inout_x(lines, modules, "AUTOINOUTIN", _INOUTIN_MARK, "AUTOINOUTIN",
                         all_in=True)


# ---------------------------------------------------------------------------
# AUTOINOUTPARAM (verilog-auto-inout-param)


def _inoutparam_decls(
    marker: _Marker,
    modules: Mapping[str, ModuleDef],
    mod_params: set[str],
    emitted: set[str],
) -> list[tuple[str, str, Signal]]:
    args = marker.args
    if not args:
        _warn("AUTOINOUTPARAM: missing module name argument, skipped")
        return []
    moddef = modules.get(args[0])
    if moddef is None:
        return []  # warned at resolution time
    regexp = args[1] if len(args) > 1 else ""
    sigs = [
        _xfer_signal(pm.name, ())
        for pm in moddef.params
        if pm.name not in mod_params and pm.name not in emitted
    ]
    sigs = _filter_name(sigs, regexp)
    for s in sigs:
        emitted.add(s.name)
    # original module order (never sorted); valueless SV2009 style
    return [("parameter", "parameter", s) for s in sigs]


def auto_inoutparam(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate /*AUTOINOUTPARAM("Mod"[,"re"])*/ — the submodule's
    ``parameter`` declarations, without values.  MODULES must hold
    definitions parsed with ``with_params=True``."""
    lines = kill_auto_inoutparam(lines)
    from .inst import map_module_spans

    def _single(span: Sequence[str]) -> list[str]:
        mod_params = get_all_paras(span)
        text = "\n".join(span)
        markers = _find_markers(text, "AUTOINOUTPARAM")
        emitted: set[str] = set()
        plans = [
            (mk, _inoutparam_decls(mk, modules, mod_params, emitted)) for mk in markers
        ]
        for mk, decls in reversed(plans):
            text = _splice_region(
                text, mk, _INOUTPARAM_HEADER, [(kw, s, "") for _, kw, s in decls]
            )
        return text.split("\n")

    return map_module_spans(lines, _INOUTPARAM_MARK, _single)


# ---------------------------------------------------------------------------
# AUTOINOUTMODPORT (verilog-auto-inout-modport)


def _inoutmodport_decls(
    marker: _Marker,
    ifaces: Mapping[str, InterfaceInfo],
    mod_ports: set[str],
    emitted: set[str],
) -> list[tuple[str, str, Signal]]:
    args = marker.args
    if len(args) < 2:
        _warn("AUTOINOUTMODPORT: need interface and modport-regexp arguments, skipped")
        return []
    ifname, mp_re = args[0], args[1]
    sig_re = args[2] if len(args) > 2 else ""
    prefix = args[3] if len(args) > 3 else ""
    info = ifaces.get(ifname)
    if info is None:
        return []  # warned at resolution time

    mi, mo, mio = _modport_lookup(info, mp_re)
    decls: list[tuple[str, str, Signal]] = []
    for direction, names in (("output", mo), ("inout", mio), ("input", mi)):
        for sig in _filter_name(_modport_var_signals(info, names), sig_re):
            pname = prefix + sig.name
            if pname in mod_ports or pname in emitted:
                # the not-in check runs AFTER prefixing (verilog-mode)
                continue
            decls.append((direction, direction, _drop_wire_reg(replace(sig, name=pname))))
            emitted.add(pname)
    return decls


def auto_inoutmodport(lines: Sequence[str], ifaces: Mapping[str, InterfaceInfo]) -> list[str]:
    """Regenerate /*AUTOINOUTMODPORT("If","mp-re"[,"re"[,"prefix"]])*/ —
    port declarations for an interface modport's signals (clocking blocks
    expanded, interface header ports excluded).  IFACES maps interface names
    to :class:`InterfaceInfo`."""
    lines = kill_auto_inoutmodport(lines)
    from .inst import map_module_spans

    def _single(span: Sequence[str]) -> list[str]:
        ports, _, _ = _module_tables(span)
        mod_ports = set(ports.signals)
        text = "\n".join(span)
        markers = _find_markers(text, "AUTOINOUTMODPORT")
        emitted: set[str] = set()
        plans = [
            (mk, _inoutmodport_decls(mk, ifaces, mod_ports, emitted)) for mk in markers
        ]
        for mk, decls in reversed(plans):
            text = _splice_region(
                text, mk, _INOUTMODPORT_HEADER, [(kw, s, "") for _, kw, s in decls]
            )
        return text.split("\n")

    return map_module_spans(lines, _INOUTMODPORT_MARK, _single)


# ---------------------------------------------------------------------------
# AUTOASSIGNMODPORT (verilog-auto-assign-modport)


def _assignmodport_rows(
    marker: _Marker, ifaces: Mapping[str, InterfaceInfo]
) -> "tuple[str, str, list[tuple[str, str]], list[tuple[str, str]]] | None":
    """(inst, prefix, outputs, inputs) for one marker; each list holds
    ``(port_signal, iface_signal)`` sorted by signal name.  None when the
    marker is unusable."""
    args = marker.args
    if len(args) < 3:
        _warn("AUTOASSIGNMODPORT: need interface, modport-regexp and instance arguments, skipped")
        return None
    ifname, mp_re, inst = args[0], args[1], args[2]
    sig_re = args[3] if len(args) > 3 else ""
    prefix = args[4] if len(args) > 4 else ""
    info = ifaces.get(ifname)
    if info is None:
        return None  # warned at resolution time
    mi, mo, _mio = _modport_lookup(info, mp_re)  # inouts unsupported, like elisp

    def resolve(names: list[str]) -> list[tuple[str, str]]:
        rows = []
        for s in _filter_name(_modport_var_signals(info, names), sig_re):
            rows.append((prefix + s.name, s.name))
        rows.sort(key=lambda r: r[1])  # verilog-signals-sort-compare, pre-prefix
        return rows

    return inst, prefix, resolve(mo), resolve(mi)


def auto_assign_modport(lines: Sequence[str], ifaces: Mapping[str, InterfaceInfo]) -> list[str]:
    """Regenerate /*AUTOASSIGNMODPORT("If","mp-re","inst"[,"re"[,"prefix"]])*/
    — ``assign`` statements wiring a modport's signals to an interface
    instance (outputs first, then inputs, each sorted by name)."""
    lines = kill_auto_assign_modport(lines)
    from .inst import map_module_spans

    def _single(span: Sequence[str]) -> list[str]:
        text = "\n".join(span)
        markers = _find_markers(text, "AUTOASSIGNMODPORT")
        plans = [(mk, _assignmodport_rows(mk, ifaces)) for mk in markers]
        for mk, plan in reversed(plans):
            if plan is None:
                continue
            inst, _prefix, outs, ins = plan
            line_start = text.rfind("\n", 0, mk.offset) + 1
            indent = len(re.match(r"[ \t]*", text[line_start:]).group(0))
            nl = text.find("\n", mk.end)
            if nl == -1:
                text += "\n"
                nl = len(text) - 1
            insert_at = nl + 1
            pad = " " * indent
            rows = [pad + _ASSIGNMODPORT_HEADER]
            for pname, vname in outs:
                rows.append(f"{pad}assign {pname} = {inst}.{vname};")
            for pname, vname in ins:
                rows.append(f"{pad}assign {inst}.{vname} = {pname};")
            rows.append(pad + _END_OF_AUTOMATICS)
            region = "\n".join(rows) + "\n"
            if outs or ins:
                text = text[:insert_at] + region + text[insert_at:]
        return text.split("\n")

    return map_module_spans(lines, _ASSIGNMODPORT_MARK, _single)


# ---------------------------------------------------------------------------
# AUTOOUTPUTEVERY (verilog-auto-output-every)


def _trailing_comments(span: Sequence[str]) -> dict[str, str]:
    """Declared-name -> its declaration line's trailing ``//...`` comment
    (verilog-sig-comment, re-emitted by verilog-insert-definition)."""
    from .autodef import _DATA_LINE, _PORT_LINE

    kw = frozenset(
        "input output inout wire reg logic parameter localparam integer genvar "
        "signed automatic".split()
    )
    out: dict[str, str] = {}
    for line in span:
        code, sep, cmt = line.partition("//")
        if not sep:
            continue
        s = code.strip()
        if not (_DATA_LINE.match(s) or _PORT_LINE.match(s)):
            continue
        s = re.split(r"=", s, maxsplit=1)[0]  # rvalues don't own the comment
        for name in re.findall(r"[a-zA-Z_]\w*", s):
            if name not in kw:
                out.setdefault(name, "//" + cmt.rstrip())
    return out


def _outputevery_decls(
    marker: _Marker,
    usrdef_sigs: list[Signal],
    port_names: set[str],
    comments: dict[str, str],
    ignore_re: "str | None",
    emitted: set[str],
    comment_on: bool,
) -> list[tuple[str, str, Signal]]:
    sigs = [s for s in usrdef_sigs if s.name not in port_names and s.name not in emitted]
    sigs = _filter_name(sigs, marker.args[0] if marker.args else "")
    sigs = _filter_not(sigs, ignore_re)
    sigs.sort(key=lambda s: s.name)
    decls: list[tuple[str, str, Signal, str]] = []
    for s in sigs:
        comment = comments.get(s.name, "") if comment_on else ""
        decls.append(("output", "output", s, comment))
        emitted.add(s.name)
    return decls


def auto_outputevery(lines: Sequence[str]) -> list[str]:
    """Regenerate /*AUTOOUTPUTEVERY[("re")]*/ — every non-port signal of the
    module becomes an ``output`` (sorted by name), honouring the
    ``verilog-auto-output-ignore-regexp`` file-local."""
    lines = kill_auto_outputevery(lines)
    full = list(lines)
    from .inst import map_module_spans

    m = re.search(
        r'^\s*//\s*verilog-auto-output-ignore-regexp\s*:\s*"([^"]+)"',
        "\n".join(full),
        re.M,
    )
    ignore_re = m.group(1) if m else None
    comment_on = _wire_comment_enabled(full)

    def _single(span: Sequence[str]) -> list[str]:
        ports, usrdef, _ = _module_tables(span)
        port_names = set(ports.signals)
        # NB: SignalTable keys are the names (Signal.name is often unset)
        usrdef_sigs = [replace(s, name=n) for n, s in usrdef.signals.items()]
        comments = _trailing_comments(span)
        text = "\n".join(span)
        markers = _find_markers(text, "AUTOOUTPUTEVERY")
        emitted: set[str] = set()
        plans = [
            (
                mk,
                _outputevery_decls(
                    mk, usrdef_sigs, port_names, comments, ignore_re, emitted, comment_on
                ),
            )
            for mk in markers
        ]
        for mk, decls in reversed(plans):
            text = _splice_region(
                text, mk, _OUTPUTEVERY_HEADER, [(kw, s, c) for _, kw, s, c in decls]
            )
        return text.split("\n")

    return map_module_spans(lines, _OUTPUTEVERY_MARK, _single)


# ---------------------------------------------------------------------------
# AUTOREGINPUT (verilog-auto-reg-input)


def auto_reginput(lines: Sequence[str], modules: Mapping[str, ModuleDef]) -> list[str]:
    """Regenerate /*AUTOREGINPUT*/ — ``reg`` for nets feeding input/inout
    ports of /*autoinst*/ instances that are neither declared nor assigned
    in this module.  Each carries ``// To {inst} of {module}.v``.  MODULES
    maps instance module names to parsed definitions."""
    from .wire import parse_ignore_concat, parse_param_value, set_ignore_concat
    from .autodef import set_param_value

    set_ignore_concat(parse_ignore_concat(lines))
    set_param_value(parse_param_value(lines))
    lines = kill_auto_reginput(lines)
    full = list(lines)
    from .inst import map_module_spans

    m = re.search(
        r'^\s*//\s*verilog-auto-reg-input-assigned-ignore-regexp\s*:\s*"([^"]+)"',
        "\n".join(full),
        re.M,
    )
    assigned_ignore = m.group(1) if m else None
    comment_on = _wire_comment_enabled(full)

    def _single(span: Sequence[str]) -> list[str]:
        driven = _inst_driven_nets(span, modules, ("input", "inout"), simple_only=True)
        if not driven:
            return list(span)
        ports, usrdef, assigns = _module_tables(span)
        excluded = set(ports.signals) | set(usrdef.signals)
        excluded |= get_all_defs(full) | get_all_paras(span)
        for a in assigns:
            if assigned_ignore and re.search(
                _auto_re_to_python(assigned_ignore), a, re.IGNORECASE
            ):
                continue  # ignored from the assigned set: still declarable
            excluded.add(a)
        from .libdirs import parse_typedef_regexp
        from .inst import set_typedef_regexp

        typedef_re = parse_typedef_regexp(full)
        set_typedef_regexp(typedef_re)
        sigs: list[Signal] = []
        comments: dict[str, str] = {}
        for name in sorted(driven):
            if name in excluded:
                continue
            if typedef_re and re.search(typedef_re, name):
                continue
            net = driven[name]
            if net.unpacked_dims:
                continue  # memories: "declare those yourself"
            sigs.append(
                Signal(
                    width=net.width, type="xfer_regin", name=name,
                    packed_dims=net.packed_dims, signed=net.signed,
                )
            )
            if comment_on:
                direction = "To/From" if net.direction == "inout" else "To"
                comments[name] = f"// {direction} {net.inst} of {net.module}.v"
        decls = [("reg", "reg", sigs[i], comments.get(sigs[i].name, "")) for i in range(len(sigs))]
        text = "\n".join(span)
        # AUTOREGINPUT takes no arguments and is always Verilog-1995 style
        # (verilog-insert-definition is called with v2k=nil)
        for mk in reversed(_find_markers(text, "AUTOREGINPUT")):
            text = _splice_region(
                text, mk, _REGINPUT_HEADER, [(kw, s, c) for _, kw, s, c in decls],
                v2k=False,
            )
        return text.split("\n")

    return map_module_spans(lines, _REGINPUT_MARK, _single)

# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.wire)


def _vb_auto_inoutmodule(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate /*AUTOINOUTMODULE*/ regions of the buffer."""
    return VerilogBuffer(auto_inoutmodule(self._lines, modules))


def _vb_auto_inoutcomp(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate /*AUTOINOUTCOMP*/ regions of the buffer."""
    return VerilogBuffer(auto_inoutcomp(self._lines, modules))


def _vb_auto_inoutin(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate /*AUTOINOUTIN*/ regions of the buffer."""
    return VerilogBuffer(auto_inoutin(self._lines, modules))


def _vb_auto_inoutparam(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate /*AUTOINOUTPARAM*/ regions of the buffer."""
    return VerilogBuffer(auto_inoutparam(self._lines, modules))


def _vb_auto_inoutmodport(
    self: VerilogBuffer, ifaces: Mapping[str, InterfaceInfo]
) -> VerilogBuffer:
    """Regenerate /*AUTOINOUTMODPORT*/ regions of the buffer."""
    return VerilogBuffer(auto_inoutmodport(self._lines, ifaces))


def _vb_auto_assign_modport(
    self: VerilogBuffer, ifaces: Mapping[str, InterfaceInfo]
) -> VerilogBuffer:
    """Regenerate /*AUTOASSIGNMODPORT*/ regions of the buffer."""
    return VerilogBuffer(auto_assign_modport(self._lines, ifaces))


def _vb_auto_outputevery(self: VerilogBuffer) -> VerilogBuffer:
    """Regenerate /*AUTOOUTPUTEVERY*/ regions of the buffer."""
    return VerilogBuffer(auto_outputevery(self._lines))


def _vb_auto_reginput(self: VerilogBuffer, modules: Mapping[str, ModuleDef]) -> VerilogBuffer:
    """Regenerate /*AUTOREGINPUT*/ regions of the buffer."""
    return VerilogBuffer(auto_reginput(self._lines, modules))


VerilogBuffer.auto_inoutmodule = _vb_auto_inoutmodule
VerilogBuffer.auto_inoutcomp = _vb_auto_inoutcomp
VerilogBuffer.auto_inoutin = _vb_auto_inoutin
VerilogBuffer.auto_inoutparam = _vb_auto_inoutparam
VerilogBuffer.auto_inoutmodport = _vb_auto_inoutmodport
VerilogBuffer.auto_assign_modport = _vb_auto_assign_modport
VerilogBuffer.auto_outputevery = _vb_auto_outputevery
VerilogBuffer.auto_reginput = _vb_auto_reginput


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.wire)


def _marker_ref_names(lines: Sequence[str], keywords: Sequence[str]) -> set[str]:
    """First quoted argument of every marker (module/interface names)."""
    text = "\n".join(lines)
    names: set[str] = set()
    for kw in keywords:
        for mk in _find_markers(text, kw):
            if mk.args:
                names.add(mk.args[0])
    return names


def _resolve_modules(args, lines: list[str], keywords: Sequence[str]) -> dict[str, ModuleDef]:
    """Module name -> ModuleDef (with_params=True) for marker references."""
    from .inst import _cli_resolve, _resolve_module_files, buffer_module_defs, _module_lines, find_interfaces
    from .libdirs import parse_typedef_regexp

    names = _marker_ref_names(lines, keywords)
    modules: dict[str, ModuleDef] = {}
    if not names:
        return modules
    libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
    files = _resolve_module_files(names, libdirs, inst_files, vc_entries, extensions)
    buffer_mods = buffer_module_defs("\n".join(lines))
    td_re = parse_typedef_regexp(lines)
    interfaces = set(find_interfaces(libdirs)) | set(args.interface)
    for name in sorted(names):
        src = _module_lines(name, files, buffer_mods)
        if src is None:
            _warn(f"module {name!r} not found, skipped")
            continue
        modules[name] = parse_module_ports(
            src, name, with_params=True, typedef_regexp=td_re, interfaces=interfaces
        )
    return modules


def _resolve_ifaces(args, lines: list[str], keywords: Sequence[str]) -> dict[str, InterfaceInfo]:
    """Interface name -> InterfaceInfo for marker references (library file
    by name, else the buffer's own interface block)."""
    from .inst import _cli_resolve, _resolve_module_files
    from .libdirs import parse_typedef_regexp

    names = _marker_ref_names(lines, keywords)
    ifaces: dict[str, InterfaceInfo] = {}
    if not names:
        return ifaces
    libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
    files = _resolve_module_files(names, libdirs, inst_files, vc_entries, extensions)
    buf_ifaces = buffer_interface_defs("\n".join(lines))
    for name in sorted(names):
        src: "list[str] | None" = None
        td_re: "str | None" = None
        if name in files:
            src = files[name].read_text().splitlines()
            td_re = parse_typedef_regexp(src)
        elif name in buf_ifaces:
            src = buf_ifaces[name]
            td_re = parse_typedef_regexp(lines)
        if src is None:
            _warn(f"interface {name!r} not found, skipped")
            continue
        try:
            ifaces[name] = parse_interface_info(src, typedef_re=td_re)
        except ValueError as exc:
            _warn(f"interface {name!r}: {exc}, skipped")
    return ifaces


def _resolve_inst_modules(args, lines: list[str]) -> dict[str, ModuleDef]:
    """Module name -> ModuleDef for /*autoinst*/ instances (AUTOREGINPUT)."""
    from .inst import _cli_resolve, _resolve_module_files, buffer_module_defs, _module_lines, find_interfaces
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
    for name in sorted(names):
        src = _module_lines(name, files, buffer_mods)
        if src is not None:
            modules[name] = parse_module_ports(
                src, name, typedef_regexp=td_re, interfaces=interfaces
            )
    return modules


_COMMANDS = [
    "ainoutmodule", "ainoutcomp", "ainoutin", "ainoutmodport", "ainoutparam",
    "aassignmodport", "aoutputevery", "areginput",
    "kill-ainoutmodule", "kill-ainoutcomp", "kill-ainoutin", "kill-ainoutmodport",
    "kill-ainoutparam", "kill-aassignmodport", "kill-aoutputevery", "kill-areginput",
]


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.xfer",
        description="verilog-mode AUTOINOUT*/AUTOOUTPUTEVERY/AUTOREGINPUT rewrite",
    )
    parser.add_argument(
        "command",
        choices=_COMMANDS,
        help="ainoutmodule/ainoutcomp/ainoutin/ainoutparam/ainoutmodport/"
        "aassignmodport/aoutputevery/areginput: expand; kill-*: delete the region",
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


_KILL_CMDS = {
    "kill-ainoutmodule": kill_auto_inoutmodule,
    "kill-ainoutcomp": kill_auto_inoutcomp,
    "kill-ainoutin": kill_auto_inoutin,
    "kill-ainoutmodport": kill_auto_inoutmodport,
    "kill-ainoutparam": kill_auto_inoutparam,
    "kill-aassignmodport": kill_auto_assign_modport,
    "kill-aoutputevery": kill_auto_outputevery,
    "kill-areginput": kill_auto_reginput,
}


def main(argv=None) -> None:
    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    cmd = args.command
    if cmd in _KILL_CMDS:
        out = _KILL_CMDS[cmd](lines)
    elif cmd in ("ainoutmodule", "ainoutcomp", "ainoutin"):
        modules = _resolve_modules(args, lines, ["AUTOINOUTMODULE", "AUTOINOUTCOMP", "AUTOINOUTIN"])
        fn = {"ainoutmodule": auto_inoutmodule, "ainoutcomp": auto_inoutcomp,
              "ainoutin": auto_inoutin}[cmd]
        out = fn(lines, modules)
    elif cmd == "ainoutparam":
        modules = _resolve_modules(args, lines, ["AUTOINOUTPARAM"])
        out = auto_inoutparam(lines, modules)
    elif cmd in ("ainoutmodport", "aassignmodport"):
        ifaces = _resolve_ifaces(args, lines, ["AUTOINOUTMODPORT", "AUTOASSIGNMODPORT"])
        out = auto_inoutmodport(lines, ifaces) if cmd == "ainoutmodport" else auto_assign_modport(lines, ifaces)
    elif cmd == "aoutputevery":
        out = auto_outputevery(lines)
    elif cmd == "areginput":
        out = auto_reginput(lines, _resolve_inst_modules(args, lines))
    else:  # pragma: no cover — argparse choices guard this
        out = lines
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
