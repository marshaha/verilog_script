"""`ifdef guards end to end (house rule 2026-11-03, user direction):
a submodule port under `ifdef, an instance pin under `ifdef, and the
ports promoted from it must all keep the guard — otherwise the design
stops compiling with the macro off.  Live Emacs 29.3 expands guarded
ports unconditionally in all three spots; preserving them is a
deliberate divergence, not parity."""

from verilog_tooling import arg as arg_mod
from verilog_tooling import emacs, inout, inst, wire
from verilog_tooling.inst import parse_module_ports

SUB = """\
module sub (
  input clk,
  input rst_n,
`ifdef EMU_AICHIP
  input FPGA_CHIP_DEBUG_UART_RXD,
  output FPGA_GPIO0_A00,
`endif
  output dout
);
  assign dout = clk;
  assign FPGA_GPIO0_A00 = clk;
endmodule
"""

TOP_STUB = """\
module sys (/*autoarg*/);
  /*AUTOINPUT*/
  /*AUTOOUTPUT*/
  /*AUTOWIRE*/
  sub u_sub (/*AUTOINST*/);
endmodule
"""

TOP_GUARDED_PINS = """\
module sys (/*autoarg*/);
  /*AUTOINPUT*/
  /*AUTOOUTPUT*/
  /*AUTOWIRE*/
  sub u_sub (/*AUTOINST*/
             // Outputs
`ifdef EMU_AICHIP
        .FPGA_GPIO0_A00             (FPGA_GPIO0_A00),
`endif
        .dout                       (dout),
             // Inputs
        .clk                        (clk),
        .rst_n                      (rst_n),
`ifdef EMU_AICHIP
        .FPGA_CHIP_DEBUG_UART_RXD   (FPGA_CHIP_DEBUG_UART_RXD)
`endif
);
endmodule
"""


def _depths(lines):
    """`ifdef nesting depth of each content line (directives excluded)."""
    depth = 0
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("`ifdef") or s.startswith("`ifndef"):
            depth += 1
            out.append(depth - 1)
        elif s.startswith("`endif"):
            depth -= 1
            out.append(depth)
        else:
            out.append(depth)
    return out


def _depth_of(lines, needle):
    ds = _depths(lines)
    for ln, d in zip(lines, ds):
        if needle in ln:
            return d
    raise AssertionError(f"{needle!r} not found")


def _sub():
    return parse_module_ports(SUB.splitlines(), with_params=True)


def test_emacs_autoinst_wraps_guarded_pins():
    out = emacs.auto_inst(TOP_STUB.splitlines(), {"sub": _sub()})
    assert sum(1 for ln in out if "`ifdef EMU_AICHIP" in ln) == 2  # Outputs + Inputs
    assert _depth_of(out, ".FPGA_GPIO0_A00") == 1
    assert _depth_of(out, ".FPGA_CHIP_DEBUG_UART_RXD") == 1
    assert _depth_of(out, ".clk") == 0
    assert _depth_of(out, ".rst_n") == 0
    assert _depth_of(out, ".dout") == 0


def test_pin_scan_records_instance_body_guards():
    nets = wire._inst_driven_nets(
        TOP_GUARDED_PINS.splitlines(), {"sub": _sub()},
        ("input", "output"), simple_only=True,
    )
    assert nets["FPGA_GPIO0_A00"].guard == (("EMU_AICHIP", True),)
    assert nets["FPGA_CHIP_DEBUG_UART_RXD"].guard == (("EMU_AICHIP", True),)
    assert nets["clk"].guard == ()
    assert nets["rst_n"].guard == ()
    assert nets["dout"].guard == ()


def test_pin_guard_multi_driver_widens_to_unconditional():
    sub = parse_module_ports(
        ["module sub (", "  output o", ");", "endmodule"], with_params=True
    )
    lines = [
        "module top ();",
        "  sub u1 (",
        "`ifdef EMU_AICHIP",
        "    .o (x)",
        "`endif",
        "  );",
        "  sub u2 (.o (x));",
        "endmodule",
    ]
    nets = wire._inst_driven_nets(lines, {"sub": sub}, ("output",), simple_only=True)
    # Guarded driver + unguarded driver: the net exists under either.
    assert nets["x"].guard == ()


