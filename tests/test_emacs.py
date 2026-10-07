"""Week 5: verilog-mode style AUTOINST / AUTOINSTPARAM (emacs.py)."""

import pytest

from verilog_tooling.emacs import (
    Param,
    auto_inst,
    auto_param,
    find_auto_markers,
    marker_modules,
    parse_module_params,
)
from verilog_tooling.inst import parse_module_ports
from verilog_tooling.template import find_auto_templates, template_at_value

IM = """\
module InstModule (o, i);
    output [31:0] o;
    input         i;
    wire [31:0] o = {32{i}};
endmodule
"""

IMP = """\
module InstModule (o, i);
    parameter PAR;
    output [31:0] o;
    input         i;
endmodule
"""

FILT = """\
module filt (
    input  wire       clk,
    input  wire [3:0] din_i,
    output wire [3:0] dout_o
);
endmodule
"""

SORTM = """\
module sortm (
    output wire z,
    output wire a,
    input  wire m
);
endmodule
"""


def mods(*texts):
    out = {}
    for t in texts:
        m = parse_module_ports(t.splitlines())
        out[m.name] = m
    return out


def params(*texts):
    out = {}
    for t in texts:
        name = parse_module_ports(t.splitlines()).name
        out[name] = parse_module_params(t.splitlines())
    return out


# ---------------------------------------------------------------------------
# parse_module_params


def test_parse_module_params_header_form():
    lines = ["module m #(parameter W = 8, D = 4) (a);", "input a;", "endmodule"]
    assert parse_module_params(lines) == (Param("W", "8"), Param("D", "4"))


def test_parse_module_params_body_form_skips_types_and_localparam():
    lines = [
        "module m (a);",
        "    input a;",
        "    parameter int W = 8, D = W*2;",
        "    parameter signed [3:0] X = -1;",
        "    localparam L = 1;",
        "endmodule",
    ]
    assert parse_module_params(lines) == (Param("W", "8"), Param("D", "W*2"), Param("X", "-1"))


def test_parse_module_params_none():
    assert parse_module_params(["module m (a);", "input a;", "endmodule"]) == ()


# ---------------------------------------------------------------------------
# auto_inst (verilog-auto-inst)


def test_auto_inst_basic_doc_example():
    buf = ["module top;", "InstModule instName (/*AUTOINST*/);", "endmodule"]
    ind = " " * 21  # column after the '(' of the pin list
    out = auto_inst(buf, mods(IM))
    assert out == [
        "module top;",
        "InstModule instName (/*AUTOINST*/",
        ind + "// Outputs",
        ind + ".o" + " " * 17 + "(o[31:0]),",
        ind + "// Inputs",
        ind + ".i" + " " * 17 + "(i));",
        "endmodule",
    ]


def test_auto_inst_is_idempotent():
    buf = ["module top;", "InstModule instName (/*AUTOINST*/);", "endmodule"]
    once = auto_inst(buf, mods(IM))
    assert auto_inst(once, mods(IM)) == once


def test_auto_inst_keeps_manual_pins_and_adds_comma():
    buf = [
        "InstModule instName (",
        "    .i (my_i)",
        "    /*AUTOINST*/",
        ");",
    ]
    ind = " " * 21
    out = auto_inst(buf, mods(IM))
    assert out == [
        "InstModule instName (",
        "    .i (my_i),",  # comma added after the manual pin
        "    /*AUTOINST*/",
        ind + "// Outputs",
        ind + ".o" + " " * 17 + "(o[31:0]));",
    ]


def test_auto_inst_regexp_filter_include_and_exclude():
    buf = ["filt u_f (/*AUTOINST(\".*_i\")*/);"]
    out = auto_inst(buf, mods(FILT))
    body = "\n".join(out)
    assert ".din_i" in body and ".clk" not in body and ".dout_o" not in body
    assert "// Inputs" in body and "// Outputs" not in body

    buf = ["filt u_f (/*AUTOINST(\"?!.*_i\")*/);"]
    out = auto_inst(buf, mods(FILT))
    body = "\n".join(out)
    assert ".din_i" not in body and ".clk" in body and ".dout_o" in body


