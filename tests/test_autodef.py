"""automatic.vim AutoDefT (ADT) rewritten in Python (autodef.py)."""

import re

from verilog_tooling.autodef import (
    auto_def_t,
    get_all_defs,
    get_all_paras,
    get_all_signals,
    get_assign_side,
    group_link_dict,
    kill_auto_def_t,
)
from verilog_tooling.inst import parse_module_ports

SUB = """\
module sub (
    input  wire       clk,
    input  wire [7:0] din,
    output wire [7:0] dout
);
endmodule
"""


def sub_mods():
    return {"sub": parse_module_ports(SUB.splitlines())}


def decl(keyword: str, msb: str, name: str) -> str:
    """One generated declaration line, automatic.vim arithmetic:
    'wire '/'reg  ' padded by CalMargin(12, len) (8 spaces, so the field is
    13 wide), '[msb:0]' unless scalar, then CalMargin(max_len, len) with
    max_len starting at 39, before 'name;'."""
    line = keyword + " " * (12 - len(keyword) + 1)
    if msb:
        line += f"[{msb}:0]"
    return line + " " * (39 - len(line) + 1) + name + ";"


# ---------------------------------------------------------------------------
# kill_auto_def_t (KillAutoDefT)


def test_kill_auto_def_t_removes_region_keeps_marker():
    lines = """\
module m (
    input clk
);
/*autodef*/
// Define io wire here
wire                                     dummy;
// Define flip-flop registers here
// Define combination registers here
// Define wires here
// Define inst wires here
// Unresolved define signals here
// End of automatic define
assign x = 1'b0;
endmodule
""".splitlines()
    assert kill_auto_def_t(lines) == [
        "module m (",
        "    input clk",
        ");",
        "/*autodef*/",
        "assign x = 1'b0;",
        "endmodule",
    ]


# ---------------------------------------------------------------------------
# pre-pass helpers


def test_get_all_defs_and_paras():
    lines = """\
`define WIDTH 8
`ifdef DEBUG
module m;
parameter DEPTH = 16;
localparam IDLE = 2'b00;
/*autopara*/ (ST_A = 0, ST_B)
endmodule
`define LATE 1
""".splitlines()
    assert get_all_defs(lines) == {"WIDTH", "DEBUG"}
    assert get_all_paras(lines) == {"DEPTH", "IDLE", "ST_A", "ST_B"}


def test_get_all_signals_excludes_keywords_numbers_ports_and_paras():
    lines = """\
module m (
    input  wire [7:0] din,
    output wire       dout
);
parameter W = 8;
`define TOP 1
assign dout = din[0] == `TOP;
assign x = 8'hff;
assign p = u_sub.data;
endmodule
""".splitlines()
    sigs = get_all_signals(lines, get_all_defs(lines), get_all_paras(lines))
    assert {"din", "dout", "x"} <= sigs
    assert "W" not in sigs and "TOP" not in sigs and "hff" not in sigs
    assert "data" not in sigs  # .port token


def test_get_all_signals_skips_based_literals_system_tasks_and_sv_keywords():
    lines = """\
module m;
parameter X = 12'H0;
initial begin
    if (state == 2'h0 || state == 'H0 || state == 3'H3)
        $display("FATAL: PARAMETER set as %d , while expected", my_sig);
    $finish;
end
assign w = $clog2(X) + 1'B1 + 1'b0;
always @(*) begin
    while (cnt < X)
        cnt = cnt + 1;
end
endmodule
""".splitlines()
    sigs = get_all_signals(lines, get_all_defs(lines), get_all_paras(lines))
    # real signals stay (a system-task argument is still a real signal)
    assert {"my_sig", "state", "w", "cnt"} <= sigs
    for bad in (
        "H0", "H3", "h0", "B1",  # literal fragments (any-case base letter)
        "display", "finish", "clog2",  # system tasks/functions
        "FATAL", "PARAMETER", "as", "set", "while", "expected", "d",  # string text
    ):
        assert bad not in sigs


def test_autodef_unresolved_excludes_instance_module_names():
    lines = """\
module m (
    input  wire clk,
    output reg  r
);
parameter X = 12'H0;
/*autodef*/
always @(posedge clk) begin
    r <= real_sig;
    if (r == 2'h0)
        $display("done %d", r);
    else
        $finish;
end
sub #(.P(1))
         u_sub (/*autoinst*/
);
endmodule
""".splitlines()
    out = auto_def_t(lines, sub_mods())
    unresolved = {
        l.split(":", 1)[1].split("//")[0].strip()
        for l in out
        if l.startswith("// unresolved:")
    }
    # the instance module/instance names and literal/system-task fragments
    # are not signals; the genuinely undeclared read-only signal stays
    assert "real_sig" in unresolved
    for bad in ("sub", "u_sub", "X", "H0", "h0", "display", "finish", "d"):
        assert bad not in unresolved


# ---------------------------------------------------------------------------
# get_assign_side (s:GetAssignSide)


def test_get_assign_side_width_forms():
    assert get_assign_side("cnt ", " 8'h00;").width == "8"
    assert get_assign_side("w", " #1 4'd0;").width == "4"  # #delay stripped
    assert get_assign_side("w", " WIDTH'h0;").width == "WIDTH"
    assert get_assign_side("b", " a[3];").width == "1"
    assert get_assign_side("b", " a[15:8];").width == "8"
    # M < N: no width recorded at all (Vim leaves the branch empty)
    assert get_assign_side("b", " a[7:8];") is None
    assert get_assign_side("flag", " (a == b);").width == "1"
    assert get_assign_side("y", " x;").link == frozenset({"x"})
    assert get_assign_side("y", " ~x;").link == frozenset({"x"})
    assert get_assign_side("y", " a & b;").link == frozenset({"a", "b"})
    assert get_assign_side("y", " sel ? a : b;").link == frozenset({"a", "b"})
    assert get_assign_side("y", " a + 1;").link == frozenset()  # unrecognised
    # indexed LHS: the index evidences the packed msb (x[3] -> [3:0])
    side = get_assign_side("mem[3] ", " 8'h00;")
    assert side.name == "mem" and side.width == "4" and side.elem_range == "3:0"


def test_group_link_dict_transitive():
    groups = group_link_dict({"a": {"b"}, "b": {"c"}, "d": {"e"}})
    assert sorted(sorted(g) for g in groups.values()) == [["a", "b", "c"], ["d", "e"]]


# ---------------------------------------------------------------------------
# auto_def_t: sections and width inference


def test_freg_creg_wire_widths_and_format():
    lines = """\
module m (
    input        clk,
    input        rst_n,
    input  [7:0] din
);
/*autodef*/
always @(posedge clk) begin
    cnt <= 8'h00;
    hi  <= din[15:8];
end
always @(*) begin
    tmp = cnt & din;
    flag = (cnt == din);
end
assign w0 = cnt[0];
assign wsel = din[7:0];
endmodule
""".splitlines()
    out = auto_def_t(lines)
    # io section: every port without explicit wire/reg, in port (seq) order
    assert out[6:10] == [
        "// Define io wire here",
        decl("wire ", "", "clk"),
        decl("wire ", "", "rst_n"),
        decl("wire ", "7", "din"),
    ]
    assert out[10:23] == [
        "// Define flip-flop registers here",
        decl("reg  ", "7", "cnt"),
        decl("reg  ", "7", "hi"),
        "// Define combination registers here",
        decl("reg  ", "", "flag"),
        # 'tmp' links to 'din' via the '&' RHS and inherits its width
        decl("reg  ", "7", "tmp"),
        "// Define wires here",
        decl("wire ", "", "w0"),
        decl("wire ", "7", "wsel"),
        "// Define inst wires here",
        "// Define integer here",
        "// Unresolved define signals here",
        "// End of automatic define",
    ]
    assert out[23:] == lines[6:]


