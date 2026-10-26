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


def test_read_inst_param_values_ignores_comment_parens():
    # ZipCPU zipsystem.v wraps instance overrides in // {{{ ... // }}}
    # fold comments.  Counting parens over the raw text (or splitting
    # the block on raw commas) let the comment parens shift the block
    # start / comma depth and produced a single entry with the garbage
    # value '7),.DW(32' — width substitution then emitted the broken
    # expression '((7),.DW(32)-1)'.
    text = (
        "busdelay #(\n"
        "\t\t\t// {{{\n"
        "\t\t\t.AW(7),.DW(32)\n"
        "\t\t\t// }}}\n"
        "\t\t) wbdelay("
    )
    open_idx = text.index("(", text.index("wbdelay"))
    assert read_inst_param_values(text, open_idx) == {"AW": "7", "DW": "32"}


def test_read_inst_param_values_paren_inside_comment():
    # a ')' sitting inside a /* */ comment within the #(...) block must
    # not terminate the raw backward paren scan early (which used to
    # locate the group start at `.W(` and read no values at all).
    text = "m #(/* ) */ .W(16)) u_m ("
    open_idx = text.index("(", text.index("u_m"))
    assert read_inst_param_values(text, open_idx) == {"W": "16"}


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


def test_param_value_folds_pin_width_after_substitution():
    """Live-Emacs verified (autowire_shifts_bug1346): with #(...)
    overrides AND param-value on, the substituted pin width is
    simplified — [w2>>w1:0] with w1=2,w2=4 folds to [1:0], and even a
    parameter-free [224*1-1:128*1] folds to [223:128].  Without
    overrides the pin keeps the declared form verbatim
    (autowire_merge_bug303)."""
    src = """\
module sub #(parameter W1 = 2, parameter W2 = 4) (y14, y18);
    output [W2>>W1:0] y14;
    output [224*1-1:128*1] y18;
endmodule
"""
    sub = parse_module_ports(src.splitlines(), with_params=True)
    buf = [
        "module top;",
        "sub #(.W1(2),.W2(4)) u0 (/*AUTOINST*/);",
        "endmodule",
    ]
    out = auto_inst(buf, {"sub": sub}, param_value=True)
    body = "\n".join(out)
    assert "(y14[1:0])" in body
    assert "(y18[223:128])" in body
    # no overrides: verbatim, no folding
    buf = ["module top;", "sub u0 (/*AUTOINST*/);", "endmodule"]
    out = auto_inst(buf, {"sub": sub}, param_value=True)
    body = "\n".join(out)
    assert "(y14[W2>>W1:0])" in body
    assert "(y18[224*1-1:128*1])" in body


def test_param_value_applies_to_template_connection():
    text = """\
/* m AUTO_TEMPLATE (
    .i (in[@:W-1]),
); */
m #(.W(16)) u_m_0 (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = auto_inst(text.splitlines(), {"m": mod()}, templates=templates, param_value=True)
    assert any("in[0:16-1]" in l for l in out)


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


def test_star_manual_pins_not_expanded():
    # Bug #47 follow-up: a .* instance already carrying a hand-written pin
    # is not star-expanded by verilog-mode — expanding would drop the
    # manual connection (autoinst_star.v instance 1).
    buf = ["module top;", "m u_s (.*, .i (custom));", "endmodule"]
    out = auto_inst(buf, {"m": mod()}, star_expand=True, star_save=True)
    assert out == buf


def test_star_expand_no_save_tags_and_collapses():
    # With star_save=False the pins are still tagged // Implicit .* so the
    # caller can collapse them back (the aall/eai on-save behaviour).
    buf = ["module top;", "m u_s (.*);", "endmodule"]
    expanded = auto_inst(buf, {"m": mod()}, star_expand=True, star_save=False)
    assert "// Implicit .*" in "\n".join(expanded)
    assert delete_auto_star_implicit(expanded) == buf


def test_delete_star_implicit_templated_pin_gets_own_close_line():
    # The last surviving pin is // Templated: fusing ");" onto its line
    # would bury the close inside the comment and unbalance the instance
    # (autoinst_star.v instance 3) — it goes on its own line instead.
    lines = [
        "m u_s (.*,",
        "      // Inouts",
        "        .o (templated),// Templated",
        "      // Inputs",
        "        .i (i) );// Implicit .*",
    ]
    out = delete_auto_star_implicit(lines)
    body = "\n".join(out)
    assert "(templated)," not in body  # trailing comma stripped
    assert out[-1].strip() == ");"
    assert "// Inputs" not in body  # orphaned section comment dropped


def test_delete_star_implicit_fold_leaves_no_orphan_sections():
    # All pins implicit: the close folds onto the .* line and the section
    # comments that followed it are orphans at paren depth 0.
    lines = [
        "m u_s (.*,",
        "      // Outputs",
        "        .o (o),// Implicit .*",
        "      // Inputs",
        "        .i (i) );// Implicit .*",
        "endmodule",
    ]
    out = delete_auto_star_implicit(lines)
    assert out == ["m u_s (.*);", "endmodule"]


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


# ---------------------------------------------------------------------------
# `ifdef-guarded submodule ports keep their guards in AUTOINST output

GSUB = """\
module gsub (
    input  wire       clk,
`ifdef HAS_EXTRA
    input  wire       extra,
`endif
    input  wire [7:0] din,
    output wire [7:0] dout
);
endmodule
"""


def _gmods(text=GSUB):
    d = parse_module_ports(text.splitlines())
    return {d.name: d}


def _expand(top: str, mods) -> list[str]:
    return auto_inst(top.splitlines(), mods)


def test_eai_emits_ifdef_around_guarded_pin():
    top = """\
