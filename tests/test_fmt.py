"""automatic.vim format command family (fmt.py): AIF / APF / ADF / AF."""

from verilog_tooling.fmt import (
    all_format,
    auto_define_format,
    auto_define_len,
    auto_inst_format,
    auto_param_decl_format,
    auto_port_format,
)
from verilog_tooling.inst import VerilogBuffer, main

M = " " * 8  # t:vlog_inst_margin

# AIF margin arithmetic: port 'clk' -> prefix_max = 3+2 = 5, so 5-3+1 = 3
# spaces before '('; connection 'clk' -> suffix floor 58, so 58-3+1 = 56
# spaces before ')'.
CLK_LINE = M + ".clk" + " " * 3 + "(clk" + " " * 56 + "),"
DIN_LINE = M + ".din" + " " * 3 + "(din[7:0]" + " " * 51 + "),"
VLD_LINE = M + ".vld" + " " * 3 + "(vld" + " " * 56 + ")"
# a one-liner's last (or only) token loses its comma (Vim '),->') ' rewrite)
CLK_LAST = M + ".clk" + " " * 3 + "(clk" + " " * 56 + ")"

EXPLODED = [
    "small u_small_0 (/*autoinst*/",
    CLK_LINE,
    DIN_LINE,
    VLD_LINE,
    M + ");",
]

SMALL_BODY = """\
module small (
    input  wire       clk,
    input  wire [7:0] din,
    output wire       vld
);
endmodule
"""


# ---------------------------------------------------------------------------
# auto_inst_format (AIF)


def test_aif_explodes_oneliner_and_realigns():
    lines = ["small u_small_0 (/*autoinst*/ .clk(clk), .din(din[7:0]), .vld(vld));"]
    assert auto_inst_format(lines) == EXPLODED


def test_aif_is_idempotent():
    assert auto_inst_format(EXPLODED) == EXPLODED


def test_aif_normalizes_emacs_last_port_first():
    lines = [
        "small u_s (/*AUTOINST*/",
        "    .clk (clk),",
        "    .din (din[7:0])); // data",
    ]
    out = auto_inst_format(lines)
    assert out == [
        "small u_s (/*AUTOINST*/",
        CLK_LINE,
        DIN_LINE[:-1] + "// data",  # '));' fully stripped: ';' does not leak
        "); ",  # leftover of the emacs-format split, untouched by the pin match
    ]


def test_aif_keeps_double_paren_terminator():
    lines = [
        "m #(",
        "    .W(16))",
        ") u_m (",
        "/*autoinst*/",
        "    .a(sig_a),",
        ");",
    ]
    out = auto_inst_format(lines)
    # suffix_max covers 'sig_a' (5) -> floor 58; '16' gets 58-2+1 = 57 spaces
    assert out[1] == M + ".W" + " " * 3 + "(16" + " " * 57 + "))"
    assert out[4] == M + ".a" + " " * 3 + "(sig_a" + " " * 54 + "),"
    assert auto_inst_format(out) == out


def test_aif_leaves_multiline_param_value_untouched():
    # mor1kx (rtl/verilog/mor1kx_bus_if_wb32.v / mor1kx.v templates): a
    # parameter override value spanning lines — `(FEATURE!= "NONE")?`
    # with the branches on following lines — is not a complete pin
    # connection on its first line.  Splitting there emitted
    # `.BURST_LENGTH ((FEATURE...)... ),` and stranded the continuation
    # lines, producing spurious verilator syntax errors.
    lines = [
        "sub #(",
        '        .BURST_LENGTH ((FEATURE_CACHE != "NONE") ?',
        "                       ((FEATURE_W == 4) ? 4 : 1)",
        "                       : 1))",
        "   ) u_sub (/*autoinst*/);",
    ]
    out = auto_inst_format(lines)
    assert out[1] == '        .BURST_LENGTH ((FEATURE_CACHE != "NONE") ?'
    assert out[2] == "                       ((FEATURE_W == 4) ? 4 : 1)"
    assert out[3] == "                       : 1))"
    assert auto_inst_format(out) == out


def test_aif_verilog_buffer_method():
    buf = VerilogBuffer(["small u_s (/*autoinst*/ .clk(clk));"])
    assert buf.auto_inst_format().lines[1] == CLK_LAST


# ---------------------------------------------------------------------------
# auto_port_format (APF)


# APF margin arithmetic: 'wire [7:0] w;' -> max_len = 5+3 = 8.
# input (not output): space_max = 20+8+2 = 30 -> CalMargin(30, 0) = 31 spaces
# after 'input' (width ''); output: space_max = 29 -> 30 spaces.
APF_BUF = [
    "module small (",
    "    input clk,",
    "    input [7:0] din, // data in",
    "    output vld",
    ");",
    "wire [7:0] w;",
    "endmodule",
]


