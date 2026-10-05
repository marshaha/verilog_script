"""verilog-mode style AUTOINST / AUTOINSTPARAM expansion.

Implements the expansion semantics of Emacs verilog-mode's
``verilog-auto-inst`` and ``verilog-auto-inst-param`` (in contrast to
:mod:`verilog_tooling.inst`, which ports automatic.vim's AIT/AIU/AIU1
commands):

- Pins are grouped in sections ``// Outputs`` / ``// Inouts`` / ``// Inputs``
  (``// Parameters`` for AUTOINSTPARAM), in declaration order by default
  (``sort=True`` sorts within each group, like ``verilog-auto-inst-sort``).
  Interface-typed ports (parsed with ``direction == 'interface'``) go in a
  ``// Interfaces`` section emitted first; their default connection is
  ``name.modport`` (``cpu_bus.master bus`` -> ``.bus (bus.master)``) or the
  plain port name when no modport is declared.
- ``.`` sits at indent-pt (the column after the opening paren); the
  connection is aligned to ``verilog-auto-inst-column`` (default 40, raised
  to ``16 + 8*ceil(indent_pt/8)`` when deeper).
- The last pin ends with ``));`` (AUTOINST) or ``))`` (AUTOINSTPARAM).
- Pins connected manually before the ``/*AUTOINST*/`` marker are preserved
  and excluded from the expansion.
- Optional regexp argument: ``/*AUTOINST("regex")*/`` keeps only matching
  pins, a ``?!`` prefix excludes them (case-insensitive, as
  ``verilog-case-fold`` defaults to t).
- AUTO_TEMPLATE rules apply to connections (exact/regexp entries, ``@``,
  ``[]``, ``\\1`` back-references, ``@"..."`` expressions) via
  :mod:`verilog_tooling.template`.
- Re-running on already expanded output is idempotent.

Also implemented: ``verilog-auto-inst-param-value`` (``#(...)`` values
substituted into port widths/connections), ``.*`` star expansion
(``star_expand``/``star_save``), gate primitives, ``/*AUTO_LISP(...)*/``
and ``@"..."`` template expressions.  Still out of scope: arbitrary elisp
in ``@"..."`` (only the documented Python/elisp subset — ``defun`` and
friends are not).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from .comments import mask_comments
from typing import Mapping, Sequence

from .inst import ModuleDef, Port
from .template import (
    AutoTemplate,
    _auto_re_to_python,
    template_at_value,
    template_connection,
    template_for_module,
)

_DEFAULT_COLUMN = 40

_TYPE_WORDS = {
    "int", "integer", "logic", "bit", "reg", "signed", "unsigned",
    "byte", "shortint", "longint", "time", "real", "realtime", "type",
}


@dataclass(frozen=True)
class Param:
    """One module parameter; ``value`` is the raw default-value text."""

    name: str
    value: str | None = None


@dataclass(frozen=True)
class AutoMarker:
    """An /*AUTOINST*/ or /*AUTOINSTPARAM*/ comment in the buffer text."""

    offset: int  # text offset of '/*'
    end: int  # text offset just past '*/'
    regexp: str | None  # argument of /*AUTOINST("regex")*/, if any


# ---------------------------------------------------------------------------
# small text utilities


def _balanced(text: str, open_idx: int) -> tuple[str, int]:
    """Text inside the parens opened at TEXT[OPEN_IDX], and the offset just
    past the matching close paren."""
    depth = 0
    j = open_idx
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : j], j + 1
        j += 1
    raise ValueError("unbalanced parentheses")


def _strip_comments(text: str) -> str:
    from .comments import strip_comments

    return strip_comments(text)


def _split_top_commas(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
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


def _scan_parens(text: str, upto: int) -> list[int]:
    """Offsets of unmatched '(' in TEXT[:UPTO], skipping comments/strings."""
    stack: list[int] = []
    i = 0
    while i < upto:
        two = text[i : i + 2]
        if two == "//":
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        if two == "/*":
            j = text.find("*/", i + 2)
            if j < 0 or j >= upto:
                break
            i = j + 2
            continue
        c = text[i]
        if c == '"':
            j = i + 1
            while j < len(text) and (text[j] != '"' or text[j - 1] == "\\"):
                j += 1
            i = min(j + 1, upto)
            continue
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            stack.pop()
        i += 1
    return stack


def _matching_paren(text: str, open_idx: int) -> int:
    """Offset of the ')' matching the '(' at OPEN_IDX."""
    depth = 0
    i = open_idx
    while i < len(text):
        two = text[i : i + 2]
        if two == "//":
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        if two == "/*":
            j = text.find("*/", i + 2)
            if j < 0:
                raise ValueError("unterminated /* */ comment")
            i = j + 2
            continue
        c = text[i]
        if c == '"':
            j = i + 1
            while j < len(text) and (text[j] != '"' or text[j - 1] == "\\"):
                j += 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced parentheses in instance")


def _scan_parens_at(text: str, offsets: Iterable[int]) -> dict[int, list[int]]:
    """Unmatched-'(' stacks at each of OFFSETS, in a single pass over TEXT.

    Equivalent to ``{o: _scan_parens(text, o) for o in offsets}`` but O(n)
    instead of O(n * len(offsets))."""
    want = sorted(set(offsets))
    out: dict[int, list[int]] = {}
    stack: list[int] = []
    wi = 0
    i = 0
    n = len(text)
    while wi < len(want) and i < n:
        while wi < len(want) and want[wi] <= i:
            out[want[wi]] = list(stack)
            wi += 1
        two = text[i : i + 2]
        if two == "//":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if two == "/*":
            j = text.find("*/", i + 2)
            if j < 0:
                break
            i = j + 2
            continue
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and (text[j] != '"' or text[j - 1] == "\\"):
                j += 1
            i = j + 1
            continue
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            stack.pop()
        i += 1
    while wi < len(want):
        out[want[wi]] = list(stack)
        wi += 1
    return out


def _skip_back(text: str, i: int) -> int:
    """Scan backwards from I over whitespace and ``//`` line comments
    (e.g. the ``))// Templated`` an EAP run leaves between a ``#( ... )``
    block and the instance name)."""
    j = i
    while True:
        while j > 0 and text[j - 1] in " \t\n":
            j -= 1
        line_start = text.rfind("\n", 0, j) + 1
        cm = text.find("//", line_start, j)
        if cm < 0:
            return j
        j = cm  # sit at the comment start; loop eats the ws before it


def _prev_word(text: str, i: int) -> tuple[str, int]:
    """(word, start) of the identifier ending before I, scanning backwards
    over trailing whitespace and line comments (bounded — no full-prefix
    copy or regex)."""
    j = _skip_back(text, i)
    end = j
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
        j -= 1
    return text[j:end], j


def _skip_group_back(text: str, i: int) -> tuple[int, int] | None:
    """Skip backwards over a balanced ``( ... )`` group ending before I.

    Returns (open_idx, close_idx); None when the text before I does not end
    in ')'. Raises ValueError when the group is unbalanced."""
    j = _skip_back(text, i)
    if j == 0 or text[j - 1] != ")":
        return None
    close = j - 1
    depth = 0
    j = close
    while j >= 0:
        c = text[j]
        if c == ")":
            depth += 1
        elif c == "(":
            depth -= 1
            if depth == 0:
                return j, close
        j -= 1
    raise ValueError("unbalanced #( ... ) parameter override")


# ---------------------------------------------------------------------------
# parameter values (verilog-read-inst-param-value, verilog-auto-inst-param-value)


def read_inst_param_values(text: str, open_idx: int) -> dict[str, str]:
    """Parse the ``#( .NAME(VALUE), ... )`` block immediately preceding the
    pin list opened at OPEN_IDX; returns {param_name: value_text}.

    Mirrors verilog-mode's ``verilog-read-inst-param-value``: positional
    entries are ignored, values have all whitespace removed.
    """
    # skip the instance name, then the #( ... ) block must close the head
    _, j = _prev_word(text, open_idx)
    try:
        group = _skip_group_back(text, j)
    except ValueError:
        return {}
    if group is None:
        return {}
    inner = text[group[0] + 1 : group[1]]
    values: dict[str, str] = {}
    for entry in _split_top_commas(inner):
        entry = _strip_comments(entry)  # drop comments + // Templated debris
        m = re.match(r"\s*\.\s*(\w+)\s*\((.*)\)\s*$", entry, re.S)
        if m:
            values[m.group(1)] = re.sub(r"\s+", "", m.group(2))
    return values


def _apply_param_values(expr: str, param_values: Mapping[str, str]) -> str:
    """Replace whole-word parameter names with their instance values, then
    fold what becomes purely numeric — ``$clog2(8)`` → ``3`` and
    ``(8)-1`` → ``7`` (verilog-mode evaluates constant expressions).
    Values may reference other parameters (``IDX_W=$clog2(VEC_W)``), so
    substitution iterates to a fixpoint.  Doubled parens are collapsed each
    round and the original-text guard cuts substitution CYCLES (a name swap
    A->B, B->A) so they cannot pile up nested parens."""
    original = expr
    for _ in range(8):
        prev = expr
        for name, value in param_values.items():
            if value == name:
                continue  # identity self-map: no-op (avoids paren explosion)
            expr = re.sub(r"\b" + re.escape(name) + r"\b", f"({value})", expr)
        while "((" in expr:  # collapse substitution-added nesting, one level at a time
            collapsed = re.sub(r"\(\((\w+)\)\)", r"(\1)", expr)
            if collapsed == expr:
                break  # unbalanced/unnested remainder — nothing more to do
            expr = collapsed
        if expr == prev:
            break
        if expr.replace("(", "").replace(")", "") == original.replace("(", "").replace(
            ")", ""
        ):
            # a pure substitution cycle (a name swap A->B, B->A): the value
            # list is contradictory — keep the original expression
            return _fold_numeric_expr(original)
    return _fold_numeric_expr(expr)


def _fold_numeric_expr(expr: str) -> str:
    """Evaluate ``$clog2`` calls and constant arithmetic in a width
    expression; anything with identifiers stays verbatim.  Handles the
    ``msb:lsb`` range form by folding each side independently, and a
    constant ``(cond) ? a : b`` ternary by folding the condition."""
    import math

    # constant ternary: (cond) ? a : b — a top-level '?' means the ':' is
    # the ternary's, not a range separator; check before the range split
    qpos, depth = -1, 0
    for k, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "?" and depth == 0:
            qpos = k
            break
    if qpos > 0:
        cond = expr[:qpos].strip()
        rest = expr[qpos + 1 :]
        cpos, depth = -1, 0
        for k, ch in enumerate(rest):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == ":" and depth == 0:
                cpos = k
                break
        if cpos > 0:
            a, b = rest[:cpos], rest[cpos + 1 :]
            c_fold = _fold_numeric_expr(cond)
            if not re.fullmatch(r"-?\d+", c_fold):
                m = re.match(r"^\((.*)\)$", cond, re.S)
                if m:
                    c_fold = _fold_numeric_expr(m.group(1).strip())
            if not re.fullmatch(r"-?\d+", c_fold) and re.fullmatch(
                r"[\d\s+\-*/%()<>=!&|]+", c_fold
            ):
                try:  # numeric comparison: (4)==1 -> 0
                    c_fold = str(int(eval(c_fold, {"__builtins__": {}}, {})))
                except Exception:
                    pass
            if re.fullmatch(r"-?\d+", c_fold):
                pick = a if int(c_fold) != 0 else b
                return _fold_numeric_expr(pick.strip())

    if ":" in expr:
        hi, lo = expr.split(":", 1)
        return _fold_numeric_expr(hi) + ":" + _fold_numeric_expr(lo)

    def clog2(m: "re.Match[str]") -> str:
        inner = m.group(1).strip()
        if not re.fullmatch(r"[\d+\-*/%() ]+", inner):
            return m.group(0)
        try:
            v = int(eval(inner, {"__builtins__": {}}, {}))
        except Exception:
            return m.group(0)
        return str(math.ceil(math.log2(v)) if v > 1 else 0)

    expr = re.sub(r"\$clog2\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", clog2, expr)
    if re.fullmatch(r"\(?\s*[\d+\-*/%() ]+\s*\)?", expr.strip()):
        try:
            return str(int(eval(expr, {"__builtins__": {}}, {})))
        except Exception:
            pass
    return expr


def _subst_at_width(conn: str, at_value: str, width: str | None,
                    param_values: Mapping[str, str] | None) -> str:
    """Width used to expand a trailing []: apply @ and parameter values."""
    if width is None:
        return ""
    w = width
    if at_value:
        w = w.replace("@", at_value)
    if param_values:
        w = _apply_param_values(w, param_values)
    return w


# ---------------------------------------------------------------------------
# AUTO_LISP (/*AUTO_LISP(expr)*/ before the AUTO marker, evaluated in order)


def read_auto_lisp(text: str, upto: int) -> dict:
    """Evaluate every ``/*AUTO_LISP(...)*/`` Python expression before UPTO
    offset, in buffer order. Returns a dict of assigned names.

    verilog-mode evaluates elisp; here the sandboxed subset is Python with a
    few verilog-flavoured helpers (vl_* are not yet in scope at parse time;
    AUTO_LISP mainly prepares constants for @"..." templates).
    """
    env: dict = {}
    for m in re.finditer(r"/\*\s*AUTO_LISP\s*\(", text[:upto]):
        expr, _ = _balanced(text[:upto], m.end() - 1)
        try:
            code = compile(expr, "<AUTO_LISP>", "exec")
            exec(code, {"__builtins__": {}}, env)
        except Exception as exc:  # noqa: BLE001 - report and continue
            raise ValueError(f"AUTO_LISP evaluation failed: {expr!r}: {exc}") from exc
    return env


# Cache for verilog-auto-python-file resolution: (filename, dirs) -> path
# or None — keyed by the include dirs too: they change per file in batch
# processes (closed loops), so a filename-only cache would go stale.
_PY_FILE_PATH_CACHE: "dict[tuple[str, tuple[str, ...]], str | None]" = {}
# Cache for loaded Python modules: path -> (mtime, env dict)
_PY_MODULE_CACHE: dict[str, tuple[float, dict]] = {}


def _local_python_files(lines: Sequence[str]) -> list[str]:
    """verilog-auto-python-file file-local: one or more Python files whose
    top-level definitions (functions) are available in @"..." template
    expressions.  Same syntax as verilog-library-files — quoted,
    whitespace-separated, optionally parenthesised::

        // verilog-auto-python-file: "myfuncs.py"
        // verilog-auto-python-file:("a.py" "b.py")

    Relative paths search the -y libdirs (and vc dirs); ``~`` and ``$VAR``
    are expanded."""
    from .libdirs import _quoted_tokens

    m = re.search(
        r"^\s*//\s*verilog-auto-python-file\s*:\s*(.+)$",
        "\n".join(lines), re.M
    )
    return _quoted_tokens(m.group(1)) if m else []


def _resolve_python_file(filename: str) -> str | None:
    """Resolve FILENAME to an absolute path, searching the include dirs
    (reusing libdirs._find_include's miss cache)."""
    import os
    from .libdirs import _find_include, include_dirs

    filename = os.path.expanduser(os.path.expandvars(filename))
    if os.path.isabs(filename):
        return filename if os.path.isfile(filename) else None
    key = (filename, include_dirs())
    if key in _PY_FILE_PATH_CACHE:
        return _PY_FILE_PATH_CACHE[key]
    path = _find_include(filename)
    _PY_FILE_PATH_CACHE[key] = path
    return path


def _load_python_file(path: str) -> dict:
    """Load a Python file's top-level definitions, cached by mtime.
    Sandboxed (no builtins), like AUTO_PYTHON blocks."""
    import os
    mtime = os.path.getmtime(path)
    cached = _PY_MODULE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    env: dict = {}
    with open(path) as f:
        code = compile(f.read(), path, "exec")
    exec(code, {"__builtins__": {}}, env)
    _PY_MODULE_CACHE[path] = (mtime, env)
    return env


def read_auto_python(text: str, upto: int) -> dict:
    """Evaluate every ``/*AUTO_PYTHON(...)*/`` block before UPTO offset, in
    buffer order. Returns a dict of defined names (typically functions).

    This is the Python-native alternative to elisp ``AUTO_LISP(defun ...)``:
    users write plain Python (def, assignments, imports of nothing) and the
    defined names become callable from ``@"..."`` template expressions, e.g.::

        /*AUTO_PYTHON(
        def testfunc(sig, bits):
            return "{" + sig + "[0" + bits + "]}"
        )*/
        /*my_mod AUTO_TEMPLATE (
            .\\(.*\\)  (@"testfunc(vl_name, vl_width)"),
        ); */

    Like AUTO_LISP, execution is sandboxed (no builtins).

    With ``// verilog-auto-python-file: "a.py" "b.py"`` file-locals, those
    files' top-level definitions are loaded first (searched via -y libdirs
    and vc dirs, cached). Inline /*AUTO_PYTHON*/ blocks override.
    """
    env: dict = {}
    # File-local Python file(s) first (lower precedence than inline blocks;
    # later files override earlier ones)
    for py_file in _local_python_files(text.split("\n")):
        resolved = _resolve_python_file(py_file)
        if resolved:
            try:
                env.update(_load_python_file(resolved))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"AUTO_PYTHON file {py_file!r} load failed: {exc}"
                ) from exc
    for m in re.finditer(r"/\*\s*AUTO_PYTHON\s*\(", text[:upto]):
        expr, _ = _balanced(text[:upto], m.end() - 1)
        try:
            code = compile(expr, "<AUTO_PYTHON>", "exec")
            exec(code, {"__builtins__": {}}, env)
        except Exception as exc:  # noqa: BLE001 - report and continue
            raise ValueError(f"AUTO_PYTHON evaluation failed: {expr!r}: {exc}") from exc
    return env


