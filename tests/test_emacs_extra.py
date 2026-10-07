"""Week 5+: verilog-mode param-value, .* star, AUTO_LISP."""

import pytest

from verilog_tooling.emacs import (
    auto_inst,
    delete_auto_star_implicit,
    parse_module_params,
    read_auto_lisp,
    read_inst_param_values,
)
from verilog_tooling.inst import parse_module_ports
from verilog_tooling.template import find_auto_templates

M = """\
module m (i, o);
    input  [W-1:0] i;
    output [W-1:0] o;
    parameter W = 8;
endmodule
"""


def mod():
    return parse_module_ports(M.splitlines(), with_params=True)


# ---------------------------------------------------------------------------
# verilog-auto-inst-param-value


def test_read_inst_param_values():
    text = "m #(.W(16), .D(W*2)) u_m ("
    open_idx = text.index("(", text.index("u_m"))
    assert read_inst_param_values(text, open_idx) == {"W": "16", "D": "W*2"}


def test_param_value_substitutes_width():
    buf = ["module top;", "m #(.W(16)) u_m (/*AUTOINST*/);", "endmodule"]
    out = auto_inst(buf, {"m": mod()}, param_value=True)
    body = "\n".join(out)
    # constant arithmetic is evaluated, like verilog-mode ((16)-1 -> 15)
    assert "(o[15:0])" in body
    assert "(i[15:0])" in body
    # without the flag, widths stay symbolic
    out = auto_inst(buf, {"m": mod()})
    assert "(o[W-1:0])" in "\n".join(out)


def test_param_value_applies_to_template_connection():
    text = """\
/* m AUTO_TEMPLATE (
    .i (in[@:W-1]),
); */
m #(.W(16)) u_m_0 (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = auto_inst(text.splitlines(), {"m": mod()}, templates=templates, param_value=True)
    assert any("(in[0:(16)-1])" in l for l in out)


# ---------------------------------------------------------------------------
# SystemVerilog .* (verilog-auto-star)


def test_star_expand_tags_implicit():
    buf = ["module top;", "m u_s (.*);", "endmodule"]
    out = auto_inst(buf, {"m": mod()}, star_expand=True, star_save=True)
    body = "\n".join(out)
    assert "m u_s (.*," in body
    assert "// Implicit .*" in body
    assert "(o[W-1:0])" in body and "(i[W-1:0])" in body


def test_star_save_false_collapses_back():
    buf = ["module top;", "m u_s (.*);", "endmodule"]
    expanded = auto_inst(buf, {"m": mod()}, star_expand=True, star_save=True)
    collapsed = delete_auto_star_implicit(expanded)
    assert collapsed == buf


def test_star_without_expand_is_untouched():
    buf = ["module top;", "m u_s (.*);", "endmodule"]
    assert auto_inst(buf, {"m": mod()}) == buf


# ---------------------------------------------------------------------------
# AUTO_LISP


def test_read_auto_lisp_python_subset():
    text = "/*AUTO_LISP(base = 10)*/\n/*AUTO_LISP(off = base + 1)*/\nm u (/*AUTOINST*/);"
    env = read_auto_lisp(text, text.index("m u"))
    assert env["base"] == 10
    assert env["off"] == 11


def test_auto_lisp_feeds_template_at_expr():
    text = """\
/*AUTO_LISP(base = 8)*/
/* m AUTO_TEMPLATE (
    .i (in[@"base + @"]),
); */
m u_m_2 (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = auto_inst(text.splitlines(), {"m": mod()}, templates=templates)
    assert any("(in[10]" in l for l in out)  # base(8) + @(2) = 10


def test_auto_lisp_error_is_reported():
    text = "/*AUTO_LISP(broken =)*/\nm u (/*AUTOINST*/);"
    with pytest.raises(ValueError, match="AUTO_LISP"):
        read_auto_lisp(text, len(text))


# ---------------------------------------------------------------------------
# AUTO_PYTHON (Python-native alternative to AUTO_LISP defun)


def test_read_auto_python_inline_block():
    from verilog_tooling.emacs import read_auto_python

    text = "/*AUTO_PYTHON(\ndef double(x):\n    return x * 2\n)*/\nm u (/*AUTOINST*/);"
    env = read_auto_python(text, len(text))
    assert env["double"](5) == 10


def test_auto_python_feeds_template_at_expr():
    text = """\
/*AUTO_PYTHON(
def surround(sig):
    return "{" + sig + "," + sig + "}"
)*/
/* m AUTO_TEMPLATE (
    .i (@"surround(vl_name)"),
); */
m u_m (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = auto_inst(text.splitlines(), {"m": mod()}, templates=templates)
    assert any("( {i,i} )" in l or "({i,i})" in l.replace(" ", "") for l in out)


def test_auto_python_file_local(tmp_path):
    from verilog_tooling.emacs import read_auto_python
    from verilog_tooling.libdirs import set_include_dirs

    (tmp_path / "myfuncs.py").write_text("def shout(s):\n    return s.upper()\n")
    set_include_dirs([str(tmp_path)])
    try:
        text = "// verilog-auto-python-file: myfuncs.py\nm u (/*AUTOINST*/);"
        env = read_auto_python(text, len(text))
        assert env["shout"]("abc") == "ABC"
    finally:
        set_include_dirs([])


def test_auto_python_file_resolution_invalidated_by_dir_change(tmp_path):
    """The same filename in different -y dirs must resolve per dirs (batch
    processes change include dirs per file)."""
    from verilog_tooling.emacs import _resolve_python_file
    from verilog_tooling.libdirs import set_include_dirs

    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir(), d2.mkdir()
    (d1 / "f.py").write_text("X = 1\n")
    (d2 / "f.py").write_text("X = 2\n")
    set_include_dirs([str(d1)])
    try:
        assert _resolve_python_file("f.py").startswith(str(d1))
        set_include_dirs([str(d2)])
        assert _resolve_python_file("f.py").startswith(str(d2))
    finally:
        set_include_dirs([])


def test_auto_python_error_is_reported():
    from verilog_tooling.emacs import read_auto_python

    text = "/*AUTO_PYTHON(\nraise RuntimeError('boom')\n)*/\nm u (/*AUTOINST*/);"
    with pytest.raises(ValueError, match="AUTO_PYTHON"):
        read_auto_python(text, len(text))


def test_auto_python_file_quoted_and_multiple(tmp_path):
    """verilog-auto-python-file accepts verilog-library-files syntax:
    quoted, whitespace-separated, parenthesised; later files override
    earlier ones."""
    from verilog_tooling.emacs import read_auto_python
    from verilog_tooling.libdirs import set_include_dirs

    (tmp_path / "a.py").write_text("def f():\n    return 'a'\ndef g():\n    return 'a'\n")
    (tmp_path / "b.py").write_text("def g():\n    return 'b'\n")
    set_include_dirs([str(tmp_path)])
    try:
        text = '// verilog-auto-python-file:("a.py" "b.py")\nm u (/*AUTOINST*/);'
        env = read_auto_python(text, len(text))
        assert env["f"]() == "a"
        assert env["g"]() == "b"  # later file wins
        text2 = '// verilog-auto-python-file: "a.py"\nm u (/*AUTOINST*/);'
        assert "f" in read_auto_python(text2, len(text2))
    finally:
        set_include_dirs([])
