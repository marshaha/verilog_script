"""verilog-mode Local Variables parsing for module-file resolution.

Real projects tell verilog-mode where module files live through a Local
Variables block at the bottom of the buffer (automatic.vim GetInsts,
lines 7118-7278)::

    // verilog-library-directories:("../pmu/" "../por_rst" )
    // verilog-library-flags:("-y ../foo" "-f ../filelist.vc" )
    // verilog-inst-file:../special/widgets.v

This module parses those lines so EAI/AIT find module files the way the
project specifies them, instead of only searching ``-y`` CLI dirs /
``g:verilog_tooling_libdirs``.

Path rules (matching the Vim original): ``~`` expands to ``$HOME``, a
leading ``$VAR`` path component expands from the environment, relative
entries resolve against the buffer file's directory, absolute paths pass
through, and the result is normalized with :func:`os.path.normpath`.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Sequence

_LIBDIRS_LINE = re.compile(r"^\s*//\s*verilog-library-directories\s*:(.*)$")
_FLAGS_LINE = re.compile(r"^\s*//\s*verilog-library-flags\s*:(.*)$")
_INST_FILE_LINE = re.compile(r"^\s*//\s*verilog-inst-file\s*:(.*)$")
_QUOTED = re.compile(r'"([^"]*)"')
_Y_FLAG = re.compile(r"-y\s*(\S+)")
_F_FLAG = re.compile(r"-f\s*(\S+)")
_ENV_HEAD = re.compile(r"^\$(\w+)")
_V_EXT = re.compile(r"\.(?:v|sv)$")


def _expand(path: str, env: Mapping[str, str]) -> str:
    """Expand ``~`` to $HOME and a leading ``$VAR`` component from ENV
    (automatic.vim: ``substitute(dir,'\\~',$HOME,"g")`` + GetRealDir_py).
    An undefined ``$VAR`` is left as-is."""
    home = env.get("HOME", str(os.path.expanduser("~")))
    path = path.replace("~", home)
    m = _ENV_HEAD.match(path)
    if m and m.group(1) in env:
        path = env[m.group(1)] + path[m.end() :]
    return path


def _resolve(path: str, base_dir: str, env: Mapping[str, str]) -> str:
    """Expand PATH, absolutize it against BASE_DIR when relative, and
    normalize (``.``/``..``, trailing slashes)."""
    path = _expand(path.strip(), env)
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    return os.path.normpath(path)


def _quoted_tokens(body: str) -> list[str]:
    """The ``"..."`` tokens of a ``(...)`` list; when nothing is quoted,
    fall back to whitespace splitting with parens stripped."""
    quoted = _QUOTED.findall(body)
    if quoted:
        return quoted
    return re.sub(r"[()]", " ", body).split()


def parse_local_variables(
    lines: Iterable[str],
    file_dir: str,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Parse a buffer's verilog-mode Local Variables.

    LINES are the buffer lines, FILE_DIR the buffer file's directory (the
    base for relative entries).  Returns ``{"dirs": [...], "vc_files":
    [...], "inst_files": {module: path}}``:

    - ``dirs``: every quoted dir of ``verilog-library-directories:("d1"
      "d2")`` lines plus every ``-y dir`` of ``verilog-library-flags``,
      resolved against FILE_DIR, in buffer order (duplicates kept).
    - ``vc_files``: every ``-f file.vc`` of ``verilog-library-flags``,
      resolved against FILE_DIR.
    - ``inst_files``: for ``verilog-inst-file:dir/file.v`` lines, the
      file's stem (the module name) mapped to the resolved file path.
    """
    if env is None:
        env = os.environ
    dirs: list[str] = []
    vc_files: list[str] = []
    inst_files: dict[str, str] = {}
    pending = ""  # "dirs" while inside a multi-line verilog-library-directories
    for line in lines:
        if pending == "dirs":
            dirs.extend(
                _resolve(tok, file_dir, env)
                for tok in _quoted_tokens(line)
                if tok.strip()
            )
            if ")" in line:
                pending = ""
            continue
        m = _LIBDIRS_LINE.match(line)
        if m:
            body = m.group(1)
            dirs.extend(
                _resolve(tok, file_dir, env)
                for tok in _quoted_tokens(body)
                if tok.strip()
            )
            # multi-line form:  // verilog-library-directories:(
            #                       "." "../lib"
            #                     )
            if ")" not in body:
                pending = "dirs"
            continue
        m = _FLAGS_LINE.match(line)
        if m:
            for tok in _quoted_tokens(m.group(1)):
                dirs.extend(
                    _resolve(ym.group(1), file_dir, env) for ym in _Y_FLAG.finditer(tok)
                )
                vc_files.extend(
                    _resolve(fm.group(1), file_dir, env) for fm in _F_FLAG.finditer(tok)
                )
            continue
        m = _INST_FILE_LINE.match(line)
        if m:
            raw = re.sub(r'[()"]', "", m.group(1)).strip()
            if raw:
                path = _resolve(raw, file_dir, env)
                stem = _V_EXT.sub("", os.path.basename(path))
                inst_files[stem] = path
    return {"dirs": dirs, "vc_files": vc_files, "inst_files": inst_files}