def test_auto_inst_sort_within_sections():
    buf = ["sortm u_s (/*AUTOINST*/);"]
    out = auto_inst(buf, mods(SORTM), sort=True)
    names = [ln.strip().split()[0] for ln in out if ln.strip().startswith(".")]
    assert names == [".a", ".z", ".m"]
    out = auto_inst(buf, mods(SORTM))
    names = [ln.strip().split()[0] for ln in out if ln.strip().startswith(".")]
    assert names == [".z", ".a", ".m"]


def test_auto_inst_dot_name():
    buf = ["InstModule instName (/*AUTOINST*/);"]
    out = auto_inst(buf, mods(IM), dot_name=True)
    assert out[-1] == " " * 21 + ".i);"  # .name shorthand, no parens
    assert "(o[31:0])" in out[2]  # vector connection keeps parens


def test_auto_inst_applies_template():
    text = """\
/* InstModule AUTO_TEMPLATE (
    .i (in_sig[@]),
); */
InstModule u_im_2 (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = auto_inst(text.splitlines(), mods(IM), templates=templates)
    ind = " " * 19
    assert out[3] == "InstModule u_im_2 (/*AUTOINST*/"
    assert out[4] == ind + "// Outputs"
    assert out[5] == ind + ".o" + " " * 19 + "(o[31:0]),"
    assert out[6] == ind + "// Inputs"
    # @ -> 2 (first digits of u_im_2); templated ports get a // Templated comment
    assert out[7] == ind + ".i" + " " * 19 + "(in_sig[2]));" + " " * 12 + "// Templated"
    # // Templated debris is stripped before re-expansion: idempotent
    assert auto_inst(out, mods(IM), templates=templates) == out


def test_auto_inst_which_and_missing_module():
    buf = [
        "InstModule u_a (/*AUTOINST*/);",
        "InstModule u_b (/*AUTOINST*/);",
    ]
    out = auto_inst(buf, mods(IM), which=1)
    assert out[0] == "InstModule u_a (/*AUTOINST*/);"
    assert out[1] == "InstModule u_b (/*AUTOINST*/"
    with pytest.raises(ValueError, match="No AUTOINST"):
        auto_inst(buf, mods(IM), which=5)
    with pytest.raises(KeyError, match="nope"):
        auto_inst(["nope u_x (/*AUTOINST*/);"], mods(IM))


def test_auto_inst_ignores_commented_marker():
    buf = ["// InstModule u_x (/*AUTOINST*/);"]
    assert auto_inst(buf, mods(IM)) == buf


# ---------------------------------------------------------------------------
# auto_param (verilog-auto-inst-param)


def test_auto_param_basic_doc_example():
    buf = [
        "module top;",
        "InstModule #(/*AUTOINSTPARAM*/)",
        "    instName (/*AUTOINST*/);",
        "endmodule",
    ]
    ind = " " * 13  # column after the '(' of '#('
    out = auto_param(buf, params(IMP))
    assert out == [
        "module top;",
        "InstModule #(/*AUTOINSTPARAM*/",
        ind + "// Parameters",
        ind + ".PAR" + " " * 23 + "(PAR))",
        "    instName (/*AUTOINST*/);",
        "endmodule",
    ]


def test_auto_param_is_idempotent():
    buf = ["InstModule #(/*AUTOINSTPARAM*/) instName (/*AUTOINST*/);"]
    once = auto_param(buf, params(IMP))
    assert auto_param(once, params(IMP)) == once


def test_auto_param_header_params_and_skip_manual():
    text = """\
module m #(parameter W = 8, D = 4) (a);
    input a;
endmodule
"""
    buf = [
        "m #(",
        "    .W (16)",
        "    /*AUTOINSTPARAM*/",
        ") u_m (a);",
    ]
    out = auto_param(buf, params(text))
    assert out[1] == "    .W (16),"  # manual override kept, comma added
    assert out[2] == "    /*AUTOINSTPARAM*/"
    assert out[3].strip() == "// Parameters"
    # the '#(' close paren is consumed; the instance text after it stays
    # unmapped parameter: identity (verilog-mode), not the default value
    assert out[4] == "    .D" + " " * 34 + "(D)) u_m (a);"
    assert len(out) == 5


def test_auto_param_regexp_filter_and_sort():
    text = """\
