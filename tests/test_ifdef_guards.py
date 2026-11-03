"""`ifdef guards end to end (house rule 2026-11-03, user direction):
a submodule port under `ifdef, an instance pin under `ifdef, and the
ports promoted from it must all keep the guard — otherwise the design
stops compiling with the macro off.  Live Emacs 29.3 expands guarded
ports unconditionally in all three spots; preserving them is a
deliberate divergence, not parity."""

import itertools
import shutil
import subprocess

import pytest

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
    # In addition to px/py under X+Y, dout/py needs a bridge under !X+Y:
    # px is absent in that branch, so dout is py's direct predecessor.
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
    assert stripped.count(",") == 2  # X+Y for px/py and !X+Y for dout/py
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


# ---------------------------------------------------------------------------
# Review regressions: balanced bridges, hand-written v2k boundaries, smart-skip
# recognition, and direct predecessors across nested guards.


def _assert_directives_balanced(lines):
    stack = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("`ifdef ") or s.startswith("`ifndef "):
            stack.append(s)
        elif s.startswith("`else") or s.startswith("`elsif"):
            assert stack, f"branch without opener: {ln}"
        elif s.startswith("`endif"):
            assert stack, f"unmatched endif: {ln}"
            stack.pop()
    assert not stack, f"unclosed directives: {stack}\n" + "\n".join(lines)


def _lint_combos(tmp_path, lines, macros, stem, top="top"):
    if shutil.which("verilator") is None:
        pytest.skip("verilator not installed")
    for values in itertools.product([False, True], repeat=len(macros)):
        src = tmp_path / (stem + "_" + "".join(str(int(v)) for v in values) + ".sv")
        src.write_text("\n".join(lines) + "\n")
        cmd = ["verilator", "--lint-only", "-Wno-fatal", "--top-module", top]
        cmd += ["-D" + m for m, value in zip(macros, values) if value]
        cmd.append(str(src))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        diagnostics = proc.stdout + proc.stderr
        assert proc.returncode == 0 and "%Warning-NULLPORT" not in diagnostics, (
            f"macro combo {dict(zip(macros, values))}:\n{diagnostics}"
        )


