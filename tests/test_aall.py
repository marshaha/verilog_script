"""aall: the single-process AALL pipeline must be byte-identical to the
sequential 8-command pipeline (eap -> eai -> aio -> aw -> areg -> adt -> arg -> af),
and the threaded/cached module resolver must keep the old priorities."""

from pathlib import Path
import re

from verilog_tooling import arg as arg_mod
from verilog_tooling import autodef, inout, inst, misc, wire
from verilog_tooling.inst import _resolve_module_files

SUB = """\
module sub #(
    parameter W = 8
)(
    input  wire         clk,
    input  wire [W-1:0] din,
    output reg  [W-1:0] dout,
    output reg          sub_done
);
endmodule
"""

TOP = """\
module top (
    /*autoarg*/
);
input        clk;
input  [7:0] din;
output       done;
output [7:0] q;
/*autodef*/
/*AUTOWIRE*/
/*AUTOREG*/
/*AUTOINPUT*/
/*AUTOOUTPUT*/

sub #(/*AUTOINSTPARAM*/
) u_sub (/*AUTOINST*/);

ghost u_ghost (/*autoinst*/);

always @(posedge clk) begin
    cnt <= 8'h00;
end

always @(*) begin
    for (k = 0; k < 4; k = k + 1) begin
        tmp[k] = din[k];
    end
end
endmodule
"""


def _run_sequential(top: Path, libdir: Path, tmp_path: Path) -> str:
    """The vim AALL chain: eight separate CLI runs, output feeding the next."""
    cur = tmp_path / "seq.v"
    cur.write_text(TOP)
    steps = [
        (inst, "eap"),
        (inst, "eai"),
        (inout, "aio"),
        (wire, "aw"),
        (wire, "ar"),
        (autodef, "adt"),
        (arg_mod, "ar"),
        (inst, "af"),
    ]
    for mod, cmd in steps:
        out = tmp_path / "seq.o"
        mod.main(
            [cmd, "-i", str(cur), "-o", str(out), "--ref_file", str(top), "-y", str(libdir)]
        )
        cur.write_text(out.read_text())
    return cur.read_text()


def _run_aall(top: Path, libdir: Path, tmp_path: Path) -> str:
    out = tmp_path / "aall.v"
    inst.main(
        ["aall", "-i", str(top), "-o", str(out), "--ref_file", str(top), "-y", str(libdir)]
    )
    return out.read_text()


def test_aall_matches_sequential_pipeline(tmp_path, capsys):
    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text(TOP)
    capsys.readouterr()  # drop the missing-module warnings
    assert _run_aall(top, libdir, tmp_path) == _run_sequential(top, libdir, tmp_path)


def test_aall_expands_everything(tmp_path, capsys):
    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text(TOP)
    capsys.readouterr()
    out = _run_aall(top, libdir, tmp_path)
    assert ".dout" in out and ".sub_done" in out          # EAI pins
    assert ".W" in out                                     # EAP param
    assert "// Define flip-flop registers here" in out     # AD sections
    assert "reg" in out and "cnt" in out                   # AD freg
    assert "integer" in out or "k;" in out                 # loop var declared
    assert "u_ghost" in out                                # missing module kept as-is


def test_resolve_module_files_priority_and_dir_cache(tmp_path):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "m.v").write_text("module m; endmodule\n")
    (d2 / "m.sv").write_text("module m; endmodule\n")
    (d2 / "only2.sv").write_text("module only2; endmodule\n")

    files = _resolve_module_files(["m", "only2", "nope"], [str(d1), str(d2)])
    assert files["m"] == d1 / "m.v"          # first libdir wins
    assert files["only2"] == d2 / "only2.sv"  # .sv found, later dir
    assert "nope" not in files

    # explicit inst-file beats the libdirs
    files = _resolve_module_files(["m"], [str(d1)], inst_files={"m": str(d2 / "m.sv")})
    assert files["m"] == d2 / "m.sv"

    # vc-file entry (stem match) beats the libdirs
    files = _resolve_module_files(["m"], [str(d1)], vc_files=[str(d2 / "m.sv")])
    assert files["m"] == d2 / "m.sv"

    # a reusable dir cache avoids rescans and a missing dir is tolerated
    cache: dict = {}
    files = _resolve_module_files(
        ["m"], [str(tmp_path / "no_such_dir"), str(d2)], _dir_cache=cache
    )
    assert files["m"] == d2 / "m.sv"
    assert str(tmp_path / "no_such_dir") in cache