# ---------------------------------------------------------------------------
# module parameter parsing (verilog-decls-get-gparams: `parameter`, not
# `localparam`, in declaration order)


def _param_from_entry(entry: str) -> Param | None:
    # strip `ifdef/`endif lines wrapping the entry (e.g. params guarded by a
    # `define inside the #(...) header)
    entry = re.sub(r"^\s*`(ifdef|ifndef|else|endif)\b[^\n]*", "", entry).strip()
    entry = re.sub(r"^(?:parameter|localparam)\b", "", entry).strip()
    while True:
        m = re.match(r"(\w+)\s+", entry)
        if m and m.group(1) in _TYPE_WORDS:
            entry = entry[m.end() :].lstrip()
            continue
        if entry.startswith("["):
            j = entry.find("]")
            if j < 0:
                return None
            entry = entry[j + 1 :].lstrip()
            continue
        break
    m = re.match(r"(\w+)\s*(?:=\s*(.*?))?\s*[;,]?\s*$", entry, re.S)
    if not m:
        return None
    value = m.group(2)
    return Param(m.group(1), value.strip() if value else None)


def parse_module_params(lines: Sequence[str]) -> tuple[Param, ...]:
    """Parse ``parameter`` declarations from a module definition.

    Covers the ANSI ``#( parameter ... )`` header list and body
    ``parameter ...;`` declarations (header entries first).  `` `include``
    directives are expanded first (analysis only) — a project-local
    ``#(`include "m_params.svh")`` parameter list would otherwise be
    invisible."""
    from .libdirs import expand_includes

    text = _strip_comments("\n".join(expand_includes(lines)))
    params: list[Param] = []
    header_span = None
    m = re.search(r"\bmodule\s+\w+\s*#\s*\(", text)
    if m:
        open_idx = text.index("(", m.start())
        inner, end = _balanced(text, open_idx)
        header_span = (open_idx, end)
        for entry in _split_top_commas(inner):
            param = _param_from_entry(entry)
            if param:
                params.append(param)
    body = text
    if header_span:
        start, end = header_span
        body = text[:start] + " " * (end - start) + text[end:]
    for bm in re.finditer(r"^\s*parameter\b([^;]*);", body, re.M):
        for entry in _split_top_commas(bm.group(1)):
            param = _param_from_entry(entry)
            if param:
                params.append(param)
    return tuple(params)


