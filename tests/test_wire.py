"""verilog-mode AUTOWIRE / AUTOREG rewritten in Python (wire.py)."""

from verilog_tooling.inst import VerilogBuffer, parse_module_ports
import re
from verilog_tooling.wire import auto_reg, auto_wire, kill_auto_reg, kill_auto_wire

SUB = """\
module sub (
    input  wire       clk,
    input  wire [7:0] din,
    output wire [7:0] dout,
    output wire       flag
);
endmodule
"""

HEADER_W = "// Beginning of automatic wires (for undeclared instantiated-module outputs)"
HEADER_R = "// Beginning of automatic regs (for this module's undeclared outputs)"
CLOSER = "// End of automatics"


def sub_mods():
    return {"sub": parse_module_ports(SUB.splitlines())}


def decl(keyword: str, msb: str, name: str, comment: str = "") -> str:
    """One generated declaration line, the autodef arithmetic: keyword
    padded by CalMargin(12, len), '[msb:0]' unless scalar, then
    CalMargin(39, len) before 'name;'; a comment starts at column 48."""
    line = keyword + " " * (12 - len(keyword) + 1)
    if msb:
        line += f"[{msb}:0]"
    line += " " * (39 - len(line) + 1) + name + ";"
    if comment:
        line += " " * (48 - len(line)) + comment
    return line


TOP = """\
module top (
    input  wire       clk,
    input  wire [7:0] din,
    output wire [7:0] dout,
    output wire       flag
);
/*AUTOWIRE*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w)
);
endmodule
"""


# ---------------------------------------------------------------------------
# kill_auto_wire / kill_auto_reg


def test_kill_auto_wire_removes_region_keeps_marker():
    lines = [
        "module top;",
        "/*AUTOWIRE*/",
        HEADER_W,
        decl("wire ", "7", "dout_w", "// From u_sub of sub.v"),
        CLOSER,
        "endmodule",
    ]
    assert kill_auto_wire(lines) == ["module top;", "/*AUTOWIRE*/", "endmodule"]


def test_kill_auto_reg_removes_region_keeps_marker():
    lines = [
        "module top;",
        "/*AUTOREG*/",
        HEADER_R,
        decl("reg  ", "7", "dout_r"),
        CLOSER,
        "endmodule",
    ]
    assert kill_auto_reg(lines) == ["module top;", "/*AUTOREG*/", "endmodule"]


def test_kill_wire_leaves_reg_region_untouched():
    """Both regions share the // End of automatics closer: killing the
    wires must only consume the region after the wires header."""
    lines = [
        "/*AUTOWIRE*/",
        HEADER_W,
        "wire a;",
        CLOSER,
        "/*AUTOREG*/",
        HEADER_R,
        "reg b;",
        CLOSER,
    ]
    assert kill_auto_wire(lines) == ["/*AUTOWIRE*/", "/*AUTOREG*/", HEADER_R, "reg b;", CLOSER]
    assert kill_auto_reg(lines) == ["/*AUTOWIRE*/", HEADER_W, "wire a;", CLOSER, "/*AUTOREG*/"]


def test_kill_idempotent_when_no_region():
    lines = ["module top;", "/*AUTOWIRE*/", "endmodule"]
    assert kill_auto_wire(lines) == lines
    assert kill_auto_reg(lines) == lines


# ---------------------------------------------------------------------------
# auto_wire


def test_auto_wire_declares_instance_output_nets():
    out = auto_wire(TOP.splitlines(), sub_mods())
    assert out[6:11] == [
        "/*AUTOWIRE*/",
        HEADER_W,
        decl("wire ", "7", "dout_w", "// From u_sub of sub.v"),
        decl("wire ", "", "flag_w", "// From u_sub of sub.v"),
        CLOSER,
    ]
    # the rest of the buffer is untouched
    assert out[:6] == TOP.splitlines()[:6]
    assert out[11:] == TOP.splitlines()[7:]