# ---------------------------------------------------------------------------
# AIO x autoarg x autodef interaction (run-2 stability)


FULL_SUB = """\
module sub (
    input  wire       clk,
    input  wire       cfg_en,
    input  wire [7:0] sel_din,
    output wire [7:0] dout,
    output wire       done
);
endmodule
"""

FULL_TOP = """\
module full_top (
    /*autoarg*/
    /*AUTOINPUT*/
    /*AUTOOUTPUT*/
);
/*autodef*/
/*AUTOWIRE*/
/*AUTOREG*/
sub u_sub (/*autoinst*/);
endmodule
"""


def test_aio_v2k_with_autoarg_and_autodef_is_stable(tmp_path):
    """AIO markers in the header + /*autoarg*/ + /*autodef*/: no duplicate
    wire/reg from ADT, autoarg stays out of the AIO-owned header, the
    module name is never flagged, and a second run is byte-identical."""
    (tmp_path / "sub.v").write_text(FULL_SUB)
    top = tmp_path / "top.v"
    top.write_text(FULL_TOP)
    o1 = tmp_path / "o1.v"
    inst.main(["aall", "-i", str(top), "-o", str(o1), "--ref_file", str(top), "-y", str(tmp_path)])
    text1 = o1.read_text()
    # inputs/outputs generated as v2k header declarations
    assert "// Beginning of automatic inputs (from unused autoinst inputs)" in text1
    assert "// Beginning of automatic outputs (from unused autoinst outputs)" in text1
    # no duplicate body declarations for the AIO ports
    decls = [
        ln.strip()
        for ln in text1.splitlines()
        if ln.strip().startswith(("wire", "reg"))
    ]
    assert not any(
        any(name in ln for name in ("cfg_en", "clk", "sel_din", "done", "dout"))
        for ln in decls
    )
    # the module's own name is never flagged unresolved
    assert "unresolved: full_top" not in text1
    # second run is byte-identical
    o2 = tmp_path / "o2.v"
    inst.main(["aall", "-i", str(o1), "-o", str(o2), "--ref_file", str(o1), "-y", str(tmp_path)])
    assert o2.read_text() == text1


def test_no_duplicate_declaration_between_autowire_and_autodef(tmp_path):
    """A net driven by an instance output is declared by AUTOWIRE; ADT
    (running after AW in the pipeline) must see that region declaration and
    not re-declare it — verilator flags the duplicate."""
    (tmp_path / "sub.v").write_text(FULL_SUB)
    top = tmp_path / "top.v"
    top.write_text(
        """\
module dup_top;
/*autodef*/
/*AUTOWIRE*/
sub u_sub (/*autoinst*/);
endmodule
"""
    )
    o1 = tmp_path / "o1.v"
    inst.main(["aall", "-i", str(top), "-o", str(o1), "--ref_file", str(top), "-y", str(tmp_path)])
    text1 = o1.read_text()
    for name in ("dout", "done"):
        decls = [
            ln
            for ln in text1.splitlines()
            if ln.strip().startswith(("wire", "reg")) and name in ln
        ]
        assert len(decls) == 1, f"{name} declared {len(decls)} times: {decls}"
    # and the second run stays identical
    o2 = tmp_path / "o2.v"
    inst.main(["aall", "-i", str(o1), "-o", str(o2), "--ref_file", str(o1), "-y", str(tmp_path)])
    assert o2.read_text() == text1


# periph_sys.sv regression: localparams of the submodule must not be
# AUTOINSTPARAM-expanded; unresolvable marker-less instances and interface
# instances (via `include) are never unresolved; '0 is a literal.

PERIPH_SUB = """\
module bus_intf_wrap #(
    localparam UART_ADDR = 32'h1000,
    localparam IDLE      = 2'b00,
    parameter   W        = 8
)(
    input  wire         clk,
    output reg  [W-1:0] q
);
localparam BODY_LP = 1;
endmodule
"""