# ---------------------------------------------------------------------------
# AUTO markers and instance resolution


def _marker_regex(keyword: str, require_comment: bool = True) -> "re.Pattern[str]":
    if require_comment:
        return re.compile(
            r"/\*\s*" + keyword + r"\b(?:\s*\(\s*\"((?:[^\"\\]|\\.)*)\"\s*\))?\s*\*/",
            re.IGNORECASE,  # verilog-mode writes AUTOINST, users often write autoinst
        )
    # .* star expansion: keyword is '.*' (already regex-escaped by caller)
    return re.compile(r"(\.\*)")


def find_auto_markers(
    lines: Sequence[str], keyword: str, *, require_comment: bool = True
) -> list[AutoMarker]:
    """All /*KEYWORD*/ markers (optionally with a ("regex") argument),
    skipping ones inside // comments. With require_comment=False, keyword is
    matched as a bare token (used for SystemVerilog .* expansion)."""
    text = "\n".join(lines)
    # line comments blanked (block comments kept — markers ARE block
    # comments), so a match whose first char was masked sits inside a //
    # comment, including a trailing one (``code; // /*autoinst*/``)
    masked = mask_comments(text, block=False)
    out = []
    for m in _marker_regex(keyword, require_comment).finditer(text):
        if masked[m.start()] == " ":
            continue  # inside a // comment
        if require_comment:
            regexp = m.group(1)
        else:
            regexp = None
        out.append(AutoMarker(offset=m.start(), end=m.end(), regexp=regexp))
    return out