def test_io_wire_generation_seq_order_and_reg_promotion():
    lines = """\
module m (
    input  wire clk,
    input  [3:0] a,
    input        b,
    output       done
);
/*autodef*/
always @(posedge clk) begin
    done <= 1'b0;
end
endmodule
""".splitlines()
    out = auto_def_t(lines)
    assert out[7:12] == [
        "// Define io wire here",
        decl("wire ", "3", "a"),
        decl("wire ", "", "b"),
        decl("reg  ", "", "done"),
        "// Define flip-flop registers here",
    ]
    # the input wire with explicit 'wire' is NOT regenerated
    assert not any(line.rstrip().endswith("clk;") for line in out[6:11])
    # the port lines themselves stay verbatim
    assert out[:6] == lines[:6]


def test_undriven_output_declared_reg():
    """An output with no driver is declared reg (matching -a/AUTOREG): the
    user will drive it from an always block next.  assign-driven and
    instance-driven outputs plus inputs/inouts stay wire."""
    lines = """\
module m (
    input        clk,
    input  [3:0] a,
    output       done,
    output [7:0] q,
    output [3:0] sub_o,
    inout        pad
);
/*autodef*/
assign q = {2{a}};
sub u_sub (/*autoinst*/
    .clk (clk),
    .din (a),
    .dout (sub_o)
);
endmodule
""".splitlines()
    out = auto_def_t(lines, sub_mods())
    region = out[out.index("// Define io wire here") : out.index("// Define flip-flop registers here")]
    assert region == [
        "// Define io wire here",
        decl("wire ", "", "clk"),
        decl("wire ", "3", "a"),
        decl("reg  ", "", "done"),    # undriven output -> reg
        decl("wire ", "7", "q"),      # assign-driven output stays wire
        decl("wire ", "3", "sub_o"),  # instance-driven output stays wire
        decl("wire ", "", "pad"),     # inout stays wire
    ]
    # idempotent: the regenerated reg is re-derived, not duplicated
    assert auto_def_t(out, sub_mods()) == out


def test_link_propagation_and_unresolved_pruning():
    lines = """\
module m (
    input        clk,
    input  [7:0] din
);
/*autodef*/
assign x = din;
always @(posedge clk) begin
    y <= x;
end
assign z = y;
endmodule
""".splitlines()
    out = auto_def_t(lines)
    # ports clk/din have no explicit wire/reg: regenerated as io wires
    assert out[5:8] == [
        "// Define io wire here",
        decl("wire ", "", "clk"),
        decl("wire ", "7", "din"),
    ]
    assert out[8:18] == [
        "// Define flip-flop registers here",
        decl("reg  ", "7", "y"),
        "// Define combination registers here",
        "// Define wires here",
        decl("wire ", "7", "x"),
        decl("wire ", "7", "z"),
        "// Define inst wires here",
        "// Define integer here",
        "// Unresolved define signals here",
        "// End of automatic define",
    ]
    assert out[18:] == lines[5:]


def test_usrdef_lines_untouched():
    lines = """\
module m (
    input clk
);
/*autodef*/
reg  [3:0] user_cnt;
wire       user_w /* keep me */;
assign user_w = 1'b0;
endmodule
""".splitlines()
    out = auto_def_t(lines)
    assert "reg  [3:0] user_cnt;" in out
    assert "wire       user_w /* keep me */;" in out
    # usrdef signals are not re-emitted in any generated section
    region = out[out.index("// Define io wire here") : out.index("// End of automatic define")]
    assert not any("user_cnt" in line or "user_w" in line for line in region)


def test_inst_wire_from_submodule_width():
    lines = """\
module m (
    input clk,
    input [7:0] din
);
/*autodef*/
sub u_sub (/*autoinst*/
    .clk (clk),
    .din (din),
    .dout (dout_w)
);
endmodule
""".splitlines()
    out = auto_def_t(lines, sub_mods())
    region = out[out.index("// Define io wire here") : out.index("// End of automatic define")]
    assert region == [
        "// Define io wire here",
        decl("wire ", "", "clk"),
        decl("wire ", "7", "din"),
        "// Define flip-flop registers here",
        "// Define combination registers here",
        "// Define wires here",
        "// Define inst wires here",
        decl("wire ", "7", "dout_w"),
        "// Define integer here",
        "// Unresolved define signals here",
    ]


def test_unresolved_signals_emitted():
    lines = """\
module m (
    input clk
);
/*autodef*/
always @(posedge clk) begin
    q <= d_unresolved;
end
endmodule
""".splitlines()
    out = auto_def_t(lines)
    assert any(l.startswith("// unresolved: d_unresolved //") for l in out)
    region = out[out.index("// Define io wire here") : out.index("// End of automatic define")]
    assert any(l.startswith("// unresolved: d_unresolved //") for l in region)


def test_autodef_off_region_skipped():
    lines = """\
module m (
    input clk
);
/*autodef off*/
assign manual = 1'b0;
/*autodef on*/
/*autodef*/
assign auto_w = 1'b1;
endmodule
""".splitlines()
    out = auto_def_t(lines)
    assert "/*autodef off*/" in out and "/*autodef on*/" in out
    assert "assign manual = 1'b0;" in out  # off/on body copied verbatim
    # but the skipped assign is not turned into a declaration
    region = out[out.index("// Define io wire here") : out.index("// End of automatic define")]
    assert not any("manual" in line for line in region)
    assert decl("wire ", "", "auto_w") in out


def test_marker_uppercase_and_idempotent():
    lines = """\
module m (
    input clk
);
/*AUTODEF*/
assign w = 1'b0;
endmodule
""".splitlines()
    once = auto_def_t(lines)
    assert once[4] == "// Define io wire here"
    assert auto_def_t(once) == once


def test_negedge_block_is_freg():
    lines = """\
module m (
    input clk
);
/*autodef*/
always @(negedge clk) begin
    if (en) q <= #1 d;
    else q <= 1'b0;
end
endmodule
""".splitlines()
    out = auto_def_t(lines)
    region = out[out.index("// Define flip-flop registers here") :]
    assert region[1] == decl("reg  ", "", "q")
    assert any(l.startswith("// unresolved: d //") for l in out)


# ---------------------------------------------------------------------------
# multidim reg/wire declared inside for-loops (loop-var -> unpacked dim)


def _adt(text, mods=None):
    return auto_def_t(text.splitlines(), mods or {})


def test_for_loop_multidim_reg_declaration():
    text = """\
module top (
    input        clk,
    input  [7:0] din
);
/*autodef*/

always @(posedge clk) begin
    for (ch = 0; ch < 4; ch = ch+1) begin
        mem[ch][7:0] <= din;
        cnt[ch]      <= cnt[ch] + 1;
    end
end

always @(*) begin
    sel_out = mem[idx];
end

assign wire2d = mem[0][3:0];
endmodule
"""
    out = "\n".join(_adt(text))
    # 2D: packed elem [7:0] + unpacked dim from loop bound ch 0..3
    assert "reg          [7:0]                      mem [0:3];" in out
    # 1D unpacked reg (scalar element)
    assert "reg                                     cnt [0:3];" in out
    # multidim RHS slice drives wire width
    assert "wire         [3:0]                      wire2d;" in out
    # loop variable is not a signal, base names not left unresolved
    assert "unresolved ch;" not in out
    assert "unresolved mem;" not in out
    assert "unresolved cnt;" not in out


def test_nested_for_loop_two_dims():
    text = """\
module top (input clk);
/*autodef*/
always @(posedge clk) begin
    for (r = 0; r < 2; r = r+1) begin
        for (c = 0; c <= 7; c = c+1) begin
            grid[r][c][3:0] <= 4'h0;
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg          [3:0]                      grid [0:1] [0:7];" in out
    assert "unresolved r;" not in out
    assert "unresolved c;" not in out


def test_loop_var_with_variable_bound_degrades_to_scalar():
    text = """\
module top (input clk, input [3:0] n);
/*autodef*/
always @(posedge clk) begin
    for (i = 0; i < n; i = i+1) begin
        arr[i][7:0] <= 8'h0;
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    # bound 'n' is not constant: the loop var cannot be resolved to a range,
    # so the array is declared with its packed width but no unpacked dim
    assert "reg          [7:0]                      arr;" in out
    assert "unresolved i;" not in out  # loop var still excluded