PERIPH_TOP = """\
module top (
    /*autoarg*/
);
input        clk;
output [7:0] q;
/*autodef*/

`include "intf_inst.sv"

bus_intf_wrap #(/*AUTOINSTPARAM*/
) u_wrap (/*AUTOINST*/);

sync_dff2b #(   .WL    (2   ),
        .Q_INIT (1'b0))
    u_sync_ack(
        .clk (clk),
        .d   (d_sync),
        .q   (q_sync)
);

assign psel_s = apb_sys_cfg.psel;
assign dbg    = '0;

endmodule
"""


def test_aall_periph_sys_unresolved_regressions(tmp_path, capsys):
    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "bus_intf_wrap.sv").write_text(PERIPH_SUB)
    (libdir / "intf_inst.sv").write_text(
        "axera_apb_interface #(\n"
        "        .AW (12),\n"
        "        .DW (32))\n"
        "apb_sys_cfg();\n"
    )
    top = tmp_path / "top.v"
    top.write_text(PERIPH_TOP)
    capsys.readouterr()
    out = _run_aall(top, libdir, tmp_path)
    unresolved = {m.group(1) for m in re.finditer(r"// unresolved: (\S+)", out)}
    # A: localparams never become #() overrides
    assert ".W" in out  # the real parameter IS expanded
    assert ".UART_ADDR" not in out and ".IDLE" not in out and ".BODY_LP" not in out
    assert not ({"UART_ADDR", "IDLE", "BODY_LP"} & unresolved)
    # B: interface instance (from the `include) and its type are not signals
    assert not ({"apb_sys_cfg", "axera_apb_interface"} & unresolved)
    # C: unresolvable marker-less instance: neither name is a signal
    assert not ({"sync_dff2b", "u_sync_ack"} & unresolved)
    # D: the '0 literal is not a signal
    assert "0" not in unresolved


AMS = """\
module top_ams;

  latch latch (/*AUTOINST*/);

endmodule

module latch (/*AUTOARG*/);

`ifdef __VAMS_ENABLE__
    output (* integer groundSensitivity="gnd "; integer supplySensitivity="vdd "; *) q;
 `else
    output q;
`endif

`ifdef __VAMS_ENABLE__
    input (* integer groundSensitivity="gnd "; integer supplySensitivity="vdd "; *) en;
 `else
    input en;
`endif

`ifdef __VAMS_ENABLE__
    input (* integer groundSensitivity="gnd "; integer supplySensitivity="vdd "; *) d;
 `else
    input d;
`endif

endmodule
"""


NAME_BUG245 = """\
module top_nb (
   );

   /*AUTOINPUT*/

   /*AUTOOUTPUT*/

   Sub sub (
            /*AUTOINST*/);

endmodule

module Sub
  (
   input  logic clk,
   output [2:0] a_NO,
   input \\escaped*dot*named 
   );
endmodule

// Local Variables:
// verilog-auto-inst-dot-name: t
// verilog-auto-inst-vector: nil
// End:
"""


def test_aall_name_bug245_idempotent(tmp_path, capsys):
    """autoinst_name_bug245 shape, two failure modes on the second run:
    (1) APF rewrote Sub's ``input \\escaped*dot*named `` header line to
    ``input named``, inventing a phantom port that AUTOINST/AUTOINPUT
    then picked up; (2) with verilog-auto-inst-vector: nil, the
    subscript on .a_NO was suppressed by the AUTOOUTPUT declaration
    the first run itself had generated — only a MANUAL parent
    declaration may suppress it.  Both runs must be byte-identical."""
    src = tmp_path / "nb.v"
    src.write_text(NAME_BUG245)
    capsys.readouterr()
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(src), "-o", str(p1)])
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2)])
    out1, out2 = p1.read_text(), p2.read_text()
    assert out1 == out2
    assert "\\escaped*dot*named" in out1  # escaped name survives verbatim
    assert "named;//" not in out1        # no phantom port declaration
    assert "a_NO[2:0]" in out1           # subscript not suppressed


AUTOWIRE_REAL = """\
module sc_top (
  input var real  Tx_vcm,
  input var real  i_DAC_in,
  input           i_Tx_SC_en,
  output var real Tx_vsc
);

endmodule

module cm_top (
  input           i_Tx_CM_en,
  output var real Tx_vcm
);

endmodule

module top (
/*AUTOOUTPUT*/
/*AUTOINPUT*/
);

/*AUTOWIRE*/

cm_top cm_buf (/*AUTOINST*/);

sc_top sc_buf (/*AUTOINST*/);

endmodule
"""