def _resolve_instance_at(text: str, open_idx: int) -> tuple[str, str]:
    """(module, instance) for the pin list opened at OPEN_IDX."""
    inst, j = _prev_word(text, open_idx)
    if not inst:
        raise ValueError("AUTOINST: cannot resolve instance name")
    group = _skip_group_back(text, j)
    if group is not None:
        # parameter override: the #( ... ) group before the instance name
        j = group[0]
        while j > 0 and text[j - 1] in " \t\n":
            j -= 1
        if j > 0 and text[j - 1] == "#":
            j -= 1
    module, _ = _prev_word(text, j)
    if not module:
        raise ValueError("AUTOINST: cannot resolve module name")
    return module, inst


def inst_pin_connections(text: str, open_idx: int) -> "list[tuple[str, str]]":
    """(pin, expression) for every named connection in the pin list opened
    at OPEN_IDX — the whole list, on either side of any AUTO marker:
    multi-pin lines, multi-line expressions and SystemVerilog ``.pin``
    shorthand (expression = pin name) included.  Expressions are returned
    verbatim, comments intact (the /*[D1][D2]*/ packed-dims note rides in
    one); callers strip as needed."""
    close_idx = _matching_paren(text, open_idx)
    span = text[open_idx + 1 : close_idx]
    masked = mask_comments(span)
    out: list[tuple[str, str]] = []
    pin_re = re.compile(r"\.\s*(\w+)")
    i = 0
    n = len(masked)
    while i < n:
        m = pin_re.search(masked, i)
        if not m:
            break
        pin = m.group(1)
        k = m.end()
        while k < n and masked[k] in " \t\n":
            k += 1
        if k >= n or masked[k] != "(":
            out.append((pin, pin))  # .pin shorthand (followed by , or close)
            i = k
            continue
        depth = 1
        j = k + 1
        while j < n and depth:
            ch = masked[j]
            if ch == '"':
                j += 1
                while j < n and masked[j] != '"':
                    j += 2 if masked[j] == "\\" else 1
                j += 1  # past the closing quote
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        out.append((pin, span[k + 1 : j - 1]))
        i = j
    return out


def _resolve_param_instance(text: str, open_idx: int, close_idx: int) -> tuple[str, str]:
    """(module, instance) for a #( ... ) parameter block: the module name
    precedes the '#', the instance name follows the block."""
    j = open_idx
    while j > 0 and text[j - 1] in " \t\n":
        j -= 1
    if j > 0 and text[j - 1] == "#":
        j -= 1
    module, _ = _prev_word(text, j)
    if not module:
        raise ValueError("AUTOINSTPARAM: cannot resolve module name")
    # the instance name follows the block; // comment lines may intervene
    mi = re.match(r"(?:\s|//[^\n]*)*(\w+)\s*\(", text[close_idx + 1 : close_idx + 1000])
    inst = mi.group(1) if mi else ""
    return module, inst


def marker_modules(
    lines: Sequence[str], keyword: str, *, include_star: bool = False
) -> list[str]:
    """Module names of all instances carrying a KEYWORD marker, in order.
    With include_star=True, SystemVerilog ``.*`` instances are included too."""
    text = "\n".join(lines)
    markers = find_auto_markers(lines, keyword)
    if include_star:
        markers = markers + find_auto_markers(lines, r"\.\*", require_comment=False)
        markers.sort(key=lambda m: m.offset)
    stacks = _scan_parens_at(text, [m.offset for m in markers])
    modules = []
    for marker in markers:
        stack = stacks[marker.offset]
        if not stack:
            continue  # unresolvable marker layout (e.g. instance arrays)
        open_idx = stack[-1]
        try:
            if keyword == "AUTOINST" or text[marker.offset : marker.end] == ".*":
                modules.append(_resolve_instance_at(text, open_idx)[0])
            else:
                modules.append(
                    _resolve_param_instance(text, open_idx, _matching_paren(text, open_idx))[0]
                )
        except ValueError:
            continue  # instance arrays / `define'd names: not expandable, skip
    return modules