module top;
gsub u_g (/*autoinst*/
);
endmodule
"""
    out = _expand(top, _gmods())
    text = "\n".join(out)
    # Live Emacs 29.3 AUTOINST drops submodule-header guards entirely:
    # the guarded pin expands as an ordinary pin, no directives.
    assert "`ifdef" not in text and "`endif" not in text
    assert ".extra" in text
    assert ".clk" in text and "(clk)," in text
    assert "(din[7:0]));" in text


def test_eai_guard_wraps_within_own_section():
    sub = """\
module gsub2 (
    input  wire clk,
    input  wire din,
`ifdef TAIL_G
    output wire tail
`endif
);
endmodule
"""
    top = """\
module top;
gsub2 u_g (/*autoinst*/
);
endmodule
"""
    out = _expand(top, _gmods(sub))
    lines = [ln.strip() for ln in out]
    # Live Emacs 29.3 AUTOINST drops submodule-header guards: sections
    # regroup by direction (Outputs first) with .tail as an ordinary
    # pin, no directives anywhere.
    assert not any(ln.startswith("`ifdef") or ln == "`endif" for ln in lines)
    (tail_line,) = [ln for ln in lines if ".tail" in ln]
    ti = lines.index(tail_line)
    assert lines[ti + 1] == "// Inputs"


def test_eai_guarded_last_pin_puts_closer_after_endif():
    sub = """\
module gsub4 (
    input  wire clk,
    input  wire din,
`ifdef TAIL_G
    input  wire tail
`endif
);
endmodule
"""
    top = """\
module top;
gsub4 u_g (/*autoinst*/
);
endmodule
"""
    out = _expand(top, _gmods(sub))
    lines = [ln.strip() for ln in out]
    # Live Emacs 29.3 AUTOINST drops submodule-header guards: the
    # formerly guarded input is the last pin overall and carries the
    # closer on its own line, no directives.
    assert not any(ln == "`endif" for ln in lines)
    (tail_line,) = [ln for ln in lines if ".tail" in ln]
    assert tail_line.endswith("));")


def test_eai_elsif_chain_becomes_ifndef_plus_ifdef():
    sub = """\
module gsub3 (
    input  wire clk,
`ifdef MODE_A
    output wire oa,
`elsif MODE_B
    output wire ob,
`else
    output wire oc,
`endif
    output wire dout
);
endmodule
"""
    top = """\