def test_autoarg_two_guarded_outputs_bridge_balanced(tmp_path):
    lines = [
        "module m (/*autoarg*/);",
        "`ifdef A",
        "output a;",
        "`endif",
        "`ifdef B",
        "output b;",
        "`endif",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    _assert_directives_balanced(out)
    assert arg_mod.auto_arg(out) == out
    _lint_combos(tmp_path, out, ["A", "B"], "autoarg_bridge", top="m")


_TWO_GUARDED_SUB = """\
module sub (
`ifdef A
    output a
`endif
`ifdef A
`ifdef B
    ,
`endif
`endif
`ifdef B
    output b
`endif
);
endmodule
"""


def test_autoinst_two_guarded_ports_bridge_balanced_all_emitters(tmp_path):
    mods = {"sub": parse_module_ports(_TWO_GUARDED_SUB.splitlines(), with_params=True)}
    for name, producer in [("emacs", emacs.auto_inst), ("vim", inst.auto_inst)]:
        out = producer(["module top;", "sub u (/*AUTOINST*/);", "endmodule"], mods)
        _assert_directives_balanced(out)
        assert producer(out, mods) == out
        _lint_combos(
            tmp_path, out + _TWO_GUARDED_SUB.splitlines(), ["A", "B"], "inst_" + name
        )


def _aio_handwritten_boundary(before):
    if before:
        sub = """module sub (\n`ifdef A\n    input z\n`endif\n);\nendmodule\n"""
        header = "module top (\n    input hand\n    /*AUTOINPUT*/\n);\n"
        pins = "`ifdef A\n    .z (z)\n`endif"
    else:
        sub = """module sub (\n    input a\n`ifdef A\n    , input z\n`endif\n);\nendmodule\n"""
        header = "module top (\n    /*AUTOINPUT*/\n    input hand\n);\n"
        pins = "    .a (a)\n`ifdef A\n    , .z (z)\n`endif"
    top = header + "sub u (\n" + pins + "\n);\nendmodule\n"
    return top, sub


@pytest.mark.parametrize("before", [True, False], ids=["hand-before", "hand-after"])
def test_aio_v2k_handwritten_boundary_all_combos(tmp_path, before):
    top, sub = _aio_handwritten_boundary(before)
    mods = {"sub": parse_module_ports(sub.splitlines())}
    out = inout.auto_io(top.splitlines(), mods)
    _assert_directives_balanced(out)
    assert inout.auto_io(out, mods) == out
    _lint_combos(tmp_path, out + sub.splitlines(), ["A"], "aio_hand_" + str(before))


@pytest.mark.parametrize("before", [True, False], ids=["hand-before", "hand-after"])
@pytest.mark.parametrize(
    "tail", ["", " // hand comment   ", " /* hand comment */   "],
    ids=["plain", "line-comment", "block-comment"],
)
def test_aio_v2k_multiline_handwritten_boundary_all_combos(
    tmp_path, before, tail
):
    if before:
        sub = """module sub (\n`ifdef A\n    input z\n`endif\n);\nendmodule\n"""
        header = (
            "module top (\n    input logic [3:0]\n        hand"
            + tail
            + "\n    /*AUTOINPUT*/\n);\n"
        )
        pins = "`ifdef A\n    .z (z)\n`endif"
    else:
        sub = """module sub (\n    input a\n`ifdef A\n    , input z\n`endif\n);\nendmodule\n"""
        header = (
            "module top (\n    /*AUTOINPUT*/\n    input logic [3:0]\n        hand"
            + tail
            + "\n);\n"
        )
        pins = "    .a (a)\n`ifdef A\n    , .z (z)\n`endif"
    top = header + "sub u0 (\n" + pins + "\n);\nendmodule\n"
    mods = {"sub": parse_module_ports(sub.splitlines())}
    out = inout.auto_input(top.splitlines(), mods)
    _assert_directives_balanced(out)
    assert inout.auto_input(out, mods) == out
    _lint_combos(
        tmp_path,
        out + sub.splitlines(),
        ["A"],
        "aio_multiline_{}_{}".format(before, len(tail)),
    )


@pytest.mark.parametrize("before", [True, False], ids=["hand-before", "hand-after"])
@pytest.mark.parametrize("guarded", [False, True], ids=["unguarded", "guarded"])
@pytest.mark.parametrize("comma", [False, True], ids=["no-comma", "same-line-comma"])
@pytest.mark.parametrize("tail", ["", " // hand comment   "], ids=["plain", "comment"])
def test_aio_v2k_bare_handwritten_boundary_all_combos(
    tmp_path, before, guarded, comma, tail
):
    hand = ("    " if before or not comma else "    , ") + "hand"
    if before and comma:
        hand += ","
    hand += tail
    hand_lines = ["`ifdef H", hand, "`endif"] if guarded else [hand]
    body_decl = ["`ifdef H", "input hand;", "`endif"] if guarded else ["input hand;"]
    if before:
        sub = """module sub (\n`ifdef A\n    input z\n`endif\n);\nendmodule\n"""
        header_lines = ["module top ("] + hand_lines + ["    /*AUTOINPUT*/", ");"]
        pins = "`ifdef A\n    .z (z)\n`endif"
    else:
        sub = """module sub (\n    input a\n`ifdef A\n    , input z\n`endif\n);\nendmodule\n"""
        header_lines = ["module top (", "    /*AUTOINPUT*/"] + hand_lines + [");"]
        pins = "    .a (a)\n`ifdef A\n    , .z (z)\n`endif"
    top_lines = (
        header_lines
        + ["sub u0 ("]
        + pins.splitlines()
        + [");"]
        + body_decl
        + ["endmodule"]
    )
    mods = {"sub": parse_module_ports(sub.splitlines())}
    out = inout.auto_input(top_lines, mods)
    _assert_directives_balanced(out)
    assert inout.auto_input(out, mods) == out
    macros = ["A", "H"] if guarded else ["A"]
    _lint_combos(
        tmp_path,
        out + sub.splitlines(),
        macros,
        "aio_bare_{}_{}_{}_{}".format(before, guarded, comma, len(tail)),
    )


@pytest.mark.parametrize(
    "prefix, unpacked",
    [
        ("input logic signed [3:0]", ""),
        ("output reg [2:0]", " [1:0]"),
    ],
    ids=["input-logic-signed", "output-reg-unpacked"],
)
def test_aio_v2k_bare_after_preserves_full_decl_and_comments(
    tmp_path, prefix, unpacked
):
    sub = """module sub (
    input a
`ifdef A
    , input z
`endif
);
endmodule
"""
    top_lines = [
        "module top (",
        "    /*AUTOINPUT*/",
        "`ifdef H",
        "    , hand // header comment",
        "`endif",
        ");",
        "sub u0 (",
        "    .a (a)",
        "`ifdef A",
        "    , .z (z)",
        "`endif",
        ");",
        "`ifdef H",
        prefix + " hand" + unpacked + "; // body declaration comment",
        "`endif",
        "endmodule",
    ]
    mods = {"sub": parse_module_ports(sub.splitlines())}
    out = inout.auto_input(top_lines, mods)
    header = next(ln for ln in out if "hand" in ln and "header comment" in ln)
    assert ", " + prefix + " hand" + unpacked + " // header comment" in header
    body_comment = next(
        ln for ln in out if ln.strip() == "// body declaration comment"
    )
    assert _depth_of(out, "hand" + unpacked + " // header comment") == 1
    assert _depth_of(out, "// body declaration comment") == 1
    assert body_comment.strip() == "// body declaration comment"
    _assert_directives_balanced(out)
    assert inout.auto_input(out, mods) == out
    _lint_combos(
        tmp_path,
        out + sub.splitlines(),
        ["A", "H"],
        "aio_bare_full_" + prefix.split()[0],
    )


def test_aio_v2k_preceding_uses_global_direct_predecessors(tmp_path):
    sub = """module sub (
    inout d
`ifdef A
    , input za
`endif
`ifdef B
    , input zb
`endif
);
endmodule
"""
    top = """module top (
    input hand
    /*AUTOINPUT*/
);
sub u0 (
    .d (d)
`ifdef A
    , .za (za)
`endif
`ifdef B
    , .zb (zb)
`endif
);
endmodule
"""
    mods = {"sub": parse_module_ports(sub.splitlines())}
    out = inout.auto_input(top.splitlines(), mods)
    _assert_directives_balanced(out)
    assert inout.auto_input(out, mods) == out
    _lint_combos(tmp_path, out + sub.splitlines(), ["A", "B"], "aio_pre_global")


def test_aio_v2k_multiple_regions_correct_in_first_pass(tmp_path):
    sub = """module sub (
    inout d
`ifdef A
    , output oa
`endif
`ifdef B
    , input ib
`endif
`ifdef C
    , input ic
`endif
);
endmodule
"""
    top = """module top (
    /*AUTOOUTPUT*/
    /*AUTOINPUT*/
);
sub u0 (
    .d (d)
`ifdef A
    , .oa (oa)
`endif
`ifdef B
    , .ib (ib)
`endif
`ifdef C
    , .ic (ic)
`endif
);
endmodule
"""
    mods = {"sub": parse_module_ports(sub.splitlines())}
    out = inout.auto_io(top.splitlines(), mods)
    _assert_directives_balanced(out)
    assert inout.auto_io(out, mods) == out
    _lint_combos(tmp_path, out + sub.splitlines(), ["A", "B", "C"], "aio_regions_first")


def test_aall_v2k_multiple_regions_lint_all_combos(tmp_path, monkeypatch):
    sub = """module sub (
    inout d
`ifdef A
    , output oa
`endif
`ifdef B
    , input ib
`endif
`ifdef C
    , input ic
`endif
);
endmodule
"""
    top = """module top (
    /*AUTOOUTPUT*/
    /*AUTOINPUT*/
);
sub u0 (/*AUTOINST*/);
endmodule
"""
    (tmp_path / "sub.v").write_text(sub)
    (tmp_path / "top.v").write_text(top)
    real_auto_io = inout.auto_io
    contexts = []

    def checked_auto_io(lines, modules, allow_sv_types, param_value, ctx):
        contexts.append(ctx)
        return real_auto_io(lines, modules, allow_sv_types, param_value, ctx)

    monkeypatch.setattr(inout, "auto_io", checked_auto_io)
    p1 = tmp_path / "p1.v"
    inst.main(["aall", "-i", str(tmp_path / "top.v"), "-o", str(p1), "-y", str(tmp_path)])
    assert len(contexts) == 1 and contexts[0] is not None
    out1 = p1.read_text()
    _assert_directives_balanced(out1.splitlines())
    _lint_combos(
        tmp_path, out1.splitlines() + sub.splitlines(), ["A", "B", "C"], "aall_regions"
    )
    p2 = tmp_path / "p2.v"
    inst.main(["aall", "-i", str(p1), "-o", str(p2), "-y", str(tmp_path)])
    assert len(contexts) == 2 and contexts[1] is not None
    assert p2.read_text() == out1


@pytest.mark.parametrize(
    "guarded_lines, body_lines",
    [
        (["    , rvfi_o"], []),
        (["    a, missing,"], ["output a;"]),
    ],
    ids=["leading-comma", "bare-name-list"],
)
def test_autoarg_smart_skip_recognizes_bare_guarded_names(
    guarded_lines, body_lines, capsys
):
    lines = (
        ["module m (/*autoarg*/", "    //Outputs", "`ifdef RVFI"]
        + guarded_lines
        + ["`endif", ");"]
        + body_lines
        + ["endmodule"]
    )
    out = arg_mod.auto_arg(lines)
    assert out == lines
    assert "preprocessor directives" in capsys.readouterr().err
    assert arg_mod.auto_arg(out) == out


@pytest.mark.parametrize("directive", ["`include \"extra.svh\"", "`define EXTRA 1", "`undef EXTRA"])
def test_autoarg_non_conditional_directive_stays_conservative(directive, capsys):
    lines = [
        "module m (/*autoarg*/",
        "    //Outputs",
        directive,
        ");",
        "output a;",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    assert out == lines
    assert "preprocessor directives" in capsys.readouterr().err


def test_autoarg_derivable_guarded_bare_name_list_regenerates(tmp_path, capsys):
    lines = [
        "module m (/*autoarg*/);",
        "`ifdef A",
        "output a;",
        "output b;",
        "`endif",
        "output c;",
        "endmodule",
    ]
    once = arg_mod.auto_arg(lines)
    assert "`ifdef A" in once
    assert arg_mod.auto_arg(once) == once
    assert "preprocessor directives" not in capsys.readouterr().err
    _assert_directives_balanced(once)
    _lint_combos(tmp_path, once, ["A"], "autoarg_bare_list", top="m")


def _assert_plan_all_combos(guards):
    trailing, leading, bridges = inst.guard_separator_plan(guards)
    macros = sorted({m for guard in guards for m, _pol in guard})
    for values in itertools.product([False, True], repeat=len(macros)):
        env = dict(zip(macros, values))

        def present(guard):
            return all(env[m] == pol for m, pol in guard)

        alive = [i for i, guard in enumerate(guards) if present(guard)]
        if alive:
            assert not trailing[alive[-1]], (env, trailing)
        for first, second in zip(alive, alive[1:]):
            count = int(trailing[first]) + int(leading[second])
            count += sum(int(present(cond)) for cond in bridges.get(second, []))
            assert count == 1, (env, first, second, trailing, leading, bridges)


def test_nonadjacent_trailing_and_leading_do_not_double(tmp_path):
    guards = [(), (("A", True),), (("A", False),), (("A", True),)]
    _assert_plan_all_combos(guards)
    lines = [
        "module top (/*autoarg*/);",
        "output u;",
        "`ifdef A",
        "output a;",
        "`else",
        "output n;",
        "`endif",
        "`ifdef A",
        "output z;",
        "`endif",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    _assert_directives_balanced(out)
    assert arg_mod.auto_arg(out) == out
    _lint_combos(tmp_path, out, ["A"], "nonadjacent_double")


def test_nested_intermediate_guard_plan_has_disjoint_bridge():
    _assert_plan_all_combos([(), (("A", True), ("B", True)), (("A", True),)])


@pytest.mark.parametrize(
    "middle",
    [(("A", True), ("B", True)), (("A", False), ("B", True))],
    ids=["nested", "elsif-shaped"],
)
def test_multi_literal_negation_bridges_are_disjoint(middle):
    _assert_plan_all_combos([(("X", True),), middle, (("Y", True),)])


def test_elsif_intermediate_guard_autoarg(tmp_path):
    lines = [
        "module top (/*autoarg*/);",
        "output u;",
        "`ifdef A",
        "`elsif B",
        "output ab;",
        "`endif",
        "`ifdef B",
        "output b;",
        "`endif",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    _assert_directives_balanced(out)
    assert arg_mod.auto_arg(out) == out
    _lint_combos(tmp_path, out, ["A", "B"], "elsif_autoarg")


_NESTED_SUB = """\
module sub (
    input u
`ifdef A
`ifdef B
    , input ab,
`endif
`endif
`ifdef A
`ifndef B
    ,
`endif
    input a
`endif
);
endmodule
"""


def test_nested_intermediate_guard_autoinst_all_emitters(tmp_path):
    mods = {"sub": parse_module_ports(_NESTED_SUB.splitlines(), with_params=True)}
    for name, producer in [("emacs", emacs.auto_inst), ("vim", inst.auto_inst)]:
        out = producer(["module top;", "sub u_sub (/*AUTOINST*/);", "endmodule"], mods)
        _assert_directives_balanced(out)
        assert producer(out, mods) == out
        _lint_combos(
            tmp_path, out + _NESTED_SUB.splitlines(), ["A", "B"], "nested_inst_" + name
        )


def test_nested_intermediate_guard_autoarg(tmp_path):
    lines = [
        "module top (/*autoarg*/);",
        "output u;",
        "`ifdef A",
        "`ifdef B",
        "output ab;",
        "`endif",
        "`endif",
        "`ifdef A",
        "output a;",
        "`endif",
        "endmodule",
    ]
    out = arg_mod.auto_arg(lines)
    _assert_directives_balanced(out)
    assert arg_mod.auto_arg(out) == out
    _lint_combos(tmp_path, out, ["A", "B"], "nested_autoarg")


def test_nested_intermediate_guard_aio_v2k(tmp_path):
    mods = {"sub": parse_module_ports(_NESTED_SUB.splitlines())}
    top = """module top (
    /*AUTOINPUT*/
);
sub u_sub (
    .u (u)
`ifdef A
`ifdef B
    , .ab (ab),
`endif
`ifndef B
    ,
`endif
    .a (a)
`endif
);
endmodule
"""
    out = inout.auto_io(top.splitlines(), mods)
    _assert_directives_balanced(out)
    assert inout.auto_io(out, mods) == out
    _lint_combos(tmp_path, out + _NESTED_SUB.splitlines(), ["A", "B"], "nested_aio")