# ---------------------------------------------------------------------------
# expansion engine


def _filter_regexp(items: list, regexp: str | None) -> list:
    """Keep items matching REGEXP (case-insensitive search, as
    verilog-case-fold=t); a leading ?! inverts the match."""
    if not regexp:
        return items
    invert = regexp.startswith("?!")
    rx = re.compile(_auto_re_to_python(regexp[2:] if invert else regexp), re.IGNORECASE)
    return [p for p in items if bool(rx.search(p.name)) != invert]


def _build_gen(sections, indent_pt: int, col_eff: int, closer: str) -> str:
    """The text inserted after the AUTO marker; CLOSER terminates the pin
    list (');' for AUTOINST, ')' for AUTOINSTPARAM).

    Templated connections get a ``// Templated`` comment, like verilog-mode
    emits by default, at column col_eff + 24 (col_eff + 16 when deeper).
    A star (.*) expansion instead tags non-templated pins with
    ``// Implicit .*`` so they can be deleted again on save.
    """
    # verilog-mode: indent-to(col_eff + 24) then insert " // ..." (leading
    # space), so the comment text starts at col_eff + 24 + 1.
    comment_col = col_eff + (24 if col_eff < 48 else 16) + 1
    out = []
    last_comma = -1  # comma position of the last port line (always out[-1])
    last_comment = None
    for header, entries in sections:
        out.append(" " * indent_pt + header)
        for name, conn, comment in entries:
            line = " " * indent_pt + "." + name
            if conn is not None:  # None => SystemVerilog .name syntax
                # indent-to col_eff: no padding when the name already reaches
                # or passes the column (matches verilog-mode).
                line += " " * max(col_eff - len(line), 0) + "(" + conn + ")"
            line += ","
            last_comma = len(line) - 1
            last_comment = comment
            if comment:
                line += " " * max(comment_col - len(line), 1) + comment
            out.append(line)
    if last_comma < 0:
        return ""  # no ports at all
    # strip the comment off the last line, replace the trailing "," with the
    # closer, then re-add the comment padded to the same target column.
    last = out[-1]
    if last_comment:
        last = last[: last.index(last_comment)].rstrip()
    last = last[:last_comma] + closer
    if last_comment:
        last += " " * max(comment_col - len(last), 1) + last_comment
    out[-1] = last
    return "\n" + "\n".join(out)

def _replace_region(
    text: str,
    marker: AutoMarker,
    open_idx: int,
    close_idx: int,
    gen: str,
    consume_semi: bool,
) -> str:
    """Splice GEN between the marker and the pin-list close paren, adding a
    comma after a manually connected pin that lacks one. For a star (.*)
    instance the marker is kept and a comma appended after it."""
    head = text[: marker.end]
    tail = text[close_idx + 1 :]
    if consume_semi:
        sm = re.match(r"\s*;[ \t]*", tail)
        if sm:
            tail = tail[sm.end() :]
    if text[marker.offset : marker.end] == ".*":
        head = text[: marker.end] + ","  # keep .*, expand after it
    pin_region = text[open_idx + 1 : marker.offset]
    stripped = pin_region.rstrip()
    if re.search(r"\.\s*\w+\s*\(", pin_region) and stripped.endswith(")"):
        insert_at = open_idx + 1 + len(stripped)
        head = head[:insert_at] + "," + head[insert_at:]
    return head + gen + tail


def _select_markers(
    lines: Sequence[str], keyword: str, which: "int | Sequence[int] | None"
) -> list[AutoMarker]:
    """Markers to process: all (None), the Nth (int), or an explicit index list."""
    markers = find_auto_markers(lines, keyword)
    if which is None:
        return markers
    if isinstance(which, int):
        try:
            return [markers[which]]
        except IndexError:
            raise ValueError(
                f"No {keyword} instance found! (which={which}, {len(markers)} markers)"
            ) from None
    return [markers[i] for i in which if 0 <= i < len(markers)]


def _indent_pt(text: str, open_idx: int) -> int:
    return open_idx - (text.rfind("\n", 0, open_idx) + 1) + 1


# Expansion debris removed by verilog-delete-auto-buffer before re-expansion
_TEMPLATED_DEBRIS = re.compile(
    r"\s*// (?:Templated(?:\s*AUTONOHOOKUP)?|Implicit \.\*)(?:[ \tLT0-9]*| LHS: .*)?$"
)


def _strip_templated_comments(lines: Sequence[str]) -> list[str]:
    """Delete trailing ``// Templated`` debris comments (anywhere, like
    verilog-delete-auto-buffer), so re-expansion stays idempotent."""
    return [_TEMPLATED_DEBRIS.sub("", line) for line in lines]


def _strip_templated_in_regions(
    text: str, regions: Sequence[tuple[int, int]]
) -> str:
    """Delete trailing ``// Templated`` / ``// Implicit .*`` debris comments
    inside (start, end) offset REGIONS only.  A command cleans the instances
    it rewrites without erasing annotations owned by other commands (e.g.
    AUTOINST must not strip AUTOINSTPARAM's ``// Templated`` tags)."""
    for start, end in sorted(regions, reverse=True):
        seg = "\n".join(
            _TEMPLATED_DEBRIS.sub("", line) for line in text[start:end].split("\n")
        )
        text = text[:start] + seg + text[end:]
    return text