def test_multidim_adt_idempotent():
    text = """\
module top (input clk, input [7:0] din);
/*autodef*/
always @(posedge clk) begin
    for (ch = 0; ch < 4; ch = ch+1) begin
        mem[ch][7:0] <= din;
    end
end
endmodule
"""
    once = _adt(text)
    # the unpacked-array declaration is preserved verbatim (not
    # re-extractable); it relocates once behind the new region, then the
    # result is a stable fixpoint
    twice = _adt("\n".join(once))
    assert _adt("\n".join(twice)) == twice
    assert sum(1 for l in twice if re.search(r"reg\s+\[7:0\]\s+mem\s+\[0:3\];", l)) == 1


# ---------------------------------------------------------------------------
# loop bounds from parameter / `define / constant expressions


def test_parameter_bound_loop():
    text = """\
module top #(parameter DEPTH = 8) (input clk, input [7:0] din);
/*autodef*/
always @(posedge clk) begin
    for (i = 0; i < DEPTH; i = i+1) begin
        preg[i][7:0] <= din;
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg          [7:0]                      preg [0:DEPTH-1];" in out
    assert "unresolved i;" not in out
    assert "unresolved DEPTH;" not in out


def test_define_bound_loop():
    text = """\
`define LANES 4
module top (input clk, input [7:0] din);
/*autodef*/
always @(posedge clk) begin
    for (d = 0; d < `LANES; d = d+1) begin
        darr[d][7:0] <= din;
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg          [7:0]                      darr [0:`LANES-1];" in out


def test_param_expression_bound():
    text = """\
module top #(parameter DEPTH = 16) (input clk, input [7:0] din);
/*autodef*/
always @(*) begin
    for (k = 0; k < DEPTH/2; k = k+1) begin
        half[k] = din[k];
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg                                     half [0:DEPTH/2-1];" in out


def test_genvar_loop_wire():
    text = """\
module top (input [7:0] din);
/*autodef*/
genvar gv;
generate
for (gv = 0; gv < 2; gv = gv+1) begin : g_blk
    assign gwire[gv] = din[gv];
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire                                    gwire [0:1];" in out
    assert "unresolved gv;" not in out


# ---------------------------------------------------------------------------
# bit-select expressions over loop variables (j*2 style) + invalid-LHS guard


def test_loop_bit_select_expression_width():
    text = """\
module a(input clk);
/*autodef*/
parameter NUM0=12;
always@(*) begin
    for(i=0;i<10;i=i+1) begin
        for(j=0;j<NUM0;j=j+1) begin
            val[i][j*2]   = 1'b0;
            val[i][j*2+1] = 1'b1;
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    # i: 0..9 unpacked (literal); j*2+1 with j:[0:NUM0-1] -> symbolic msb
    assert "reg          [2*NUM0-1:0]               val [0:9];" in out
    assert "unresolved val;" not in out
    assert "unresolved i;" not in out and "unresolved j;" not in out


def test_invalid_lhs_does_not_emit_bogus_reg():
    # `val[i][j*2]+1 = ...` is not a legal lvalue; it must not declare "1"
    text = """\
module a(input clk);
/*autodef*/
always@(*) begin
    for(i=0;i<4;i=i+1) begin
        arr[i]   = 1'b0;
        arr[i]+1 = 1'b1;
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg                                     1;" not in out
    assert "arr" in out  # the valid write still declares arr


def test_loop_bit_select_multi_variable_symbolic():
    text = """\
module b(input clk);
/*autodef*/
parameter NUM0=12;
parameter NUM1=12;
always@(*) begin
    for(i=0;i<10;i=i+1) begin
        for(j=0;j<NUM0;j=j+1) begin
            for(k=0;k<NUM1;k=k+1) begin
                val[i][j*2+k][k]   = 1'b0;
                val[i][j*2+k+1][k] = 1'b1;
            end
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    # j:[0:NUM0-1], k:[0:NUM1-1]; max of j*2+k+1 = 2*(NUM0-1)+(NUM1-1)+1
    assert "reg          [2*NUM0+NUM1-2:0]          val [0:9] [0:NUM1-1];" in out
    assert "unresolved val;" not in out
    for v in ("i", "j", "k"):
        assert f"unresolved {v};" not in out


# ---------------------------------------------------------------------------
# marker-less instances inside generate-for + indexed part-selects

LFSR = """\
module isp_dither_lfsr (clk, sr_seed_out, dat);
    input clk;
    output [31:0] sr_seed_out;
    output [7:0] dat;
endmodule
"""


def lfsr_mods():
    return {"isp_dither_lfsr": parse_module_ports(LFSR.splitlines())}


def _inst_section(out: str) -> list[str]:
    region = out[out.index("// Define inst wires here") : out.index("// Define integer here")]
    return region.splitlines()


def _inst_decl(out: str, name: str) -> str | None:
    """The generated inst-wire declaration line for NAME, or None."""
    for line in _inst_section(out):
        if line.rstrip().endswith(f"{name};"):
            return line
    return None


def test_generate_for_instance_without_marker():
    text = """\
module top(input clk);
/*autodef*/
parameter DITHER_NUM=4;
genvar gv_i;
generate
for ( gv_i = 0; gv_i < DITHER_NUM; gv_i = gv_i + 1 ) begin : gen_x
    isp_dither_lfsr u_lfsr(
        .clk (clk),
        .sr_seed_out   (sr_seed_out[32*gv_i+:32]),
        .dat           (dat_lfsr[8*gv_i+:8])
);
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text, lfsr_mods()))
    # both output nets declared in the inst-wire section, msb symbolic
    assert "[32*DITHER_NUM-1:0]" in _inst_decl(out, "sr_seed_out")
    assert "[8*DITHER_NUM-1:0]" in _inst_decl(out, "dat_lfsr")
    assert _inst_decl(out, "sr_seed_out") in _inst_section(out)
    assert _inst_decl(out, "dat_lfsr") in _inst_section(out)
    # structure tokens are not signals
    for tok in ("gv_i", "gen_x", "u_lfsr", "isp_dither_lfsr"):
        assert f"unresolved {tok};" not in out
    # idempotent
    assert _adt("\n".join(_adt(text, lfsr_mods())), lfsr_mods()) == _adt(text, lfsr_mods())


def _blk_mods():
    blk = """\
module blk (
    input  wire [7:0] din,
    output wire [7:0] dout,
    output wire [3:0] q4,
    inout  wire [7:0] io
);
endmodule
"""
    return {"blk": parse_module_ports(blk.splitlines())}


def test_partselect_numeric_folds_to_int():
    text = """\
module top(input clk);
/*autodef*/
genvar g;
generate
for (g = 0; g < 3; g = g + 1) begin : gen_b
    blk u_b(
        .din  (din_bus[8*g+:8]),
        .dout (dout_bus[8*g+:8])
    );
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text, _blk_mods()))
    # max index of 8*g over g:[0:2] is 16 -> width 16+8 = 24
    assert "[23:0]" in _inst_decl(out, "dout_bus")
    # din is an INPUT of blk: no inst_wire for its net
    assert _inst_decl(out, "din_bus") is None


def test_partselect_downselect_variant():
    text = """\
module top(input clk);
/*autodef*/
genvar g;
generate
for (g = 0; g < 4; g = g + 1) begin : gen_b
    blk u_b(
        .q4   (q_bus[4*g-:4]),
        .io   (io_bus[8*g-:8])
    );
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text, _blk_mods()))
    # -: width is max(EXPR)+1: 4*3+1 = 13 ; 8*3+1 = 25
    assert "[12:0]" in _inst_decl(out, "q_bus")
    assert "[24:0]" in _inst_decl(out, "io_bus")  # inout drives a decl too


def test_partselect_on_input_port_creates_no_inst_wire():
    text = """\
module top(input clk);
/*autodef*/
genvar g;
generate
for (g = 0; g < 2; g = g + 1) begin : gen_b
    blk u_b(
        .din (din_bus[8*g+:8])
    );
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text, _blk_mods()))
    assert _inst_decl(out, "din_bus") is None
    assert _inst_section(out) == ["// Define inst wires here"]


def test_partselect_port_width_when_omitted():
    # no loop var: a constant index with the port's own width
    text = """\
module top(input clk);
/*autodef*/
blk u_b(
    .din  (din_bus[0+:8]),
    .dout (dout_bus[8+:8])
);
endmodule
"""
    out = "\n".join(_adt(text, _blk_mods()))
    # constant index 8 with port width 8 -> msb 15
    assert "[15:0]" in _inst_decl(out, "dout_bus")


def test_usrdef_reg_no_space_before_bracket():
    # reg[W-1:0] name;  (no space between reg and [) must be recognised as a
    # user declaration, not dropped into unresolved.
    text = """\
module top (input clk);
/*autodef*/
reg[8*THROUGHPUT*2-1:0]                             obits_buf;
reg[1:0]                                            obits_cnt;
parameter THROUGHPUT=8;
always @(posedge clk) begin
    obits_buf[8*THROUGHPUT*0+:8*THROUGHPUT] <= 'h0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "unresolved obits_buf;" not in out
    assert "unresolved obits_cnt;" not in out
    # user declarations stay verbatim (not regenerated)
    assert "reg[8*THROUGHPUT*2-1:0]" in out


def test_usrdef_wire_no_space():
    text = """\
module top (input clk);
/*autodef*/
wire[3:0]a;
reg   [7:0]b;
assign c = a[0];
endmodule
"""
    out = "\n".join(_adt(text))
    assert "unresolved a;" not in out
    assert "unresolved b;" not in out


# ---------------------------------------------------------------------------
# concatenation LHS ({a, b[..], ...} = ...), single- and multi-line


def test_concat_lhs_single_line_multidim_members():
    text = """\
module a(input clk);
/*autodef*/
parameter NUM0=12;
parameter NUM1=12;
always@(*) begin
    for(i=0;i<10;i=i+1) begin
        for(j=0;j<NUM0;j=j+1) begin
            for(k=0;k<NUM1;k=k+1) begin
                {val[i][j*2+k][k], val1[i][j*2+k][k]} = 2'b0;
            end
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    # both members classified independently: symbolic packed width + 2 dims
    assert "reg          [2*NUM0+NUM1-3:0]          val [0:9] [0:NUM1-1];" in out
    assert "reg          [2*NUM0+NUM1-3:0]          val1 [0:9] [0:NUM1-1];" in out
    assert "unresolved val;" not in out and "unresolved val1;" not in out
    for v in ("i", "j", "k"):
        assert f"unresolved {v};" not in out


def test_concat_lhs_multi_line():
    text = """\
module a(input clk);
/*autodef*/
parameter NUM0=12;
parameter NUM1=12;
always@(*) begin
    for(i=0;i<10;i=i+1) begin
        for(j=0;j<NUM0;j=j+1) begin
            for(k=0;k<NUM1;k=k+1) begin
                {val[i][j*2+k][k], val1[i][j*2+k][k]} = 2'b0;
                {val2[i][j*2+k][k],                                 // c
                    val3[i][j*2+k][k]} = 2'b0;
                val[i][j*2+k+1][k] = 1'b1;
            end
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    # val merges the later j*2+k+1 write; the others see j*2+k only
    assert "reg          [2*NUM0+NUM1-2:0]          val [0:9] [0:NUM1-1];" in out
    for name in ("val1", "val2", "val3"):
        assert f"reg          [2*NUM0+NUM1-3:0]          {name} [0:9] [0:NUM1-1];" in out
        assert f"unresolved {name};" not in out


def test_concat_lhs_mixed_scalar_and_slice_members():
    text = """\
module m (
    input clk
);
/*autodef*/
always @(posedge clk) begin
    {a[3:0], b[7:0]} <= 12'h0;
    {c, d[2:0]} <= 4'b0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    # each sliced member keeps its own select width, never the RHS total
    assert "reg          [3:0]                      a;" in out
    assert "reg          [7:0]                      b;" in out
    assert "reg                                     c;" in out  # scalar member
    assert "reg          [2:0]                      d;" in out
    assert not any("[11:0]" in l and (" a;" in l or " b;" in l) for l in out.splitlines())


def test_concat_lhs_nested_braces():
    text = """\
module m(input clk);
/*autodef*/
always @(*) begin
    {p, {q, r[1:0]}} = 4'h0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg                                     p;" in out
    assert "reg                                     q;" in out
    assert "reg          [1:0]                      r;" in out
    for name in ("p", "q", "r"):
        assert f"unresolved {name};" not in out


def test_concat_lhs_in_assign_is_wire():
    text = """\
module m(input [7:0] din);
/*autodef*/
assign {w1, w2} = {din[7:4], din[3:0]};
assign {w3,
        w4[3:0]} = 5'h0;
endmodule
"""
    out = "\n".join(_adt(text))
    region = out[out.index("// Define wires here") : out.index("// Define inst wires here")]
    # RHS concat elements are not sliced-linked (nice-to-have): w1/w2 stay
    # scalar but are still typed as wires
    assert "wire                                    w1;" in region
    assert "wire                                    w2;" in region
    # multi-line assign concat: scalar member + sliced member
    assert "wire                                    w3;" in region
    assert "wire         [3:0]                      w4;" in region
    # wire section, not combination registers
    comb = out[out.index("// Define combination registers here") : out.index("// Define wires here")]
    assert not any(name in l for name in ("w1", "w2", "w3", "w4") for l in comb)


def test_concat_lhs_rhs_concat_member_link():
    text = """\
module m (
    input clk,
    input [7:0] din
);
/*autodef*/
always @(*) begin
    {p, q} = {din, 1'b0};
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg          [7:0]                      p;" in out  # linked to din
    assert "reg                                     q;" in out  # scalar literal


def test_concat_lhs_invalid_and_plain_still_guarded():
    text = """\
module a(input clk);
/*autodef*/
always@(*) begin
    for(i=0;i<4;i=i+1) begin
        arr[i]   = 1'b0;
        arr[i]+1 = 1'b1;
    end
    {ok[1:0], fine} <= 3'b0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg                                     1;" not in out  # val[i]+1 rejected
    assert "reg                                     arr [0:3];" in out  # plain LHS intact
    # '<=' concat inside a combinational block is ignored (only '=' lines)
    assert "// unresolved: ok //" in out and "// unresolved: fine //" in out


# ---------------------------------------------------------------------------
# legal-Verilog formatting robustness (multi-line always headers, inline
# block comments, assign spacing) — B5-B9


def test_usrdef_decl_with_block_comment_between_keyword_and_range():
    text = """\
module m (
    input clk
);
/*autodef*/
wire /* comment */ [3:0] a;
always @(*) begin
    b = a[0];
end
endmodule
"""
    out = "\n".join(_adt(text))
    # the /* ... */ between keyword and range must not hide the declaration
    assert "unresolved a;" not in out
    # usrdef lines stay verbatim on output (comment intact) and `a` is
    # treated as user-defined, not unresolved
    assert "wire /* comment */ [3:0] a;" in out


def test_always_multiline_header_is_freg():
    text = """\
module m (
    input clk
);
/*autodef*/
always @(
    posedge clk
) begin
    q <= 1'b0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert decl("reg  ", "", "q") in out
    assert "unresolved q;" not in out


def test_always_multiline_header_negedge_is_freg():
    text = """\
module m (
    input clk
);
/*autodef*/
always @(
    negedge clk
) begin
    q <= 1'b0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert decl("reg  ", "", "q") in out
    assert "unresolved q;" not in out


def test_always_body_multiline_block_comment_does_not_hide_assignment():
    text = """\
module m (
    input clk
);
/*autodef*/
always @(posedge clk) begin
    q <= 1'b0;
    /* multi
       line
       comment */
    r <= 1'b1;
end
endmodule
"""
    out = "\n".join(_adt(text))
    # the /* ... */ block comment lines are skipped, both <= captured as freg
    assert decl("reg  ", "", "q") in out
    assert decl("reg  ", "", "r") in out
    assert "unresolved q;" not in out and "unresolved r;" not in out


def test_assign_with_extra_spaces_and_ampersand_link():
    text = """\
module m (
    input        clk,
    input  [3:0] x,
    input  [3:0] y
);
/*autodef*/
assign w = x & y ;
assign   w2   =   x   ;
assign z=x;
endmodule
"""
    out = "\n".join(_adt(text))
    # w links to x & y and inherits their width; extra padding must parse
    assert decl("wire ", "3", "w") in out
    assert decl("wire ", "3", "w2") in out
    assert decl("wire ", "3", "z") in out
    assert "unresolved w;" not in out
    assert "unresolved w2;" not in out
    assert "unresolved z;" not in out


# ---------------------------------------------------------------------------
# hardening variants: signed decls, nameless header lines, join guards


def test_signed_port_and_usrdef_wire_are_recognised():
    text = """\
module m (
    input clk,
    input signed [7:0] s
);
/*autodef*/
wire signed [3:0] a;
always @(*) begin
    b = a[0];
end
endmodule
"""
    out = "\n".join(_adt(text))
    # signed port keeps its width AND its signedness; 'signed' is never an
    # unresolved signal
    assert "signed [7:0]" in out and " s;" in out
    assert "unresolved signed;" not in out
    # the signed usrdef wire registers (not unresolved) and stays verbatim
    assert "wire signed [3:0] a;" in out
    assert "unresolved a;" not in out


def test_nameless_header_port_line_does_not_crash_or_eat_next_decl():
    # an ANSI header line 'input [7:0]' whose name never comes (the next
    # line closes the header): the join must stop at ');' and the nameless
    # line must be skipped, not crash extend_io_from_line
    text = """\
module m (
    input [7:0]
);
/*autodef*/
wire [3:0] a;
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire [3:0] a;" in out  # usrdef line untouched, no exception
    assert "unresolved a;" not in out


def test_nameless_header_port_line_before_always_block():
    # same guard when the following statement is an always block
    text = """\
module m (
    input [7:0]
);
/*autodef*/
always @(*) begin
    b = 1'b0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert decl("reg  ", "", "b") in out
    assert "unresolved b;" not in out


def test_decl_line_with_trailing_comma_does_not_eat_next_statement():
    # a complete decl line that already names a signal and ends in ',' must
    # not join the following statement (the join guard only fires while the
    # statement has no name yet)
    text = """\
module m (
    input clk
);
/*autodef*/
wire [3:0] a,
     [3:0] b;
wire c;
endmodule
"""
    out = "\n".join(_adt(text))
    # 'a' registers as usrdef; the 'wire c;' line was not consumed into it
    assert "wire c;" in out
    assert "unresolved c;" not in out


def test_link_no_width_source_defaults_to_scalar():
    # assign w = x & y; with x,y undeclared: no width source, w is still
    # declared as a 1-bit wire (per "no width written => 1 bit").
    text = """\
module t(input clk);
/*autodef*/
assign w = x & y ;
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire                                    w;" in out
    assert "unresolved w;" not in out


def test_ansi_single_line_header_io_wire():
    text = """\
module t(input clk, input [3:0] d, output [3:0] q);
/*autodef*/
assign q = d;
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire                                    clk;" in out
    assert "wire         [3:0]                      d;" in out
    assert "wire         [3:0]                      q;" in out


# ---------------------------------------------------------------------------
# stale usrdef width updated in place; //DT exempts


def test_stale_usrdef_width_updated_from_driver():
    text = """\
module top(input clk, input [7:0] din);
/*autodef*/
wire [3:0] stale_w;
reg  [1:0] stale_r;
assign stale_w = din;
always @(posedge clk) begin
    stale_r <= din;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire [7:0] stale_w;" in out
    assert "reg  [7:0] stale_r;" in out
    assert "wire [3:0] stale_w;" not in out
    assert "reg  [1:0] stale_r;" not in out


def test_usrdef_dt_comment_exempts_update():
    text = """\
module top(input clk, input [7:0] din);
/*autodef*/
wire [3:0] keep_w;         //DT
assign keep_w = din;
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire [3:0] keep_w;         //DT" in out
    assert "wire [7:0] keep_w;" not in out


def test_usrdef_width_update_preserves_headroom_and_symbolic():
    text = """\
module top(input clk, input [7:0] din);
/*autodef*/
wire [15:0] headroom_w;
wire [W-1:0] sym_w;
parameter W=4;
assign headroom_w = din[7:0];
assign sym_w = din;
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire [15:0] headroom_w;" in out  # headroom kept (not shrunk)
    assert "wire [W-1:0] sym_w;" in out      # symbolic width untouched


def test_scalar_usrdef_gains_vector_width():
    text = """\
module top(input clk, input [7:0] din);
/*autodef*/
wire scalar_w;
assign scalar_w = din;
endmodule
"""
    out = "\n".join(_adt(text))
    assert "wire [7:0] scalar_w;" in out


def test_usrdef_decl_with_initializer_is_not_rewritten():
    # verilog-ethernet eth_mac_10g.v: `reg r = 1'b0;` declarations whose
    # linked group grows to [7:0] were rewritten by _rewrite_usrdef_range
    # into phantom declarations of a signal named `b0` (the based literal's
    # text after `1'`), destroying the user's declaration and duplicating
    # `b0` once per such line.  A declaration carrying an initializer is
    # not a "simple decl" and must stay verbatim.
    text = """\
module top (
    output wire [7:0] o_sig,
    input wire clk
);
    reg r_sig = 1'b0;
    always @(posedge clk) begin
            r_sig <= i_sig;
    end
    assign i_sig = 0;
    assign o_sig = 0;
    /*autodef*/
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg r_sig = 1'b0;" in out
    assert "reg [7:0] b0;" not in out
    assert " b0;" not in out


def test_const_index_between_loop_indices_becomes_dim():
    # val3[i][3][j*2+k][k]: the constant [3] sits between loop indices, so it
    # is an unpacked dim [0:3]; a trailing constant [3] alone stays a bit-select.
    text = """\
module c(input clk);
/*autodef*/
parameter NUM0=12;
parameter NUM1=12;
always@(*) begin
    for(i=0;i<10;i=i+1) begin
        for(j=0;j<NUM0;j=j+1) begin
            for(k=0;k<NUM1;k=k+1) begin
                val3[i][3][j*2+k][k] = 1'b0;
            end
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg          [2*NUM0+NUM1-3:0]          val3 [0:9] [0:3] [0:NUM1-1];" in out


# ---------------------------------------------------------------------------
# for-loop variable declarations (integer / genvar)


def test_always_for_loop_vars_declared_integer():
    text = """\
module top (input clk, input [7:0] din);
/*autodef*/
always @(posedge clk) begin
    for (i = 0; i < 4; i = i+1) begin
        for (j = 0; j < 4; j = j+1) begin
            mem[i][j] <= din;
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    # nested loop vars each get their own aligned integer declaration
    assert decl("integer", "", "i") in out
    assert decl("integer", "", "j") in out
    # in the new section, after the wires
    section = out[out.index("// Define integer here") : out.index("// Unresolved define signals here")]
    assert decl("integer", "", "i") in section
    assert decl("integer", "", "j") in section
    # loop vars are not signals / not unresolved; mem is declared as before
    assert "unresolved i;" not in out
    assert "unresolved j;" not in out
    assert "mem [0:3] [0:3];" in out


def test_generate_for_loop_var_declared_genvar():
    text = """\
module top (input [7:0] din);
/*autodef*/
generate
for (gv = 0; gv < 8; gv = gv+1) begin : g_w
    assign w[gv] = din[gv];
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text))
    assert out.count("genvar gv;") == 1
    assert "unresolved gv;" not in out
    assert "unresolved g_w;" not in out
    assert "w [0:7];" in out