def test_apf_keeps_udt_when_name_glued_to_range():
    # pulp axi: first-pass output is `output axi_req_t [W-1:0]mst_o,`
    # (name glued to the range); formatting it again must not drop
    # the user-defined type word.
    out = auto_port_format(["output axi_req_t [NoMstPorts-1:0]mst_reqs_o,"])
    assert out[0].startswith("output axi_req_t"), out
    assert out[0].rstrip().endswith("mst_reqs_o,"), out


def test_apf_leaves_port_with_initializer_untouched():
    # hdmi: ``output logic [BIT_WIDTH-1:0] cx = START_X,`` — a port
    # default.  The last-token name extraction used to rebuild this as
    # a port named START_X, deleting cx and the initializer (and
    # colliding with the START_X parameter: Verilator duplicate).
    lines = [
        "    output logic [BIT_WIDTH-1:0] cx = START_X,",
        "    output logic [BIT_HEIGHT-1:0] cy = START_Y,",
        "    input logic clk,",
    ]
    out = auto_port_format(lines)
    assert out[:2] == lines[:2]
    assert "cx = START_X" in out[0] and "cy = START_Y" in out[1]


def test_apf_aligns_ports_and_preserves_terminator_and_comment():
    out = auto_port_format(APF_BUF)
    assert out == [
        "module small (",
        "input" + " " * 31 + "clk,",
        "input[7:0]" + " " * 26 + "din,// data in",  # CalMargin(30, 5) = 26
        "output" + " " * 30 + "vld",
        ");",
        "wire [7:0] w;",
        "endmodule",
    ]


def test_apf_semicolon_terminator_and_inout():
    out = auto_port_format(["inout pad; // pad ring", "wire w;"])
    # 'wire w;' gives max_len = 3 -> inout space_max = 25 -> 26 spaces
    assert out == ["inout" + " " * 26 + "pad;// pad ring", "wire w;"]


def test_apf_keeps_name_and_terminator_with_unpacked_dimensions():
    # ibex_core: ``input logic [TagSizeECC-1:0] ic_tag_rdata_i
    # [IC_NUM_WAYS],`` — the line ends in `]` so the old end-anchored
    # ``\w+`` name/terminator extraction found neither and APF emitted
    # a nameless, comma-less declaration (Verilator syntax error).
    out = auto_port_format([
        "    input  logic [7:0] plain_i,",
        "    input  logic [7:0] arr_i [2],",
        "    output logic [7:0] arr_o [2][3],",
        "    output logic [7:0] glued_o[4]",
    ])
    assert out[0].startswith("input logic [7:0]"), out
    assert out[0].rstrip().endswith("plain_i,"), out
    assert out[1].rstrip().endswith("arr_i [2],"), out
    assert out[2].rstrip().endswith("arr_o [2][3],"), out
    assert out[3].rstrip().endswith("glued_o[4]"), out


# ---------------------------------------------------------------------------
# auto_define_format (ADF)


def test_adf_type_dependent_column_floors():
    # all widths [3:0] (len 5): reg dominates -> max_len = 5+4 = 9
    out = auto_define_format(
        [
            "wire [3:0] a;",
            "reg [3:0] b; // flop",
            "integer k;",
            "genvar gv;",
        ]
    )
    assert out == [
        "wire[3:0]" + " " * 28 + "a;",  # space_max 20+9+3 = 32
        "reg[3:0]" + " " * 29 + "b; // flop",  # space_max 33
        "integer" + " " * 30 + "k;",  # space_max 20+9+0 = 29, width '' -> 30
        "genvar" + " " * 31 + "gv;",  # space_max 30, width '' -> 31
    ]


def test_adf_verilog_buffer_method():
    buf = VerilogBuffer(["wire tmp;"])
    # 'wire' itself contributes bonus 3 -> max_len 3 -> space_max 26 -> 27 spaces
    assert buf.auto_define_format().lines == ["wire" + " " * 27 + "tmp;"]


def test_adf_name_after_expression_width():
    # verilog-axi axi_interconnect.v: the name search must be anchored
    # after the packed range — an unanchored `(?:\s+|\]\s*)[A-Za-z]`
    # search matched `CL_S_COUNT-1` INSIDE the ternary width expression
    # and emitted it as the declared name, breaking the syntax.
    line = "wire [(CL_S_COUNT > 0? CL_S_COUNT-1: 0):0] s_select;"
    out = auto_define_format([line])
    assert len(out) == 1
    assert out[0].endswith("s_select;")
    assert "CL_S_COUNT-1:0):0]s_select" not in out[0]
    # multi-name declarations still keep every name
    out = auto_define_format(["wire [3:0] a, b;"])
    assert out[0].endswith("a,b;")


# ---------------------------------------------------------------------------
# auto_define_len


def test_auto_define_len():
    assert auto_define_len(["wire [7:0] a;", "reg b;", "integer k;"]) == 8
    assert auto_define_len(["module m;", "endmodule"]) == 0
    assert auto_define_len(["genvar g;"]) == 1