module m (a);
    parameter ZP = 1;
    parameter AP = 2;
    parameter OTHER = 3;
    input a;
endmodule
"""
    buf = ["m #(/*AUTOINSTPARAM(\"P$\")*/) u_m (a);"]
    out = auto_param(buf, params(text), sort=True)
    names = [ln.strip().split()[0] for ln in out if ln.strip().startswith(".")]
    assert [n for n in names if n.startswith(".")] == [".AP", ".ZP"]  # OTHER excluded, sorted


def test_auto_param_template_and_missing_module():
    text = """\
/* InstModule AUTO_TEMPLATE (
    .PAR (PAR*2),
); */
InstModule #(/*AUTOINSTPARAM*/) u_im_1 (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = auto_param(text.splitlines(), params(IMP), templates=templates)
    assert "(PAR*2))" in out[-1] and "// Templated" in out[-1]
    with pytest.raises(KeyError, match="nope"):
        auto_param(["nope #(/*AUTOINSTPARAM*/) u (/*AUTOINST*/);"], params(IMP))


def test_find_auto_markers_does_not_confuse_autoinstparam():
    lines = ["m #(/*AUTOINSTPARAM*/) u (/*AUTOINST*/);"]
    assert len(find_auto_markers(lines, "AUTOINST")) == 1
    assert len(find_auto_markers(lines, "AUTOINSTPARAM")) == 1


# ---------------------------------------------------------------------------
# multidimensional ports (validated against real verilog-mode)


MM = """\
module mm (
    input  [3:0][7:0]      packed2,
    input  [7:0]           unpacked_arr [0:3],
    output [1:0][3:0][7:0] packed3,
    input  [7:0]           mixed [0:1][0:2],
    input                  plain
);
endmodule
"""


def test_parse_module_ports_multidim():
    moddef = parse_module_ports(MM.splitlines())
    by_name = {p.name: p for p in moddef.ports}
    assert by_name["packed2"].packed == ("3:0", "7:0")
    assert by_name["packed2"].unpacked == ()
    assert by_name["unpacked_arr"].unpacked == ("0:3",)
    assert by_name["packed3"].packed == ("1:0", "3:0", "7:0")
    assert by_name["packed3"].direction == "output"  # direction not lost
    assert by_name["mixed"].packed == ("7:0",)
    assert by_name["mixed"].unpacked == ("0:1", "0:2")
    assert by_name["plain"].width is None


def test_auto_inst_multidim_comment_form():
    # matches real verilog-mode: name/*[packed].[unpacked]*/
    buf = ["module top;", "mm u_mm (/*AUTOINST*/);", "endmodule"]
    out = auto_inst(buf, {"mm": parse_module_ports(MM.splitlines())})
    body = "\n".join(out)
    assert "(packed3/*[1:0][3:0][7:0]*/)," in body
    assert "(packed2/*[3:0][7:0]*/)," in body
    assert "(unpacked_arr/*[7:0].[0:3]*/)," in body
    assert "(mixed/*[7:0].[0:1][0:2]*/)," in body
    assert "(plain));" in body
    # outputs section is present and ordered before inputs
    assert body.index("// Outputs") < body.index("// Inputs")


# ---------------------------------------------------------------------------
# parameters wrapped in `ifdef inside the #( ... ) header (real RTL style)


def test_parse_module_params_ifdef_wrapped():
    lines = [
        "module m #(",
        "    `ifdef DBG",
        "        parameter ADDR_WIDTH = 32  ,",
        "        parameter DATA_WIDTH = 64 ,",
        "    `endif",
        "    parameter BAUD_VALUE     = 8'h0c",
        ") (input clk);",
        "endmodule",
    ]
    names = [p.name for p in parse_module_params(lines)]
    assert names == ["ADDR_WIDTH", "DATA_WIDTH", "BAUD_VALUE"]


# ---------------------------------------------------------------------------
# verilog-mode regexp features on AUTOINST / AUTOINSTPARAM