TRIO_TOP = """\
module top (
    /*autoarg*/
);
input        clk;
input  [7:0] din;
output       done;
output [7:0] q;
/*autodef*/
/*AUTOWIRE*/
/*AUTOREG*/
/*AUTOLOGIC*/
/*AUTOINPUT*/
/*AUTOOUTPUT*/

sub u_sub0 (.clk(clk), .din(din), .dout(q), .sub_done(done));
sub u_sub1 (.clk(clk), .din(din), .dout(q), .sub_done(done));
endmodule
"""


def test_aall_autowire_real_idempotent(tmp_path, capsys):
    """autowire_real shape: APF dropped the ``var`` keyword (and the
    ``real`` type it introduces) from the submodules' own headers on
    the first run, so the second run re-derived their ports as plain
    wires (``real Tx_vcm;`` decayed to ``wire Tx_vcm;`` and the
    AUTOINPUT/AUTOOUTPUT declarations lost ``real``)."""
    src = tmp_path / "wr.v"
    src.write_text(AUTOWIRE_REAL)
    capsys.readouterr()
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(src), "-o", str(p1)])
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2)])
    out1, out2 = p1.read_text(), p2.read_text()
    assert out1 == out2
    assert "input var real" in out1 and "output var real" in out1
    assert "real" in out1 and "Tx_vcm;" in out1
    assert not any(ln.startswith("wire") for ln in out1.splitlines())


def test_aall_ams_vorwerk_idempotent(tmp_path, capsys):
    """autoinst_ams_vorwerk shape: latch's ports are declared in both
    `ifdef/`else branches, so they are unconditional and their
    AUTOINST pins carry no `ifdef wrapper.  The second aall run used
    to drop the wrappers the first run had emitted (the expanded
    AUTOARG header made the parser stop tracking body guards), so the
    two runs disagreed; they must now be byte-identical."""
    src = tmp_path / "ams.v"
    src.write_text(AMS)
    capsys.readouterr()
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(src), "-o", str(p1)])
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2)])
    out1, out2 = p1.read_text(), p2.read_text()
    assert out1 == out2
    top_section = out1.split("endmodule")[0]
    assert ".q" in top_section and "`ifdef" not in top_section


def test_pin_scan_cache_shared_by_logic_wire_reg(tmp_path):
    """Perf: AUTOLOGIC/AUTOWIRE/AUTOREG share one _inst_pin_scan per module
    span (3 full instance scans -> 1).  The cached trio must be
    byte-identical to the uncached sequential runs, and the later passes
    must hit the cache instead of rescanning."""
    modules = {"sub": inst.parse_module_ports(SUB.splitlines(), with_params=True)}
    lines = TRIO_TOP.splitlines()

    ref = misc.auto_logic(lines, modules)
    ref = wire.auto_wire(ref, modules)
    ref = wire.auto_reg(ref, modules)
    assert "q" in "\n".join(ref)  # the scan really found driven nets

    cache: dict = {}
    got = misc.auto_logic(lines, modules, cache)
    assert len(cache) == 1  # one span: AUTOLOGIC fills the cache
    assert len(cache["top"]["instances"]) == 2
    got = wire.auto_wire(got, modules, cache)
    got = wire.auto_reg(got, modules, cache)
    assert len(cache) == 1  # AUTOWIRE/AUTOREG reused it — no rescan
    assert got == ref


def test_inst_driven_nets_same_named_instances_both_collected():
    """Two instances sharing one instance name (illegal Verilog, but the
    official verilog-mode tests_ok/autoinst_regexp_inst.v has it): the
    pin-scan dedup keys on the instance's opening-paren position, not
    (module, inst), so the second instance's nets are still collected."""
    modules = {"sub": inst.parse_module_ports(SUB.splitlines(), with_params=True)}
    lines = [
        "module top;",
        "sub sub1 (.clk(clk), .din(din), .dout(q1), .sub_done(done1));",
        "sub sub1 (.clk(clk), .din(din), .dout(q2), .sub_done(done2));",
        "endmodule",
    ]
    nets = wire._inst_driven_nets(lines, modules)
    assert {"q1", "q2", "done1", "done2"} <= set(nets)
    # the cached phase-1 scan finds the same two instances
    scan = wire._inst_pin_scan(lines, modules)
    assert len(scan["instances"]) == 2