def _connect(
    template: AutoTemplate | None,
    at_value: str,
    port: Port,
    param_values: Mapping[str, str] | None = None,
    env: dict | None = None,
    inst_name: str = "",
    inst_vector: str = "t",
    simplify: bool = True,
    parent_signals: Mapping[str, str] | None = None,
) -> tuple[str, bool]:
    """(connection, templated) for PORT: identity ``port[bits]`` unless a
    template entry matches. Multidimensional ports get a verilog-mode
    ``port/*[packed].[unpacked]*/`` comment instead of an expanded range.
    With ``param_values`` (the instance's ``#(...)`` overrides), parameters
    inside the port's width are substituted. ENV feeds @"..." templates.
    PARENT_SIGNALS maps net name -> width in the instantiating module (for
    verilog-auto-inst-vector:nil matching)."""
    width = port.width
    if width and param_values:
        width = _apply_param_values(width, param_values)
    if width and simplify:
        # verilog-auto-simplify-expressions (default t): fold constants
        width = _fold_numeric_expr(width)
    # a multidim port's shape rides as a verilog-mode /*[D1][D2]*/ note —
    # in default connections AND as the []/[][] expansion in templates.
    # Dims get the instance's #(...) substitutions, redundant-paren
    # stripping and pure-numeric folding (2*16-1 -> 31); symbols stay
    # verbatim.
    packed_note = ""
    if port.is_multidim:
        from .autodef import _clean_dim

        dims = port.packed
        if param_values:
            dims = tuple(_apply_param_values(d, param_values) for d in dims)
        inner = "".join(f"[{_clean_dim(d)}]" for d in dims)
        if port.unpacked:
            inner += "." + "".join(f"[{u}]" for u in port.unpacked)
        packed_note = f"/*{inner}*/"
    if template is not None:
        conn = template_connection(
            template, port.name, at_value, width, env,
            vl_cell_name=inst_name, vl_dir=port.direction or "",
            packed_note=packed_note,
        )
        if conn is not None:
            if param_values:
                conn = _apply_param_values(conn, param_values)
            return conn, True
    # interface ports connect to a same-named interface in the parent,
    # qualified with the modport when the port declares one (bus.master)
    if port.is_interface:
        return (f"{port.name}.{port.modport}" if port.modport else port.name), False
    # verilog-mode: multidimensional ports are not expanded into the
    # connection; their full shape is attached as a /*...*/ comment.
    if port.is_multidim:
        return f"{port.name}{packed_note}", False
    # verilog-auto-inst-vector: t (default) always subscripts, nil skips when
    # the net is declared in the parent with matching width, unsigned only
    # for unsigned ports
    if inst_vector == "t":
        use_vector = True
    elif inst_vector == "unsigned":
        use_vector = not port.signed
    else:  # nil
        # Skip subscript only if parent declares the net with matching width
        parent_width = (parent_signals or {}).get(port.name)
        use_vector = not (parent_width and parent_width == width)
    return port.name + (f"[{width}]" if width and use_vector else ""), False


# ---------------------------------------------------------------------------
# AUTOINST (verilog-auto-inst)


_INST_TEMPLATE_REQUIRED_RE = re.compile(
    r"^\s*//\s*verilog-auto-inst-template-required\s*:\s*(\w+)", re.M
)
_INST_VECTOR_RE = re.compile(
    r"^\s*//\s*verilog-auto-inst-vector\s*:\s*(\w+)", re.M
)


def _inst_template_required(lines: Sequence[str]) -> bool:
    """verilog-auto-inst-template-required file-local (default nil): non-nil
    omits ports without a template from AUTOINST."""
    m = _INST_TEMPLATE_REQUIRED_RE.search("\n".join(lines))
    return bool(m) and m.group(1).lower() != "nil"


def _inst_vector(lines: Sequence[str]) -> str:
    """verilog-auto-inst-vector file-local (default t): bus subscripts on
    default connections; nil skips them, unsigned subscripts only unsigned
    ports."""
    m = _INST_VECTOR_RE.search("\n".join(lines))
    if m and m.group(1).lower() in ("nil", "unsigned"):
        return m.group(1).lower()
    return "t"


def _local_bool(lines: Sequence[str], name: str, default: bool = False) -> bool:
    """A boolean file-local variable: ``// {name}: nil`` -> False,
    ``// {name}: t`` (or anything else) -> True, absent -> DEFAULT."""
    import re
    m = re.search(r"^\s*//\s*" + name + r"\s*:\s*(\w+)", "\n".join(lines), re.M)
    if not m:
        return default
    return m.group(1).lower() != "nil"


def _local_inst_sort(lines: Sequence[str]) -> bool:
    """verilog-auto-inst-sort file-local (default nil): sort AUTOINST pins
    within each direction group."""
    return _local_bool(lines, "verilog-auto-inst-sort", False)


def _local_inst_dot_name(lines: Sequence[str]) -> bool:
    """verilog-auto-inst-dot-name file-local (default nil): use SystemVerilog
    .name shorthand when port connects to same-named net."""
    return _local_bool(lines, "verilog-auto-inst-dot-name", False)


def _local_simplify_expressions(lines: Sequence[str]) -> bool:
    """verilog-auto-simplify-expressions file-local (default t): when nil,
    AUTO expansions keep range expressions verbatim instead of folding
    constants (e.g. [2*2:1] stays, not [4:1])."""
    return _local_bool(lines, "verilog-auto-simplify-expressions", True)


