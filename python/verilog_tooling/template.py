"""verilog-mode AUTO_TEMPLATE parsing and expansion (regex-related subset).

Implements the regular-expression parts of Emacs verilog-mode's AUTOINST
template machinery:

- Optional ``"REGEXP"`` after the AUTO_TEMPLATE keyword selecting what ``@``
  expands to (default: the first group of digits in the instance name).
- Template entries whose port pattern is a regular expression; ``\\(...\\)``
  groups are back-substituted as ``\\1`` ... in the connection expression.
- ``@`` inside a *port pattern* matches a digit group ``([0-9]+)``.
- ``@`` inside a *connection* expands to the instance number.
- ``[]`` in a connection expands to the connected port's declared packed
  range (removed for scalar ports).
- ``@"expr"`` evaluates a connection expression: Python expressions, or
  parenthesised elisp forms through the mini evaluator below (``@`` and
  the AUTO_LISP bindings are in scope).

Out of scope (raise or left literal): ``[].[@]`` unpacked-element
connections; ``[][]`` multi-dimensional comments (treated like ``[]``);
elisp forms outside the supported subset (``defun`` etc.).

The module is pure syntax/semantic layer: it never touches files.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

_LITERAL_PORT = re.compile(r"[A-Za-z0-9`_$]+")
_DEFAULT_AT_REGEXP = r"([0-9]+)"

_TEMPLATE_BLOCK = re.compile(
    r"/\*((?:[^*]|\*(?!/))*?\bAUTO_TEMPLATE\b(?:[^*]|\*(?!/))*?)\*/",
    re.S | re.IGNORECASE,
)
_MODULE_HEADER = re.compile(r"(\w+)\s+AUTO_TEMPLATE\b", re.IGNORECASE)


@dataclass(frozen=True)
class TemplateEntry:
    """One ``.pattern(connection)`` line of an AUTO_TEMPLATE block."""

    pattern: str
    connection: str
    is_regex: bool


@dataclass(frozen=True)
class AutoTemplate:
    """Parsed ``/* module AUTO_TEMPLATE "regexp" ( ... ); */`` block."""

    modules: tuple[str, ...]
    at_regexp: str | None
    entries: tuple[TemplateEntry, ...]
    line_no: int = 0  # 0-based line where the /* ... */ block starts

    def matches(self, module: str) -> bool:
        return module in self.modules


def _auto_re_to_python(pattern: str) -> str:
    """Translate a template/filter regexp to Python ``re``, auto-detecting
    the dialect.

    Emacs verilog-mode writes ``\\(`` ``\\)`` for groups and ``\\|`` for
    alternation; Python users write ``(`` ``)`` and ``|``.  The two only
    conflict on those three escapes, and a *literal* parenthesis or pipe
    can never appear in the things these regexps match (port/parameter/
    instance/type names are identifiers) — so the escapes unambiguously
    mark the Emacs dialect:

    - contains ``\\(`` ``\\)`` or ``\\|``  -> Emacs dialect: rewrite the
      escapes to ``(`` ``)`` ``|``; everything else passes through.
    - otherwise                            -> Python dialect: returned
      unchanged (``(foo|bar)`` groups, ``|`` alternation, ``\\w`` etc.
      already are Python).

    Mixed patterns degrade gracefully: Emacs escapes are rewritten,
    remaining bare parens/pipes are taken as Python constructs.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt in "()":
                out.append(nxt)
                i += 2
                continue
            if nxt == "|":
                out.append("|")
                i += 2
                continue
            out.append(pattern[i : i + 2])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


# back-compatible name (the dialect is auto-detected now)
_emacs_re_to_python = _auto_re_to_python


def _emacs_repl_to_python(repl: str) -> str:
    """Translate replacement backrefs for ``re.Match.expand``, accepting both
    dialects: Emacs ``\\1``/``\\&`` become ``\\g<1>``/``\\g<0>``; Python
    ``\\g<1>``/``\\g<0>`` pass through unchanged (``\\1`` is valid in both)."""
    out: list[str] = []
    i = 0
    while i < len(repl):
        c = repl[i]
        if c == "\\" and i + 1 < len(repl):
            nxt = repl[i + 1]
            if nxt == "&":
                out.append(r"\g<0>")
                i += 2
                continue
            if nxt.isdigit():
                out.append("\\g<" + nxt + ">")
                i += 2
                continue
            if nxt == "g":
                # Python-style \g<1> / \g<0>: pass the \g through, the
                # following <...> are plain replacement text
                out.append("\\g")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\\\")
                i += 2
                continue
            raise ValueError(f"unsupported escape \\{nxt} in template connection {repl!r}")
        out.append(c)
        i += 1
    return "".join(out)