def test_auto_wire_skips_input_port_nets():
    # clk/din connect to submodule INPUT ports: driven by this module, not
    # declared by AUTOWIRE
    out = auto_wire(TOP.splitlines(), sub_mods())
    region = out[out.index(HEADER_W) : out.index(CLOSER)]
    assert not any("clk" in line or "din" in line for line in region)


def test_auto_wire_skips_declared_nets():
    lines = TOP.replace("/*AUTOWIRE*/", "/*AUTOWIRE*/\nwire [7:0] dout_w;").splitlines()
    out = auto_wire(lines, sub_mods())
    region = out[out.index(HEADER_W) : out.index(CLOSER)]
    assert not any("dout_w" in line for line in region)
    assert decl("wire ", "", "flag_w", "// From u_sub of sub.v") in out
    # the user declaration stays in place
    assert "wire [7:0] dout_w;" in out


def test_auto_wire_skips_ports_and_parameters():
    text = """\
module top (
    input  wire       clk,
    output wire [7:0] dout_w,
    output wire       flag_w
);
parameter P = 1;
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (P),
    .dout (dout_w),
    .flag (flag_w)
);
/*AUTOWIRE*/
endmodule
"""
    out = auto_wire(text.splitlines(), sub_mods())
    # both nets are already declared as ports: no region is emitted at all
    assert HEADER_W not in out


def test_auto_wire_sorted_by_name():
    text = """\
module top (
    input wire clk
);
/*AUTOWIRE*/
sub u_b (/*autoinst*/
    .clk  (clk),
    .din  (din_b),
    .dout (zz_dout),
    .flag (aa_flag)
);
sub u_a (/*autoinst*/
    .clk  (clk),
    .din  (din_a),
    .dout (mm_dout),
    .flag (zz_flag)
);
endmodule
"""
    out = auto_wire(text.splitlines(), sub_mods())
    region = out[out.index(HEADER_W) + 1 : out.index(CLOSER)]
    names = [line.split(";")[0].split()[-1] for line in region]
    assert names == ["aa_flag", "mm_dout", "zz_dout", "zz_flag"]
    # each carries its own instance in the From comment
    assert "// From u_b of sub.v" in region[0]
    assert "// From u_a of sub.v" in region[1]


def test_auto_wire_scalar_port_bare_decl():
    out = auto_wire(TOP.splitlines(), sub_mods())
    assert decl("wire ", "", "flag_w", "// From u_sub of sub.v") in out
    assert not any("[" in line and "flag_w" in line for line in out)


def test_auto_wire_idempotent():
    once = auto_wire(TOP.splitlines(), sub_mods())
    assert auto_wire(once, sub_mods()) == once


def test_auto_wire_without_marker_is_noop():
    lines = ["module top;", "endmodule"]
    assert auto_wire(lines, sub_mods()) == lines


def test_auto_wire_missing_module_def_declares_nothing():
    out = auto_wire(TOP.splitlines(), {})
    assert HEADER_W not in out


def test_auto_wire_verilog_buffer_method():
    buf = VerilogBuffer(TOP.splitlines()).auto_wire(sub_mods())
    assert HEADER_W in buf.lines


# ---------------------------------------------------------------------------
# auto_reg


def test_auto_reg_declares_undeclared_assigned_output():
    text = """\
module top (
    input  wire       clk,
    input  wire [7:0] din,
    output [7:0]      dout,
    output            flag
);
/*AUTOREG*/
always @(posedge clk) begin
    dout <= din;
    flag <= 1'b0;
end
endmodule
"""
    out = auto_reg(text.splitlines())
    assert out[6:11] == [
        "/*AUTOREG*/",
        HEADER_R,
        decl("reg  ", "7", "dout"),
        decl("reg  ", "", "flag"),
        CLOSER,
    ]
    assert out[11:] == text.splitlines()[7:]


def test_auto_reg_skips_declared_outputs():
    text = """\
module top (
    input  wire  clk,
    output reg   done,
    output [3:0] cnt
);
/*AUTOREG*/
reg [3:0] cnt_reg;
always @(posedge clk) done <= 1'b1;
endmodule
"""
    out = auto_reg(text.splitlines())
    # 'done' has the reg keyword; 'cnt' is not otherwise declared/assigned
    assert decl("reg  ", "3", "cnt") in out
    region = out[out.index(HEADER_R) : out.index(CLOSER)]
    assert not any("done" in line for line in region)