DUP_SPAN_TOP = """\
`ifdef ALT
module dup_top;
/*AUTOWIRE*/
sub u_a (.clk(clk), .din(din), .dout(net_a), .sub_done(done_a));
endmodule
`else
module dup_top;
/*AUTOWIRE*/
sub u_b (.clk(clk), .din(din), .dout(net_b), .sub_done(done_b));
endmodule
`endif
"""


def test_aall_same_named_module_spans_no_cache_collision(tmp_path, capsys):
    """Two same-named module spans in one file (`ifdef/`else variants):
    module_spans is purely lexical and yields both, and the aall pin-scan
    cache keys on the span's module name — without the duplicate-name
    fallback, the SECOND span's scan (processed first, bottom-up) would be
    served to the first, leaving net_a undeclared.  Each span must declare
    exactly its own driven net."""
    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "sub.v").write_text(SUB)
    top = tmp_path / "dup.v"
    top.write_text(DUP_SPAN_TOP)
    capsys.readouterr()
    out = _run_aall(top, libdir, tmp_path)
    first, second = out.split("endmodule")[:2]
    assert "net_a" in first and "done_a" in first
    assert "net_b" not in first and "done_b" not in first
    assert "net_b" in second and "done_b" in second
    assert "net_a" not in second and "done_a" not in second


BITSEL_TPL = """\
`define DW 16
module sub (
   output [`DW-1:0] bb
   );
endmodule

module top (
   /*AUTOOUTPUT*/
   );

   /*sub AUTO_TEMPLATE (
    .bb (test_b[3]),
    );*/
   sub u_inst0 (/*AUTOINST*/);

endmodule
"""


def test_aall_template_bit_select_width(tmp_path, capsys):
    """#54: a template connection that bit-selects one bit of a wider
    port — .bb (test_b[3]) on a [`DW-1:0] port — declares the promoted
    net [3:3] (verilog-mode records the select as the sig's vec), not
    the port's raw width expression [`DW-1:0]."""
    src = tmp_path / "bitsel.v"
    src.write_text(BITSEL_TPL)
    capsys.readouterr()
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(src), "-o", str(p1)])
    out1 = p1.read_text()
    assert "output[3:3]" in out1 and "test_b" in out1
    assert not re.search(r"output\[`DW-1:0\]\s+test_b", out1)
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2)])
    assert p2.read_text() == out1  # idempotent


PAREN_CONN = """\
module sub (
   output [3:0] bb,
   output [3:0] cc,
   output [3:0] dd
   );
endmodule

module top (
   /*AUTOOUTPUT*/
   );

   sub u_inst0 (
      .bb ({test_b}),
      .cc ((test_c)),
      .dd (test_d)
      );

endmodule
"""


def test_aall_paren_wrapped_connection_exempt(tmp_path, capsys):
    """A (...) wrapped plain net is exempt from AUTOOUTPUT candidacy
    exactly like a {...} one (verilog-auto-ignore-concat, default on);
    with the setting nil it extracts WITH the port's width — before
    the fix ((test_c)) was promoted as a scalar, its width lost in
    the parenthesized-expression path."""
    src = tmp_path / "paren.v"
    src.write_text(PAREN_CONN)
    capsys.readouterr()
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(src), "-o", str(p1)])
    out1 = p1.read_text()
    assert "test_d" in out1 and "output[3:0]" in out1  # plain control
    assert not re.search(r"^\s*output.*\btest_b\b", out1, re.M)  # {} exempt
    assert not re.search(r"^\s*output.*\btest_c\b", out1, re.M)  # () exempt
    src2 = tmp_path / "paren_nil.v"
    src2.write_text(PAREN_CONN + "// verilog-auto-ignore-concat:nil\n")
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(src2), "-o", str(p2)])
    out2 = p2.read_text()
    assert re.search(r"output\[3:0\]\s+test_c", out2)  # promoted, width kept