module top;
gsub3 u_g (/*autoinst*/
);
endmodule
"""
    out = _expand(top, _gmods(sub))
    text = "\n".join(out)
    # Live Emacs 29.3 AUTOINST drops submodule-header guards entirely:
    # the `elsif/`else chain ports all expand as ordinary pins.
    assert "`ifdef" not in text and "`endif" not in text
    assert ".oa" in text and ".ob" in text and ".oc" in text


def test_read_auto_lisp_elisp_setq():
    """The elisp form Emacs itself documents: /*AUTO_LISP(setq num 1)*/
    feeds @"num" templates (autoinst_lopaz.v). It used to fail Python
    compilation, so the whole instance was skipped with a warning."""
    text = "/*AUTO_LISP(setq num 1)*/\\nm u (/*AUTOINST*/);"
    assert read_auto_lisp(text, len(text))["num"] == 1
    # later blocks win, in buffer order
    text2 = "/*AUTO_LISP(setq num 1)*/\\n/*AUTO_LISP(setq num 2)*/\\nm u (/*AUTOINST*/);"
    assert read_auto_lisp(text2, len(text2))["num"] == 2


def test_read_local_eval_defuns():
    """Local Variables eval defuns are in scope for template @"..."
    expressions (autoinst_ma_io_prefix.v's trailing block)."""
    from verilog_tooling.emacs import read_local_eval_defuns
    from verilog_tooling.template import _elisp_eval, _elisp_parse, _elisp_tokenize

    # trailing block of autoinst_ma_io_prefix.v, inlined (not read from
    # /tmp — the verilog-mode checkout there is wiped on VM regeneration)
    text = """\
/*
 Local Variables:
 eval:
  (defun vl-prefix-i-o (dir)
        (cond ((equal dir "input")
               "i_")
              ((equal dir "output")
               "o_")
              ((equal dir "inout")
               "t_")
              (t "")))
 End:
*/
"""
    env = read_local_eval_defuns(text)
    assert "vl-prefix-i-o" in env
    for expr, want in [
        ('(vl-prefix-i-o "input")', "i_"),
        ('(vl-prefix-i-o "output")', "o_"),
        ('(vl-prefix-i-o "other")', ""),
    ]:
        assert _elisp_eval(_elisp_parse(_elisp_tokenize(expr)), env) == want


def test_buffer_defines_ignores_commented_out():
    """`define map for detick-text: valueless defines map to "1", and a
    `define inside a comment is not picked up."""
    from verilog_tooling.emacs import _buffer_defines

    text = "`define foo 6\n// `define fake 9\n`define flag\n"
    assert _buffer_defines(text) == {"foo": "6", "flag": "1"}


def test_detick_name():
    """verilog-symbol-detick with wing-it for module names."""
    from verilog_tooling.emacs import detick_name

    defines = {"SUBMOD_A": "submod_mod", "DECL": "submod_decl_from_def", "CHAIN": "`DECL"}
    assert detick_name("`SUBMOD_A", defines) == "submod_mod"
    assert detick_name("`SUBMOD_UNDEFED", defines) == "SUBMOD_UNDEFED"
    assert detick_name("submod_mod", defines) == "submod_mod"
    assert detick_name("`CHAIN", defines) == "submod_decl_from_def"


def test_param_packed_dims_inherited_across_comma_list():
    """parameter logic [5:0] BLUP=3, ZOT=4 shares the dims."""
    from verilog_tooling.emacs import parse_module_params

    src = (
        "module xyz\n"
        "  #(parameter int FOO=1,\n"
        "    parameter logic [5:0] BLUP=3, ZOT=4,\n"
        "    parameter LDWRDS=5)\n"
        "   (input x1, output y);\n"
        "endmodule\n"
    ).splitlines()
    params = {p.name: p for p in parse_module_params(src)}
    assert params["FOO"].packed == ()
    assert params["BLUP"].packed == ("5:0",)
    assert params["ZOT"].packed == ("5:0",)
    assert params["LDWRDS"].packed == ()


def test_simplify_range_expression_emacs_examples():
    """Port of verilog-simplify-range-expression, checked against the
    examples documented in verilog-mode.el itself."""
    from verilog_tooling.emacs import simplify_range_expression as s

    assert s("[DW-1+2:0]") == "[DW+1:0]"
    assert s("[DW+1-2:0]") == "[DW-1:0]"
    assert s("[DW-1+1:0]") == "[DW-0:0]"
    assert s("[FOO*4-1*2]") == "[FOO*4-2]"
    assert s("[FOO*4+1-1]") == "[FOO*4+0]"
    assert s("[FOO-1+1-1+1]") == "[FOO-0]"
    assert s("[(((16)+1)+1+(1+1))]") == "[20]"
    assert s("[(2*3+6*7)]") == "[48]"
    assert s("[1:3]") == "[1:3]"
    assert s("[(1):3]") == "[1:3]"
    assert s("[func(BAR)]") == "[func(BAR)]"
    assert s("[2*2:1]") == "[4:1]"


def test_read_local_eval_defuns_accepts_comment_end_line():
    """The Local Variables End: line may carry a // prefix
    (autoinst_regexp_match.v) or stand bare (autoinst_ma_io_prefix.v)."""
    from verilog_tooling.emacs import read_local_eval_defuns

    text = (
        "module m;\n"
        "endmodule\n"
        "// Local Variables:\n"
        "// eval:(defun uc (x) (upcase x))\n"
        "// End:\n"
    )
    assert "uc" in read_local_eval_defuns(text)


# ---------------------------------------------------------------------------
# AUTOINSERTLISP elisp form (ExampInsertLisp / autolisp_truex / include)
# ---------------------------------------------------------------------------


def test_auto_insert_lisp_defun_insert_concat():
    """The hello defun: Local Variables eval defun + insert + concat,
    with \\n as a real newline (not the letter n)."""
    from verilog_tooling.misc import auto_insert_lisp

    lines = [
        "module ExampInsertLisp;",
        '   /*AUTOINSERTLISP(my-verilog-insert-hello "world")*/',
        "endmodule",
        "/*",
        " Local Variables:",
        " eval:",
        '   (defun my-verilog-insert-hello (who)',
        '     (insert (concat "initial $write(\\"hello " who "\\");\\n")))',
        " End:",
        "*/",
    ]
    out = auto_insert_lisp(lines)
    body = "\n".join(out)
    assert 'initial $write("hello world");' in body
    assert '");n' not in body
    assert "// Beginning of automatic insert lisp" in body


def test_auto_insert_lisp_empty_and_shell_forms():
    """(+ 1) inserts nothing (no region); the shell-command form
    inserts the command output."""
    from verilog_tooling.misc import auto_insert_lisp

    lines = [
        "module m;",
        "   /*AUTOINSERTLISP(+ 1)*/",
        '   /*AUTOINSERTLISP(insert (shell-command-to-string "echo //hello"))*/',
        "endmodule",
    ]
    out = auto_insert_lisp(lines)
    body = "\n".join(out)
    assert body.count("// Beginning of automatic insert lisp") == 1
    assert "//hello" in body


def test_auto_insert_lisp_insert_file(tmp_path):
    """(insert-file ...) resolves against base_dir (the buffer file's
    directory) and inserts the file's contents."""
    from verilog_tooling.misc import auto_insert_lisp

    inc = tmp_path / "decls.vh"
    inc.write_text("  input bar;\n  output foo;\n")
    lines = [
        "module m;",
        '   /*AUTOINSERTLISP(verilog-library-filenames (insert-file "decls.vh"))*/',
        "endmodule",
    ]
    out = auto_insert_lisp(lines, base_dir=str(tmp_path))
    body = "\n".join(out)
    assert "input bar;" in body
    assert "output foo;" in body


# ---------------------------------------------------------------------------
# Escaped identifiers (escape_a / escape_top / autoinst_name_bug245)
# ---------------------------------------------------------------------------


def test_parse_module_ports_escaped_names():
    """An escaped identifier runs to whitespace: brackets and a ';'
    inside it are name characters, not a range or a terminator."""
    from verilog_tooling.inst import parse_module_ports

    lines = [
        "module escape_a (/*AUTOARG*/",
        "   // Outputs",
        "   \\o[10] , \\o[2] ,",
        "   // Inputs",
        "   \\i&e; ",
        "   );",
        "   output \\o[10] ;",
        "   output \\o[2] ;",
        "   input  \\i&e; ;",
        "endmodule",
    ]
    ports = parse_module_ports(lines).ports
    assert [(p.name, p.direction) for p in ports] == [
        ("\\o[10]", "output"),
        ("\\o[2]", "output"),
        ("\\i&e;", "input"),
    ]


def test_collect_ports_escaped_trailing_semicolon():
    """The ';' inside \\i&e; survives header collection; only a ';'
    following whitespace is the statement terminator."""
    from verilog_tooling.arg import _collect_ports

    inputs, outputs, _ = _collect_ports(
        ["   output \\o[10] ;", "   output \\o[2] ;", "   input  \\i&e; ;"]
    )
    assert inputs == ["\\i&e;"]
    assert outputs == ["\\o[10]", "\\o[2]"]


def test_kill_auto_arg_escaped_semicolon_does_not_block():
    """A header whose port list contains \\i&e; still collapses:
    the ';' inside the escaped name is not a statement terminator."""
    from verilog_tooling.arg import kill_auto_arg

    lines = [
        "module escape_a (/*AUTOARG*/",
        "   // Outputs",
        "   \\o[10] , \\o[2] ,",
        "   // Inputs",
        "   \\i&e; ",
        "   );",
        "endmodule",
    ]
    assert kill_auto_arg(lines)[0] == "module escape_a (/*AUTOARG*/);"


def test_elem_net_escaped_name_and_select():
    """_ELEM_NET: brackets inside an escaped name are name characters;
    a bracket group after whitespace is a bit-select."""
    from verilog_tooling.wire import _ELEM_NET

    m = _ELEM_NET.match("\\o[10]")
    assert m and m.group(1) == "\\o[10]" and m.group(2) == ""
    m = _ELEM_NET.match("\\c-escaped-vec [2:0]")
    assert m and m.group(1) == "\\c-escaped-vec" and "[2:0]" in m.group(2)


def test_emit_signal_escaped_name_space_before_terminator():
    """Emacs keeps the terminating whitespace of an escaped name, so a
    declaration emits `name ;` — a glued ';' would be re-read as part
    of the name on the next pass."""
    from verilog_tooling.autodef import Signal, _emit_signal

    sig = Signal(name="\\i&e;", width="c0", type="io_input")
    assert _emit_signal(sig, 39, "input", emacs_dims=True).rstrip().endswith(
        "\\i&e; ;"
    )
    sig = Signal(name="clk", width="c0", type="io_input")
    assert _emit_signal(sig, 39, "input", emacs_dims=True).rstrip().endswith(
        "clk;"
    )



def test_inst_pin_scan_skips_presection_pins_for_marked_instances():
    """verilog-read-sub-decls reads pins only after a section comment:
    a pin before the first section comment (bug245's .bign1_dotnamed)
    is invisible to AIO/AUTOWIRE. Marker-less instances keep the full
    pin list (our extension)."""
    from verilog_tooling.inst import parse_module_ports
    from verilog_tooling import wire

    sub = parse_module_ports(
        ["module Sub (output first_out, output second_out);", "endmodule"]
    )
    lines = [
        "module top ();",
        "   Sub u_sub (",
        "	    .first_out,",
        "	    // Outputs",
        "	    .second_out,",
        "	    /*AUTOINST*/);",
        "endmodule",
    ]
    scan = wire._inst_pin_scan(lines, {"Sub": sub})
    pins = scan["instances"][0][4][0]
    assert [p[0] for p in pins] == ["second_out"]

    # Marker-less: same pins, no section filter.
    lines2 = [
        "module top ();",
        "   Sub u_sub (",
        "	    .first_out,",
        "	    .second_out);",
        "endmodule",
    ]
    scan2 = wire._inst_pin_scan(lines2, {"Sub": sub})
    pins2 = scan2["instances"][0][4][0]
    assert [p[0] for p in pins2] == ["first_out", "second_out"]

    # Marked but section-less (standalone AW/AIO on unexpanded text):
    # no section comments, so no filter — the full pin list stays.
    lines3 = [
        "module top ();",
        "   Sub u_sub (",
        "	    .first_out,",
        "	    .second_out,",
        "	    /*AUTOINST*/);",
        "endmodule",
    ]
    scan3 = wire._inst_pin_scan(lines3, {"Sub": sub})
    pins3 = scan3["instances"][0][4][0]
    assert [p[0] for p in pins3] == ["first_out", "second_out"]