def test_hand_written_integer_not_duplicated():
    text = """\
module top (input clk);
/*autodef*/
integer i;
always @(posedge clk) begin
    for (i = 0; i < 4; i = i+1) begin
        cnt[i] <= 1'b0;
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    # the hand-written declaration stays; nothing is emitted in the section
    assert out.count("integer i;") == 1
    section = out[out.index("// Define integer here") : out.index("// Unresolved define signals here")]
    assert "i;" not in section


def test_hand_written_integer_dt_kept_verbatim():
    text = """\
module top (input clk);
/*autodef*/
integer i; //DT
always @(posedge clk) begin
    for (i = 0; i < 4; i = i+1) begin
        cnt[i] <= 1'b0;
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "integer i; //DT" in out
    # exactly one line declares integer i (no generated duplicate)
    assert sum(1 for l in out.splitlines() if re.match(r"\s*integer\s+i\s*;", l)) == 1


def test_hand_written_multi_name_integer_not_duplicated():
    text = """\
module top (input clk);
/*autodef*/
integer i, j;
always @(posedge clk) begin
    for (i = 0; i < 2; i = i+1) begin
        for (j = 0; j < 2; j = j+1) begin
            m2[i][j] <= 1'b0;
        end
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "integer i, j;" in out
    section = out[out.index("// Define integer here") : out.index("// Unresolved define signals here")]
    assert "i;" not in section and "j;" not in section


def test_hand_written_genvar_not_duplicated():
    text = """\
