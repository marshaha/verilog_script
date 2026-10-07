"""Reproduction cases found by running the tool over real GitHub
projects (picorv32, serv, Hazard3, Ibex, OpenTitan prims, ZipCPU
wb2axip, ulx3s_examples) and linting the expansion with Verilator.

Each test is the minimal shape that misbehaved before its fix; the
project/origin is named per test.  Run with: pytest tests/
"""

from verilog_tooling.emacs import auto_inst
from verilog_tooling.fmt import auto_define_format, auto_port_format
from verilog_tooling.inst import parse_module_ports


def _mods(*texts):
    out = {}
    for t in texts:
        m = parse_module_ports(t.splitlines())
        out[m.name] = m
    return out


# ---------------------------------------------------------------------------
# inst.py — user-defined-type ports (typedef name before the port name)


def test_parse_module_ports_user_defined_types():
    """``output crash_dump_t crash_dump_o`` (ibex_core): the port name is
    the second word, not the type.  Covers plain, pkg::-scoped,
    packed-dimension-between-type-and-name (prim_ram_1p_scr) and the
    ``unsigned`` signing word forms."""
    lines = [
        "module m (",
        "    output crash_dump_t crash_dump_o,",
        "    input  ibex_mubi_t fetch_enable_i,",
        "    input  ram_1p_cfg_req_t [NumRamInst-1:0] cfg_i,",
        "    input  testcase_pkg::enum_t top_enum,",
        "    input  unsigned [3:0] v,",
        "    input  logic clk_i",
        ");",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.data_type, p.width) for p in moddef.ports] == [
        ("crash_dump_o", "output", "crash_dump_t", None),
        ("fetch_enable_i", "input", "ibex_mubi_t", None),
        ("cfg_i", "input", "ram_1p_cfg_req_t", "NumRamInst-1:0"),
        ("top_enum", "input", "testcase_pkg::enum_t", None),
        ("v", "input", "unsigned", "3:0"),
        ("clk_i", "input", "", None),
    ]


# ---------------------------------------------------------------------------
# emacs.py — SystemVerilog .pin shorthand, tail-comment comma


SUB_SH = """\
module sub (
    input  wire a,
    input  wire b,
    output wire o
);
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


def test_auto_inst_does_not_duplicate_shorthand_connections():
    """SystemVerilog ``.a,`` shorthand (ibex_top's prim_buf/core
    instances): a pin already connected with the shorthand must be
    recognised as connected, not regenerated — regenerating it is a
    duplicate pin connection (verilator error on the real ibex_top)."""
    buf = ["module top;", "  sub u (.a, .b(x) /*AUTOINST*/);", "endmodule"]
    out = auto_inst(buf, _mods(SUB_SH))
    body = "\n".join(out)
    assert body.count(".a") == 1  # kept shorthand only; no regenerated `.a (a)`
    assert ".b(x)" in body and ".o" in body


def test_auto_inst_tail_comment_keeps_comma():
    """A kept pin list whose last line trails a ``//}}}`` fold comment
    (ZipCPU wb2axip) must still get a comma before the generated
    connections — the comment hides the region's trailing ')' from
    the tail check."""
    buf = [
        "filt u_f (",
        "    .clk (clk),",
        "    .din_i (din) //}}}",
        "    /*AUTOINST*/",
        ");",
    ]
    out = auto_inst(buf, _mods(FILT))
    body = "\n".join(out)
    assert ".din_i (din)," in body  # comma separates kept and generated pins
    assert ".dout_o" in body


# ---------------------------------------------------------------------------
# fmt.py — APF (auto-port-format) / ADF (auto-define-format) survival


def test_apf_ansi_last_port_keeps_header_close():
    """ANSI header whose last port carries ');' on the same line (e.g.
    serv_top's 'output wire o_mdu_valid);') must keep the ');' — dropping
    it leaves the module header unclosed (verilator syntax error)."""
    out = auto_port_format(["module m (", "  input a,", "  output b);"])
    assert out[-1].endswith("b);")


def test_apf_keeps_multi_name_declaration_line():
    """``input clk, wen,`` declares two ports on one line (attosoc's
    picosoc_regs); APF must not drop all but the last name."""
    lines = ["module picosoc_regs (", "\tinput clk, wen,", "\tinput [5:0] waddr", ");"]
    out = auto_port_format(lines)
    assert out[1] == "\tinput clk, wen,"
    assert any("waddr" in x for x in out) and any(x.strip() == ");" for x in out)


def test_apf_keeps_user_defined_type():
    """``input ibex_mubi_t fetch_enable_i`` (ibex_top.sv): dropping the
    user-defined type turns the port into an implicit 1-bit net (the
    verilator WIDTHEXPAND seen on the real ibex_top).  A signed word
    sits in the same slot and must survive as well."""
    line = auto_port_format(["input ibex_mubi_t fetch_enable_i,"])[0]
    assert line.startswith("input ibex_mubi_t") and line.rstrip().endswith("fetch_enable_i,")
    line = auto_port_format(["input signed [7:0] sdata,"])[0]
    assert line.startswith("input signed [7:0]") and line.rstrip().endswith("sdata,")
    line = auto_port_format(["input ram_1p_cfg_req_t [NumRamInst-1:0] cfg_i,"])[0]
    assert line.startswith("input ram_1p_cfg_req_t [NumRamInst-1:0]") and line.rstrip().endswith("cfg_i,")


def test_adf_keeps_declaration_initializers():
    """Net declaration assignments (``wire x = a && b;``) — including
    multi-line initializers (hazard3 style) — are not plain declarations;
    aligning them must not drop the name/initializer."""
    lines = [
        "wire x_stall = a && b ||",
        "        c && d;",
        "wire single = a && b;",
        "reg [7:0] q = 8'hFF; // reset",
    ]
    assert auto_define_format(lines) == lines
