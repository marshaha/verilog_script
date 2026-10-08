"""automatic.vim AutoArg (AR) / KillAutoArg rewritten in Python (arg.py)."""

from verilog_tooling.arg import (
    VerilogBuffer,
    auto_arg,
    kill_auto_arg,
    main,
)

# 4-space s:vlog_arg_margin, 40-column s:vlog_max_col.
M = "    "


# ---------------------------------------------------------------------------
# kill_auto_arg (KillAutoArg)


def test_kill_auto_arg_collapses_generated_block_to_stub():
    lines = [
        "module top (/*autoarg*/",
        M + "//Outputs",
        M + "dout, vld, ",
        "",
        M + "//Inouts",
        M + "sda",
        "",
        M + "//Inputs",
        M + "clk, din, rst_n, ",
        ");",
        "endmodule",
    ]
    assert kill_auto_arg(lines) == ["module top (/*autoarg*/);", "endmodule"]


def test_kill_auto_arg_keeps_terminated_marker_verbatim():
    lines = ["module top (/*autoarg*/);", "    input wire clk;", "endmodule"]
    assert kill_auto_arg(lines) == lines


def test_kill_auto_arg_copies_other_lines_unchanged():
    lines = [
        "// leading comment",
        "module top (/*autoarg*/",
        M + "clk",
        ");",
        "assign x = 1;",
    ]
    assert kill_auto_arg(lines) == [
        "// leading comment",
        "module top (/*autoarg*/);",
        "assign x = 1;",
    ]


def test_kill_auto_arg_uppercase_marker():
    lines = ["module top (/*AUTOARG*/", M + "clk", ");"]
    assert kill_auto_arg(lines) == ["module top (/*AUTOARG*/);"]


# ---------------------------------------------------------------------------
# auto_arg (AutoArg)


def test_auto_arg_generates_sections_in_emacs_outputs_inouts_inputs_order():
    lines = [
        "module top (/*autoarg*/);",
        "    input  wire        clk;",
        "    input  wire [7:0]  din;",
        "    output wire [15:0] dout;",
        "    inout  wire        sda;",
        "endmodule",
    ]
    assert auto_arg(lines) == [
        "module top (/*autoarg*/",
        M + "//Outputs",
        M + "dout, ",
        "",
        M + "//Inouts",
        M + "sda, ",
        "",
        M + "//Inputs",
        M + "clk, din",
        ");",
        "    input  wire        clk;",
        "    input  wire [7:0]  din;",
        "    output wire [15:0] dout;",
        "    inout  wire        sda;",
        "endmodule",
    ]


def test_auto_arg_strips_known_types_widths_and_trailing_comments():
    lines = [
        "module top (/*autoarg*/);",
        "    input logic clk; // the clock",
        "    input wire signed [W-1:0] din_q;",
        "    input [3:0] w;",
        "    output reg [7:0] dout;",
        "endmodule",
    ]
    # s:VlogTypeDatas strips wire/reg/parameter/localparam/genvar/integer and
    # the packed range, but not logic/signed -- those pass through verbatim,
    # exactly as the Vim original emits them (verified against real Vim).
    assert auto_arg(lines) == [
        "module top (/*autoarg*/",
        M + "//Outputs",
        M + "dout, ",
        "",
        M + "//Inputs",
        M + "logic clk, signed [W-1:0] din_q, w",
        ");",
        "    input logic clk; // the clock",
        "    input wire signed [W-1:0] din_q;",
        "    input [3:0] w;",
        "    output reg [7:0] dout;",
        "endmodule",
    ]


def test_auto_arg_strips_reg_and_integer_types():
    lines = [
        "module top (/*autoarg*/);",
        "    input reg r_q; /* inline block */",
        "    input integer cnt;",
        "endmodule",
    ]
    assert auto_arg(lines) == [
        "module top (/*autoarg*/",
        M + "//Inputs",
        M + "r_q, cnt",
        ");",
        "    input reg r_q; /* inline block */",
        "    input integer cnt;",
        "endmodule",
    ]