def test_aio_mirrors_pin_guard_onto_ports():
    lines = TOP_GUARDED_PINS.splitlines()
    out = inout.auto_input(lines, {"sub": _sub()})
    out = inout.auto_output(out, {"sub": _sub()})
    assert _depth_of(out, "FPGA_CHIP_DEBUG_UART_RXD;") == 1
    assert _depth_of(out, "FPGA_GPIO0_A00;") == 1
    assert _depth_of(out, "clk;") == 0
    assert _depth_of(out, "dout;") == 0


def test_autoarg_mirrors_body_guard_onto_header():
    lines = [
        "module sys (/*autoarg*/);",
        "`ifdef EMU_AICHIP",
        "output FPGA_GPIO0_A00;",
        "`endif",
        "output dout;",
        "input clk;",
        "`ifdef EMU_AICHIP",
        "input FPGA_CHIP_DEBUG_UART_RXD;",
        "`endif",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    assert _depth_of(out, "FPGA_GPIO0_A00") == 1
    assert _depth_of(out, "FPGA_CHIP_DEBUG_UART_RXD") == 1
    assert _depth_of(out, "dout,") == 0
    # Leading comma on the trailing guarded entry: no trailing comma
    # before `);` in the all-off branch.
    assert "    , FPGA_CHIP_DEBUG_UART_RXD" in out
    assert "    clk," not in out


def test_autoarg_guarded_header_regenerates_when_derivable(capsys):
    lines = [
        "module sys (/*autoarg*/);",
        "`ifdef EMU_AICHIP",
        "output FPGA_GPIO0_A00;",
        "`endif",
        "output dout;",
        "endmodule",
    ]
    once = arg_mod.auto_arg(lines)
    assert "`ifdef EMU_AICHIP" in once
    # Run 2: the header now carries directives, but every guarded entry
    # is derivable from a body declaration, so it regenerates (the smart
    # skip must not freeze it) — and reproduces the same header.
    twice = arg_mod.auto_arg(once)
    assert twice == once
    assert "preprocessor directives" not in capsys.readouterr().err


def test_autoarg_guarded_header_skipped_when_not_derivable(capsys):
    lines = [
        "module m (/*autoarg*/,",
        "    output logic a_o,",
        "`ifdef RVFI",
        "    output logic rvfi_o,",
        "`endif",
        "    input logic clk_i",
        ");",
        "`ifdef EMU_AICHIP",
        "output FPGA_GPIO0_A00;",
        "`endif",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    # rvfi_o has no body declaration: the header is left untouched.
    assert out == lines
    assert "preprocessor directives" in capsys.readouterr().err


def test_autoarg_ifdef_else_duplicate_stays_unguarded():
    lines = [
        "module m (/*autoarg*/);",
        "`ifdef __VAMS_ENABLE__",
        "    output q;",
        "`else",
        "    output q;",
        "`endif",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    header = out[: out.index(");") + 1]
    assert sum(1 for ln in header if ln.strip() == "q") == 1
    assert not any("`ifdef" in ln or "`endif" in ln for ln in header)


def test_autoarg_ifndef_polarity_preserved():
    lines = [
        "module sys (/*autoarg*/);",
        "`ifndef NO_FPGA",
        "output FPGA_GPIO0_A00;",
        "`endif",
        "output dout;",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    assert "`ifndef NO_FPGA" in out
    assert _depth_of(out, "FPGA_GPIO0_A00") == 1


def test_aall_ifdef_guards_end_to_end(tmp_path):
    (tmp_path / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text(TOP_STUB)
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(top), "-o", str(p1), "-y", str(tmp_path)])
    out1 = p1.read_text()
    # AUTOINST pins + AIO declarations + AUTOARG header, each guarded.
    assert out1.count("`ifdef EMU_AICHIP") == 6
    assert _depth_of(out1.splitlines(), ".FPGA_GPIO0_A00") == 1
    assert _depth_of(out1.splitlines(), "FPGA_GPIO0_A00;") == 1
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2), "-y", str(tmp_path)])
    assert p2.read_text() == out1  # idempotent


# ---------------------------------------------------------------------------
# v2k (header-embedded) AIO regions: the guard must survive in the header
# list too, with plan-driven commas — a dropped guard here references a
# port that does not exist when the macro is off, same as the body case.

_V2K_INST_G = """sub u_sub (/*AUTOINST*/
    .clk  (clk),
    .rst_n (rst_n),
`ifdef EMU_AICHIP
    .FPGA_CHIP_DEBUG_UART_RXD (FPGA_CHIP_DEBUG_UART_RXD),
    .FPGA_GPIO0_A00 (FPGA_GPIO0_A00),
`endif
    .dout (dout)
);"""