def test_auto_inst_regexp_port_pattern_with_backref():
    # .pci_req\([0-9]+\)_l (pci_req_jtag_[\1])  ->  per-port backreference
    text = r"""/* m AUTO_TEMPLATE (
    .pci_req\([0-9]+\)_l (pci_req_jtag_[\1]),
); */
m u_m_0 (/*AUTOINST*/);
"""
    mod = parse_module_ports(
        """module m (
    input pci_req0_l,
    input pci_req2_l,
    input pci_req10_l,
    input other,
    output done
);
endmodule""".splitlines()
    )
    out = auto_inst(text.splitlines(), {"m": mod}, templates=find_auto_templates(text))
    body = "\n".join(out)
    assert "(pci_req_jtag_[0])," in body
    assert "(pci_req_jtag_[2])," in body
    assert "(pci_req_jtag_[10])," in body
    assert body.count("// Templated") == 3  # all three matched ports tagged
    assert "(other));" in body  # unmatched port -> identity (last port)


def test_auto_inst_at_regexp_custom_capture_group():
    # AUTO_TEMPLATE "REGEXP" changes what @ expands to (first () group).
    text = r"""/* m AUTO_TEMPLATE "_\([0-9]+\)" (
    .din (src[@]),
); */
m u_m_7 (/*AUTOINST*/);
"""
    mod = parse_module_ports(
        """module m (
    input [7:0] din,
    output vld
);
endmodule""".splitlines()
    )
    out = auto_inst(text.splitlines(), {"m": mod}, templates=find_auto_templates(text))
    assert any("(src[7])" in l for l in out)


def test_auto_inst_at_in_port_pattern_is_digit_group():
    # @ inside a port PATTERN means ([0-9]+); \1 refers to it.
    text = r"""/* m AUTO_TEMPLATE (
    .in@ (out[\1]),
); */
m u_m_0 (/*AUTOINST*/);
"""
    mod = parse_module_ports(
        """module m (
    input in3,
    input inx,
    output done
);
endmodule""".splitlines()
    )
    out = auto_inst(text.splitlines(), {"m": mod}, templates=find_auto_templates(text))
    body = "\n".join(out)
    assert "(out[3])," in body
    assert "(inx));" in body  # non-matching port identity (last port)


def test_auto_param_regexp_port_pattern_with_backref():
    text = r"""/* m AUTO_TEMPLATE (
    .CFG_\([0-9]+\) (cfg_jtag_[\1]),
); */
m #(/*AUTOINSTPARAM*/) u_m_0 (/*AUTOINST*/);
"""
    mod = parse_module_ports(
        """module m (a);
    parameter CFG_0 = 1;
    parameter CFG_12 = 2;
    parameter OTHER = 3;
    input a;
endmodule""".splitlines(),
        with_params=True,
    )
    out = auto_param(
        text.splitlines(), {"m": mod.params}, templates=find_auto_templates(text)
    )
    body = "\n".join(out)
    assert "(cfg_jtag_[0])" in body
    assert "(cfg_jtag_[12])" in body
    assert body.count("// Templated") == 2
    assert ".OTHER" in body and "(OTHER))" in body  # unmatched param -> identity


def test_auto_param_at_regexp_custom_capture_group():
    text = r"""/* m AUTO_TEMPLATE "_\([0-9]+\)" (
    .MODE (mode[@]),
); */
m #(/*AUTOINSTPARAM*/) u_m_5 (/*AUTOINST*/);
"""
    mod = parse_module_ports(
        """module m (a);
    parameter MODE = 0;
    input a;
endmodule""".splitlines(),
        with_params=True,
    )
    out = auto_param(
        text.splitlines(), {"m": mod.params}, templates=find_auto_templates(text)
    )
    assert any("(mode[5])" in l for l in out)


def test_at_regexp_matches_emacs_leftmost_match():
    # u_m_FOO with "_\([a-z]+\)" captures 'm' (leftmost match), NOT 'FOO' —
    # this is exactly what emacs string-match does (verified against emacs).
    (t,) = find_auto_templates('/* m AUTO_TEMPLATE "_\\([a-z]+\\)" (\n .x (y), ); */')
    assert template_at_value(t, "u_m_FOO") == "m"
    assert template_at_value(t, "ms2_FOO") == "FOO"


# ---------------------------------------------------------------------------
# SystemVerilog interface ports (// Interfaces section)