def auto_inst(
    lines: Sequence[str],
    modules: Mapping[str, ModuleDef],
    *,
    which: int | None = None,
    templates: Sequence[AutoTemplate] | None = None,
    sort: bool = False,
    dot_name: bool = False,
    column: int = _DEFAULT_COLUMN,
    param_value: bool = False,
    star_expand: bool = False,
    star_save: bool = False,
) -> list[str]:
    """Expand /*AUTOINST*/ markers verilog-mode style.

    Sections are emitted in Outputs/Inouts/Inputs order; pins already
    connected before the marker are kept and skipped.  ``dot_name=True``
    emits SystemVerilog ``.name`` shorthand where the connection equals the
    port name (``verilog-auto-inst-dot-name``).

    ``param_value=True`` substitutes parameters from the instance's ``#(...)``
    override into port widths and template connections
    (``verilog-auto-inst-param-value``).

    ``star_expand=True`` also expands SystemVerilog ``.*`` instances as if
    they were AUTOINST (``verilog-auto-star-expand``); ``star_save=True``
    keeps the expansion on re-runs instead of deleting it back to ``.*``
    (``verilog-auto-star-save``).

    Every ``/*AUTO_LISP(...)*/`` before each instance is evaluated first and
    its bindings feed ``@"..."`` template expressions (Python subset).
    """
    # file-local vars OR with CLI flags (either enables the behavior)
    sort = sort or _local_inst_sort(lines)
    dot_name = dot_name or _local_inst_dot_name(lines)
    text = "\n".join(lines)
    markers = _select_markers(lines, "AUTOINST", which)
    if star_expand:
        for m in find_auto_markers(lines, r"\.\*", require_comment=False):
            markers.append(m)
        markers.sort(key=lambda m: m.offset)
    stacks = _scan_parens_at(text, [m.offset for m in markers])
    # strip // Templated debris only inside the instances this command
    # rewrites — param sections belong to AUTOINSTPARAM and keep their tags.
    # Regions extend to end-of-line: the debris comment trails the `));`.
    regions = []
    for m in markers:
        st = stacks[m.offset]
        if st:
            try:
                close = _matching_paren(text, st[-1])
            except ValueError:
                continue
            eol = text.find("\n", close)
            regions.append((st[-1], len(text) if eol < 0 else eol))
    text = _strip_templated_in_regions(text, regions)
    lines = text.split("\n")
    # offsets shifted by the strip: re-select markers and rescan
    markers = _select_markers(lines, "AUTOINST", which)
    if star_expand:
        for m in find_auto_markers(lines, r"\.\*", require_comment=False):
            markers.append(m)
        markers.sort(key=lambda m: m.offset)
    # one pass for all markers; offsets of unprocessed markers stay valid
    # because the loop below rewrites text bottom-up (later offsets first)
    stacks = _scan_parens_at(text, [m.offset for m in markers])
    for marker in reversed(markers):
        stack = stacks[marker.offset]
        if not stack:
            # a stray marker outside any instance pin list (typically left
            # in a comment or pasted as documentation) — warn, don't abort
            print(
                f"warning: AUTOINST marker outside an instance pin list "
                f"(offset {marker.offset}), skipped",
                file=sys.stderr,
            )
            continue
        open_idx = stack[-1]
        close_idx = _matching_paren(text, open_idx)
        is_star = text[marker.offset : marker.end] == ".*"
        if is_star and not star_expand:
            continue
        try:
            module, inst = _resolve_instance_at(text, open_idx)
        except ValueError:
            continue  # instance array / `define'd name: skip, not expandable
        try:
            moddef = modules[module]
        except KeyError:
            raise KeyError(f"module {module!r} not found for AUTOINST instance {inst!r}") from None
        try:
            text = _auto_inst_one(
                text, marker, open_idx, close_idx, moddef, module, inst,
                templates, sort, dot_name, column, param_value, is_star, star_save,
                template_required=_inst_template_required(lines),
                inst_vector=_inst_vector(lines),
                simplify=_local_simplify_expressions(lines),
            )
        except ValueError as exc:
            # AUTO_LISP / @"..." template evaluation outside the Python
            # subset: warn and leave the instance untouched — one bad
            # template must not abort the whole file
            print(
                f"warning: AUTOINST {inst} ({module}) skipped: {exc}",
                file=sys.stderr,
            )
            continue
    return text.split("\n")