module top (input [7:0] din);
/*autodef*/
genvar gv;
generate
for (gv = 0; gv < 2; gv = gv+1) begin : g_blk
    assign gwire[gv] = din[gv];
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text))
    assert out.count("genvar gv;") == 1
    section = out[out.index("// Define integer here") : out.index("// Unresolved define signals here")]
    assert "gv;" not in section


def test_loop_var_reused_in_two_loops_declared_once():
    text = """\
module top (input clk);
/*autodef*/
always @(posedge clk) begin
    for (i = 0; i < 2; i = i+1) begin
        a[i] <= 1'b0;
    end
    for (i = 0; i < 4; i = i+1) begin
        b[i] <= 1'b1;
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert out.count(decl("integer", "", "i")) == 1


def test_loop_var_decls_idempotent():
    text = """\
module top (input clk, input [7:0] din);
/*autodef*/
always @(posedge clk) begin
    for (i = 0; i < 4; i = i+1) begin
        mem[i] <= din;
    end
end
generate
for (gv = 0; gv < 8; gv = gv+1) begin : g_w
    assign w[gv] = din[gv];
end
endgenerate
endmodule
"""
    once = _adt(text)
    # preserved unpacked-array declaration relocates once, then fixpoint
    twice = _adt("\n".join(once))
    assert _adt("\n".join(twice)) == twice


# ---------------------------------------------------------------------------
# regression: mm_common batch findings


def test_symbolic_index_lhs_recovers_packed_msb():
    """x[PIPELINE_LENGTH-1] = ... evidences a packed vector even when the
    hand-written declaration was dropped; a loop-var select x[i] must not
    turn it into an unpacked array."""
    text = """\
