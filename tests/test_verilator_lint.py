"""Verilator lint of generated code (skipped when verilator is absent).

Duplicate/conflicting declarations from AUTO regions only show up at
compile time — lint the aall output, not just its text."""

import shutil
import subprocess

import pytest

from verilog_tooling import inst

pytestmark = pytest.mark.skipif(
    shutil.which("verilator") is None, reason="verilator not installed"
)

SUB = """\
module sub (
    input  wire       clk,
    input  wire       cfg_en,
    input  wire [7:0] sel_din,
    output wire [7:0] dout,
    output wire       done
);
endmodule
"""

TOP = """\
module lint_top (
    /*autoarg*/
    /*AUTOINPUT*/
    /*AUTOOUTPUT*/
);
/*autodef*/
/*AUTOWIRE*/
/*AUTOREG*/
sub u_sub (/*autoinst*/);
assign cfg_en = 1'b1;
assign sel_din = 8'h5a;
endmodule
"""


def _lint(lines: str, libdir) -> None:
    src = libdir / "lint_top.sv"
    src.write_text(lines)
    proc = subprocess.run(
        ["verilator", "--lint-only", "-y", str(libdir), str(src), "-top-module", "lint_top"],
        capture_output=True,
        text=True,
    )
    errors = [ln for ln in proc.stdout.splitlines() if ln.startswith("%Error")]
    assert not errors, "verilator errors:\n" + "\n".join(errors)


def test_aall_output_lints_clean_twice(tmp_path):
    (tmp_path / "sub.v").write_text(SUB)
    top = tmp_path / "top.v"
    top.write_text(TOP)
    o1 = tmp_path / "o1.sv"
    inst.main(
        ["aall", "-i", str(top), "-o", str(o1), "--ref_file", str(top), "-y", str(tmp_path)]
    )
    _lint(o1.read_text(), tmp_path)
    o2 = tmp_path / "o2.sv"
    inst.main(
        ["aall", "-i", str(o1), "-o", str(o2), "--ref_file", str(o1), "-y", str(tmp_path)]
    )
    _lint(o2.read_text(), tmp_path)
