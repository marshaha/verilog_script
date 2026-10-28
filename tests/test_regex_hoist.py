"""Guard for the regex-hoisting perf work: hot paths must reuse module-level
compiled patterns instead of passing a string pattern to ``re`` on every
line/pin/instance (each such call pays a ``re._compile`` cache lookup; the
400-instance bench showed 785k of them before hoisting, 147k after).

The test counts string-pattern ``re._compile`` calls during one small
``aall`` run and caps them.  The cap is deliberately loose (several times
the measured value) so it only catches a wholesale relapse — e.g. someone
re-introducing ``re.search("...", line)`` inside a per-line loop — not a
single new call site.
"""

import re

from verilog_tooling import inst

_SUB = """\
module sub #(parameter W = 8) (
    input  wire         clk,
    input  wire [W-1:0] din,
    output reg  [W-1:0] dout,
    output reg          done
);
endmodule
"""

_TOP = """\
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

sub #(/*AUTOINSTPARAM*/) u_sub (/*AUTOINST*/);
sub u_sub2 (/*AUTOINST*/);

// a line comment with (parens) and a /* block */ comment
/* template note: .* patterns live here, not instances */

always @(posedge clk) begin
    cnt <= 8'h00;
end
endmodule
"""


def _count_compile_calls(top, libdir, tmp_path, monkeypatch):
    libdir.mkdir(exist_ok=True)
    (libdir / "sub.v").write_text(_SUB)
    top_path = tmp_path / "top.v"
    top_path.write_text(_TOP)
    out = tmp_path / "out.v"
    calls = []
    orig = re._compile

    def counting(pattern, flags=0):
        if isinstance(pattern, str):
            calls.append(1)
        return orig(pattern, flags)

    monkeypatch.setattr(re, "_compile", counting)
    inst.main(
        ["aall", "-i", str(top_path), "-o", str(out),
         "--ref_file", str(top_path), "-y", str(libdir)]
    )
    return len(calls), out.read_text()


def test_aall_reuses_compiled_patterns(tmp_path, monkeypatch):
    n, out = _count_compile_calls(None, tmp_path / "lib", tmp_path, monkeypatch)
    assert n < 4000, f"string-pattern re._compile calls relapsed: {n}"
    # the run still expands everything (guard against a hoist that broke
    # a pattern while moving it to module level)
    assert ".clk" in out and ".dout" in out