def test_auto_reg_skips_assign_driven_output():
    text = """\
module top (
    input  wire clk,
    output wire a_w,
    output      b_r
);
/*AUTOREG*/
assign a_w = 1'b1;
always @(*) b_r = 1'b0;
endmodule
"""
    out = auto_reg(text.splitlines())
    region = out[out.index(HEADER_R) : out.index(CLOSER)]
    assert not any("a_w" in line for line in region)
    assert decl("reg  ", "", "b_r") in out


def test_auto_reg_skips_instance_driven_output():
    text = """\
module top (
    input  wire       clk,
    input  wire [7:0] din,
    output [7:0]      dout,
    output            flag
);
/*AUTOREG*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout),
    .flag (flag_w)
);
always @(posedge clk) flag <= 1'b0;
endmodule
"""
    out = auto_reg(text.splitlines(), sub_mods())
    region = out[out.index(HEADER_R) : out.index(CLOSER)]
    # dout is driven by the instance output: left as a wire (AUTOWIRE's job)
    assert not any("dout" in line for line in region)
    assert decl("reg  ", "", "flag") in out


def test_auto_reg_nothing_to_declare():
    text = """\
module top (
    input  wire clk,
    output reg  done
);
/*AUTOREG*/
endmodule
"""
    out = auto_reg(text.splitlines())
    assert HEADER_R not in out
    assert out == text.splitlines()


def test_auto_reg_idempotent():
    text = """\
module top (
    input  wire clk,
    output      done
);
/*AUTOREG*/
always @(posedge clk) done <= 1'b0;
endmodule
"""
    once = auto_reg(text.splitlines())
    assert auto_reg(once) == once


def test_auto_reg_verilog_buffer_method():
    text = "module top(\n    input wire clk,\n    output     done\n);\n/*AUTOREG*/\nendmodule\n"
    buf = VerilogBuffer(text.splitlines()).auto_reg()
    assert HEADER_R in buf.lines
    assert decl("reg  ", "", "done") in buf.lines


# ---------------------------------------------------------------------------
# kill round-trips


def test_kill_round_trip():
    lines = TOP.splitlines()
    assert kill_auto_wire(auto_wire(lines, sub_mods())) == lines
    reg_lines = """\
module top (
    input  wire clk,
    output      done
);
/*AUTOREG*/
always @(posedge clk) done <= 1'b0;
endmodule
""".splitlines()
    assert kill_auto_reg(auto_reg(reg_lines)) == reg_lines


def test_autowire_multiple_markers_emit_once():
    """With two /*autowire*/ markers the signal set is generated once
    (first marker); duplicating it would be a compile error."""
    from verilog_tooling.wire import auto_wire
    from verilog_tooling.inst import parse_module_ports

    SUB = """\
module sub (
    input  wire       clk,
    output wire [7:0] dout
);
endmodule
"""
    mods = {"sub": parse_module_ports(SUB.splitlines())}
    lines = """\
module top;
/*autowire*/
sub u_a (/*autoinst*/
    .clk (clk),
    .dout (dout_a)
);
sub u_b (/*autoinst*/
    .clk (clk),
    .dout (dout_b)
);
/*autowire*/
endmodule
""".splitlines()
    out = auto_wire(lines, mods)
    assert sum(1 for l in out if re.search(r"\bdout_a\s*;", l)) == 1
    assert sum(1 for l in out if re.search(r"\bdout_b\s*;", l)) == 1


def test_autowire_multidim_packed_port():
    """AUTOWIRE declares multi-dim packed nets with their dimensions."""
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
/*AUTOWIRE*/
sub_md u_sub_md (/*autoinst*/
    .clk (clk),
    .bid (bid_w/*[WM-1:0][3:0]*/));