def _v2k_mods():
    return {"sub": parse_module_ports(SUB.splitlines())}


def test_aio_v2k_guards_header_decls():
    top = (
        "module sys (\n    input ext_clk,\n    /*AUTOINPUT*/\n    /*AUTOOUTPUT*/\n);\n"
        + _V2K_INST_G
        + "\nendmodule\n"
    )
    out = inout.auto_io(top.splitlines(), _v2k_mods())
    # Both FPGA declarations sit inside their guard in the header list.
    assert _depth_of(out, "FPGA_CHIP_DEBUG_UART_RXD,") == 1
    assert _depth_of(out, "FPGA_GPIO0_A00,") == 1
    # The last declaration overall carries no dangling comma: the close
    # repair deleted the boundary comma past the `endif lines.
    dout_decl = next(
        ln for ln in out if "dout" in ln and ln.strip().startswith("output")
    )
    assert not dout_decl.split("//")[0].rstrip().endswith(",")
    assert out == inout.auto_io(out, _v2k_mods())  # idempotent


def test_aio_v2k_guarded_last_decl_combined_plan():
    # The AUTOINPUT region ends in a guarded declaration while the
    # AUTOOUTPUT region follows: a per-region plan alone would leave
    # clk without a comma in the macro-off branch (clk, dout with no
    # separator).  The combined plan sees dout's unguarded guard and
    # gives clk its trailing comma.
    sub2 = """module sub2 (
  input  clk,
`ifdef EMU_AICHIP
  input  z_fpga,
`endif
  output dout
);
endmodule
"""
    mods = {"sub2": parse_module_ports(sub2.splitlines())}
    inst2 = """sub2 u_s (/*AUTOINST*/
    .clk (clk),
`ifdef EMU_AICHIP
    .z_fpga (z_fpga),
`endif
    .dout (dout)
);"""
    top = "module sys2 (\n    /*AUTOINPUT*/\n    /*AUTOOUTPUT*/\n);\n" + inst2 + "\nendmodule\n"
    out = inout.auto_io(top.splitlines(), mods)
    clk_decl = next(
        ln for ln in out if ln.strip().startswith("input") and "clk" in ln
    )
    assert clk_decl.split("//")[0].rstrip().endswith(",")
    assert _depth_of(out, "z_fpga,") == 1
    assert out == inout.auto_io(out, mods)  # idempotent


def test_aio_v2k_bridge_final_region():
    # Two incomparable guards, final region, no unguarded follower:
    # the separator between px and py is a bridge comma under X+Y.
    sub3 = """module sub3 (
`ifdef HAS_X
  input  px,
`endif
`ifdef HAS_Y
  input  py,
`endif
  output dout
);
endmodule
"""
    mods = {"sub3": parse_module_ports(sub3.splitlines())}
    inst3 = """sub3 u_s (/*AUTOINST*/
`ifdef HAS_X
    .px (px),
`endif
`ifdef HAS_Y
    .py (py),
`endif
    .dout (dout)
);"""
    top = "module sys3b (output dout,\n    /*AUTOINPUT*/\n);\n" + inst3 + "\nendmodule\n"
    out = inout.auto_io(top.splitlines(), mods)
    stripped = [ln.strip() for ln in out]
    assert stripped.count(",") == 1  # exactly the X+Y bridge comma
    assert _depth_of(out, "input                                   px") == 1
    assert _depth_of(out, "input                                   py") == 1
    assert out == inout.auto_io(out, mods)  # idempotent


def test_aall_v2k_guards_end_to_end(tmp_path):
    (tmp_path / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text(
        "module sys (\n    input ext_clk,\n    /*AUTOINPUT*/\n    /*AUTOOUTPUT*/\n);\n"
        "  sub u_sub (/*AUTOINST*/);\nendmodule\n"
    )
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(top), "-o", str(p1), "-y", str(tmp_path)])
    out1 = p1.read_text()
    # AUTOINST pins (2 blocks) + v2k header declarations (2 blocks).
    assert out1.count("`ifdef EMU_AICHIP") == 4
    assert _depth_of(out1.splitlines(), "FPGA_CHIP_DEBUG_UART_RXD,") == 1
    assert _depth_of(out1.splitlines(), "FPGA_GPIO0_A00,") == 1
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2), "-y", str(tmp_path)])
    assert p2.read_text() == out1  # idempotent