BITSEL_WIRE = """\
module sub (
   output [7:0] bb
   );
endmodule
module sink (
   input [7:0] x
   );
endmodule
module top;
   /*AUTOWIRE*/
   /*sub AUTO_TEMPLATE (
    .bb (mid[3]),
    );*/
   sub u0 (/*AUTOINST*/);
   /*sink AUTO_TEMPLATE (
    .x (mid),
    );*/
   sink u1 (/*AUTOINST*/);
endmodule
"""


def test_aall_bit_select_internal_net_autowire_width(tmp_path, capsys):
    """#54, AUTOWIRE side: a net driven through a lone bit-select and
    consumed whole stays internal; its wire declaration takes the
    select's [3:3] shape, not the driving port's [7:0]."""
    src = tmp_path / "bitsel_wire.v"
    src.write_text(BITSEL_WIRE)
    capsys.readouterr()
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(src), "-o", str(p1)])
    out1 = p1.read_text()
    assert re.search(r"wire\[3:3\]\s+mid;", out1)


ARG_DUP = """\
module latch (/*AUTOARG*/);
`ifdef __VAMS_ENABLE__
    output q;
 `else
    output q;
`endif
`ifdef __VAMS_ENABLE__
    input en;
 `else
    input en;
`endif
`ifdef __VAMS_ENABLE__
    input d;
 `else
    input d;
`endif
endmodule
"""


def test_aall_autoarg_dedups_ifdef_else_redeclaration(tmp_path, capsys):
    """autoinst_ams_vorwerk shape: a port declared in both `ifdef/`else
    branches enters the AUTOARG header once — verilog-mode's decl table
    is keyed by name.  Before the fix the header read `q, q,` and
    `en, en, d, d`."""
    src = tmp_path / "dup.v"
    src.write_text(ARG_DUP)
    capsys.readouterr()
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(src), "-o", str(p1)])
    out1 = p1.read_text()
    assert "q, q" not in out1 and "en, en" not in out1 and "d, d" not in out1
    assert re.search(r"//Outputs\s+q,", out1)
    assert re.search(r"//Inputs\s+en, d", out1)
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2)])
    assert p2.read_text() == out1  # idempotent


def test_ainj_typeless_io_no_wire_companions(tmp_path, capsys):
    """Field shape (tt.sv): hand-written type-less io declarations in a
    name-list module.  ainj must not add `wire clk;`-style companions
    for the inputs in the /*autodef*/ io section; the undriven output
    still gets its `reg` from the AUTOREG region."""
    src = tmp_path / "ttshape.sv"
    src.write_text("""\
module tt (/*autoarg*/);
/*autoinput*/
/*autooutput*/

input                                   clk;
input                                   rst_n;
input        [7:0]                      data;
output                                  ack;

/*autodef*/
/*autowire*/
/*autoreg*/

assign ack = rst_n;

endmodule
""")
    capsys.readouterr()
    p1 = tmp_path / "p1.sv"
    inst.main(["ainj", "-i", str(src), "-o", str(p1)])
    out1 = p1.read_text()
    assert not re.search(r"(?m)^wire\s+clk;", out1)
    assert not re.search(r"(?m)^wire\s+rst_n;", out1)
    assert not re.search(r"(?m)^wire\[7:0\]\s+data;", out1)
    p2 = tmp_path / "p2.sv"
    inst.main(["ainj", "-i", str(p1), "-o", str(p2)])
    assert p2.read_text() == out1  # idempotent


SUB_SV_VARTYPES = """\
module sub (
    input  logic [3:0] i,
    output reg  [7:0] a,
    output logic [3:0] b
);
endmodule
"""

SUB2_SV = """\
module sub2 (
    input [7:0] i
);
endmodule
"""

TOP_VARTYPES = """\
module top (/*autoarg*/);
   output reg [7:0] q;
   /*autooutput*/
   /*autoinput*/
   /*autoinst*/
   sub u_sub (.a (na), .b (nb), .i (nin));
endmodule
"""

TOP_WIRE_VARTYPES = """\
module topw (/*autoarg*/);
   /*autoinst*/
   sub u_sub (.a (mid));
   sub2 u_sub2 (.i (mid));
   /*autowire*/
endmodule
"""