endmodule
""".splitlines()
    mods = {"sub_md": parse_module_ports(sub_md.splitlines())}
    text = "\n".join(auto_wire(lines, mods))
    assert "[WM-1:0][3:0]" in text and "bid_w" in text


# ---------------------------------------------------------------------------
# verilog-auto-ignore-concat / verilog-auto-wire-comment (file-local vars)


def test_autowire_concat_driven_declared_by_default():
    """House default (ignore-concat = t) exempts concat nets from AIO
    candidacy, but a net driven by an instance OUTPUT through a {...}
    connection IS driven — a fact, not a candidacy — so AUTOWIRE declares
    it (scalar: the per-member split of the port width is unknowable)."""
    top = """\
module top;
/*AUTOWIRE*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout ({a_w, b_w}),
    .flag (flag_w)
);
endmodule
"""
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [
        decl("wire ", "", "a_w", "// From u_sub of sub.v"),
        decl("wire ", "", "b_w", "// From u_sub of sub.v"),
        decl("wire ", "", "flag_w", "// From u_sub of sub.v"),
    ]


def test_autowire_concat_extracted_when_nil():
    top = """\
module top;
/*AUTOWIRE*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout ({a_w, b_w[3:0]}),
    .flag (flag_w)
);
endmodule
// Local Variables:
// verilog-auto-ignore-concat: nil
// End:
"""
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [
        decl("wire ", "", "a_w", "// From u_sub of sub.v"),
        decl("wire ", "3", "b_w", "// From u_sub of sub.v"),
        decl("wire ", "", "flag_w", "// From u_sub of sub.v"),
    ]


def test_autowire_comment_nil():
    top = TOP.replace("/*AUTOWIRE*/", "/*AUTOWIRE*/").replace(
        "endmodule",
        "endmodule\n// Local Variables:\n// verilog-auto-wire-comment: nil\n// End:",
    )
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires
    assert not any("// From" in ln for ln in wires)


def test_autowire_empty_connection_no_comment_scrape():
    """An empty ``.dout()// Templated`` connection declares nothing — the
    net name must not be scraped out of the trailing comment."""
    top = """\
module top;
/*AUTOWIRE*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (                                                        ),// Templated
    .flag (flag_w                                                    )// Templated
);
endmodule
"""
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [decl("wire ", "", "flag_w", "// From u_sub of sub.v")]


def test_autowire_multi_pin_one_line():
    """EAI-style extraction: several connections on one line all count."""
    top = """\
module top;
/*AUTOWIRE*/
sub u_sub (/*autoinst*/ .clk (clk), .din (din), .dout (dout_w), .flag (flag_w));
endmodule
"""
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [
        decl("wire ", "7", "dout_w", "// From u_sub of sub.v"),
        decl("wire ", "", "flag_w", "// From u_sub of sub.v"),
    ]


def test_autowire_pins_before_marker():
    """Connections BEFORE the marker (manual pins of a marked instance)
    drive extraction too — emacs reads the whole pin list."""
    top = """\
module top;
/*AUTOWIRE*/
sub u_sub (
    .dout (pre_w),
    /*autoinst*/
    .clk  (clk),
    .din  (din),
    .flag (flag_w)
);
endmodule
"""
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [
        decl("wire ", "", "flag_w", "// From u_sub of sub.v"),
        decl("wire ", "7", "pre_w", "// From u_sub of sub.v"),
    ]


def test_autowire_multiline_concat_extracted_when_nil():
    """A concat spanning lines is extracted (the old line-based scan gave
    up on it)."""
    top = """\
module top;
/*AUTOWIRE*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout ({a_w,
            b_w[3:0]}),
    .flag (flag_w)
);
endmodule
// Local Variables:
// verilog-auto-ignore-concat: nil
// End:
"""
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [
        decl("wire ", "", "a_w", "// From u_sub of sub.v"),
        decl("wire ", "3", "b_w", "// From u_sub of sub.v"),
        decl("wire ", "", "flag_w", "// From u_sub of sub.v"),
    ]