module top (input clk);
parameter PIPELINE_LENGTH = 4;
/*autodef*/
always @(*) begin
    pip_trans_able[PIPELINE_LENGTH-1] = last_pip_rdy;
    for (i = 0; i < PIPELINE_LENGTH-1; i = i + 1) begin
        pip_trans_able[i] = pip_trans_able[i+1];
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    m = re.search(r"creg.*?\n((?:.*\n)*?)// Define", out)
    assert re.search(r"reg\s+\[PIPELINE_LENGTH-1:0\]\s+pip_trans_able;", out)
    assert "pip_trans_able [" not in out  # not an unpacked array


def test_scalar_select_side_never_widens_packed_width():
    """A scalar element side ('c0') from x[i] must not overwrite the packed
    symbolic width coming from x[PIPELINE_LENGTH-1:0]."""
    text = """\
module top (input clk);
parameter PIPELINE_LENGTH = 4;
/*autodef*/
always @(posedge clk) begin
    pip_ctrl_r[PIPELINE_LENGTH-1:0] <= 'h0;
    for (i = 0; i < PIPELINE_LENGTH; i = i + 1) begin
        pip_ctrl_r[i] <= pip_ctrl_r[i-1];
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert re.search(r"reg\s+\[PIPELINE_LENGTH-1:0\]\s+pip_ctrl_r;", out)
    assert "pip_ctrl_r [" not in out


def test_kill_autodef_end_marker_irregular_spacing():
    """Old regions end with ``// End of automatic     define`` (extra
    spaces); the kill must stop there, not eat the rest of the file."""
    text = """\
module top (input clk);
/*autodef*/
// Define flip-flop registers here
reg[1:0]     cur_sta;    //
// End of automatic     define
always @(posedge clk) begin
    cur_sta <= 'h0;
end
endmodule
"""
    out = _adt(text)
    assert any("always @(posedge clk)" in line for line in out)  # body survives
    # cur_sta is still declared (width comes from the driver, here scalar)
    assert any(re.search(r"reg\s+cur_sta;", line) for line in out)


def test_inst_wire_nonlocal_symbolic_width_is_unresolved():
    """An inst_wire whose port width uses the SUBMODULE's parameter names
    (not visible here) must be flagged unresolved, not declared broken."""
    SUB_P = """\
module subp (
    input  wire                         clk,
    output wire [SUBW-1:0]              dout
);
endmodule
"""
    mods = {"subp": parse_module_ports(SUB_P.splitlines())}
    text = """\
module top;
/*autodef*/
subp u_subp (/*autoinst*/
    .clk (clk),
    .dout (dout_w[SUBW-1:0])
);
endmodule
"""
    out = "\n".join(auto_def_t(text.splitlines(), mods))
    assert "wire [SUBW-1:0]" not in out
    assert "// unresolved: dout_w //" in out


def test_multi_packed_dim_declaration_preserved():
    """reg [A:0][B:0] mem; cannot be re-extracted from assignments (packed
    vs unpacked is undecidable), so kill keeps it verbatim."""
    text = """\
module top (input clk);
parameter A = 2;
parameter B = 4;
/*autodef*/
// Define flip-flop registers here
reg[A-1:0][B-1:0]     mem;    //
// End of automatic     define
always @(posedge clk) begin
    mem[0][addr_w] <= 'h0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "reg[A-1:0][B-1:0]     mem;    //" in out  # verbatim, not garbage
    assert "[addr_w:0]" not in out


def test_unclassifiable_multi_index_lhs_no_fabricated_width():
    """mem[0][addr_w] must not produce a fabricated [addr_w:0] width or a
    [0:0] unpacked dim."""
    text = """\
module top (input clk);
/*autodef*/
always @(posedge clk) begin
    mem[0][addr_w] <= 'h0;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert "[addr_w:0]" not in out
    assert "mem [0:0]" not in out


def test_usrdef_unpacked_array_declaration_registered():
    """wire [1:0] name [DIM-1:0]; (packed range + unpacked dim) must register
    NAME as declared — a greedy range regex used to swallow the whole line
    and lose the name, causing duplicate declarations downstream."""
    from verilog_tooling.autodef import SignalTable
    from verilog_tooling.wire import auto_wire
    from verilog_tooling.inst import parse_module_ports

    tbl = SignalTable()
    tbl.extend_usrdef_from_line("wire[1:0]     tm_depth[R_SLAVE_NUM-1:0];", 0)
    assert "tm_depth" in tbl.signals
    assert tbl.signals["tm_depth"].width == "1"

    tbl2 = SignalTable()
    tbl2.extend_usrdef_from_line("reg [A-1:0][B-1:0]     mem;", 0)
    assert "mem" in tbl2.signals
    assert tbl2.signals["mem"].width == "A-1"


def test_declaration_between_always_blocks_not_swallowed():
    """A reg declared between two always blocks must register as usrdef —
    otherwise the always scan eats it and the region re-declares it (dup)."""
    text = """\