# ---------------------------------------------------------------------------
# all_format (AF): AutoPortFormat then AutoDefineFormat then AutoInstFormat


def test_all_format_runs_all_three():
    lines = SMALL_BODY.splitlines() + ["small u_s (/*autoinst*/ .clk(clk));"]
    out = all_format(lines)
    # APF ran: port names aligned; the reg/wire/logic keyword after the
    # direction is preserved (dropping 'reg' breaks 'output reg' ports)
    assert out[1] == "input wire" + " " * 18 + "clk,"
    assert out[2] == "input wire [7:0]" + " " * 12 + "din,"
    assert out[3] == "output wire" + " " * 17 + "vld"
    # AIF ran last: the one-liner instance was exploded and aligned
    assert out[6] == "small u_s (/*autoinst*/"
    assert out[7] == CLK_LAST
    assert out[8] == M + ");"


# ---------------------------------------------------------------------------
# CLI


def test_cli_format_subcommands(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("small u_s (/*autoinst*/ .clk(clk));\nwire tmp;\n")
    for cmd, check in (
        ("aif", lambda ls: ls[0] == "small u_s (/*autoinst*/" and ls[1] == CLK_LAST),
        ("adf", lambda ls: ls[-1] == "wire" + " " * 27 + "tmp;"),
        ("apf", lambda ls: ls == buf.read_text().splitlines()),
        ("af", lambda ls: ls[1] == CLK_LAST and ls[-1] == "wire" + " " * 27 + "tmp;"),
    ):
        out_file = tmp_path / f"{cmd}.v"
        main([cmd, "-i", str(buf), "-o", str(out_file)])
        assert check(out_file.read_text().splitlines()), cmd


# ---------------------------------------------------------------------------
# parameter/localparam declaration alignment


def test_param_decl_format_aligns_name_and_equals():
    lines = [
        "parameter AXSIZE = 3'b100 ;   // comment",
        "parameter IN_AXLEN_BITS = 4      ;",
        "localparam CMD_SPLIT_TYPE = (A==B) ? C : 1'b0;",
        "localparam DATA_WIDTH = 1<<AXSIZE ;",
    ]
    out = auto_param_decl_format(lines)
    # keyword padded to localparam's length: names share one column
    assert out[0] == "parameter   AXSIZE         = 3'b100 ;   // comment"
    assert out[1] == "parameter   IN_AXLEN_BITS  = 4      ;"
    assert out[2] == "localparam  CMD_SPLIT_TYPE = (A==B) ? C : 1'b0;"
    assert out[3] == "localparam  DATA_WIDTH     = 1<<AXSIZE ;"
    assert auto_param_decl_format(out) == out  # idempotent


def test_param_decl_format_comment_does_not_break_run_and_ranges():
    lines = [
        "parameter A = 1;",
        "// filler",
        "parameter [3:0] W = 8;",
        "wire x;",
        "parameter B = 2;",
    ]
    out = auto_param_decl_format(lines)
    # run has only `parameter`: keyword field is len("parameter")+2
    assert out[0] == "parameter  A       = 1;"
    assert out[2] == "parameter  [3:0] W = 8;"
    # a wire line breaks the run: B gets its own single-decl padding
    assert out[4] == "parameter  B = 2;"


def test_aif_param_inst_header_gets_own_line():
    """The combined `#(...)) inst (/*autoinst*/` last-param line: the pin is
    aligned, the `#(` close stays with it, and the instance header moves to
    its own line (never mangled into a fake connection)."""
    lines = [
        "sub #(/*autoinstparam*/",
        "        .W (W))   u_sub(/*autoinst*/",
        "        .din (din),",
        "        .dout (dout));",
    ]
    out = auto_inst_format(lines)
    assert out[1].endswith("))")
    assert out[2] == "u_sub(/*autoinst*/"


def test_aif_aligns_last_param_line_with_siblings():
    """The `#(...)) inst (/*autoinst*/` last-param line gets the same
    column alignment as the params above it; the header tail is verbatim."""
    lines = [
        "sub #(/*autoinstparam*/",
        "        .AXI_ADDR_WIDTH (AXI_ADDR_WIDTH),",
        "        .ITP_CRC_NUM (ITP_CRC_NUM))   u_sub(/*autoinst*/",
        "        .din (din),",
        "        .dout (dout));",
    ]
    out = auto_inst_format(lines)
    crc = next(l for l in out if "ITP_CRC_NUM" in l)
    axi = next(l for l in out if "AXI_ADDR_WIDTH" in l)
    # the pin part aligns with its sibling and closes the #(...); the
    # instance header follows on its own line
    assert crc.index("(ITP_CRC_NUM") == axi.index("(AXI_ADDR_WIDTH")
    assert crc.endswith("))")
    assert out[out.index(crc) + 1] == "u_sub(/*autoinst*/"