def test_autowire_dot_name_shorthand():
    """SystemVerilog ``.flag`` shorthand connects net flag (undeclared, and
    not a port of this module — AUTOWIRE declares it)."""
    top = """\
module top;
/*AUTOWIRE*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag
);
endmodule
"""
    out = auto_wire(top.splitlines(), sub_mods())
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [
        decl("wire ", "7", "dout_w", "// From u_sub of sub.v"),
        decl("wire ", "", "flag", "// From u_sub of sub.v"),
    ]


def test_autowire_param_value_folds_local_consts():
    """verilog-auto-inst-param-value:t — a generated width naming a constant
    parameter of THIS module folds to the integer (stronger than emacs,
    which leaves W-1 symbolic)."""
    subw = """\
module subw #(parameter W = 4) (
    input  wire       clk,
    output wire [W-1:0] dout
);
endmodule
"""
    mods = {"subw": parse_module_ports(subw.splitlines())}
    top = """\
module top;
localparam W = 8;
/*AUTOWIRE*/
subw #(.W(W)) u_sub (/*autoinst*/
    .clk  (clk),
    .dout (dout_w)
);
endmodule
// Local Variables:
// verilog-auto-inst-param-value: t
// End:
"""
    out = auto_wire(top.splitlines(), mods)
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [decl("wire ", "7", "dout_w", "// From u_sub of subw.v")]


def test_autowire_param_value_off_keeps_symbolic():
    """Default (nil): the width stays symbolic."""
    subw = """\
module subw #(parameter W = 4) (
    input  wire       clk,
    output wire [W-1:0] dout
);
endmodule
"""
    mods = {"subw": parse_module_ports(subw.splitlines())}
    top = """\
module top;
localparam W = 8;
/*AUTOWIRE*/
subw #(.W(W)) u_sub (/*autoinst*/
    .clk  (clk),
    .dout (dout_w)
);
endmodule
"""
    out = auto_wire(top.splitlines(), mods)
    wires = [ln for ln in out if ln.startswith("wire")]
    assert wires == [decl("wire ", "W-1", "dout_w", "// From u_sub of subw.v")]


def test_autowire_unpacked_array_index_merge():
    """Element connections of an unpacked-array port across instances merge
    into one array declaration (abc[0]+abc[2] -> wire [6:0] abc [0:2]); a
    symbolic single index stays symbolic (nn [IDX])."""
    sub = """\
module sub (output wire [6:0] test2, output wire bitout);
endmodule
"""
    mods = {"sub": parse_module_ports(sub.splitlines())}
    top = """\
module top;
/*AUTOWIRE*/
sub u2 (/*autoinst*/
    .test2  (abc[2]/*[6:0].[2]*/),
    .bitout (sbit[2]/*.[2]*/)
);
sub u0 (/*autoinst*/
    .test2  (abc[0]/*[6:0].[0]*/),
    .bitout (sbit[0]/*.[0]*/)
);
sub un (/*autoinst*/
    .test2  (nn[IDX]/*[6:0].[IDX]*/),
    .bitout (nb[1]/*.[1]*/)
);
endmodule
"""
    out = auto_wire(top.splitlines(), mods)
    wires = [ln.strip() for ln in out if ln.startswith("wire")]
    assert any("abc [0:2];" in ln for ln in wires)
    assert any("sbit [0:2];" in ln for ln in wires)
    assert any("[6:0]" in ln and "nn [IDX];" in ln for ln in wires)


def test_kill_auto_wire_keeps_autologic_region():
    """A /*AUTOLOGIC*/ region (not implemented) must survive kill_auto_wire —
    its header matches but the marker says AUTOLOGIC, not AUTOWIRE."""
    lines = [
        "module top;",
        "/*AUTOLOGIC*/",
        "// Beginning of automatic wires (for undeclared instantiated-module outputs)",
        "logic signed [15:0] par [0:7];",
        "// End of automatics",
        "/*AUTOWIRE*/",
        "// Beginning of automatic wires (for undeclared instantiated-module outputs)",
        "wire a;",
        "// End of automatics",
        "endmodule",
    ]
    out = kill_auto_wire(lines)
    assert "logic signed [15:0] par [0:7];" in out  # AUTOLOGIC kept
    assert not any(ln.strip() == "wire a;" for ln in out)  # AUTOWIRE killed