SUB_IF = """\
module sub (cpu_bus.master bus, cpu_bus misc, input clk, input [7:0] din, output done);
endmodule
"""


def mods_iface(*texts, ifaces=("cpu_bus",)):
    out = {}
    for t in texts:
        m = parse_module_ports(t.splitlines(), interfaces=set(ifaces))
        out[m.name] = m
    return out


def test_auto_inst_interface_section_matches_emacs():
    # verified against real verilog-mode: interface ports come first in a
    # // Interfaces section; a modport port connects as name.modport
    buf = ["sub u_sub (/*AUTOINST*/);"]
    out = auto_inst(buf, mods_iface(SUB_IF))
    ind = " " * 11  # column after the '(' of the pin list
    assert out == [
        "sub u_sub (/*AUTOINST*/",
        ind + "// Interfaces",
        ind + ".bus" + " " * 25 + "(bus.master),",
        ind + ".misc" + " " * 24 + "(misc),",
        ind + "// Outputs",
        ind + ".done" + " " * 24 + "(done),",
        ind + "// Inputs",
        ind + ".clk" + " " * 25 + "(clk),",
        ind + ".din" + " " * 25 + "(din[7:0]));",
    ]
    # re-running on the expanded output is idempotent
    assert auto_inst(out, mods_iface(SUB_IF)) == out


def test_auto_inst_no_interface_section_without_interface_ports():
    buf = ["InstModule instName (/*AUTOINST*/);"]
    out = auto_inst(buf, mods(IM))
    assert not any(ln.strip() == "// Interfaces" for ln in out)


def _pins_after_marker(out, marker="/*AUTOINST*/"):
    """The instance-pin lines of OUT, from the marker line onward (skips any
    AUTO_TEMPLATE comment block above the instance)."""
    start = next(i for i, ln in enumerate(out) if marker in ln)
    return out[start:]


def test_auto_inst_interface_template_exact_and_regexp():
    text = """\
/* sub AUTO_TEMPLATE (
    .bus (x_if.master),
    .\\(.*\\)sc (\\1_if),
); */
sub u_sub (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = _pins_after_marker(auto_inst(text.splitlines(), mods_iface(SUB_IF), templates=templates))
    bus = next(ln for ln in out if ln.strip().startswith(".bus"))
    misc = next(ln for ln in out if ln.strip().startswith(".misc"))
    # exact template entry wins for .bus; both get // Templated
    assert "(x_if.master)" in bus and "// Templated" in bus
    # regexp entry: \(.*\)sc matches "misc" with \1 = "mi"
    assert "(mi_if)" in misc and "// Templated" in misc
    # non-matching interface ports keep their default connection
    done = next(ln for ln in out if ln.strip().startswith(".done"))
    assert "(done)" in done and "// Templated" not in done


def test_auto_inst_interface_template_at_and_digit_pattern():
    text = """\