def parse_align_typedef_words(lines: Iterable[str]) -> list[str]:
    """The buffer's ``// verilog-align-typedef-words: ("w1" "w2")`` Local
    Variable.  verilog-mode builds its internal typedef regexp from these
    words, so ports typed by them parse as (possibly modport'd) interface
    ports — e.g. ``svi.master m`` / ``svi m`` in a module header."""
    for line in lines:
        m = re.search(r"verilog-align-typedef-words\s*:\s*\(([^)]*)\)", line)
        if m:
            return re.findall(r'"([^"]+)"', m.group(1))
    return []


def parse_typedef_regexp(lines: Iterable[str]) -> str | None:
    """The buffer's ``// verilog-typedef-regexp:"..."`` Local Variable (a
    regexp matching names that are TYPES, not nets — they must never be
    declared as wires).  ``verilog-align-typedef-words`` feeds the same
    typedef regexp inside verilog-mode, so its words are honored too."""
    words = parse_align_typedef_words(lines)
    for line in lines:
        m = re.search(r"verilog-typedef-regexp\s*:\s*\"([^\"]*)\"", line)
        if m:
            pats = [m.group(1)] + [rf"\b{w}\b" for w in words]
            return "|".join(pats)
    if words:
        return "|".join(rf"\b{w}\b" for w in words)
    return None


def read_vc_file(path: str, env: Mapping[str, str] | None = None) -> dict:
    """Read a ``-f``/EMACS_VC vc filelist (Verilog-XL ``-f`` format).

    Returns ``{"dirs": [...], "files": [...], "extensions": [...]}``:

    - ``-y dir`` / ``+incdir+dir`` / ``-Idir`` → ``dirs`` (library dirs)
    - ``-v file`` / bare ``file`` → ``files`` (module files)
    - ``+libext+.v+.sv`` → ``extensions``
    - ``#`` / ``//`` comment lines and blank lines are skipped.

    Paths resolve relative to the vc file's own directory (or absolute,
    ``$VAR``, ``~``), like a library dir."""
    if env is None:
        env = os.environ
    path = _expand(path, env)
    base_dir = os.path.dirname(os.path.abspath(path))
    dirs: list[str] = []
    files: list[str] = []
    extensions: list[str] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if line.startswith("+incdir+"):
                dirs.append(_resolve(line[len("+incdir+") :], base_dir, env))
            elif line.startswith("-I"):
                dirs.append(_resolve(line[2:].strip(), base_dir, env))
            elif line.startswith("-y"):
                dirs.append(_resolve(line[2:].strip(), base_dir, env))
            elif line.startswith("-v"):
                files.append(_resolve(line[2:].strip(), base_dir, env))
            elif line.startswith("+libext+"):
                extensions.extend(
                    e for e in line[len("+libext+") :].split("+") if e
                )
            else:
                files.append(_resolve(line, base_dir, env))
    return {"dirs": dirs, "files": files, "extensions": extensions}