# ---------------------------------------------------------------------------
# submodule parameter defaults supply the instance-effective width

_PMODA = """\
module moda #(parameter W = 8) (
    input  wire         clk,
    output wire [W-1:0] dout
);
endmodule
"""

_PTOP = """\
module top (
    input wire clk
);
/*AUTOWIRE*/
moda u_a (/*autoinst*/
    .clk  (clk),
    .dout (mid)
);
endmodule
"""


def test_auto_wire_uses_submodule_param_default():
    mods = {"moda": parse_module_ports(_PMODA.splitlines(), with_params=True)}
    out = auto_wire(_PTOP.splitlines(), mods)
    region = out[out.index(HEADER_W) : out.index(CLOSER)]
    # no #(...) override: width from moda's own default (W=8 -> [7:0]),
    # not the unresolved symbolic 'W-1'
    assert region == [
        HEADER_W,
        decl("wire ", "7", "mid", "// From u_a of moda.v"),
    ]


_PMODC = """\
module modc #(parameter W = D*K, parameter D = 4) (
    input  wire         clk,
    output wire [W-1:0] dout
);
endmodule
"""

_PTOP_C = """\
module top (
    input wire clk
);
/*AUTOWIRE*/
modc u_a (/*autoinst*/
    .clk  (clk),
    .dout (mid)
);
endmodule
"""


def test_auto_wire_symbolic_driver_dimension_declared():
    mods = {"modc": parse_module_ports(_PMODC.splitlines(), with_params=True)}
    out = auto_wire(_PTOP_C.splitlines(), mods)
    region = out[out.index(HEADER_W) : out.index(CLOSER)]
    # D folds to 4 but K stays symbolic: declare at the driver's
    # dimension instead of dropping the net entirely
    assert len(region) == 2
    assert "mid;" in region[1] and "K" in region[1]


def test_autowire_markerless_instance_output_concat():
    """A marker-less instance whose module resolves by name: its output-pin
    concatenation drives hi/lo, so AUTOWIRE declares them (scalar — the
    per-member split of the port width is unknowable) even with the
    ignore-concat default on."""
    top = """\
module top(input [3:0] in_pad, output [3:0] out_pad);
  sub u0 (
    .din({2'b0, val}),
    .dout({hi, lo})
  );
  /*autowire*/
endmodule
"""
    mods = {"sub": parse_module_ports("""\
module sub(input [3:0] din, output [3:0] dout);
endmodule
""".splitlines())}
    out = "\n".join(auto_wire(top.splitlines(), mods))
    assert re.search(r"^\s*wire\s+hi;\s*// From u0 of sub\.v$", out, re.M)
    assert re.search(r"^\s*wire\s+lo;\s*// From u0 of sub\.v$", out, re.M)


def test_aall_autowire_output_concat_end_to_end(tmp_path):
    """aall end-to-end on the concat2.v repro: both modules in ONE file,
    marker-less instance, output-side concat."""
    from verilog_tooling import inst

    top = tmp_path / "concat2.v"
    top.write_text("""\
module sub(input [3:0] din, output [3:0] dout);
endmodule
module top(input [3:0] in_pad, output [3:0] out_pad);
  sub u0 (
    .din({2'b0, val}),
    .dout({hi, lo})
  );
  /*autowire*/
  /*autodef*/
endmodule
""")
    out = tmp_path / "o.v"
    inst.main(["aall", "-i", str(top), "-o", str(out), "--ref_file", str(top), "-y", str(tmp_path)])
    text = out.read_text()
    assert re.search(r"^wire\s+hi;\s*// From u0 of sub\.v$", text, re.M)
    assert re.search(r"^wire\s+lo;\s*// From u0 of sub\.v$", text, re.M)
    # input-side val has no driver: not AW's job — ADT flags it unresolved
    assert "// unresolved: val" in text