/* sub AUTO_TEMPLATE (
    .misc (if_@),
); */
sub u_sub_7 (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    out = _pins_after_marker(auto_inst(text.splitlines(), mods_iface(SUB_IF), templates=templates))
    misc = next(ln for ln in out if ln.strip().startswith(".misc"))
    # @ expands to the instance number (first digit group of u_sub_7)
    assert "(if_7)" in misc and "// Templated" in misc

    # @ inside a port pattern is a digit group; \1 back-substitutes
    text2 = """\
/* sub2 AUTO_TEMPLATE (
    .bus@ (x_if\\1.master),
); */
sub2 u_sub2_3 (/*AUTOINST*/);
"""
    sub2 = """\
module sub2 (cpu_bus.master bus0, cpu_bus.master bus12, input clk);
endmodule
"""
    templates2 = find_auto_templates(text2)
    out2 = _pins_after_marker(auto_inst(text2.splitlines(), mods_iface(sub2), templates=templates2))
    bus0 = next(ln for ln in out2 if ln.strip().startswith(".bus0"))
    bus12 = next(ln for ln in out2 if ln.strip().startswith(".bus12"))
    assert "(x_if0.master)" in bus0 and "// Templated" in bus0
    assert "(x_if12.master)" in bus12 and "// Templated" in bus12


def test_auto_inst_interface_manual_pin_kept_and_regexp_filter():
    buf = [
        "sub u_sub (",
        "    .bus (hand_bus.master)",
        "    /*AUTOINST*/",
        ");",
    ]
    out = auto_inst(buf, mods_iface(SUB_IF))
    body = "\n".join(out)
    assert ".bus (hand_bus.master)," in body  # manual pin kept, comma added
    assert body.count(".bus") == 1
    assert ".misc" in body  # other interface ports still expanded

    # /*AUTOINST("?!bus")*/ excludes interface ports by name too
    buf2 = ["sub u_sub (/*AUTOINST(\"?!bus\")*/);"]
    out2 = auto_inst(buf2, mods_iface(SUB_IF))
    body2 = "\n".join(out2)
    assert ".bus" not in body2 and ".misc" in body2


def test_auto_inst_interface_dot_name():
    # identity interface connection (.misc (misc)) collapses to .misc
    buf = ["sub u_sub (/*AUTOINST*/);"]
    out = auto_inst(buf, mods_iface(SUB_IF), dot_name=True)
    misc = next(ln for ln in out if ln.strip().startswith(".misc"))
    assert misc.strip() == ".misc,"
    bus = next(ln for ln in out if ln.strip().startswith(".bus"))
    assert "(bus.master)" in bus  # modport connection keeps parens


# ---------------------------------------------------------------------------
# regression: mm_common batch findings


def test_auto_template_keyword_case_insensitive():
    """Lowercase /* m auto_template ( ... ) */ blocks are honoured too."""
    text = """\
/* InstModule auto_template (
    .i (in_sig[@]),
); */
InstModule u_im_2 (/*AUTOINST*/);
"""
    templates = find_auto_templates(text)
    assert len(templates) == 1
    out = auto_inst(text.splitlines(), mods(IM), templates=templates)
    assert any("in_sig[2]" in line for line in out)


def test_resolve_instance_skips_templated_comment_before_name():
    """EAP leaves ``))// Templated`` between the #( ... ) block and the
    instance name; the comment must not resolve as the module name."""
    buf = [
        "InstModule #(",
        "    .PAR (PAR))// Templated",
        "    u_im (/*AUTOINST*/",
        "    .o (o),",
        "    .i (i));",
        "endmodule",
    ]
    assert marker_modules(buf, "AUTOINST") == ["InstModule"]


def test_auto_param_forward_skips_comment_before_instance_name():
    buf = [
        "InstModule #(/*AUTOINSTPARAM*/",
        "    .PAR (PAR))// Templated",
        "    u_im (/*AUTOINST*/);",
    ]
    out = auto_param(buf, params(IMP))
    assert any("// Parameters" in line for line in out)


def test_consume_semi_eats_trailing_spaces():
    """A leftover ``); `` (trailing space) before the tail must not leak a
    stray space after the regenerated ``// Templated`` comment."""
    text = """\
/* InstModule AUTO_TEMPLATE (
    .i (in_sig[@]),
); */
InstModule u_im_2 (/*AUTOINST*/
// Outputs
    .o (o[31:0]),
// Inputs
    .i (in_sig[2])// Templated
); 
"""
    templates = find_auto_templates(text)
    once = auto_inst(text.splitlines(), mods(IM), templates=templates)
    twice = auto_inst(once, mods(IM), templates=templates)
    assert once == twice
    assert all(line == line.rstrip() for line in twice)


def test_auto_inst_keeps_param_section_templated_tag():
    """EAP's // Templated tags inside #( ... ) survive an EAI run (the strip
    is scoped to the instances each command rewrites)."""
    buf = [
        "InstModule #(",
        "    // Parameters",
        "    .PAR (PAR),     // Templated",
        "    .PAR2 (PAR2))",
        "    u_im (/*AUTOINST*/);",
    ]
    templates = find_auto_templates(
        "/* InstModule AUTO_TEMPLATE (\n.PAR (PAR),\n); */\n" + "\n".join(buf)
    )
    out = auto_inst(buf, mods(IM), templates=templates)
    assert any("// Templated" in line and ".PAR (PAR)" in line for line in out)


def test_auto_inst_regexp_filter_python_dialect():
    """Python-style alternation in /*AUTOINST("regex")*/ (auto-detected)."""
    buf = ["filt u_f (/*AUTOINST(\"din_i|dout_o\")*/);"]
    out = auto_inst(buf, mods(FILT))
    body = "\n".join(out)
    assert ".din_i" in body and ".dout_o" in body and ".clk" not in body

    buf = ["filt u_f (/*AUTOINST(\"?!din_i|dout_o\")*/);"]
    out = auto_inst(buf, mods(FILT))
    body = "\n".join(out)
    assert ".clk" in body and ".din_i" not in body and ".dout_o" not in body


def test_auto_inst_templated_multidim_note():
    """A templated multidim pin gets the /*[D1][D2]*/ note from [][]
    (real verilog-mode writes it too), params substituted and folded."""
    md = """\
module md #(parameter W = 4) (
    input  wire        clk,
    output reg [W-1:0][3:0] bid
);
endmodule
"""
    buf = r"""module top;
/* md AUTO_TEMPLATE (
    .b\(.*\) (x_\1[][]),
) */
md #(.W (8)) u_md (/*AUTOINST*/);
endmodule""".splitlines()
    from verilog_tooling.emacs import auto_inst
    from verilog_tooling.template import find_auto_templates
    out = auto_inst(
        buf, {"md": parse_module_ports(md.splitlines())},
        templates=find_auto_templates("\n".join(buf)),
    )
    body = "\n".join(out)
    # b\(.*\) captures "id" from bid; no param_value -> dims stay symbolic
    assert "x_id/*[W-1:0][3:0]*/" in body
    # with param_value the #(...) override is substituted and folded
    out = auto_inst(
        buf, {"md": parse_module_ports(md.splitlines())},
        templates=find_auto_templates("\n".join(buf)), param_value=True,
    )
    assert "x_id/*[7:0][3:0]*/" in "\n".join(out)


# ---------------------------------------------------------------------------
# stray / commented-out AUTO markers must not abort the expansion


def test_marker_in_trailing_line_comment_ignored():
    """`code; // /*AUTOINST*/` — the commented marker is not a marker."""
    buf = """\
module top;
    InstModule u_a (/*AUTOINST*/); // /*AUTOINST*/
endmodule
"""
    out = auto_inst(buf.splitlines(), mods(IM))
    assert sum(1 for ln in out if ".o" in ln) == 1
    assert sum(1 for ln in out if ".i" in ln) == 1
    assert any("// /*AUTOINST*/" in ln for ln in out)  # comment untouched


def test_stray_autoinst_marker_warns_and_skips(capsys):
    """A marker outside any instance pin list: warn, expand the rest."""
    buf = """\
module top;
/*AUTOINST*/
    InstModule u_a (/*AUTOINST*/);
endmodule
"""
    out = auto_inst(buf.splitlines(), mods(IM))
    assert any(".o" in ln for ln in out) and any(".i" in ln for ln in out)
    assert "outside an instance pin list" in capsys.readouterr().err


def test_stray_autoinstparam_marker_warns_and_skips(capsys):
    buf = """\
module top;
    InstModule u_a (/*AUTOINST*/);
endmodule
/*AUTOINSTPARAM*/
"""
    out = auto_inst(buf.splitlines(), mods(IM))
    assert any(".o" in ln for ln in out)
    out_p = auto_param(buf.splitlines(), mods(IMP))
    assert "outside an instance parameter" in capsys.readouterr().err
    assert sum("/*AUTOINSTPARAM*/" in ln for ln in out_p) == 1  # stray kept


def test_autoinst_template_eval_failure_warns_and_skips(capsys):
    """A template whose @"..." evaluation fails (outside the Python subset)
    warns and leaves that instance untouched — it must NOT abort the file:
    other instances and the marker itself stay processable on the next run."""
    text = """\
/* InstModule AUTO_TEMPLATE (
    .i (sig[@"(elisp-unsupported-fn x)"]),
); */
InstModule u_bad (/*AUTOINST*/);
"""
    buf = text.splitlines()
    out = auto_inst(buf, mods(IM), templates=find_auto_templates(text))
    assert out == buf  # instance left untouched
    err = capsys.readouterr().err
    assert "skipped" in err and "u_bad" in err