def _balanced_inner(text: str, start: int) -> tuple[str, int]:
    """Return the text inside the parens opened at text[start], and the index
    just past the matching close paren."""
    depth = 0
    j = start
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : j], j + 1
        j += 1
    raise ValueError("AUTO_TEMPLATE: unbalanced parentheses")


_ENTRY_DOT = re.compile(r"\.\s*")
_ENTRY_SEPARATORS = " \t\r\n,;"


def _scan_entry(inner: str, i: int) -> "tuple[str, str, int] | None":
    """Scan the next ``.pattern (connection)`` entry at/after index I.
    Returns ``(pattern, connection, next_index)`` or None when no entry
    remains.

    The pattern may itself contain parentheses (Python-dialect regexp
    groups — Emacs dialect escapes them as ``\\(``), so the connection
    cannot simply be the first paren group.  It is the first unescaped
    ``(`` whose balanced close is followed only by separators and then the
    next entry dot or the end of the body; earlier groups belong to the
    pattern."""
    m = _ENTRY_DOT.search(inner, i)
    if not m:
        return None
    pat_start = m.end()
    j = pat_start
    while j < len(inner):
        c = inner[j]
        if c == "\\":
            j += 2  # escaped char (e.g. emacs \( \) \|) is pattern text
            continue
        if c == "(":
            try:
                conn, end = _balanced_inner(inner, j)
            except ValueError:
                return None
            k = end
            while k < len(inner) and inner[k] in _ENTRY_SEPARATORS:
                k += 1
            if k >= len(inner) or inner[k] == ".":
                pattern = inner[pat_start:j].strip()
                if not pattern:
                    return None
                return pattern, conn.strip(), end
            j = end  # a pattern group (python-dialect regexp): keep scanning
            continue
        j += 1
    return None


def _parse_template_body(body: str, line_no: int) -> AutoTemplate:
    headers = list(_MODULE_HEADER.finditer(body))
    if not headers:
        raise ValueError("AUTO_TEMPLATE block without module name")
    # the body paren follows the last AUTO_TEMPLATE keyword (+ optional "regexp")
    pos = headers[-1].end()
    at_regexp = None
    m = re.match(r"\s*", body[pos:])
    pos += m.end()
    if pos < len(body) and body[pos] == '"':
        j = pos + 1
        while j < len(body) and (body[j] != '"' or body[j - 1] == "\\"):
            j += 1
        if j >= len(body):
            raise ValueError("AUTO_TEMPLATE: unterminated regexp string")
        at_regexp = body[pos + 1 : j]
        pos = j + 1
    try:
        paren = body.index("(", pos)
    except ValueError:
        raise ValueError("AUTO_TEMPLATE block without ( ... ) body") from None
    modules = tuple(m.group(1) for m in _MODULE_HEADER.finditer(body[:paren]))
    inner, _ = _balanced_inner(body, paren)
    # verilog-mode treats comments inside the template body as whitespace;
    # the state-machine masker keeps @"..." strings (even ones containing
    # // or /*) intact while blanking real comments to spaces
    from .comments import mask_comments

    inner = mask_comments(inner)

    entries: list[TemplateEntry] = []
    i = 0
    while True:
        scanned = _scan_entry(inner, i)
        if scanned is None:
            break
        pattern, conn, j = scanned
        entries.append(
            TemplateEntry(
                pattern=pattern,
                connection=conn,
                is_regex=_LITERAL_PORT.fullmatch(pattern) is None,
            )
        )
        i = j
    return AutoTemplate(modules=modules, at_regexp=at_regexp, entries=tuple(entries), line_no=line_no)


def _mask_line_comments(text: str) -> str:
    """Replace every ``//`` comment's text with spaces (offsets preserved) so
    a ``/* AUTO_TEMPLATE ( ... ) */`` block sitting INSIDE a line comment is
    never matched.  Block comments stay visible (they may BE the template),
    but the scan is state-aware: a ``//`` inside a ``/* */`` block or inside
    a string is not masked."""
    from .comments import mask_comments

    return mask_comments(text, block=False)


def find_auto_templates(text: str) -> list[AutoTemplate]:
    """Find every ``/* ... AUTO_TEMPLATE ... */`` block in a buffer."""
    masked = _mask_line_comments(text)
    templates = []
    for m in _TEMPLATE_BLOCK.finditer(masked):
        line_no = text.count("\n", 0, m.start())
        templates.append(_parse_template_body(m.group(1), line_no))
    return templates