def test_aall_pure_verilog_demotes_var_types(tmp_path):
    """A .v top file must not inherit reg/logic from SV submodule ports
    into generated AIO declarations: a continuously driven net cannot be
    a variable in Verilog-2001 (iverilog -g2001 rejects it).  The .sv
    control run keeps the submodule's types, and the hand-written body
    declaration is untouched."""
    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "sub.sv").write_text(SUB_SV_VARTYPES)
    top = tmp_path / "top.v"
    top.write_text(TOP_VARTYPES)
    out = _run_aall(top, libdir, tmp_path)
    assert re.search(r"(?m)^output\[7:0\]\s+na;", out)
    assert re.search(r"(?m)^output\[3:0\]\s+nb;", out)
    assert re.search(r"(?m)^input\[3:0\]\s+nin;", out)
    assert "logic" not in out
    assert out.count("output reg") == 1  # only the hand-written q
    assert re.search(r"(?m)^output reg \[7:0\]\s+q;", out)
    # idempotent through a real .v name
    top2 = tmp_path / "top2.v"
    top2.write_text(out)
    assert _run_aall(top2, libdir, tmp_path) == out
    # .sv control: the submodule's types are preserved
    top_sv = tmp_path / "top.sv"
    top_sv.write_text(TOP_VARTYPES)
    out_sv = _run_aall(top_sv, libdir, tmp_path)
    assert re.search(r"(?m)^output\s+reg\s+\[7:0\]\s+na;", out_sv)
    assert re.search(r"(?m)^output\s+logic\s+\[3:0\]\s+nb;", out_sv)
    assert re.search(r"(?m)^input\s+logic\s+\[3:0\]\s+nin;", out_sv)


def test_aall_pure_verilog_demotes_autowire_var_types(tmp_path):
    """Same rule for the AUTOWIRE head: an internal net driven by an
    instance output port must not be declared reg/logic in a .v file."""
    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "sub.sv").write_text(SUB_SV_VARTYPES)
    (libdir / "sub2.sv").write_text(SUB2_SV)
    top = tmp_path / "topw.v"
    top.write_text(TOP_WIRE_VARTYPES)
    out = _run_aall(top, libdir, tmp_path)
    assert re.search(r"(?m)^wire\[7:0\]\s+mid;", out)
    assert "reg" not in out and "logic" not in out
    top_sv = tmp_path / "topw.sv"
    top_sv.write_text(TOP_WIRE_VARTYPES)
    out_sv = _run_aall(top_sv, libdir, tmp_path)
    assert re.search(r"(?m)^reg\[7:0\]\s+mid;", out_sv)


def test_auto_logic_drops_sub_port_reg_type():
    """AUTOLOGIC is AUTOWIRE with wire-type forced to logic, and a
    submodule port's wire/reg type is not propagated upwards
    (verilog-read-sub-decls-type). A sub port `output reg [7:0] count`
    therefore becomes `logic [7:0] count`, not `reg` (autoinst_interface)."""
    sub = """\
module sub (
    output reg [7:0] count
);
endmodule
"""
    top = """\
module top;
/*AUTOLOGIC*/
sub u_sub (/*autoinst*/
    .count (count)
);
endmodule
"""
    out = misc.auto_logic(top.splitlines(), {"sub": inst.parse_module_ports(sub.splitlines(), with_params=True)})
    decls = [ln for ln in out if "count" in ln and ln.strip().startswith(("logic", "reg", "wire"))]
    assert len(decls) == 1
    assert decls[0].strip().startswith("logic")


def test_aall_tick_define_module_names(tmp_path, capsys):
    """`define'd module names expand at both ends (autoinst_moddefine.v):
    `SUBMOD_A in the instance position resolves to submod_mod, and a
    `module `SUBMOD_DECL` declaration registers under its expanded name
    submod_decl_from_def; an undefined tick (SUBMOD_UNDEFED) keeps its
    bare name. All three instances expand."""
    top = tmp_path / "top.v"
    top.write_text(
        "module top (/*AUTOARG*/);\n"
        "   /*AUTOWIRE*/\n"
        "`define SUBMOD_A submod_mod\n"
        "`define SUBNAME_B subname_b\n"
        "   `SUBMOD_A `SUBNAME_B\n"
        "     (/*AUTOINST*/);\n"
        "   `SUBMOD_UNDEFED subundefed\n"
        "     (/*AUTOINST*/);\n"
        "   submod_decl_from_def subundefed2\n"
        "     (/*AUTOINST*/);\n"
        "endmodule\n"
        "module submod_mod (/*AUTOARG*/);\n"
        "   input a;\n"
        "   output q;\n"
        "endmodule\n"
        "module SUBMOD_UNDEFED (/*AUTOARG*/);\n"
        "   output q;\n"
        "endmodule\n"
        "`define SUBMOD_DECL submod_decl_from_def\n"
        "module `SUBMOD_DECL (/*AUTOARG*/);\n"
        "   output q;\n"
        "endmodule\n"
    )
    capsys.readouterr()
    out = _run_aall(top, tmp_path, tmp_path)
    assert ".q" in out and ".a" in out  # submod_mod instance expanded
    assert out.count(".q") == 3  # all three instances contribute q
    assert "wire" in out and "q;" in out