module top (input clk);
/*autodef*/
always @(posedge clk) begin
    a <= 'h0;
end
reg[63:0]     cnt;    //
always @(posedge clk) begin
    cnt[63:0] <= cnt[63:0] + 1;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert out.count("fifo") == 0
    decls = [l for l in out.splitlines() if re.search(r"reg.*\bcnt\s*;", l)]
    assert len(decls) == 1  # declared once, verbatim


def test_genvar_only_for_pure_generate_for():
    """A for loop inside an always block gets integer even when the always
    sits inside a generate region; only a pure generate-for gets genvar."""
    text = """\
module top (input clk);
parameter N = 4;
/*autodef*/
generate
if (1) begin: g1
    always @(*) begin
        for (i = 0; i < N; i = i + 1) begin
            a[i] = b[i];
        end
    end
end
endgenerate
generate
for (k = 0; k < N; k = k + 1) begin: g2
    always @(posedge clk) c[k] <= d[k];
end
endgenerate
endmodule
"""
    out = "\n".join(_adt(text))
    assert re.search(r"integer\s+i\s*;", out)
    assert re.search(r"genvar\s+k\s*;", out)
    assert not re.search(r"genvar\s+i\s*;", out)


def test_function_local_loop_var_not_declared():
    """A loop inside a function body must not produce a module-level
    declaration colliding with the function name."""
    text = """\
module top;
/*autodef*/
function integer log2(input integer d);
    for (log2 = 0; d > 0; log2 = log2 + 1) begin
        d = d >> 1;
    end
endfunction
endmodule
"""
    out = "\n".join(_adt(text))
    assert not re.search(r"^\s*integer\s+log2\s*;", out, re.M)


def test_multi_var_for_header_all_declared():
    """for (i=0, j=0; ...) declares both loop variables."""
    text = """\
module top (input clk);
/*autodef*/
always @(*) begin
    for (i = 0, j = 0; i < 4; i = i + 1, j = j + 1) begin
        a[i] = b[j];
    end
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert re.search(r"integer\s+i\s*;", out)
    assert re.search(r"integer\s+j\s*;", out)


def test_constant_index_lhs_recovers_packed_width():
    """lfsr_c[0] .. lfsr_c[15] constant selects -> reg [15:0], widest wins."""
    text = """\
module top (input clk);
/*autodef*/
always @(*) begin
    lfsr_c[0] = a;
    lfsr_c[7] = b;
    lfsr_c[15] = c;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert re.search(r"reg\s+\[15:0\]\s+lfsr_c;", out)


def test_variable_index_side_cannot_poison_packed_width():
    """x[R-1:0] = ... gives the packed width; a later x[var_idx] = ... (a
    variable, not a constant) must not replace it with an invalid
    ``reg[var_idx:0]`` — nor erase it via the widest-merge."""
    text = """\
module top (input clk);
parameter R_SLAVE_NUM = 4;
/*autodef*/
always @(*) begin
    req_r[R_SLAVE_NUM-1:0] = 'h0;
    req_r[chn_sel_idx]     = a;
end
endmodule
"""
    out = "\n".join(_adt(text))
    assert re.search(r"reg\s+\[R_SLAVE_NUM-1:0\]\s+req_r;", out)
    assert "chn_sel_idx:0" not in out


def test_auto_param_unmapped_uses_identity():
    """verilog-mode alignment: a parameter with no AUTO_TEMPLATE entry and
    no manual pre-marker connection connects by identity (``.A (A)``), even
    when the parent does not define the name — the user fills it in or
    templates it; defaults are NOT substituted and nothing is omitted."""
    from verilog_tooling.emacs import auto_param, parse_module_params
    SUBP = """\
module subp (o);
    parameter A = 1;
    parameter B = A + 1;
    parameter C;
    output o;
endmodule
"""
    buf = ["subp #(/*AUTOINSTPARAM*/) u_s (/*AUTOINST*/ .o (o));"]
    out = auto_param(buf, {"subp": parse_module_params(SUBP.splitlines())})
    body = "\n".join(out)
    assert ".A" in body and "(A)" in body  # identity, not the default 1
    assert ".B" in body and "(B)" in body  # identity, not A + 1
    assert ".C" in body and "(C)" in body


def test_nasty_comments_dont_confuse_autodef():
    """/* // */ ordering, // /* lines, // inside strings, inline blocks —
    none of them may leak phantom signals or hide real ones."""
    lines = """\
module m (
    input        clk,
    input  [7:0] din
);
/*autodef*/
// /* not_a_signal
/* // still comment */
assign w0 = din[0];           /* inline */ 
assign s = "http://example";
always @(posedge clk) begin
    $display("100%% // not a comment // nor this */");
    q <= w0;
end
endmodule
""".splitlines()
    out = auto_def_t(lines)
    region = out[out.index("// Define io wire here") : out.index("// End of automatic define")]
    text = "\n".join(region)
    assert "not_a_signal" not in text
    assert "http" not in text and "example" not in text
    assert decl("wire ", "", "w0") in region  # the real assign LHS survives
    assert decl("reg  ", "", "q") in region


def test_input_port_width_fallback():
    """A net seen only on submodule INPUT ports takes the input port's
    width (plain net or explicit full range) instead of staying
    unresolved; a real driver still trumps the hint."""
    lines = """\
module m (
    input clk
);
/*autodef*/
sub u_sub (/*autoinst*/
    .clk (clk),
    .din (din_w)
);
sub2 u_sub2 (
    .data (cfg[13:0]),
    .flag (flag_w)
);
endmodule
""".splitlines()
    sub2 = """\
module sub2 (
    input  wire        data,
    input  wire [3:0]  flag
);
endmodule
"""
    mods = sub_mods()
    mods["sub2"] = parse_module_ports(sub2.splitlines())
    out = auto_def_t(lines, mods)
    region = "\n".join(out[out.index("// Define inst wires here") : out.index("// Define integer here")])
    assert decl("wire ", "7", "din_w") in region       # from sub's din input width
    assert decl("wire ", "13", "cfg") in region        # explicit range in the connection
    assert decl("wire ", "3", "flag_w") in region      # from sub2's flag input width
    assert "unresolved: din_w" not in "\n".join(out)
    assert "unresolved: cfg" not in "\n".join(out)
    assert "unresolved: flag_w" not in "\n".join(out)


def test_last_pin_on_close_line_is_scanned():
    """The pin sharing a line with the closing `));` (every emacs-style
    last pin) must be recorded, not skipped."""
    lines = """\
module m (
    input clk
);
/*autodef*/
sub u_sub (/*autoinst*/
    .clk (clk),
    .din (din),
    .dout (last_w));
endmodule
""".splitlines()
    out = auto_def_t(lines, sub_mods())
    region = "\n".join(out[out.index("// Define inst wires here") : out.index("// Define integer here")])
    assert decl("wire ", "7", "last_w") in region
    assert "unresolved: last_w" not in "\n".join(out)


def test_unresolved_reasons_attached():
    """Every unresolved signal carries a human-readable reason."""
    lines = """\
module m (
    input clk
);
/*autodef*/
always @(posedge clk) begin
    q <= d_unresolved;
end
subp u_subp (/*autoinst*/
    .clk (clk),
    .dout (foreign_w[SUBW-1:0])
);
endmodule
""".splitlines()
    subp = """\
module subp (
    input  wire             clk,
    output wire [SUBW-1:0]  dout
);
endmodule
"""
    mods = {"subp": parse_module_ports(subp.splitlines())}
    out = auto_def_t(lines, mods)
    text = "\n".join(out)
    assert (
        "// unresolved: d_unresolved // no driver or declaration found" in text
    )
    assert (
        "// unresolved: foreign_w // width 'SUBW-1' references symbol(s) "
        "not visible in this module: SUBW" in text
    )