def test_auto_arg_wraps_when_running_column_exceeds_40():
    lines = [
        "module top (/*autoarg*/);",
        "    input wire aaaaaaaa0;",
        "    input wire aaaaaaaa1;",
        "    input wire aaaaaaaa2;",
        "    input wire aaaaaaaa3;",
        "    input wire aaaaaaaa4;",
        "endmodule",
    ]
    assert auto_arg(lines) == [
        "module top (/*autoarg*/",
        M + "//Inputs",
        M + "aaaaaaaa0, aaaaaaaa1, aaaaaaaa2, aaaaaaaa3, ",
        M + "aaaaaaaa4",
        ");",
        "    input wire aaaaaaaa0;",
        "    input wire aaaaaaaa1;",
        "    input wire aaaaaaaa2;",
        "    input wire aaaaaaaa3;",
        "    input wire aaaaaaaa4;",
        "endmodule",
    ]


def test_auto_arg_wrap_exact_column_boundary():
    # margin(4) + 2*(len+2) = 40 is not > 40, so 4 names of 8 chars fill one line
    lines = ["module top (/*autoarg*/);"] + [
        f"    input wire name_000{i};" for i in range(4)
    ] + ["endmodule"]
    out = auto_arg(lines)
    assert out[2] == M + "name_0000, name_0001, name_0002, name_0003"
    assert out[3] == ");"


def test_auto_arg_omits_empty_sections():
    lines = [
        "module top (/*autoarg*/);",
        "    output wire vld;",
        "endmodule",
    ]
    assert auto_arg(lines) == [
        "module top (/*autoarg*/",
        M + "//Outputs",
        M + "vld",
        ");",
        "    output wire vld;",
        "endmodule",
    ]


def test_auto_arg_no_ports_at_all_leaves_marker_plus_close():
    lines = ["module top (/*autoarg*/);", "endmodule"]
    # nothing to declare: the collapsed one-line stub stays as-is
    assert auto_arg(lines) == ["module top (/*autoarg*/);", "endmodule"]


def test_auto_arg_is_idempotent():
    lines = [
        "module top (/*autoarg*/);",
        "    input  wire        clk;",
        "    input  wire [7:0]  din;",
        "    output wire        vld;",
        "    inout  wire        sda;",
        "endmodule",
    ]
    once = auto_arg(lines)
    assert auto_arg(once) == once


def test_auto_arg_preserves_lines_outside_marker_region():
    lines = [
        "// header",
        "`timescale 1ns/1ps",
        "module top (/*autoarg*/);",
        "    input wire clk;",
        "endmodule",
        "module other (a);",
        "    input a;",
        "endmodule",
    ]
    out = auto_arg(lines)
    assert out[:2] == ["// header", "`timescale 1ns/1ps"]
    assert out[-4:] == [
        "endmodule",
        "module other (a);",
        "    input a;",
        "endmodule",
    ]


def test_auto_arg_skips_function_bodies():
    lines = [
        "module top (/*autoarg*/);",
        "    input wire clk;",
        "    function f;",
        "        input wire hidden_f;",
        "    endfunction",
        "    output wire vld;",
        "endmodule",
    ]
    out = auto_arg(lines)
    # the generated port region holds only 'clk'; the function body is skipped
    # by the filter (s:Filter) and copied verbatim afterwards as source text
    generated = out[1 : out.index(");")]
    assert M + "//Inputs" in generated
    assert not any("hidden_f" in line for line in generated)


def test_auto_arg_ignores_declarations_after_endmodule():
    lines = [
        "module a (/*autoarg*/);",
        "    input wire clk_a;",
        "endmodule",
        "module b ();",
        "    input wire clk_b;",
        "endmodule",
    ]
    assert auto_arg(lines) == [
        "module a (/*autoarg*/",
        M + "//Inputs",
        M + "clk_a",
        ");",
        "    input wire clk_a;",
        "endmodule",
        "module b ();",
        "    input wire clk_b;",
        "endmodule",
    ]


# ---------------------------------------------------------------------------
# VerilogBuffer methods


def test_buffer_methods_return_new_buffers():
    buf = VerilogBuffer(["module top (/*autoarg*/", M + "clk", ");"])
    killed = buf.kill_auto_arg()
    assert isinstance(killed, VerilogBuffer)
    assert killed.lines == ["module top (/*autoarg*/);"]
    assert buf.lines == ["module top (/*autoarg*/", M + "clk", ");"]  # untouched

    expanded = VerilogBuffer(
        ["module top (/*autoarg*/);", "    input wire clk;", "endmodule"]
    ).auto_arg()
    assert isinstance(expanded, VerilogBuffer)
    assert expanded.lines == [
        "module top (/*autoarg*/",
        M + "//Inputs",
        M + "clk",
        ");",
        "    input wire clk;",
        "endmodule",
    ]