def template_for_module(
    templates: list[AutoTemplate], module: str, before_line: int | None = None
) -> AutoTemplate | None:
    """Pick the template for MODULE; the last one above BEFORE_LINE wins
    (verilog-mode searches up for the closest template)."""
    found = None
    for t in templates:
        if t.matches(module) and (before_line is None or t.line_no < before_line):
            found = t
    return found


def template_at_value(template: AutoTemplate, instance_name: str) -> str:
    """Value substituted for ``@``: first group of the template regexp
    (default ``([0-9]+)``) matched against the instance name; '' if no match."""
    rx = template.at_regexp or _DEFAULT_AT_REGEXP
    # verilog-mode matches tpl-regexp with verilog-case-fold (default t)
    m = re.search(_auto_re_to_python(rx), instance_name, re.IGNORECASE)
    if not m:
        return ""
    return m.group(1) if m.groups() else m.group(0)


# ---------------------------------------------------------------------------
# minimal elisp (S-expression) evaluator for @"..." template expressions


def _elisp_tokenize(code: str) -> list:
    tokens: list = []
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c.isspace():
            i += 1
        elif c in "()":
            tokens.append(c)
            i += 1
        elif c == "'":
            tokens.append(c)
            i += 1
        elif c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                if code[j] == "\\" and j + 1 < n:
                    buf.append(code[j + 1])
                    j += 2
                elif code[j] == '"':
                    break
                else:
                    buf.append(code[j])
                    j += 1
            tokens.append(("str", "".join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and not code[j].isspace() and code[j] not in "()\"'":
                j += 1
            tokens.append(code[i:j])
            i = j
    return tokens


def _elisp_parse(tokens: list):
    if not tokens:
        raise ValueError("empty elisp expression")
    tok = tokens.pop(0)
    if tok == "(":
        out = []
        while tokens and tokens[0] != ")":
            out.append(_elisp_parse(tokens))
        if not tokens:
            raise ValueError("unbalanced ( in elisp")
        tokens.pop(0)
        return out
    if tok == ")":
        raise ValueError("unexpected ) in elisp")
    if tok == "'":
        return ["quote", _elisp_parse(tokens)]
    if isinstance(tok, tuple):
        return tok[1]  # string literal
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    return ("sym", tok)


def _elisp_str(value) -> str:
    if value is True:
        return "t"
    if value is False or value == "":
        return ""
    return str(value)


def _elisp_num(value) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _elisp_eval(form, scope: dict):
    if isinstance(form, (int, str)):
        return form
    if isinstance(form, tuple) and form[0] == "sym":
        sym = form[1]
        if sym == "t":
            return True
        if sym == "nil":
            return ""
        if sym in scope:
            return scope[sym]
        raise ValueError(f"name {sym!r} is not defined")
    if not isinstance(form, list) or not form:
        return ""
    head = form[0]
    name = head[1] if isinstance(head, tuple) and head[0] == "sym" else None
    args = form[1:]
    if name == "quote":
        f = args[0]
        return f[1] if isinstance(f, tuple) else f
    if name == "if":
        cond = _elisp_eval(args[0], scope)
        if cond is not False and cond != "":
            return _elisp_eval(args[1], scope)
        return _elisp_eval(args[2], scope) if len(args) > 2 else ""
    if name == "substring":
        s = _elisp_str(_elisp_eval(args[0], scope))
        start = _elisp_num(_elisp_eval(args[1], scope))
        end = _elisp_num(_elisp_eval(args[2], scope)) if len(args) > 2 else None
        return s[start:end]
    if name == "downcase":
        return _elisp_str(_elisp_eval(args[0], scope)).lower()
    if name == "upcase":
        return _elisp_str(_elisp_eval(args[0], scope)).upper()
    if name == "capitalize":
        return _elisp_str(_elisp_eval(args[0], scope)).capitalize()
    if name == "concat":
        return "".join(_elisp_str(_elisp_eval(a, scope)) for a in args)
    if name == "int-to-string":
        return _elisp_str(_elisp_eval(args[0], scope))
    if name == "string-to-number":
        return _elisp_num(_elisp_eval(args[0], scope))
    if name in ("equal", "eq", "string="):
        return _elisp_eval(args[0], scope) == _elisp_eval(args[1], scope)
    if name in ("+", "-", "*", "/"):
        vals = [_elisp_num(_elisp_eval(a, scope)) for a in args]
        if not vals:
            return 0
        r = vals[0]
        for v in vals[1:]:
            if name == "+":
                r += v
            elif name == "-":
                r -= v
            elif name == "*":
                r *= v
            else:
                r = r // v if v else r
        return r
    if name == "progn":
        r: object = ""
        for a in args:
            r = _elisp_eval(a, scope)
        return r
    if name == "setq":
        r = ""
        for k in range(0, len(args) - 1, 2):
            var = args[k][1] if isinstance(args[k], tuple) else ""
            r = _elisp_eval(args[k + 1], scope)
            scope[var] = r
        return r
    if name == "let":
        inner = dict(scope)
        binds = args[0]
        if isinstance(binds, list):
            for b in binds:
                if isinstance(b, list) and len(b) == 2 and isinstance(b[0], tuple):
                    inner[b[0][1]] = _elisp_eval(b[1], scope)
        r = ""
        for a in args[1:]:
            r = _elisp_eval(a, inner)
        return r
    raise ValueError(f"unsupported elisp form: {name!r}")


def expand_connection(
    expr: str, at_value: str, port_width: str | None, env: dict | None = None,
    *, vl_name: str = "", vl_cell_name: str = "", vl_dir: str = "",
    packed_note: str = "",
) -> str:
    """Apply ``@``, ``@"expr"`` and ``[]`` expansion to a connection.

    ``@"expr"`` is evaluated with ``@`` bound to the instance number and ENV
    (from AUTO_LISP) providing extra names.  Parenthesised forms go through a
    small elisp evaluator (substring/downcase/concat/if/equal/arithmetic/
    let/setq/progn with vl-name/vl-cell-name/vl-width/vl-dir bound); anything
    else is a Python expression (the historical Python subset)."""
    if '@"' in expr:
        def _eval(m: re.Match) -> str:
            code = m.group(1).replace('\\"', '"').replace("@", at_value or "0")
            scope = dict(env or {})
            scope.setdefault("vl-name", vl_name)
            scope.setdefault("vl-cell-name", vl_cell_name)
            scope.setdefault("vl-width", port_width or "")
            scope.setdefault("vl-dir", vl_dir)
            try:
                if code.lstrip().startswith("("):
                    value = _elisp_eval(_elisp_parse(_elisp_tokenize(code)), scope)
                else:
                    value = eval(code, {"__builtins__": {}}, scope)  # noqa: S307
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f'AUTO_TEMPLATE @"..." evaluation failed: {code!r}: {exc}') from exc
            return _elisp_str(value)

        expr = re.sub(r'@"((?:[^"\\]|\\.)*)"', _eval, expr)
    expr = expr.replace("@", at_value)
    # a multidim port has no single range: []/[][] expand to the
    # verilog-mode dimensions note /*[D1][D2]*/ instead (packed_note)
    rng = f"[{port_width}]" if port_width else packed_note
    expr = expr.replace("[][]", rng)
    expr = expr.replace("[]", rng)
    return expr


def template_connection(
    template: AutoTemplate,
    port_name: str,
    at_value: str,
    port_width: str | None,
    env: dict | None = None,
    *,
    vl_cell_name: str = "",
    vl_dir: str = "",
    packed_note: str = "",
) -> str | None:
    """Connection for PORT_NAME under TEMPLATE, or None when no entry matches.

    Exact port-name entries are tried first, then regexp entries — matching
    verilog-mode's ``verilog-auto-inst-port``: an exact ``assoc`` over the
    consed list makes the LAST exact entry in file order win, while the
    wildcard ``while`` loop re-assigns on every match, so among regexp
    entries the FIRST (top-most) in file order wins.
    Regexp entries substitute ``\\1`` groups captured from the port name.
    ENV feeds ``@"..."`` expressions (from AUTO_LISP).
    """
    for entry in reversed(template.entries):
        if not entry.is_regex and entry.pattern == port_name:
            return expand_connection(
                entry.connection, at_value, port_width, env,
                vl_name=port_name, vl_cell_name=vl_cell_name, vl_dir=vl_dir,
                packed_note=packed_note,
            )
    for entry in template.entries:  # top-most matching regexp wins
        if not entry.is_regex:
            continue
        pat = entry.pattern.replace("@", r"([0-9]+)")
        try:
            rx = re.compile("^" + _auto_re_to_python(pat) + "$")
        except re.error as exc:
            raise ValueError(
                f"AUTO_TEMPLATE: invalid regexp entry {entry.pattern!r}: {exc}"
            ) from exc
        m = rx.match(port_name)
        if m:
            expr = m.expand(_emacs_repl_to_python(entry.connection))
            return expand_connection(
                expr, at_value, port_width, env,
                vl_name=port_name, vl_cell_name=vl_cell_name, vl_dir=vl_dir,
                packed_note=packed_note,
            )
    return None
