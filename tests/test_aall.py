"""aall: the single-process AALL pipeline must be byte-identical to the
sequential 8-command pipeline (eap -> eai -> aio -> aw -> areg -> adt -> arg -> af),
and the threaded/cached module resolver must keep the old priorities."""

from pathlib import Path

from verilog_tooling import arg as arg_mod
from verilog_tooling import autodef, inout, inst, wire
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