def _auto_inst_one(
    text, marker, open_idx, close_idx, moddef, module, inst,
    templates, sort, dot_name, column, param_value, is_star, star_save,
    template_required=False, inst_vector="t", simplify=True,
):
    lisp_env = read_auto_lisp(text, marker.offset)
    # AUTO_PYTHON user functions merge into the template expression scope
    # (Python-native alternative to elisp defun)
    lisp_env.update(read_auto_python(text, marker.offset))
    tpl = (
        template_for_module(
            list(templates), module, before_line=text.count("\n", 0, marker.offset)
        )
        if templates
        else None
    )
    at_value = template_at_value(tpl, inst) if tpl else ""
    param_values = read_inst_param_values(text, open_idx) if param_value else {}
    # Parent module signal declarations (for inst-vector:nil width matching):
    # every wire/reg/logic AND io declaration of the enclosing module —
    # multi-name decls included (misc._scan_decls handles both)
    parent_signals: dict[str, str] = {}
    if inst_vector == "nil":
        from .inst import module_spans
        from .misc import _scan_decls

        tlines = text.split("\n")
        for lo, hi in module_spans(tlines):
            if lo <= text.count("\n", 0, marker.offset) <= hi:
                for d in _scan_decls(tlines[lo : hi + 1]):
                    if d.bits:
                        rng = re.sub(r"\s+", "", d.bits).strip("[]")
                        parent_signals.setdefault(d.name, rng)
                break
    # commented-out pins before the marker are NOT connected: mask
    # comments (positions preserved) before collecting names
    pins = set(
        re.findall(
            r"\.\s*(\w+)\s*\(",
            mask_comments(text[open_idx + 1 : marker.offset], block=True),
        )
    )
    sections = []
    for header, direction in (
        ("// Interfaces", "interface"),
        ("// Outputs", "output"),
        ("// Inouts", "inout"),
        ("// Inputs", "input"),
    ):
        ports = [p for p in moddef.ports if p.direction == direction and p.name not in pins]
        ports = _filter_regexp(ports, marker.regexp)
        if sort:
            ports = sorted(ports, key=lambda p: p.name)
        entries = []
        for port in ports:
            conn, templated = _connect(
                tpl, at_value, port, param_values, lisp_env, inst_name=inst,
                inst_vector=inst_vector, simplify=simplify,
                parent_signals=parent_signals,
            )
            if template_required and not templated:
                continue  # verilog-auto-inst-template-required: omit the port
            if dot_name and conn == port.name:
                conn = None
            # star expansions tag non-templated pins so they can be
            # deleted again on save (verilog-delete-auto-star-implicit)
            if templated:
                comment = "// Templated"
            elif is_star and star_save:
                comment = "// Implicit .*"
            else:
                comment = None
            entries.append((port.name, conn, comment))
        if entries:
            sections.append((header, entries))
    if not sections:
        return text
    indent_pt = _indent_pt(text, open_idx)
    col_eff = max(column, 16 + 8 * ((indent_pt + 7) // 8))
    gen = _build_gen(sections, indent_pt, col_eff, ");")
    return _replace_region(text, marker, open_idx, close_idx, gen, consume_semi=True)


# ---------------------------------------------------------------------------
# AUTOINSTPARAM (verilog-auto-inst-param)


def auto_param(
    lines: Sequence[str],
    module_params: Mapping[str, Sequence[Param]],
    *,
    which: int | None = None,
    templates: Sequence[AutoTemplate] | None = None,
    sort: bool = False,
    column: int = _DEFAULT_COLUMN,
) -> list[str]:
    """Expand /*AUTOINSTPARAM*/ markers verilog-mode style.

    Emits a ``// Parameters`` section of connections inside the instance's
    ``#( ... )`` block, resetting whatever was there before: the
    AUTO_TEMPLATE entry when one matches, otherwise the parameter connected
    by identity (``.PAR (PAR)`` — verilog-mode emits identity even when the
    parent does not define the name; the user fills it in or templates it).
    """
    text = "\n".join(lines)
    markers = _select_markers(lines, "AUTOINSTPARAM", which)
    stacks = _scan_parens_at(text, [m.offset for m in markers])
    # strip // Templated debris only inside the #( ... ) blocks this command
    # rewrites — AUTOINST pin sections keep their own tags.  Regions extend
    # to end-of-line: the debris comment trails the closing `))`.
    regions = []
    for m in markers:
        st = stacks[m.offset]
        if st:
            try:
                close = _matching_paren(text, st[-1])
            except ValueError:
                continue
            eol = text.find("\n", close)
            regions.append((st[-1], len(text) if eol < 0 else eol))
    text = _strip_templated_in_regions(text, regions)
    lines = text.split("\n")
    markers = _select_markers(lines, "AUTOINSTPARAM", which)
    stacks = _scan_parens_at(text, [m.offset for m in markers])
    for marker in reversed(markers):
        stack = stacks[marker.offset]
        if not stack:
            # a stray marker outside any instance parameter list — warn,
            # don't abort (same treatment as AUTOINST)
            print(
                f"warning: AUTOINSTPARAM marker outside an instance parameter "
                f"list (offset {marker.offset}), skipped",
                file=sys.stderr,
            )
            continue
        open_idx = stack[-1]
        close_idx = _matching_paren(text, open_idx)
        try:
            module, inst = _resolve_param_instance(text, open_idx, close_idx)
        except ValueError:
            continue  # instance array / `define'd name: skip, not expandable
        try:
            params = module_params[module]
        except KeyError:
            raise KeyError(f"module {module!r} not found for AUTOINSTPARAM instance {inst!r}") from None
        try:
            text = _auto_param_one(
                text, marker, open_idx, close_idx, params, module, inst,
                templates, sort, column,
            )
        except ValueError as exc:
            # AUTO_LISP / @"..." template evaluation outside the Python
            # subset: warn and leave the instance untouched
            print(
                f"warning: AUTOINSTPARAM {inst} ({module}) skipped: {exc}",
                file=sys.stderr,
            )
            continue
    return text.split("\n")


def _auto_param_one(
    text, marker, open_idx, close_idx, params, module, inst,
    templates, sort, column,
):
    tpl = (
        template_for_module(
            list(templates), module, before_line=text.count("\n", 0, marker.offset)
        )
        if templates
        else None
    )
    at_value = template_at_value(tpl, inst) if tpl else ""
    lisp_env = read_auto_lisp(text, marker.offset)
    lisp_env.update(read_auto_python(text, marker.offset))
    # commented-out pins before the marker are NOT connected: mask
    # comments (positions preserved) before collecting names
    pins = set(
        re.findall(
            r"\.\s*(\w+)\s*\(",
            mask_comments(text[open_idx + 1 : marker.offset], block=True),
        )
    )
    kept = [p for p in params if p.name not in pins]
    kept = _filter_regexp(kept, marker.regexp)
    if sort:
        kept = sorted(kept, key=lambda p: p.name)
    entries = []
    for param in kept:
        conn = template_connection(tpl, param.name, at_value, None, lisp_env) if tpl else None
        templated = conn is not None
        if not templated:
            # verilog-mode: every parameter connects by identity (even a
            # name the parent does not define — the user fills it in or
            # templates it); entries manually written before the marker
            # are reset, not preserved
            conn = param.name
        entries.append((param.name, conn,
                        "// Templated" if templated else None))
    if not entries:
        return text
    indent_pt = _indent_pt(text, open_idx)
    col_eff = max(column, 16 + 8 * ((indent_pt + 7) // 8))
    gen = _build_gen([("// Parameters", entries)], indent_pt, col_eff, ")")
    return _replace_region(text, marker, open_idx, close_idx, gen, consume_semi=False)


# ---------------------------------------------------------------------------
# .* save handling (verilog-delete-auto-star-implicit)

_IMPLICIT_STAR = re.compile(r"\s*// Implicit \.\*")


def delete_auto_star_implicit(lines: Sequence[str]) -> list[str]:
    """Delete non-templated ``.*`` expansion pins (those tagged
    ``// Implicit .*``), restoring the bare ``.*`` — the on-save behaviour
    when ``verilog-auto-star-save`` is nil.

    Whole pin lines are deleted; orphaned ``// Outputs``/``// Inputs``
    section comments are dropped; the last remaining pin keeps the ``);``.
    """
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _IMPLICIT_STAR.search(line):
            # this pin line is deleted; if it carried the "));" terminator,
            # push the close onto the previous real pin, else fold back to ".*);"
            body = _strip_comments(line)
            if re.search(r"\)\s*;\s*$", body):
                j = len(out) - 1
                while j >= 0 and not re.search(r"\.\s*\w+\s*\(|\.\*", out[j]):
                    j -= 1
                if j >= 0 and re.search(r"\.\s*\w+\s*\(", out[j]):
                    out[j] = out[j].rstrip()
                    if out[j].endswith(","):
                        out[j] = out[j][:-1].rstrip()
                    out[j] += ");"
                elif j >= 0 and re.search(r"\.\*", out[j]):
                    # nothing but .* left: close the pin list on the .* line
                    out[j] = re.sub(r"\.\*\s*,?\s*$", ".*);", out[j])
                i += 1
                continue
            i += 1
            continue
        out.append(line)
        i += 1
    # drop orphaned section comments (no pin line between it and the close)
    result: list[str] = []
    for idx, line in enumerate(out):
        if re.match(r"^\s*//\s*(Outputs|Inouts|Inputs|Interfaces|Interfaced)\s*$", line):
            keep = False
            for later in out[idx + 1 :]:
                if re.search(r"\.\s*\w+\s*\(", later):
                    keep = True
                    break
                if re.search(r"\)\s*;\s*$", later):
                    break
            if not keep:
                continue
        result.append(line)
    return result
