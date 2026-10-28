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
from dataclasses import dataclass, replace as _dc_replace

from .comments import mask_comments
from typing import Mapping, Sequence

from .inst import ModuleDef, Port
_NONWORD_RE = re.compile(r"\W")
_PAREN_PAIR_RE = re.compile(r"\(\((\w+)\)\)")
_CLOG2_RE = re.compile(r"\$clog2\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_NUMERIC_PAREN_EXPR_RE = re.compile(r"\(?\s*[\d+\-*/%() ]+\s*\)?")
_TEMPLATED_SPAN_RE = re.compile(r"[ \t]*,?[ \t]*//[^\n]*Templated")
from .template import (
    AutoTemplate,
    _auto_re_to_python,
    template_at_value,
    template_connection,
    template_for_module,
)

# Verilog gate primitives: name -> list of port directions.
# First port is output (or inout for tran), rest are inputs (implied).
# From emacs verilog-mode verilog-gate-ios.
_GATE_PRIMITIVES: dict[str, list[str]] = {
    "and": ["output"],
    "buf": ["output"],
    "bufif0": ["output"],
    "bufif1": ["output"],
    "cmos": ["output"],
    "nand": ["output"],
    "nmos": ["output"],
    "nor": ["output"],
    "not": ["output"],
    "notif0": ["output"],
    "notif1": ["output"],
    "or": ["output"],
    "pmos": ["output"],
    "pulldown": ["output"],
    "pullup": ["output"],
    "rcmos": ["output"],
    "rnmos": ["output"],
    "rpmos": ["output"],
    "rtran": ["inout", "inout"],
    "rtranif0": ["inout", "inout"],
    "rtranif1": ["inout", "inout"],
    "tran": ["inout", "inout"],
    "tranif0": ["inout", "inout"],
    "tranif1": ["inout", "inout"],
    "xnor": ["output"],
    "xor": ["output"],
}

_DEFAULT_COLUMN = 40

_TYPE_WORDS = {
    "int", "integer", "logic", "bit", "reg", "signed", "unsigned",
    "byte", "shortint", "longint", "time", "real", "realtime", "type",
    "string",
}


@dataclass(frozen=True)
class Param:
    """One module parameter; ``value`` is the raw default-value text.
    ``packed`` holds the declared packed ranges (``logic [5:0] BLUP``),
    which AUTOINSTPARAM renders as a subscript (``.BLUP (BLUP[5:0])``).
    ``unpacked`` holds dimensions after the name
    (``parameter bit No3 [No1:0][No2-1:0]``), rendered as a
    ``/*.[...][...]*/`` note (autoinst_unsigned_bug302.v).
    ``is_type`` marks ``parameter type NAME``."""

    name: str
    value: str | None = None
    packed: tuple = ()
    unpacked: tuple = ()
    is_type: bool = False


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


_MP_TOKEN = re.compile(r'\(|\)|"|//|/\*')


def _matching_paren(text: str, open_idx: int) -> int:
    """Offset of the ')' matching the '(' at OPEN_IDX."""
    # token-driven scan (regex search per token) — the char-by-char loop
    # cost ~2x the whole scan in profiles (per-instance callers × buffers)
    depth = 0
    pos = open_idx
    n = len(text)
    search = _MP_TOKEN.search
    while True:
        m = search(text, pos)
        if m is None:
            raise ValueError("unbalanced parentheses in instance")
        c = m.group(0)
        i = m.start()
        if c == "(":
            depth += 1
            pos = i + 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
            pos = i + 1
        elif c == '"':
            j = i + 1
            while j < n and (text[j] != '"' or text[j - 1] == "\\"):
                j += 1
            pos = j + 1
        elif c == "//":
            j = text.find("\n", i)
            pos = n if j < 0 else j
        else:  # /*
            j = text.find("*/", i + 2)
            if j < 0:
                raise ValueError("unterminated /* */ comment")
            pos = j + 2


def _scan_parens_at(text: str, offsets: Iterable[int]) -> dict[int, list[int]]:
    """Unmatched-'(' stacks at each of OFFSETS, in a single pass over TEXT.

    Equivalent to ``{o: _scan_parens(text, o) for o in offsets}`` but O(n)
    instead of O(n * len(offsets))."""
    # token-driven scan (same tokenizer as _matching_paren): the char loop
    # cost ~0.02s per whole-buffer call on big tops
    want = sorted(set(offsets))
    out: dict[int, list[int]] = {}
    stack: list[int] = []
    wi = 0
    n = len(text)
    pos = 0
    search = _MP_TOKEN.search
    while wi < len(want):
        m = search(text, pos)
        tok = n if m is None else m.start()
        # offsets before the next token see the stack as-is (comments and
        # strings never change it); an offset ON a token records BEFORE
        # the token is processed, matching the char-loop ordering
        while wi < len(want) and want[wi] <= tok:
            out[want[wi]] = list(stack)
            wi += 1
        if m is None:
            break
        c = m.group(0)
        i = m.start()
        if c == "(":
            stack.append(i)
            pos = i + 1
        elif c == ")":
            if stack:
                stack.pop()
            pos = i + 1
        elif c == '"':
            j = i + 1
            while j < n and (text[j] != '"' or text[j - 1] == "\\"):
                j += 1
            pos = j + 1
        elif c == "//":
            j = text.find("\n", i)
            pos = n if j < 0 else j
        else:  # /*
            j = text.find("*/", i + 2)
            if j < 0:
                break
            pos = j + 2
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


def _with_tick(text: str, word: str, start: int) -> str:
    """Reattach a leading backtick when the word in TEXT at START was
    written ticked (`` `NAME ``) — _prev_word strips it, but module
    resolution needs to know: a ticked name is a `define reference."""
    if word and start > 0 and text[start - 1] == "`":
        return "`" + word
    return word


def detick_name(name: str, defines: "dict[str, str]") -> str:
    """verilog-symbol-detick with wing-it for a module name: while NAME
    starts with a backtick, strip it and expand through DEFINES; an
    undefined macro keeps its bare name (`` `SUBMOD_UNDEFED `` still
    names module SUBMOD_UNDEFED — autoinst_moddefine.v). A plain name
    is returned unchanged (a module literally named like a macro is
    not expanded). Chained defines resolve in a loop, capped."""
    for _ in range(100):
        if not name.startswith("`"):
            return name
        bare = name[1:]
        val = defines.get(bare)
        if val is None:
            return bare
        name = val.strip()
    return name


def _skip_group_back(
    text: str, i: int, masked: "str | None" = None
) -> tuple[int, int] | None:
    """Skip backwards over a balanced ``( ... )`` group ending before I.

    Returns (open_idx, close_idx); None when the text before I does not end
    in ')'. Raises ValueError when the group is unbalanced.  Parens inside
    comments or strings do not count toward the balance: an unbalanced
    paren in a comment (e.g. an unbalanced ``// }}`` fold) would otherwise
    shift the located group start and silently corrupt the parameter
    values read from the block.  MASKED optionally supplies
    mask_comments(text) so repeated calls on slices of the same buffer
    do not re-mask the prefix from scratch."""
    j = _skip_back(text, i)
    if j == 0 or text[j - 1] != ")":
        return None
    close = j - 1
    # comment masking preserves length and position, so the mask of any
    # prefix text[:close+1] is identically mask_comments(text)[:close+1]
    m_slice = (
        masked[: close + 1]
        if masked is not None
        else mask_comments(text[: close + 1])
    )
    depth = 0
    j = close
    while j >= 0:
        c = m_slice[j]
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


def read_inst_param_values(
    text: str, open_idx: int, masked: "str | None" = None
) -> dict[str, str]:
    """Parse the ``#( .NAME(VALUE), ... )`` block immediately preceding the
    pin list opened at OPEN_IDX; returns {param_name: value_text}.

    Mirrors verilog-mode's ``verilog-read-inst-param-value``: positional
    entries are ignored, values have all whitespace removed.
    """
    # skip the instance name, then the #( ... ) block must close the head
    _, j = _prev_word(text, open_idx)
    # _skip_group_back scans parens over comment-masked text, so parens
    # inside comments cannot shift the located group; the group it
    # finds must also open with `#(`.  If it does not, the block cannot
    # be read reliably — no parameter values are applied rather than
    # garbage ones (a mangled read once produced the width expression
    # `((7),.DW(32)-1)` on a real ZipCPU file).
    try:
        group = _skip_group_back(text, j, masked)
    except ValueError:
        return {}
    if group is None:
        return {}
    h = group[0]
    while h > 0 and text[h - 1] in " \t\r\n":
        h -= 1
    if h == 0 or text[h - 1] != "#":
        return {}
    # split on the MASKED inner text: parens inside comments must not
    # shift comma depth either (`// {{{` inside a #(...) block once made
    # `.AW(7),.DW(32)` parse as a single entry with value `7),.DW(32`).
    # Masking preserves length, so slices and the value regexp below
    # still operate on the original characters.
    m_text = masked if masked is not None else mask_comments(text)
    inner = m_text[group[0] + 1 : group[1]]
    values: dict[str, str] = {}
    for entry in _split_top_commas(inner):
        entry = _strip_comments(entry)  # drop comments + // Templated debris
        m = re.match(r"\s*\.\s*(\w+)\s*\((.*)\)\s*$", entry, re.S)
        if m:
            values[m.group(1)] = re.sub(r"\s+", "", m.group(2))
    return values


def param_default_values(moddef) -> dict[str, str]:
    """Submodule parameter name -> default value text: the bindings an
    instance carries before any ``#(...)`` override is applied."""
    out: dict[str, str] = {}
    for p in getattr(moddef, "params", ()) or ():
        if p.value:
            out[p.name] = p.value
    return out


def effective_param_values(moddef, overrides) -> dict[str, str]:
    """Instance-effective parameter bindings: the submodule's defaults
    with the instance's own ``#(...)`` overrides winning.  A port width
    like ``[W-1:0]`` resolves against THESE, so an instance without an
    override declares its nets at the submodule's default width
    instead of leaving the width naming a submodule-local parameter
    the parent cannot see."""
    merged = param_default_values(moddef)
    merged.update(overrides or {})
    return merged


def _apply_param_values(expr: str, param_values: Mapping[str, str]) -> str:
    """Replace whole-word parameter names with their instance values, then
    fold what becomes purely numeric — ``$clog2(8)`` → ``3`` and
    ``(8)-1`` → ``7`` (verilog-mode evaluates constant expressions).
    Values may reference other parameters (``IDX_W=$clog2(VEC_W)``), so
    substitution iterates to a fixpoint.  Names iterate in REVERSE
    insertion order: verilog-mode conses par-values while reading the
    #(...) block, so its single sequential pass substitutes the last
    parameter first — for a name swap A->B, B->A that makes B win on
    both pins (autoinst_swapped_vec.v: both [] expand to [B_WL-1:0]),
    and reversed fixpoint iteration converges to the same answer.
    Doubled parens are collapsed each round and the original-text guard
    cuts substitution CYCLES so they cannot pile up nested parens."""
    original = expr
    name_res = {n: re.compile(r"\b" + re.escape(n) + r"\b") for n in param_values}
    for _ in range(8):
        prev = expr
        for name, value in reversed(list(param_values.items())):
            if value == name:
                continue  # identity self-map: no-op (avoids paren explosion)
            # Only wrap in parens if the value needs it (contains operators).
            # A simple identifier (KeyWidth) doesn't need parens; adding them
            # causes dim mismatches like (KeyWidth)-1:0 vs KeyWidth-1:0.
            replacement = f"({value})" if _NONWORD_RE.search(value) else value
            expr = name_res[name].sub(replacement, expr)
        while "((" in expr:  # collapse substitution-added nesting, one level at a time
            collapsed = _PAREN_PAIR_RE.sub(r"(\1)", expr)
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

    expr = _CLOG2_RE.sub(clog2, expr)
    if _NUMERIC_PAREN_EXPR_RE.fullmatch(expr.strip()):
        try:
            return str(int(eval(expr, {"__builtins__": {}}, {})))
        except Exception:
            pass
    return expr


_SR_PRE_PAREN = r"[\[({:*/<>+-]"
_SR_POST_PAREN = r"[])}:*/<>.+-]"
_SR_PRE_PM = r"[\[({:<>+-]"
_SR_POST_PM = r"[])}:<>+-]"
_SR_PRE_SHIFT = r"[\[({:]"
_SR_POST_SHIFT = r"[])}:<>]"
_SR_PAREN_RE = re.compile(_SR_PRE_PAREN + r"\((\w+)\)" + _SR_POST_PAREN)
_SR_CLOG2_RE = re.compile(_SR_PRE_PAREN + r"\$clog2\s*\((\d+)\)" + _SR_POST_PAREN)
_SR_MUL_RE = re.compile(_SR_PRE_PAREN + r"([0-9]+)\s*([*/])\s*([0-9]+)" + _SR_POST_PAREN)
_SR_PM_RE = re.compile(_SR_PRE_PM + r"([0-9]+)\s*([+-])\s*([0-9]+)" + _SR_POST_PM)
_SR_SHIFT_RE = re.compile(_SR_PRE_SHIFT + r"([0-9]+)\s*(<<<?|>>>?)\s*([0-9]+)" + _SR_POST_SHIFT)


def simplify_range_expression(expr: str) -> str:
    """Port of verilog-simplify-range-expression: fold constant
    arithmetic inside a bracketed bus string (``[DW-1+2:0]`` ->
    ``[DW+1:0]``), the verilog-auto-simplify-expressions behaviour
    (default t) applied by verilog-signals-combine-bus to every signal
    bus — AUTOWIRE/AIO widths, and AUTOINST pin widths once parameter
    substitution is active (param_values non-empty; without overrides
    the pin keeps the declared form).  Only numeric-numeric operations
    fold; symbols stay verbatim.  The +/- pass absorbs the preceding sign into the
    left operand (FOO-1+2 is FOO+1, FOO-1+1 keeps the -0 residual);
    division folds only when exact; $clog2 of a constant folds."""
    import math

    if not re.search(r"[-+*/<>()]", expr):
        return expr

    def _run(pat, repl, s):
        # Elisp string-match restarts from 0 after each replacement
        while True:
            m = pat.search(s)
            if not m:
                return s
            r = repl(m)
            if r is None:  # blocked (e.g. inexact division): stop pass
                return s
            s = s[: m.start()] + r + s[m.end() :]

    def _clog2(v):
        return 0 if v <= 1 else (v - 1).bit_length()

    def _paren(m):
        pre, ident, post = m.group(0)[0], m.group(1), m.group(0)[-1]
        return pre + ident + post

    def _clog2_repl(m):
        pre, num, post = m.group(0)[0], m.group(1), m.group(0)[-1]
        return pre + str(_clog2(int(num))) + post

    def _mul(m):
        pre, lhs, op, rhs, post = m.group(0)[0], m.group(1), m.group(2), m.group(3), m.group(0)[-1]
        lhs, rhs = int(lhs), int(rhs)
        if op == "/":
            if rhs == 0 or lhs % rhs != 0:
                return None
            return pre + str(lhs // rhs) + post
        return pre + str(lhs * rhs) + post

    def _pm(m):
        pre, lhs, op, rhs, post = m.group(0)[0], m.group(1), m.group(2), m.group(3), m.group(0)[-1]
        lhs, rhs = int(lhs), int(rhs)
        if pre == "-":
            lhs = -lhs
        val = lhs - rhs if op == "-" else lhs + rhs
        if val < 0:
            prefix = "" if pre in ("+", "-") else pre
        elif val > 0:
            prefix = "+" if pre == "-" else pre
        else:
            prefix = pre  # zero keeps the old +0/-0 residual
        return prefix + str(val) + post

    def _shift(m):
        pre, lhs, op, rhs, post = m.group(0)[0], m.group(1), m.group(2), m.group(3), m.group(0)[-1]
        lhs, rhs = int(lhs), int(rhs)
        if op.startswith("<"):
            return pre + str(lhs << rhs) + post
        return pre + str(lhs >> rhs) + post

    out = expr
    last = None
    while last != out:
        last = out
        out = _run(_SR_PAREN_RE, _paren, out)
        out = _run(_SR_CLOG2_RE, _clog2_repl, out)
        out = _run(_SR_MUL_RE, _mul, out)
        out = _run(_SR_PM_RE, _pm, out)
        out = _run(_SR_SHIFT_RE, _shift, out)
    return out


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


def _buffer_defines(text: str) -> dict[str, str]:
    """Raw `` `define NAME value `` text map for
    ``verilog-symbol-detick-text`` (Emacs's vh-* table, buffer scope;
    a valueless define maps to "1", as in verilog-set-define).
    Comments are masked first so a commented-out `define is not picked
    up.  Include files are not read (limitation — the common case is
    in-file defines)."""
    from .comments import mask_comments

    defines: dict[str, str] = {}
    for m in re.finditer(
        r"^\s*`define\s+(\w+)(?:\s+(.*?))?\s*$",
        mask_comments(text),
        re.M,
    ):
        defines[m.group(1)] = m.group(2).strip() if m.group(2) else "1"
    return defines


def _read_defines_vh_vars(text: str) -> dict[str, str]:
    """``vh-<name>`` -> value for simple parameter/localparam assignments
    in TEXT, for ``eval:(verilog-read-defines)``: verilog-mode stores
    every parameter (and localparam) value in a ``vh-`` variable, which
    ``@\"...\"`` templates and AUTOINSERTLISP ``insert`` forms then read
    (read_define_param.v: vh-Pp=1, vh-Lp=2, vh-Pt=3).  A UDT type word
    ahead of the name (``mytype_t Pt``) is skipped like any other type
    word; packed/unpacked dims are skipped; valueless names get no
    entry.  Header ``#(...)`` lists are scanned as well as body decls."""
    from .comments import mask_comments

    masked = mask_comments(text)
    values: dict[str, str] = {}

    def _entries_from(chunk: str) -> None:
        for entry in _split_top_commas(chunk):
            s = entry.strip()
            s = re.sub(r"^(parameter|localparam)\b", "", s).strip()
            if not s:
                continue
            # the assignment '=' at bracket/paren depth 0
            depth = 0
            eq = -1
            for k, ch in enumerate(s):
                if ch in "[({":
                    depth += 1
                elif ch in "])}":
                    depth -= 1
                elif ch == "=" and depth == 0:
                    eq = k
                    break
            if eq < 0:
                continue
            lhs, rhs = s[:eq], s[eq + 1 :].strip()
            # strip bracket groups; the name is the last identifier left
            lhs = re.sub(r"\[[^\]]*\]", " ", lhs)
            names = re.findall(r"[a-zA-Z_]\w*", lhs)
            if not names or not rhs:
                continue
            values[names[-1]] = rhs

    for bm in re.finditer(r"^\s*(?:parameter|localparam)\b([^;]*);", masked, re.M):
        _entries_from(bm.group(1))
    m = re.search(r"\bmodule\s+\w+\s*(?:import\s+[\w:]+\s*::\s*\*\s*;\s*)?#\s*\(", masked)
    if m:
        try:
            inner, _end = _balanced(masked, masked.index("(", m.start()))
        except ValueError:
            inner = ""
        _entries_from(inner)
    return {f"vh-{name}": val for name, val in values.items()}


def read_local_eval_defuns(text: str) -> dict:
    """User elisp definitions from the file's Local Variables ``eval:``
    block(s) — Emacs evaluates them when the file is visited, so they
    are in scope for every ``@"..."`` template expression, e.g. the
    ``vl-prefix-i-o`` defun in autoinst_ma_io_prefix.v's trailing block::

        Local Variables:
        eval:
         (defun vl-prefix-i-o (dir) (cond ...))
        End:

    Only ``defun`` (and ``setq`` side effects) are kept; a form that
    fails to parse or evaluate is ignored — a bad eval block must not
    abort the file.  Returns a name -> value env dict.
    """
    from .template import _elisp_eval, _elisp_parse, _elisp_tokenize

    env: dict = {}
    # The End: line may carry a line-comment prefix (// End: in
    # autoinst_regexp_match.v) or stand bare ( End: in
    # autoinst_ma_io_prefix.v) — accept both.
    m = re.search(r"Local Variables:(.*?)^[ \t]*(?://)?[ \t]*End:", text, re.S | re.M)
    if not m:
        return env
    section = m.group(1)
    var_line = re.compile(r"^\s*[A-Za-z][\w-]*\s*:")
    for em in re.finditer(r"eval\s*:", section):
        lines = section[em.end() :].split("\n")
        buf: list[str] = []
        depth = 0
        started = False
        for raw in lines:
            line = raw
            if not started and not buf:
                if var_line.match(line):
                    break
                if not line.strip():
                    continue
            buf.append(line)
            # paren balance, ignoring strings and ;-to-EOL comments
            in_str = False
            i = 0
            while i < len(line):
                c = line[i]
                if in_str:
                    if c == "\\":
                        i += 2
                        continue
                    if c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == ";":
                    break
                elif c == "(":
                    depth += 1
                    started = True
                elif c == ")":
                    depth -= 1
                i += 1
            if started and depth <= 0:
                form_text = "\n".join(buf).strip()
                buf = []
                started = False
                try:
                    _elisp_eval(
                        _elisp_parse(_elisp_tokenize(form_text)), env
                    )
                except Exception:  # noqa: BLE001 - ignore bad eval forms
                    pass
                # a following defun (not a variable line) continues;
                # a variable line ends this eval: entry
                continue
    # eval:(verilog-read-defines ...) populates vh-<param> variables
    # from the file's parameter/localparam values (read_define_param.v);
    # a filename argument (another file's defines) is not supported.
    if re.search(r"eval\s*:\s*\(\s*verilog-read-defines\b", section):
        env.update(_read_defines_vh_vars(text))
    return env


def read_auto_lisp(text: str, upto: int) -> dict:
    """Evaluate every ``/*AUTO_LISP(...)*/`` expression before UPTO
    offset, in buffer order. Returns a dict of assigned names.

    Two tracks, in order: the Python subset first (existing behavior —
    ``/*AUTO_LISP(base = 10)*/``), then the elisp s-expression the
    marker's own parentheses imply (``/*AUTO_LISP(setq num 1)*/`` feeds
    ``@"num"`` templates — autoinst_lopaz.v).  The elisp track wraps a
    bare form in parens (the marker already consumed them); an already
    parenthesised form parses as-is.  Failing both tracks still raises
    ValueError, and a failing instance is left untouched by the caller.
    """
    from .template import _elisp_eval, _elisp_parse, _elisp_tokenize

    env: dict = {}
    if "AUTO_LISP" not in text:
        return env  # no marker can match the regex below (substring gate)
    import warnings

    for m in re.finditer(r"/\*\s*AUTO_LISP\s*\(", text[:upto]):
        expr, _ = _balanced(text[:upto], m.end() - 1)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                code = compile(expr, "<AUTO_LISP>", "exec")
            exec(code, {"__builtins__": {}}, env)
            continue
        except Exception:  # noqa: BLE001 - fall through to the elisp track
            pass
        try:
            src = expr.strip()
            if not src.startswith("("):
                src = f"({src})"
            _elisp_eval(_elisp_parse(_elisp_tokenize(src)), env)
        except Exception as exc:  # noqa: BLE001 - report, as before
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


def _read_auto_python_inline(text: str, upto: int, base_env: dict | None = None) -> dict:
    """Evaluate inline ``/*AUTO_PYTHON(...)*/`` blocks before UPTO, starting
    from a precomputed BASE_ENV (file-local Python files, hoisted by the
    caller). This avoids re-scanning file-local vars per instance (perf)."""
    env: dict = dict(base_env) if base_env else {}
    if "AUTO_PYTHON" not in text:
        return env  # no marker can match the regex below (substring gate)
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
    # localparam is never overridable: verilog-decls-get-gparams (and the
    # AUTOINSTPARAM built on it) collects `parameter` only — a localparam in
    # the #(...) header must not become a .NAME(NAME) override
    if re.match(r"^localparam\b", entry):
        return None
    entry = re.sub(r"^parameter\b", "", entry).strip()
    # `parameter type NAME`: a type parameter (its override substitutes
    # into port data_types under verilog-auto-inst-param-value-type).
    is_type = False
    _tm = re.match(r"^type\s+(?=\w)", entry)
    if _tm:
        is_type = True
        entry = entry[_tm.end() :].lstrip()
    packed: list[str] = []
    while True:
        m = re.match(r"(\w+)\s+", entry)
        if m and m.group(1) in _TYPE_WORDS:
            entry = entry[m.end() :].lstrip()
            continue
        if entry.startswith("["):
            j = entry.find("]")
            if j < 0:
                return None
            packed.append(re.sub(r"\s+", "", entry[1:j]))
            entry = entry[j + 1 :].lstrip()
            continue
        break
    m = re.match(r"(\w+)", entry)
    if not m:
        return None
    name = m.group(1)
    rest = entry[m.end():].lstrip()
    # Unpacked dimensions after the name: No3 [No1:0][No2-1:0] = ...
    unpacked: list[str] = []
    while rest.startswith("["):
        j = rest.find("]")
        if j < 0:
            return None
        unpacked.append(re.sub(r"\s+", "", rest[1:j]))
        rest = rest[j + 1:].lstrip()
    # Now rest should be [= value] [,;] or empty
    vm = re.match(r"(?:=\s*(.*?))?\s*[;,]?\s*$", rest, re.S)
    if not vm:
        return None
    value = vm.group(1)
    return Param(
        name, value.strip() if value else None, tuple(packed),
        tuple(unpacked), is_type,
    )


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
    # Allow 'import pkg::*;' between module name and #( (SystemVerilog)
    m = re.search(r"\bmodule\s+\w+\s*(?:import\s+[\w:]+\s*::\s*\*\s*;\s*)?#\s*\(", text)
    if m:
        open_idx = text.index("(", m.start())
        inner, end = _balanced(text, open_idx)
        header_span = (open_idx, end)
        params.extend(_params_from_entries(_split_top_commas(inner)))
    body = text
    if header_span:
        start, end = header_span
        body = text[:start] + " " * (end - start) + text[end:]
    for bm in re.finditer(r"^\s*parameter\b([^;]*);", body, re.M):
        params.extend(_params_from_entries(_split_top_commas(bm.group(1))))
    return tuple(params)


def _params_from_entries(entries) -> list[Param]:
    """Parse comma-split parameter entries, with type/dims inheritance:
    in ``parameter logic [5:0] BLUP=3, ZOT=4`` the type and dims apply
    to every name in the list (autoinstparam_int.v: ZOT gets [5:0]
    too).  A new ``parameter``/``localparam`` keyword starts a new
    declaration and resets the inherited dims."""
    out: list[Param] = []
    cur_packed: tuple = ()
    cur_is_type = False
    for entry in entries:
        stripped = entry.strip()
        is_new = stripped.startswith("parameter") or stripped.startswith("localparam")
        param = _param_from_entry(entry)
        if param is None:
            if stripped.startswith("localparam"):
                cur_packed = ()
                cur_is_type = False
            continue
        if is_new:
            cur_packed = param.packed
            cur_is_type = param.is_type
        else:
            if not param.packed:
                param = _dc_replace(param, packed=cur_packed)
            if not param.is_type and cur_is_type:
                param = _dc_replace(param, is_type=True)
        out.append(param)
    return out


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
    matched as a bare token (used for SystemVerilog .* expansion) and block
    comments are masked too: a real .* is code, never comment text — an
    AUTO_TEMPLATE regexp like ``.foo_\\(.*\\)_n (bar_\\1)`` must not be
    collected as a star marker (it used to spam "marker outside an
    instance pin list" on template-heavy files)."""
    text = "\n".join(lines)
    # line comments blanked, so a match whose first char was masked sits
    # inside a // comment, including a trailing one (``code; // /*autoinst*/``)
    # Block comments kept for real markers (markers ARE block comments),
    # masked for the bare .* token (it must live in actual code).
    masked = mask_comments(text, block=not require_comment)
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


def _resolve_instance_at(
    text: str, open_idx: int, masked: "str | None" = None
) -> tuple[str, str]:
    """(module, instance) for the pin list opened at OPEN_IDX.

    The module keeps a leading backtick when written ticked
    (`` `SUBMOD_A ``) — callers expand it with detick_name before
    looking the module up; the instance name is returned bare."""
    m_full = masked if masked is not None else mask_comments(text)
    inst, j = _prev_word(text, open_idx)
    if not inst:
        # Pin list preceded by a #(...) parameter-override group:
        # module inst #(...) ( ... ) — skip the group (and its #)
        # and read the instance name before it (autoinstparam_int.v)
        grp = _skip_group_back(text, open_idx, m_full)
        if grp is not None:
            k = grp[0]
            while k > 0 and text[k - 1] in " \t\n":
                k -= 1
            if k > 0 and text[k - 1] == "#":
                k -= 1
                inst, j = _prev_word(text, k)
    if not inst:
        # Might be an array instance: u_or [31:0] (
        # Skip back over [...] and try again
        k = _skip_back(text, open_idx)
        if k > 0 and text[k - 1] == "]":
            # Find matching [
            depth = 1
            k -= 1
            while k > 0 and depth > 0:
                k -= 1
                if text[k] == "]":
                    depth += 1
                elif text[k] == "[":
                    depth -= 1
            if depth == 0:
                inst, j = _prev_word(text, k)
    if not inst:
        raise ValueError("AUTOINST: cannot resolve instance name")
    # a ticked instance name (`` `SUBNAME_B ``) leaves j at the word start
    # with the backtick just before it; step past it so the module word
    # (and any #(...) group) before it is reachable
    if j > 0 and text[j - 1] == "`":
        j -= 1
    group = _skip_group_back(text, j, m_full)
    if group is not None:
        # parameter override: the #( ... ) group before the instance name
        j = group[0]
        while j > 0 and text[j - 1] in " \t\n":
            j -= 1
        if j > 0 and text[j - 1] == "#":
            j -= 1
    module, mj = _prev_word(text, j)
    module = _with_tick(text, module, mj)
    # Handle # <value> (e.g., buf #1 mybuf): module would be "1", skip it
    if module and module[0].isdigit():
        # Skip back over the number and #
        k = mj
        while k > 0 and text[k - 1] in " \t\n":
            k -= 1
        if k > 0 and text[k - 1] == "#":
            k -= 1
            module, mk = _prev_word(text, k)
            module = _with_tick(text, module, mk)
    if not module:
        raise ValueError("AUTOINST: cannot resolve module name")
    if module in ("module", "macromodule"):
        # A module declaration's own port list (module name #(...)
        # (...)) is never an instance pin list: the #(...) group-skip
        # above reads the declaration name as the instance and the
        # keyword as the module.  This also keeps a literal .* inside
        # an AUTOARG regexp in such a header (autoinst_tennant.v) from
        # resolving as a star-expansion instance.
        raise ValueError("AUTOINST: module declaration, not an instance")
    return module, inst


def inst_pin_connections(
    text: str,
    open_idx: int,
    mark_shorthand: bool = False,
    with_positions: bool = False,
) -> "list[tuple]":
    """(pin, expression) for every named connection in the pin list opened
    at OPEN_IDX — the whole list, on either side of any AUTO marker:
    multi-pin lines, multi-line expressions and SystemVerilog ``.pin``
    shorthand (expression = pin name) included.  Expressions are returned
    verbatim, comments intact (the /*[D1][D2]*/ packed-dims note rides in
    one); callers strip as needed.  With MARK_SHORTHAND, each item gains
    two more elements: the ``.pin`` shorthand flag, and True when the
    pin carries a ``// Templated`` comment (EAI template expansion).
    The driven-net scan needs both because verilog-mode inherits the
    port's bits ONLY for shorthand (verilog-read-sub-decls-sig: ``(if
    dotname (verilog-sig-bits portdata) vec)``) — but non-template
    /*AUTOINST*/ pins gain brackets from EAI before AW/AIO run, so port
    inheritance is the correct stand-in for them; only a template pin
    without brackets or a dims note stays widthless (autoinst_star.v:
    ``wire templated`` next to ``wire [w-1:0] pin``)."""
    close_idx = _matching_paren(text, open_idx)
    span = text[open_idx + 1 : close_idx]
    masked = mask_comments(span)
    out: list[tuple] = []
    pin_re = re.compile(r"\.\s*(\\[^ \t(,]+|\w+)")
    # Escaped pin names run to whitespace (.\o[10] , .\i&e; ) — the
    # plain \w+ alternative never matches past the backslash.
    # With_positions appends the pin's absolute text offset as the
    # last element, for the section-visibility filter in the driven
    # net scan (pins before the first section comment are invisible
    # to verilog-read-sub-decls).
    i = 0
    n = len(masked)
    while i < n:
        m = pin_re.search(masked, i)
        if not m:
            break
        pin = m.group(1)
        pos = open_idx + 1 + m.start()
        k = m.end()
        while k < n and masked[k] in " \t\n":
            k += 1
        if k >= n or masked[k] != "(":
            # .pin shorthand (followed by , or close)
            item = (pin, pin, True, False) if mark_shorthand else (pin, pin)
            out.append(item + (pos,) if with_positions else item)
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
        templated = False
        if mark_shorthand:
            # EAI marks template-expanded pins with a // Templated
            # comment after the close paren, same line — with an
            # optional trailing comma between (aall's star expansion
            # emits `.pin (templated), // Templated`). Masking
            # preserves offsets, so the unmasked span reads at j too.
            templated = (
                _TEMPLATED_SPAN_RE.match(span[j:]) is not None
            )
        item = (
            (pin, span[k + 1 : j - 1], False, templated)
            if mark_shorthand
            else (pin, span[k + 1 : j - 1])
        )
        out.append(item + (pos,) if with_positions else item)
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
    module, mj = _prev_word(text, j)
    module = _with_tick(text, module, mj)
    if not module:
        raise ValueError("AUTOINSTPARAM: cannot resolve module name")
    # the instance name follows the block; // comment lines may intervene
    mi = re.match(r"(?:\s|//[^\n]*)*(\w+)\s*\(", text[close_idx + 1 : close_idx + 1000])
    inst = mi.group(1) if mi else ""
    if not inst:
        # shape 2: module inst #(...) ( ... ) — the word before '#'
        # was the instance name, not the module (autoinstparam_int.v)
        inst = module
        module, mj = _prev_word(text, mj)
        module = _with_tick(text, module, mj)
        # Handle # <value> (e.g., buf #1 mybuf): module would be "1"
        if module and module[0].isdigit():
            k = mj
            while k > 0 and text[k - 1] in " \t\n":
                k -= 1
            if k > 0 and text[k - 1] == "#":
                k -= 1
                module, mk = _prev_word(text, k)
                module = _with_tick(text, module, mk)
        if not module:
            raise ValueError("AUTOINSTPARAM: cannot resolve module name")
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
    text_masked = mask_comments(text)
    defines = _buffer_defines(text)
    modules = []
    for marker in markers:
        stack = stacks[marker.offset]
        if not stack:
            continue  # unresolvable marker layout (e.g. instance arrays)
        open_idx = stack[-1]
        try:
            if keyword == "AUTOINST" or text[marker.offset : marker.end] == ".*":
                modules.append(
                    detick_name(
                        _resolve_instance_at(text, open_idx, text_masked)[0],
                        defines,
                    )
                )
            else:
                modules.append(
                    detick_name(
                        _resolve_param_instance(
                            text, open_idx, _matching_paren(text, open_idx)
                        )[0],
                        defines,
                    )
                )
        except ValueError:
            continue  # instance arrays: not expandable, skip
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

    Entries are (name, conn, comment, guard); GUARD is a port's
    preprocessor condition tuple from the submodule header (empty for
    unconditional pins).  Guarded pins are re-emitted inside their
    `` `ifdef ``/`` `ifndef ``/`` `endif `` directives.  Commas must be
    right in every macro branch: between two adjacent pins the
    separator rides on the earlier pin when the later pin is present
    whenever the earlier is, on the later pin (leading comma) when the
    earlier is present whenever the later is, and on its own line
    under the intersection guard when the two guards are incomparable.
    The final pin never carries a trailing comma; when it is guarded
    the closer goes on its own line after the closing `` `endif ``.
    """
    # verilog-mode: indent-to(col_eff + 24) then insert " // ..." (leading
    # space), so the comment text starts at col_eff + 24 + 1.
    comment_col = col_eff + (24 if col_eff < 48 else 16) + 1
    if not any(entries for _h, entries in sections):
        return ""  # no ports at all
    if not any(guard for _h, entries in sections for *_e, guard in entries):
        out = []
        last_comma = -1  # comma position of the last port line (always out[-1])
        last_comment = None
        for header, entries in sections:
            out.append(" " * indent_pt + header)
            for name, conn, comment, _guard in entries:
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
        # strip the comment off the last line, replace the trailing "," with
        # the closer, then re-add the comment padded to the same target column.
        last = out[-1]
        if last_comment:
            last = last[: last.index(last_comment)].rstrip()
        last = last[:last_comma] + closer
        if last_comment:
            last += " " * max(comment_col - len(last), 1) + last_comment
        out[-1] = last
        return "\n" + "\n".join(out)

    flat = [
        (si, name, conn, comment, guard)
        for si, (_h, entries) in enumerate(sections)
        for (name, conn, comment, guard) in entries
    ]
    guards = [g for *_x, g in flat]
    from .inst import guard_separator_plan

    trailing, leading, bridges = guard_separator_plan(guards)
    n = len(flat)

    out: list[str] = []
    cur: tuple = ()

    def guard_to(target: tuple) -> None:
        nonlocal cur
        while cur != target[: len(cur)]:
            out.append(" " * indent_pt + "`endif")
            cur = cur[:-1]
        for macro, pol in target[len(cur) :]:
            out.append(" " * indent_pt + ("`ifdef " if pol else "`ifndef ") + macro)
        cur = target

    def pin_line(idx: int, last: bool) -> str:
        _si, name, conn, comment, _g = flat[idx]
        line = " " * indent_pt + (", ." if leading[idx] else ".") + name
        if conn is not None:  # None => SystemVerilog .name syntax
            line += " " * max(col_eff - len(line), 0) + "(" + conn + ")"
        if trailing[idx]:
            line += ","
        if last and not flat[idx][4]:
            line += closer
        if comment:
            line += " " * max(comment_col - len(line), 1) + comment
        return line

    prev_si = -1
    for idx in range(n):
        si = flat[idx][0]
        for cond in bridges.get(idx, []):
            guard_to(cond)
            out.append(" " * indent_pt + ",")
            guard_to(())
        if si != prev_si:
            guard_to(())  # a guard never spans a section header
            out.append(" " * indent_pt + sections[si][0])
            prev_si = si
        guard_to(flat[idx][4])
        out.append(pin_line(idx, idx == n - 1))
    guard_to(())
    if flat[-1][4]:
        out.append(" " * indent_pt + closer)
    return "\n" + "\n".join(out)


def _region_edit(
    text: str,
    marker: AutoMarker,
    open_idx: int,
    close_idx: int,
    gen: str,
    consume_semi: bool,
) -> "tuple[int, int, str]":
    """The (start, end, replacement) edit for splicing GEN between the
    marker and the pin-list close paren, adding a comma after a manually
    connected pin that lacks one. For a star (.*) instance the marker is
    kept and a comma appended after it.  Edits of different instances are
    disjoint, so a pass can accumulate and apply them in one rebuild."""
    head_end = marker.end
    tail_start = close_idx + 1
    if consume_semi:
        sm = re.match(r"\s*;[ \t]*", text[tail_start:])
        if sm:
            tail_start += sm.end()
    star = text[marker.offset : marker.end] == ".*"
    pin_region = text[open_idx + 1 : marker.offset]
    # Trailing comment lines between the last pin and the marker (e.g.
    # ZipCPU's ``// }}}`` fold markers) hide the pin's closing paren from
    # the endswith check; peel them off so the comma is still added --
    # otherwise the generated pins fuse with the last kept pin into
    # invalid syntax (emacs exhibits the same gap, but an invalid result
    # is not worth replicating).
    code_tail = re.sub(r"(\s*(//[^\n]*|/\*[\s\S]*?\*/)\s*)+$", "", pin_region).rstrip()
    if star:
        # keep .*, expand after it
        return marker.end, tail_start, "," + gen
    if code_tail.endswith(".*"):
        # a kept star line (VeeR-EL2 style: `.clk(clk), .* /*AUTOINST*/`)
        # needs the same separator comma before the generated pins —
        # verilog-mode emits `.*,`; without it the splice is a syntax
        # error (`.*` followed directly by `.port(...)`).
        insert_at = open_idx + 1 + len(code_tail)
        return insert_at, tail_start, "," + text[insert_at:marker.end] + gen
    if re.search(r"\.\s*\w+\s*\(", pin_region) and code_tail.endswith(")"):
        insert_at = open_idx + 1 + len(code_tail)
        return insert_at, tail_start, "," + text[insert_at:marker.end] + gen
    return head_end, tail_start, gen


def _apply_text_edits(text: str, edits: "list[tuple[int, int, str]]") -> str:
    """Apply non-overlapping (start, end, replacement) EDITS in one pass."""
    out = []
    pos = 0
    for s, e, rep in sorted(edits):
        out.append(text[pos:s])
        out.append(rep)
        pos = e
    out.append(text[pos:])
    return "".join(out)


def _replace_region(
    text: str,
    marker: AutoMarker,
    open_idx: int,
    close_idx: int,
    gen: str,
    consume_semi: bool,
) -> str:
    """Splice GEN between the marker and the pin-list close paren (single
    edit, see :func:`_region_edit`)."""
    return _apply_text_edits(text, [_region_edit(text, marker, open_idx, close_idx, gen, consume_semi)])


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
    parent_iface_modports: Mapping[str, str | None] | None = None,
) -> tuple[str, bool, bool]:
    """(connection, templated, nohookup) for PORT: identity ``port[bits]``
    unless a template entry matches. Multidimensional ports get a
    verilog-mode ``port/*[packed].[unpacked]*/`` comment instead of an
    expanded range. With ``param_values`` (the instance's ``#(...)``
    overrides), parameters inside the port's width are substituted. ENV
    feeds @"..." templates. PARENT_SIGNALS maps net name -> width in
    the instantiating module (for verilog-auto-inst-vector:nil
    matching). NOHOOKUP is the matched template entry's trailing //
    AUTONOHOOKUP flag (False for non-template connections)."""
    width = port.width
    if width and param_values:
        width = _apply_param_values(width, param_values)
        # Live-Emacs verified: once an instance carries #(...) overrides
        # AND verilog-auto-inst-param-value is on (i.e. param_values is
        # non-empty), the substituted pin width IS simplified — [4>>2:0]
        # folds to [1:0] (autowire_shifts_bug1346), and even a
        # parameter-free [224*1-1:128*1] folds to [223:128].  With no
        # overrides (or the flag off) the pin keeps the declared form
        # verbatim (autowire_merge_bug303 keeps [224*1-1:128*1]).
        if simplify:
            folded = simplify_range_expression(f"[{width}]")
            width = folded[1:-1] if folded.startswith("[") else folded
    # a multidim port's shape rides as a verilog-mode /*[D1][D2]*/ note —
    # in default connections AND as the []/[][] expansion in templates.
    # Dims get the instance's #(...) substitutions, redundant-paren
    # stripping and pure-numeric folding (2*16-1 -> 31); symbols stay
    # verbatim.
    packed_note = ""
    packed_last = ""
    if port.is_multidim:
        from .autodef import _clean_dim

        dims = port.packed
        if param_values:
            dims = tuple(_apply_param_values(d, param_values) for d in dims)
        inner = "".join(f"[{_clean_dim(d)}]" for d in dims)
        if port.unpacked:
            udims = port.unpacked
            if param_values:
                udims = tuple(_apply_param_values(u, param_values) for u in udims)
            inner += "." + "".join(f"[{_clean_dim(u)}]" for u in udims)
        packed_note = f"/*{inner}*/"
        # verilog-mode vl-bits: the LAST packed dimension — what a single
        # [] in a template expands to (autoinst_array_braket.v)
        if dims:
            packed_last = f"[{_clean_dim(dims[-1])}]"
    if template is not None:
        hit: list = []
        conn = template_connection(
            template, port.name, at_value, width, env,
            vl_cell_name=inst_name, vl_dir=port.direction or "",
            packed_note=packed_note, packed_last=packed_last,
            on_hit=hit.append,
        )
        if conn is not None:
            if param_values:
                conn = _apply_param_values(conn, param_values)
            return conn, True, bool(hit and hit[0].nohookup)
    # interface ports connect to a same-named interface in the parent,
    # qualified with the modport when the port declares one (bus.master);
    # unpacked array dims ride as a /*.[dims]*/ note (bus.master
    # bus_slave [NUM_SLAVES] -> bus_slave.master/*.[NUM_SLAVES]*/).
    # EXCEPTION (autoinst_mplist.sv, documented in the file itself):
    # when the parent already declares that interface WITH a modport
    # (mbl_if.master msg_resp_if), the modport must NOT be respecified
    # on the connection — VCS rejects the respecification.
    if port.is_interface:
        if (
            parent_iface_modports
            and parent_iface_modports.get(port.name)
        ):
            conn = port.name
        else:
            conn = f"{port.name}.{port.modport}" if port.modport else port.name
        if port.unpacked:
            conn += "/*." + "".join(f"[{u}]" for u in port.unpacked) + "*/"
        return conn, False, False
    # verilog-mode: multidimensional ports are not expanded into the
    # connection; their full shape is attached as a /*...*/ comment.
    if port.is_multidim:
        return f"{port.name}{packed_note}", False, False
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
    return port.name + (f"[{width}]" if width and use_vector else ""), False, False


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


def _local_wire_type(lines: Sequence[str]) -> str:
    """verilog-auto-wire-type file-local (e.g. // verilog-auto-wire-type:
    "logic"): the type word AUTOINPUT/AUTOOUTPUT/AUTOINOUT add to an
    otherwise untyped declaration.  Empty when unset."""
    m = re.search(
        r"verilog-auto-wire-type\s*:\s*\"?([A-Za-z_][\w$]*)\"?",
        "\n".join(lines),
    )
    return m.group(1) if m else ""


def _local_inst_templated_comment(lines: Sequence[str]) -> bool:
    """verilog-auto-inst-templated-comment file-local (default t): when nil,
    AUTOINST omits the '// Templated' comment on template-expanded pins
    (issue #1898)."""
    return _local_bool(lines, "verilog-auto-inst-templated-comment", True)


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
    they were AUTOINST (``verilog-auto-star-expand``).  Star pins are
    always tagged ``// Implicit .*``; a caller producing final output
    with ``star_save=False`` must run :func:`delete_auto_star_implicit`
    over the result to restore the bare ``.*``
    (``verilog-auto-star-save`` nil behaviour).

    Every ``/*AUTO_LISP(...)*/`` before each instance is evaluated first and
    its bindings feed ``@"..."`` template expressions (Python subset).
    """
    # file-local vars OR with CLI flags (either enables the behavior)
    sort = sort or _local_inst_sort(lines)
    dot_name = dot_name or _local_inst_dot_name(lines)
    templated_comment = _local_inst_templated_comment(lines)
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
        if text[m.offset : m.end] == ".*":
            # star regions are regenerated wholesale (pins follow the
            # marker and are discarded), and their // Templated tags must
            # survive: they distinguish auto pins from hand-written ones
            continue
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
    # mask once: _resolve_instance_at needs the masked text, and masking
    # the full buffer per-instance is O(n^2) on many instances (perf)
    m_full = mask_comments(text)
    # file-local vars are per-file, not per-instance: hoist out of the loop
    # (was O(n) regex scans on many instances)
    template_required = _inst_template_required(lines)
    inst_vector = _inst_vector(lines)
    simplify = _local_simplify_expressions(lines)
    from .autodef import set_simplify as _set_simplify

    _set_simplify(simplify)  # combine-bus/_clean_dim share the gate
    # `define table for detick-expanding ticked module names (`SUBMOD_A);
    # define lines are never rewritten by the loop, so one scan suffices
    defines = _buffer_defines(text)
    # Per-enclosing-module parent facts (inst_vector:nil decl ranges,
    # interface modport ports), memoized across the loop: the loop
    # rewrites instance bodies only, so they are constant for the pass
    # (was: buffer split + module_spans + header re-parse PER INSTANCE).
    parent_facts = _ParentFactsCache(text, want_signals=inst_vector == "nil")
    # file-local Python files: load once, not per-instance
    py_base_env: dict = {}
    for py_file in _local_python_files(lines):
        resolved = _resolve_python_file(py_file)
        if resolved:
            try:
                py_base_env.update(_load_python_file(resolved))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"AUTO_PYTHON file {py_file!r} load failed: {exc}"
                ) from exc
    edits: list = []
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
        if is_star and _star_region_has_manual_pins(text[marker.end : close_idx]):
            # a .* instance already carrying hand-written pins
            # (``.*, .foo (x)``) is NOT star-expanded by verilog-mode —
            # regenerating the region would drop the manual connections
            continue
        try:
            module, inst = _resolve_instance_at(text, open_idx, m_full)
            module = detick_name(module, defines)
        except ValueError:
            continue  # instance array: skip, not expandable
        try:
            moddef = modules[module]
        except KeyError:
            raise KeyError(f"module {module!r} not found for AUTOINST instance {inst!r}") from None
        try:
            edit = _auto_inst_one(
                text, marker, open_idx, close_idx, moddef, module, inst,
                templates, sort, dot_name, column, param_value, is_star, star_save,
                template_required=template_required,
                inst_vector=inst_vector,
                simplify=simplify,
                m_full=m_full,
                templated_comment=templated_comment,
                py_base_env=py_base_env,
                defines=defines,
                parent_facts=parent_facts,
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
        if edit is not None:
            edits.append(edit)
    # one rebuild for all instances: disjoint regions, bottom-up order —
    # every read in _auto_inst_one is at-or-before the instance being
    # expanded, so the original text serves every iteration
    return _apply_text_edits(text, edits).split("\n")


def _parent_module_facts(
    tlines: Sequence[str], line_no: int, want_signals: bool
) -> "tuple[dict[str, str], dict[str, str | None]]":
    """(parent_signals, parent_iface_modports) of the module span holding
    LINE_NO in TLINES.

    parent_signals: the enclosing module's MANUAL declaration ranges, for
    inst_vector:nil width matching — every wire/reg/logic AND io
    declaration, multi-name decls included; declarations inside an
    AUTO-generated region (// Beginning of automatic ... // End of
    automatics) do NOT count: verilog-mode skips the subscript "when it
    matches the entire bus as declared in the module (AUTOWIRE signals
    always are subscripted, you must manually declare the wire to have
    the subscripts removed)", so the second run's own AUTOOUTPUT/AUTOWIRE
    declarations must not suppress what the first run emitted.  Computed
    only when WANT_SIGNALS (inst_vector == "nil").

    parent_iface_modports: the enclosing module's interface ports WITH a
    modport (header declarations like ``mbl_if.master msg_resp_if``):
    connections to them must not respecify the modport (see _connect)."""
    from .inst import module_spans, parse_module_ports
    from .misc import _scan_decls

    parent_signals: dict[str, str] = {}
    parent_iface_modports: dict[str, str | None] = {}
    if want_signals:
        for lo, hi in module_spans(tlines):
            if lo <= line_no <= hi:
                seg: list[str] = []
                in_auto = False
                for ln in tlines[lo : hi + 1]:
                    if re.match(r"\s*// Beginning of automatic\b", ln):
                        in_auto = True
                    if in_auto:
                        seg.append("")
                        if "// End of automatics" in ln:
                            in_auto = False
                    else:
                        seg.append(ln)
                for d in _scan_decls(seg):
                    if d.bits:
                        rng = re.sub(r"\s+", "", d.bits).strip("[]")
                        parent_signals.setdefault(d.name, rng)
                break
    try:
        for lo, hi in module_spans(tlines):
            if lo <= line_no <= hi:
                for p in parse_module_ports(tlines[lo : hi + 1]).ports:
                    if p.is_interface:
                        parent_iface_modports[p.name] = p.modport
                break
    except Exception:
        parent_iface_modports = {}
    return parent_signals, parent_iface_modports


class _ParentFactsCache:
    """Memoized :func:`_parent_module_facts` over one auto_inst pass.

    The bottom-up loop rewrites instance bodies only, so a module's
    declaration text — and therefore these facts — is constant for the
    whole pass; marker offsets of unprocessed instances stay valid, so
    their line numbers can be read off the pre-loop text.  Keyed by the
    enclosing span's start line.  Without this, each instance re-split
    the buffer and re-parsed its module header (O(instances x buffer)).
    """

    def __init__(self, text: str, want_signals: bool) -> None:
        from .inst import module_spans

        self._tlines = text.split("\n")
        self._spans = module_spans(self._tlines)
        self._want_signals = want_signals
        self._line_off = [0]
        for ln in self._tlines:
            self._line_off.append(self._line_off[-1] + len(ln) + 1)
        self._facts: "dict[int, tuple[dict[str, str], dict[str, str | None]]]" = {}

    def facts(
        self, marker_offset: int
    ) -> "tuple[dict[str, str], dict[str, str | None]]":
        import bisect

        line_no = bisect.bisect_right(self._line_off, marker_offset) - 1
        for s, e in self._spans:
            if s <= line_no <= e:
                if s not in self._facts:
                    self._facts[s] = _parent_module_facts(
                        self._tlines, line_no, self._want_signals
                    )
                return self._facts[s]
        return {}, {}


def _auto_inst_one(
    text, marker, open_idx, close_idx, moddef, module, inst,
    templates, sort, dot_name, column, param_value, is_star, star_save,
    template_required=False, inst_vector="t", simplify=True,
    m_full=None, templated_comment=True, py_base_env=None, defines=None,
    parent_facts=None,
):
    # m_full: precomputed mask_comments(text) from the caller. The auto_inst
    # loop rewrites bottom-up, so text before open_idx is unchanged and the
    # cached mask stays valid for read_inst_param_values (which scans back).
    # py_base_env: precomputed file-local Python env (auto_inst hoists it;
    # per-instance inline AUTO_PYTHON blocks are still scanned here).
    lisp_env = read_auto_lisp(text, marker.offset)
    # Local Variables eval defuns are file-global (they sit after the
    # instance, so read_auto_lisp's before-marker scan cannot see them);
    # AUTO_LISP wins over them.
    for _k, _v in read_local_eval_defuns(text).items():
        lisp_env.setdefault(_k, _v)
    # `define table for verilog-symbol-detick-text in @"..." expressions.
    # defines: precomputed by auto_inst (define lines sit outside instance
    # regions, which are all the loop rewrites — one scan suffices).
    lisp_env.setdefault(
        "__defines__", defines if defines is not None else _buffer_defines(text)
    )
    # AUTO_PYTHON user functions merge into the template expression scope
    # (Python-native alternative to elisp defun)
    lisp_env.update(_read_auto_python_inline(text, marker.offset, py_base_env))
    tpl = (
        template_for_module(
            list(templates), module, before_line=text.count("\n", 0, marker.offset)
        )
        if templates
        else None
    )
    at_value = template_at_value(tpl, inst) if tpl else ""
    if m_full is None:
        m_full = mask_comments(text)
    # AUTO_LISP may setq verilog-auto-inst-param-value mid-file
    # (autoinst_rogoff.v): it overrides the file/CLI value for the
    # instances that follow, until another setq changes it.
    if "verilog-auto-inst-param-value" in lisp_env:
        param_value = bool(lisp_env["verilog-auto-inst-param-value"])
    param_values = (
        read_inst_param_values(text, open_idx, m_full) if param_value else {}
    )
    # Parent module declarations (inst_vector:nil width matching) and
    # interface modport ports of the enclosing module — per-module facts,
    # shared across the pass when the caller provides a _ParentFactsCache
    # (EAI rewrites instance bodies only, so they cannot change mid-loop).
    if parent_facts is not None:
        parent_signals, parent_iface_modports = parent_facts.facts(marker.offset)
    else:
        parent_signals, parent_iface_modports = _parent_module_facts(
            text.split("\n"), text.count("\n", 0, marker.offset),
            inst_vector == "nil",
        )
    # commented-out pins before the marker are NOT connected: mask
    # comments (positions preserved) before collecting names
    pins = set(
        re.findall(
            # `.pin (` named, plus the SystemVerilog `.pin` shorthand
            # (`.rst_ni,` or trailing) — both count as connected; missing
            # the shorthand regenerates the pin and duplicates it.
            r"\.\s*(\w+)\s*(?:\(|,|\Z)",
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
            conn, templated, nohookup = _connect(
                tpl, at_value, port, param_values, lisp_env, inst_name=inst,
                inst_vector=inst_vector, simplify=simplify,
                parent_signals=parent_signals,
                parent_iface_modports=parent_iface_modports,
            )
            if template_required and not templated:
                continue  # verilog-auto-inst-template-required: omit the port
            if dot_name and conn == port.name:
                conn = None
            # star expansion pins are ALWAYS tagged // Implicit .* — the
            # tag is the marker a caller deletes on when the expansion is
            # not to be saved (verilog-delete-auto-star-implicit); whether
            # it survives is the caller's star_save decision, not ours.
            if templated and templated_comment:
                comment = (
                    "// Templated AUTONOHOOKUP"
                    if nohookup
                    else "// Templated"
                )
            elif is_star:
                comment = "// Implicit .*"
            else:
                comment = None
            # verilog-mode AUTOINST drops submodule-header guards: live
            # Emacs 29.3 expands guarded ports unconditionally, whether
            # the guard sits mid-list (fredrickson) or trailing (g3).
            # (AIU keeps wrapping newly added guarded pins; that path
            # does not go through here.)
            entries.append((port.name, conn, comment, ()))
        if entries:
            sections.append((header, entries))
    if not sections:
        return None
    indent_pt = _indent_pt(text, open_idx)
    col_eff = max(column, 16 + 8 * ((indent_pt + 7) // 8))
    gen = _build_gen(sections, indent_pt, col_eff, ");")
    # an edit, not a splice: the caller (auto_inst's bottom-up loop)
    # accumulates them and rebuilds the buffer once — every read above is
    # at-or-before this instance's close paren, where the accumulated
    # edits of lower instances never reach
    return _region_edit(text, marker, open_idx, close_idx, gen, consume_semi=True)


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
    # `define table for detick-expanding ticked module names
    defines = _buffer_defines(text)
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
            module = detick_name(module, defines)
        except ValueError:
            continue  # instance array: skip, not expandable
        try:
            params = module_params[module]
        except KeyError:
            raise KeyError(f"module {module!r} not found for AUTOINSTPARAM instance {inst!r}") from None
        try:
            text = _auto_param_one(
                text, marker, open_idx, close_idx, params, module, inst,
                templates, sort, column, defines=defines,
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
    templates, sort, column, defines=None,
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
    for _k, _v in read_local_eval_defuns(text).items():
        lisp_env.setdefault(_k, _v)
    lisp_env.setdefault(
        "__defines__", defines if defines is not None else _buffer_defines(text)
    )
    lisp_env.update(read_auto_python(text, marker.offset))
    # commented-out pins before the marker are NOT connected: mask
    # comments (positions preserved) before collecting names
    pins = set(
        re.findall(
            r"\.\s*(\w+)\s*(?:\(|,|\Z)",
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
            # are reset, not preserved.  A declared packed range rides
            # as a subscript (autoinstparam_int.v: BLUP[5:0]); unpacked
            # dimensions ride as a /*.[...]*/ note (unsigned_bug302).
            conn = param.name + "".join(f"[{d}]" for d in param.packed)
            if param.unpacked:
                conn += "/*." + "".join(f"[{d}]" for d in param.unpacked) + "*/"
        entries.append((param.name, conn,
                        "// Templated" if templated else None, ()))
    if not entries:
        return text
    indent_pt = _indent_pt(text, open_idx)
    col_eff = max(column, 16 + 8 * ((indent_pt + 7) // 8))
    gen = _build_gen([("// Parameters", entries)], indent_pt, col_eff, ")")
    return _replace_region(text, marker, open_idx, close_idx, gen, consume_semi=False)


# ---------------------------------------------------------------------------
# .* save handling (verilog-delete-auto-star-implicit)

def _star_region_has_manual_pins(region: str) -> bool:
    """True when the text after a ``.*`` marker (up to the pin-list close)
    holds a hand-written pin — a ``.name (`` on a line carrying neither
    the ``// Implicit .*`` nor the ``// Templated`` tag.  verilog-mode
    leaves such instances alone: their author opted out of star
    expansion.  (The pin may share the marker's line: ``(.* , .i (x))``.)
    """
    for line in region.split("\n"):
        if _IMPLICIT_STAR.search(line) or "// Templated" in line:
            continue
        if re.search(r"\.\s*\w+\s*\(", line):
            return True
    return False


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
                while j >= 0 and not re.search(r"\.\s*\w+\s*\(|\.\*", _strip_comments(out[j])):
                    j -= 1
                if j >= 0 and re.search(r"\.\s*\w+\s*\(", _strip_comments(out[j])):
                    raw = out[j].rstrip()
                    code, sep, comment = raw.partition("//")
                    code = code.rstrip()
                    if code.endswith(","):
                        code = code[:-1].rstrip()
                    if sep:
                        # fusing ");" onto the line would bury it inside
                        # the trailing comment (e.g. // Templated) and
                        # unbalance the instance — close on its own line
                        out[j] = code + sep + comment
                        indent = re.match(r"\s*", raw).group(0)
                        out.insert(j + 1, indent + ");")
                    else:
                        out[j] = code + ");"
                elif j >= 0 and re.search(r"\.\*", _strip_comments(out[j])):
                    # nothing but .* left: close the pin list on the .* line
                    code, sep, comment = out[j].partition("//")
                    code = re.sub(r"\.\*\s*,?\s*$", ".*);", code.rstrip())
                    out[j] = code + (sep + comment if sep else "")
                i += 1
                continue
            i += 1
            continue
        out.append(line)
        i += 1
    # drop orphaned section comments: a `// Outputs`-style comment is only
    # meaningful inside a pin list, and the fold above can leave one
    # stranded after the folded `.*);` (or after an inserted `);` line).
    # Walk the comment-masked text tracking paren depth and drop section
    # comments sitting at depth 0.
    masked = mask_comments("\n".join(out)).split("\n")
    result: list[str] = []
    depth = 0
    for idx, line in enumerate(out):
        if (
            depth == 0
            and re.match(r"^\s*//\s*(Outputs|Inouts|Inputs|Interfaces|Interfaced)\s*$", line)
        ):
            continue
        result.append(line)
        code = masked[idx] if idx < len(masked) else ""
        depth += code.count("(") - code.count(")")
        if depth < 0:
            depth = 0
    return result