def test_buffer_methods_are_bare_delegates():
    # a marker with no plausible bare-`);` region terminator is left
    # untouched — KillAutoArg must not drop real code (the old behaviour
    # deleted everything it couldn't prove was generated)
    buf = VerilogBuffer(["module top (/*autoarg*/)", M + "clk"])
    assert buf.kill_auto_arg().lines == ["module top (/*autoarg*/)", M + "clk"]


# ---------------------------------------------------------------------------
# CLI


def test_cli_ar_end_to_end(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("module top (/*autoarg*/);\n    input wire clk;\nendmodule\n")
    out_file = tmp_path / "out.v"
    main(["ar", "-i", str(buf), "-o", str(out_file)])
    assert out_file.read_text().splitlines() == [
        "module top (/*autoarg*/",
        M + "//Inputs",
        M + "clk",
        ");",
        "    input wire clk;",
        "endmodule",
    ]


def test_cli_kill_end_to_end(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("module top (/*autoarg*/\n" + M + "clk\n);\nendmodule\n")
    out_file = tmp_path / "out.v"
    main(["kill", "-i", str(buf), "-o", str(out_file)])
    assert out_file.read_text().splitlines() == [
        "module top (/*autoarg*/);",
        "endmodule",
    ]


def test_auto_arg_header_style_ports_no_double_comma():
    """Ports declared in the header (input clk,) must not keep their comma."""
    lines = [
        "module top (",
        "    input        clk,",
        "    input  [7:0] din,",
        "    output       done,",
        "    output [7:0] q",
        "    /*autoarg*/",
        ");",
        "endmodule",
    ]
    out = auto_arg(lines)
    assert not any(",," in line for line in out)
    body = "\n".join(out)
    assert "clk, din\n" in body
    assert "done, q, " in body
    assert out[-2] == ");"
    assert out[-1] == "endmodule"


def test_auto_arg_header_style_multiname_decl():
    lines = [
        "module top (/*autoarg*/);",
        "    input        clk,",
        "    input  [7:0] a, b,",
        "    output       done",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert ",," not in body
    assert "a, b\n" in body  # multi-name decl stays one entry, trailing comma gone


def test_misplaced_marker_after_header_is_untouched():
    """/*autoarg*/ after the header's `);`: kill must not eat code, ar must
    not expand it (the Vim original did both)."""
    lines = [
        "module top (",
        "    input        clk,",
        "    output       done",
        ");",
        "/*autoarg*/",
        "/*autodef*/",
        "sub u_sub (/*autoinst*/);",
        "endmodule",
    ]
    assert auto_arg(lines) == lines
    assert kill_auto_arg(lines) == lines


def test_unterminated_marker_keeps_tail():
    """A marker region with no `);` terminator must not drop the buffer tail."""
    lines = [
        "module top (/*autoarg*/",
        "    clk, din",
        "endmodule",
    ]
    assert kill_auto_arg(lines) == lines


def test_auto_arg_robust_spaces_and_unpacked_dims():
    """val[3:0] / val [3:0] unpacked dims, space before comma/semicolon,
    extra spaces everywhere — the port list gets the bare name."""
    lines = [
        "module top (/*autoarg*/);",
        "    input        clk ,",
        "    input  [7:0] din  ,",
        "    input        val[3:0],",
        "    input        val2 [3:0],",
        "    output [7:0] q   ;",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert ",," not in body
    assert "clk, din, val, val2\n" in body
    assert "q" in body and "[3:0]" not in body.split("//Inputs")[1].split(");")[0]


def test_auto_arg_robust_crossline_decl():
    """A declaration split after the width/type is joined: the name on the
    next line still lands in the port list."""
    lines = [
        "module top (/*autoarg*/);",
        "    input  [7:0]",
        "        din,",
        "    input",
        "        clk,",
        "    output [3:0][7:0]",
        "        q,",
        "    output       done",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert "din" in body and "clk" in body
    assert "q, " in body and "done" in body
    assert ",," not in body


def test_auto_arg_robust_unpacked_multidim_and_multi_names():
    lines = [
        "module top (/*autoarg*/);",
        "    input  [7:0] a, b [3:0],",
        "    input        mem [3:0][7:0],",
        "    output       done",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert "a, b, mem\n" in body
    assert "done" in body


def test_auto_arg_concat_port_header():
    """Non-ANSI header with a concatenation port ({a, b}): names come from
    the body declarations, the marker list is generated normally."""
    lines = [
        "module top (/*autoarg*/);",
        "    input  a, b, c;",
        "    output done;",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert "a, b, c\n" in body
    assert "done" in body


def test_auto_arg_single_line_multi_port_header():
    """module m (input a, input b, output c); — several direction groups on
    ONE line must be collected individually."""
    lines = [
        "module m (input a, input [3:0] b /*autoarg*/);",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert "a, b" in body
    assert ",," not in body and "b)" not in body


def test_auto_arg_multi_decl_one_line_body():
    lines = [
        "module top (/*autoarg*/);",
        "    input a, input [3:0] b, output c;",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert "//Inputs" in body and "a, b\n" in body
    assert "//Outputs" in body and "\n    c" in body


def test_auto_arg_multi_decl_crossline_chain():
    """Cross-line decl that also mixes directions on the joined line."""
    lines = [
        "module top (/*autoarg*/);",
        "    input [7:0]",
        "        a, output",
        "        c,",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert "a, " in body and "\n    c" in body


def test_auto_arg_inline_block_comment_in_decl():
    """An inline /* ... */ comment on a declaration line is stripped before
    the name is taken."""
    lines = [
        "module m (input a, input [3:0] b /*autoarg*/);",
        "endmodule",
    ]
    out = auto_arg(lines)
    body = "\n".join(out)
    assert "a, b" in body and "autoarg" not in body.split("//Inputs")[1]


# ---------------------------------------------------------------------------
# autoarg x AUTOINPUT/AUTOOUTPUT cohabitation


def test_auto_arg_yields_when_aio_markers_share_the_header():
    """AIO markers inside the module header: the AIO regions own the port
    declarations — autoarg must not pack a name list (it would duplicate
    them, and its `);` would split the header)."""
    lines = [
        "module top (",
        "    /*autoarg*/",
        "    /*AUTOINPUT*/",
        "    /*AUTOOUTPUT*/",
        ");",
        "sub u_sub (/*autoinst*/",
        "    .clk (clk)",
        ");",
        "endmodule",
    ]
    assert auto_arg(lines) == lines


def test_auto_arg_collects_body_aio_decls():
    """The canonical combo (autoarg in the header, AIO regions in the
    body): the generated input/output declarations are collected into the
    header name list."""
    lines = [
        "module top (/*autoarg*/);",
        "/*AUTOINPUT*/",
        "// Beginning of automatic inputs (from unused autoinst inputs)",
        "input                                   clk; // To u of sub.v",
        "// End of automatics",
        "/*AUTOOUTPUT*/",
        "// Beginning of automatic outputs (from unused autoinst outputs)",
        "output                                  done; // From u of sub.v",
        "// End of automatics",
        "endmodule",
    ]
    out = auto_arg(lines)
    text = "\n".join(out)
    assert "//Inputs" in text and "clk" in text.split("//Inputs")[1]
    assert "//Outputs" in text and "done" in text.split("//Outputs")[1]


def test_auto_arg_warns_when_names_vanish(capsys):
    """A port-list name with no input/output/inout declaration (only wire —
    not a legal port) is dropped from the regenerated list, with a warning
    naming every dropped name."""
    lines = [
        "module top (/*autoarg*/",
        "    clk, gpio_pad",
        ");",
        "input clk;",
        "wire gpio_pad;",
        "endmodule",
    ]
    out = auto_arg(lines)
    err = capsys.readouterr().err
    assert "dropped from the port list" in err and "gpio_pad" in err
    text = "\n".join(out)
    assert "clk" in text and "gpio_pad" not in text.split(");")[0]


# ---------------------------------------------------------------------------
# inout inference for direction-less port-list names

INOUT_SUB = """\
module gpio_wrap (
    input  wire clk,
    inout  wire gpio
);
endmodule
"""


def test_auto_arg_infers_inout_for_port_list_names():
    """A direction-less name in the old /*autoarg*/ list that connects to an
    instance inout pin is KEPT in the //Inouts section and gets an
    `inout wire` body declaration (not dropped, no supplementary wire)."""
    from verilog_tooling.inst import parse_module_ports

    mods = {"gpio_wrap": parse_module_ports(INOUT_SUB.splitlines())}
    lines = [
        "module top (/*autoarg*/",
        "    clk, gpio",
        ");",
        "input clk;",
        "gpio_wrap u0 (/*autoinst*/",
        "    .clk  (clk),",
        "    .gpio (gpio)",
        ");",
        "endmodule",
    ]
    out = auto_arg(lines, mods)
    text = "\n".join(out)
    assert "//Inouts" in text and "gpio" in text.split("//Inouts")[1]
    assert any(ln.strip().startswith("inout wire") and "gpio" in ln for ln in out)
    # second run: the inout wire decl is collected normally — byte-stable
    assert auto_arg(out, mods) == out


def test_auto_arg_still_drops_directionless_non_inout_names(capsys):
    from verilog_tooling.inst import parse_module_ports

    mods = {"gpio_wrap": parse_module_ports(INOUT_SUB.splitlines())}
    lines = [
        "module top (/*autoarg*/",
        "    clk, not_a_port",
        ");",
        "input clk;",
        "wire not_a_port;",
        "gpio_wrap u0 (/*autoinst*/",
        "    .clk  (clk),",
        "    .gpio (gpio)",
        ");",
        "endmodule",
    ]
    out = auto_arg(lines, mods)
    err = capsys.readouterr().err
    assert "not_a_port" in err  # dropped with warning


def test_auto_arg_ansi_header_decls_move_to_body():
    """ANSI io declarations inside a /*autoarg*/ header are consumed and
    re-emitted in the body as semicolon-terminated 1995 declarations — the
    regenerated name list would otherwise leave them dangling (a header
    ``output wire a,`` after the new ``);`` is a syntax error)."""
    lines = [
        "module m ( /*autoarg*/",
        "//all outputs",
        "    output    wire          o1,",
        "    output    wire [31:0]   o2,",
        "",
        "//all inputs",
        "    input     wire          i1,",
        "    input     wire [9:0]    i2);",
        "endmodule",
    ]
    out = auto_arg(lines)
    text = "\n".join(out)
    assert "//Inputs" in text and "//Outputs" in text
    body = out[out.index(");") + 1 :]
    assert body[0] == "//all outputs"
    assert "    output    wire          o1;" in body
    assert "    output    wire [31:0]   o2;" in body
    assert "//all inputs" in body
    assert "    input     wire          i1;" in body
    assert "    input     wire [9:0]    i2;" in body
    # nothing dangles: every declaration line in the body ends with ';'
    assert all(not ln.rstrip().endswith(",") for ln in body)
    assert auto_arg(out) == out  # idempotent


def test_auto_arg_ansi_header_wrapped_multi_name_decl():
    """A multi-name declaration whose name list wraps onto an indented
    continuation line keeps every name (wbuart32 rtl/wbuart.v shape):
    the names must all reach the regenerated port list and the moved
    body declaration, and no fragment may be left dangling."""
    lines = [
        "module m #(parameter W = 8) (/*autoarg*/",
        "    input wire i1,",
        "    // group comment",
        "    input wire i2,",
        "    output wire o_long_a, o_long_b,",
        "                o_long_c, o_long_d",
        ");",
        "endmodule",
    ]
    out = auto_arg(lines)
    text = "\n".join(out)
    assert "o_long_a, o_long_b, o_long_c, o_long_d" in text
    assert "output wire o_long_a, o_long_b, o_long_c, o_long_d;" in text
    assert any("// group comment" in ln for ln in out)  # comment keeps its place
    body = out[out.index(");") + 1 :]
    assert all(not ln.rstrip().endswith(",") for ln in body)
    assert auto_arg(out) == out  # idempotent


def test_auto_arg_ansi_single_line_header():
    lines = ["module m (/*autoarg*/ input a, output wire [3:0] b);", "endmodule"]
    out = auto_arg(lines)
    text = "\n".join(out)
    assert "input a;" in text and "output wire [3:0] b;" in text
    assert "//Inputs" in text and "//Outputs" in text
    assert auto_arg(out) == out


def test_auto_arg_non_ansi_untouched_by_consume():
    """1995-style modules (declarations already in the body) are byte-identical."""
    lines = [
        "module m (/*autoarg*/",
        "    a, b",
        ");",
        "input a;",
        "output b;",
        "endmodule",
    ]
    out = auto_arg(lines)
    assert "input a;" in out and "output b;" in out
    assert auto_arg(out) == out
