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


def test_pulp_udt_declared_driver_not_duplicated(tmp_path):
    """pulp-platform/axi (axi_demux.sv): UDT variables driven by typed
    ports must keep their ORIGINAL declaration.  Before the fix the
    declared-but-invisible UDT names were re-emitted as bare
    `wire`-style declarations or flagged unresolved, duplicating the
    user's declarations."""
    (tmp_path / "cc_spill_register.v").write_text(
        "module cc_spill_register\n"
        "  #(parameter type data_t = logic, parameter bit Bypass = 1'b0)\n"
        "  (\n"
        "   input logic clk_i,\n"
        "   output data_t data_o\n"
        "  );\n"
        "endmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\n"
        "  select_t slv_aw_select;\n"
        "  cc_spill_register #(\n"
        "    .data_t ( select_t ),\n"
        "    .Bypass ( 1'b0 )\n"
        "  ) i_aw_spill (\n"
        "    /*AUTOINST*/\n"
        "    .clk_i  ( clk_i ),\n"
        "    .data_o ( slv_aw_select )\n"
        "  );\n"
        "  /*AUTOWIRE*/\n"
        "  /*autodef*/\n"
        "endmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aall", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert "slv_aw_select;  \n" not in out        # no bare re-declaration
    assert out.count("select_t slv_aw_select") == 1
    assert "unresolved: slv_aw_select" not in out


def test_aiu_tail_marker_instance_not_stub_no_crash(tmp_path):
    """Issue found on mor1kx (2026-10-07): an instance whose last kept
    connection line ends with `/*AUTOINST*/);` was misclassified as a
    stub, and AIU then crashed (stale line numbers after the first stub
    expansion).  The instance is fully connected; AIU must reorder-update
    it without crashing.
    """
    (tmp_path / "sub.v").write_text(
        "module sub(input wire a, input wire b, output wire y);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\n"
        "wire a, b, y;\n"
        "sub u1(.b(b),\n"
        "       .a(a),\n"
        "       .y(y) /*AUTOINST*/);\n"
        "sub u2(.b(b),\n"
        "       .a(a),\n"
        "       .y(y) /*AUTOINST*/);\n"
        "endmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aiu", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert out.count(".a(a)") == 2
    assert out.count(".y(y)") == 2


def test_aiu1_last_pin_with_close_paren_not_duplicated(tmp_path):
    """Issue found on mor1kx (2026-10-07): `aiu1` met the emacs-style
    last connection `.y(y)); // comment` and counted that pin as unseen,
    emitting it again (INST_NEW) while leaving the instance unclosed.
    """
    (tmp_path / "sub.v").write_text(
        "module sub(input wire a, output wire y);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\n"
        "wire a, y;\n"
        "sub u(/*AUTOINST*/\n"
        "  .a(a),\n"
        "  .y(y)); // last\n"
        "endmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aiu1", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert out.count(".y") == 1
    assert ".y(y)" in out


def test_aiu1_shorthand_connections_not_reemitted_as_new(tmp_path):
    """Issue found on pulp axi_xbar (2026-10-07): connections written in
    the SystemVerilog shorthand form `.clk_i,` (no parentheses) with the
    last kept pin comma-less were not recognised at all, so AIU1 appended
    every shorthand pin again as INST_NEW behind a missing comma.
    """
    (tmp_path / "sub.v").write_text(
        "module sub(input wire clk_i, input wire rst_ni,"
        " output wire [7:0] q_o);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\n"
        "wire clk_i, rst_ni;\n"
        "wire [7:0] q;\n"
        "sub u(/*AUTOINST*/\n"
        "  .clk_i,\n"
        "  .rst_ni,\n"
        "  .q_o (q)\n"
        ");\n"
        "endmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aiu1", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert out.count(".clk_i") == 1
    assert out.count(".rst_ni") == 1
    assert "INST_NEW" not in out


def test_aiu1_new_pin_gets_comma_after_comma_less_kept_pin(tmp_path):
    """Issue found on pulp axi_mux (2026-10-07): when AIU1 appends pins
    after a kept list whose final pin has no trailing comma, the first
    appended pin needs a comma on the previously last line."""
    (tmp_path / "sub.v").write_text(
        "module sub(input wire a, input wire b, output wire y);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\n"
        "wire a, b, y;\n"
        "sub u(/*AUTOINST*/\n"
        "  .a(a),\n"
        "  .b (b)\n"
        ");\n"
        "endmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aiu1", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert out.count(".y") == 1
    assert ".b (b)," in out


def test_autoarg_strips_udt_from_port_name_list(tmp_path):
    """Issue found on pulp axi_demux (2026-10-07): AUTOARG carried the
    user-defined type text into the regenerated name list
    (`axi_req_t [NoMstPorts-1:0] mst_reqs_o,`), which Verilator rejects
    (ranges/types do not belong in a name list)."""
    top = tmp_path / "top.v"
    top.write_text(
        "module top (/*AUTOARG*/\n"
        "  input logic clk_i,\n"
        "  input axi_req_t [3:0] reqs_i,\n"
        "  output axi_resp_t resp_o\n"
        ");\n"
        "endmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aall", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    # the regenerated header is a name list: bare names only, no types
    head = out.split("/*AUTOARG*/", 1)[1].split(");", 1)[0]
    assert "reqs_i" in head and "axi_req_t" not in head and "resp_o" in head


def test_aiu_positional_instance_marker_at_tail_not_overrun(tmp_path):
    """Issue found on zipcpu zipsystem (2026-10-07): an instance with
    positional connections and a tail marker was neither a stub nor
    recognised, so the updater scanned past the instance end for a `);`
    and emitted ports over following code.  The marker must be moved to
    the instance head and the (unkeyable) positional lines regenerated."""
    (tmp_path / "busdelay.v").write_text(
        "module busdelay(input i_clk, input i_reset, output o_wb_ack);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\n"
        "busdelay wbdelay(\n"
        "  i_clk, i_reset,\n"
        "  o_wb_ack\n"
        " /*AUTOINST*/);\n"
        "`ifdef FORMAL\n"
        "`endif\n"
        "endmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aiu", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert ".i_clk" in out
    assert "`ifdef FORMAL" in out and "`endif" in out
    # the positional connection lines must not survive next to the named
    # ones (mixing positional and named connections is illegal)
    assert "i_clk, i_reset," not in out
    assert " o_wb_ack\n" not in out


def test_ait_param_block_directives_not_in_connections(tmp_path):
    """Issue found on zipcpu busdelay (2026-10-07): the submodule header
    carries `ifdef FORMAL around a localparam *inside its #(...)
    parameter block*; the port parser collected those directives as
    port-list Keep lines, so regenerated instances contained
    `ifdef/`endif` in the middle of the connection list."""
    (tmp_path / "sub.v").write_text(
        "module sub #(\n"
        "  parameter AW = 32,\n"
        "`ifdef FORMAL\n"
        "  localparam F_LGDEPTH = 4,\n"
        "`endif\n"
        "  parameter DW = 32\n"
        ") (\n"
        "  input wire clk,\n"
        "  output wire [DW-1:0] q\n"
        ");\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\nwire clk;\nwire [31:0] q;\n"
        "sub u(/*AUTOINST*/);\nendmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["ait", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert "`ifdef" not in out
    assert ".clk" in out and ".q" in out


def test_aiu_multi_connection_line_not_duplicated(tmp_path):
    """Issue found on zipcpu zipsystem (2026-10-07): hand-written
    instances packing several connections on one line
    (`.a(x), .b(y),`) were only recognised by their first pin, so the
    updater re-emitted the rest as INST_NEW duplicates."""
    (tmp_path / "sub.v").write_text(
        "module sub(input wire a, input wire b, output wire y);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\nwire a, b, y;\n"
        "sub u(/*AUTOINST*/\n"
        "     .a(a), .b(b),\n"
        "     .y(y)\n"
        "    );\nendmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aiu", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert out.count(".a(") == 1
    assert out.count(".b(") == 1
    assert out.count(".y(") == 1
    assert "INST_NEW" not in out


def test_aiu1_multi_connection_line_seen_not_duplicated(tmp_path):
    """Same multi-connection-line hazard on the aiu1 update path."""
    (tmp_path / "sub.v").write_text(
        "module sub(input wire a, input wire b, output wire y);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top;\nwire a, b, y;\n"
        "sub u(/*AUTOINST*/\n"
        "     .a(a), .b(b),\n"
        "     .y(y)\n"
        "    );\nendmodule\n"
    )
    from verilog_tooling import inst

    out_file = tmp_path / "out.v"
    inst.main(["aiu1", "-i", str(top), "-o", str(out_file),
               "--ref_file", str(top), "-y", str(tmp_path)])
    out = out_file.read_text()
    assert out.count(".b(") == 1
    assert "INST_NEW" not in out