def resolve_libdirs(
    lines: Iterable[str],
    file_dir: str,
    extra_dirs: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Full module search path: EXTRA_DIRS (CLI ``-y`` /
    g:verilog_tooling_libdirs) first, then the dirs from
    :func:`parse_local_variables`, then FILE_DIR itself (verilog-mode's
    ``verilog-library-directories`` defaults to ``(".")`` — same-directory
    submodules resolve without any configuration), deduped preserving
    order."""
    if env is None:
        env = os.environ
    parsed = parse_local_variables(lines, file_dir, env=env)
    out: list[str] = []
    for d in list(extra_dirs or ()) + parsed["dirs"] + [file_dir]:
        nd = os.path.normpath(d)
        if nd not in out:
            out.append(nd)
    return out


# ---------------------------------------------------------------------------
# `include expansion (analysis only)
#
# Real projects put the module's parameter list in an included header
# (``module m #(`include "m_params.svh") (...)``) or share `define/localparam
# blocks via `include.  Parameter collection (parse_module_params,
# get_all_paras, _const_symbols) must see through the directive or every
# param-value / width-known check downstream silently degrades.

_INCLUDE_DIRS: tuple[str, ...] = ()
_INCLUDE_WARNED: set[str] = set()
_INCLUDE_RE = re.compile(r'`include\s+"([^"]+)"')


def set_include_dirs(dirs: Sequence[str]) -> None:
    """Search path for `` `include "x"`` resolution (the module libdirs:
    ``-y`` + Local-Variables dirs + the buffer file's own directory)."""
    global _INCLUDE_DIRS
    _INCLUDE_DIRS = tuple(dict.fromkeys(d for d in dirs if d))


def include_dirs() -> tuple[str, ...]:
    return _INCLUDE_DIRS


def expand_includes(lines: Iterable[str], _seen: frozenset = frozenset()) -> list[str]:
    """Return LINES with every `` `include "x"`` spliced out into the file's
    recursively-expanded contents.  The directive may sit mid-line
    (``module m #(`include "m_params.svh") (``): the surrounding text is
    kept on its own lines — ANALYSIS ONLY (parameter/define collection),
    the transformed output text always keeps the original line.  Missing
    files keep the line untouched (one warning per name per process);
    include cycles are cut by re-emitting the directive."""
    import sys

    out: list[str] = []
    for line in lines:
        m = _INCLUDE_RE.search(line)
        if not m or "//" in line[: m.start()]:
            out.append(line)
            continue
        name = m.group(1)
        path = None
        for d in _INCLUDE_DIRS:
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                path = os.path.realpath(cand)
                break
        if path is None:
            if name not in _INCLUDE_WARNED:
                _INCLUDE_WARNED.add(name)
                print(
                    f"[verilog_tooling] warning: include file not found: "
                    f"{name} (searched {list(_INCLUDE_DIRS)})",
                    file=sys.stderr,
                )
            out.append(line)
            continue
        if path in _seen:
            out.append(line)  # include cycle: keep the directive, stop here
            continue
        sub = _read_include(path)
        if sub is None:
            out.append(line)
            continue
        prefix, suffix = line[: m.start()], line[m.end() :]
        if prefix.strip():
            out.append(prefix)
        out.extend(expand_includes(sub, _seen | {path}))
        if suffix.strip():
            out.append(suffix)
    return out


_INCLUDE_CACHE: dict[str, tuple[float, list[str]]] = {}


def _read_include(path: str) -> "list[str] | None":
    """Include file contents, cached by (path, mtime) — parameter collection
    calls expand_includes many times per command on large trees."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _INCLUDE_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            sub = fh.read().splitlines()
    except OSError:
        return None
    _INCLUDE_CACHE[path] = (mtime, sub)
    return sub