def test_aall_inst_before_param_group_and_param_dims(tmp_path, capsys):
    """module inst #(...) (...) shape (autoinstparam_int.v): the pin
    list after a parameter-override group resolves, AUTOINSTPARAM
    resolves the module too, logic [5:0] params connect with their
    subscript (BLUP[5:0], ZOT[5:0] shared across the comma list), and
    two declarations on one body line (input x; output y;) both parse."""
    top = tmp_path / "top.v"
    top.write_text(
        "module xyz\n"
        "  #(parameter int FOO=1,\n"
        "    parameter logic [5:0] BLUP=3, ZOT=4)\n"
        "   (input x1, output y);\n"
        "endmodule\n"
        "module pdq;\n"
        "   input x; output y;\n"
        "endmodule\n"
        "module abc;\n"
        "   xyz XYZ\n"
        "     #(/*AUTOINSTPARAM*/)\n"
        "   (/*AUTOINST*/);\n"
        "   pdq PDQ\n"
        "     #(/*AUTOINSTPARAM*/)\n"
        "   (/*AUTOINST*/);\n"
        "endmodule\n"
    )
    capsys.readouterr()
    out = _run_aall(top, tmp_path, tmp_path)
    import re as _re

    squashed = _re.sub(r"\s+", " ", out).replace(" )", ")")
    assert ".BLUP (BLUP[5:0])" in squashed
    assert ".ZOT (ZOT[5:0])" in squashed
    assert ".FOO (FOO)" in squashed
    assert ".y (y)" in squashed and ".x (x)" in squashed


def test_aall_module_header_with_param_group_not_an_instance(tmp_path, capsys):
    """autoinst_tennant.v regression: a module header `module top
    #(...) (...)` whose AUTOARG regexp contains a literal .* must not
    resolve as a star-expansion instance (module='module', inst='top'
    used to raise KeyError and abort aall)."""
    top = tmp_path / "top.v"
    top.write_text(
        "module sub (input clk, output q);\n"
        "endmodule\n"
        "module top\n"
        "  #(parameter X=4)\n"
        "   (/*AUTOARG*/\n"
        "    /*AUTOINPUT(\"^x.*\")*/\n"
        "    );\n"
        "   sub u_sub (/*AUTOINST*/);\n"
        "endmodule\n"
    )
    capsys.readouterr()
    out = _run_aall(top, tmp_path, tmp_path)
    import re as _re

    squashed = _re.sub(r"\s+", " ", out).replace(" )", ")")
    assert ".q (q)" in squashed and ".clk (clk)" in squashed


def test_aall_autologic_fills_wire_type_on_untyped_inputs(tmp_path, capsys):
    """verilog-auto-logic-setup: a module with /*AUTOLOGIC*/ gets
    verilog-auto-wire-type "logic", so an otherwise untyped AUTOINPUT
    port is declared `input logic ...` (autooutput_simplify.v)."""
    top = tmp_path / "top.v"
    top.write_text(
        "module sub (input [W-1:0] x);\n"
        "endmodule\n"
        "module top\n"
        "  (/*AUTOINPUT*/\n"
        "   );\n"
        "   /*AUTOLOGIC*/\n"
        "   sub u_sub (/*AUTOINST*/);\n"
        "endmodule\n"
    )
    capsys.readouterr()
    out = _run_aall(top, tmp_path, tmp_path)
    import re as _re

    squashed = _re.sub(r"\s+", " ", out).replace(" )", ")")
    assert "input logic [W-1:0] x" in squashed