def test_multidim_packed_port_declared_with_dims():
    """A net driven by a multi-dim packed port keeps the port's dimensions
    in its declaration (not a 1-bit wire); the EAI /*[D1][D2]*/ note wins
    over the port's raw (unsubstituted) dims."""
    sub_md = """\
module sub_md (
    input  wire             clk,
    output reg  [W-1:0][3:0] bid,
    output reg  [7:0]         data
);
endmodule
"""
    lines = """\
module m #(
    parameter W = 8
)(
    input clk
);
/*autodef*/
sub_md u_sub_md (/*autoinst*/
    .clk (clk),
    .bid (bid_w),
    .data (data_w)
);
endmodule
""".splitlines()
    mods = {"sub_md": parse_module_ports(sub_md.splitlines())}
    out = auto_def_t(lines, mods)
    text = "\n".join(out)
    assert "wire[W-1:0][3:0]" in text.replace(" ", "") or "[W-1:0][3:0]" in text
    assert "unresolved: bid_w" not in text
    assert decl("wire ", "7", "data_w") in text


def test_multidim_dims_from_connection_note():
    """The dims in the EAI connection comment (already param-substituted)
    are used verbatim for the declaration."""
    sub_md = """\
module sub_md (
    input  wire             clk,
    output reg  [W-1:0][3:0] bid
);
endmodule
"""
    lines = """\
module m (
    input clk
);
/*autodef*/
sub_md u_sub_md (/*autoinst*/
    .clk (clk),
    .bid (bid_w/*[WM-1:0][3:0]*/)
);
endmodule
""".splitlines()
    mods = {"sub_md": parse_module_ports(sub_md.splitlines())}
    text = "\n".join(auto_def_t(lines, mods))
    assert "[WM-1:0][3:0]" in text


def test_inst_port_width_substituted_by_instance_params():
    """A port width naming the SUBMODULE's own parameter resolves via the
    instance's #(...) override — on the AD path, with no /*AUTOWIRE*/
    marker and a plain (range-less) connection."""
    subp = """\
module subp #(parameter R_MASTER_NUM = 2) (
    input  wire                     clk,
    output reg  [R_MASTER_NUM-1:0]  arvalid,
    input  wire  [R_MASTER_NUM-1:0]  arready
);
endmodule
"""
    lines = """\
module top;
localparam R_MASTER_NUM_ITP = 5;
/*autodef*/
subp #(.R_MASTER_NUM (R_MASTER_NUM_ITP)) u_subp (/*autoinst*/
    .clk (clk),
    .arvalid (arvalid_w),
    .arready (arready_w)
);
endmodule
""".splitlines()
    mods = {"subp": parse_module_ports(subp.splitlines())}
    text = "\n".join(auto_def_t(lines, mods))
    assert text.count("R_MASTER_NUM_ITP") >= 2
    assert "unresolved: arvalid_w" not in text
    assert "unresolved: arready_w" not in text
    assert "R_MASTER_NUM-1:0" not in text  # the raw submodule param is gone


# ---------------------------------------------------------------------------
# multi-dim declarations and the kill waiver


def test_multidim_io_port_gets_no_supplementary_wire():
    """A multi-dim io port (input [A:0][B:0] x;) is a complete declaration;
    ADT must not emit a supplementary body wire for it (the kill waiver
    would shuttle it out of the region on the next run)."""
    lines = """\
module top (a2);
input [1:0][3:0] a2;
/*autodef*/
endmodule
""".splitlines()
    out = auto_def_t(lines, {})
    assert not any(ln.strip().startswith("wire") and "a2" in ln for ln in out)


def test_waived_inst_derived_multidim_wire_is_absorbed(tmp_path):
    """A waived multi-dim wire that instance evidence re-derives identically
    (from the connection's /*[D1][D2]*/ note) returns to the region and the
    orphan line is dropped — runs are idempotent."""
    sub = tmp_path / "sub.v"
    sub.write_text("module sub (input [1:0][3:0] din, output [1:0][3:0] dout); endmodule\n")
    mods = {"sub": parse_module_ports(sub.read_text().splitlines())}
    # run-1 output shape: ADT emitted the inst-driven multi-dim wire inside
    # the region, the kill waiver then orphaned it after the closer
    lines = """\
module top;
/*autodef*/
sub u_sub (/*autoinst*/
    .din  (din2/*[1:0][3:0]*/),
    .dout (dout2/*[1:0][3:0]*/)
);
endmodule
""".splitlines()
    once = auto_def_t(lines, mods)
    twice = auto_def_t(once, mods)
    assert once == twice
    decls = [ln for ln in twice if "dout2" in ln and ln.strip().startswith("wire")]
    assert len(decls) == 1  # declared exactly once, inside the region
    assert twice.index(decls[0]) < twice.index("// End of automatic define")


def test_handwritten_multidim_reg_survives_kill():
    """The waiver still protects a hand-written multi-dim reg ADT cannot
    re-derive: it is kept verbatim outside the region, not re-emitted."""
    lines = [
        "module top;",
        "/*autodef*/",
        "// Define io wire here",
        "reg [1:0][3:0] mem;",
        "// End of automatic define",
        "always @(posedge clk) begin",
        "    mem[i] <= 4'h0;",
        "end",
        "endmodule",
    ]
    out = auto_def_t(lines, {})
    assert any(ln.strip() == "reg [1:0][3:0] mem;" for ln in out)
    assert sum("mem" in ln and ln.strip().startswith("reg") for ln in out) == 1


def test_autoregion_bare_inout_is_complete():
    """emacs behavior: a bare ``inout pad;`` inside an AUTO region
    (AUTOINOUT) is complete by construction — no companion ``wire pad;`` in
    the io-wire section (a net that only interconnects instances internally
    is AUTOWIRE's job)."""
    lines = [
        "module top(pad);",
        "/*AUTOINOUT*/",
        "// Beginning of automatic inouts (from unused autoinst inouts)",
        "inout pad;",
        "// End of automatics",
        "/*autodef*/",
        "endmodule",
    ]
    once = auto_def_t(lines, {})
    assert sum("pad" in ln and ln.strip().startswith("wire") for ln in once) == 0
    twice = auto_def_t(once, {})
    assert twice == once


def test_autoregion_inout_wire_stays_single():
    """An explicit ``inout wire pad;`` is complete — no companion wire."""
    lines = [
        "module top(pad);",
        "/*AUTOINOUT*/",
        "// Beginning of automatic inouts (from unused autoinst inouts)",
        "inout wire pad;",
        "// End of automatics",
        "/*autodef*/",
        "endmodule",
    ]
    out = auto_def_t(lines, {})
    assert sum("pad" in ln and ln.strip().startswith("wire") for ln in out) == 0


def test_io_decl_dimension_matrix():
    """extend_io_from_line must parse every packed/unpacked dimension shape
    in body io decls (regression: greedy [.*: swallowed unpacked dims;
    multi-dim packed kept only the first range)."""
    from verilog_tooling.autodef import SignalTable

    cases = [
        # (decl, name, msb, has more dims visible in rest handling)
        ("output [7:0] a;", "a", "7"),
        ("output [7:0][3:0] a;", "a", "7"),          # multi-dim packed
        ("output signed [15:0] y [0:7];", "y", "15"),  # packed + unpacked
        ("input wire [W-1:0][V-1:0] a, b;", "a", "W-1"),
        ("inout [3:0] pad [2];", "pad", "3"),
        ("output [`RANGE] r;", "r", "`RANGE"),        # define range
        ("output [(A+1)*2-1:0] e;", "e", "(A+1)*2-1"),  # expression msb
    ]
    for decl, name, msb in cases:
        t = SignalTable()
        t.extend_io_from_line(decl, 0)
        sig = t.get(name)
        assert sig is not None, f"{decl}: {name} not registered"
        assert sig.width == msb, f"{decl}: width {sig.width!r} != {msb!r}"


def test_get_assign_side_malformed_range_no_crash():
    """Obfuscated vendor models (random token soup) can produce elem strings
    with 2+ colons — must degrade to width None, never crash."""
    from verilog_tooling.autodef import get_assign_side

    side = get_assign_side("x[1:2:3]", "", {}, {}, {})
    assert side is not None and side.width is None
