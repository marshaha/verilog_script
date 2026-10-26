#!/usr/bin/env python3
"""Standard multi-instance aall benchmark.

Generates the 400-instance multidim case (a submodule with packed +
unpacked + parameterized ports, instantiated N times with varying
parameter overrides, all AUTO markers present) and times the full
`aall` pipeline on it.  With --emacs, also times real Emacs
verilog-mode (verilog-auto) on the same file for reference.

Usage:
    python tests/bench_aall_perf.py [-n 400] [--runs 3] [--emacs]

Historical reference points (same machine, N=400, median of 3):
    9d4036e (pre #16-#33 batch)   ~12.4 s
    10298e1 (batch, regression)   ~25.2 s   <- _buffer_defines per-instance
    0e5ebe3 (hoist fix)           ~13.6 s
    emacs verilog-mode            ~0.5 s    <- see notes below

Not a pytest: too slow for the gate suite.  Run it before/after perf
work, or when a batch touches the per-instance hot paths (mask_comments,
_inst_pin_scan, _auto_inst_one, read_inst_param_values).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

SUB_V = """\
module sub #(parameter W=8, parameter DEPTH=4) (
   input  clk,
   input  [W-1:0]        din,
   input  [W-1:0][3:0]   din2,
   output [W-1:0]        dout,
   output [3:0]          dout_u [DEPTH-1:0],
   output                flag,
   inout                 pad
);
endmodule
"""


def gen_case(work: Path, n: int) -> Path:
    (work / "sub.v").write_text(SUB_V)
    lines = [
        "module top;",
        "/*AUTOINPUT*/",
        "/*AUTOOUTPUT*/",
        "/*AUTOINOUT*/",
        "/*AUTOWIRE*/",
        "/*AUTOREG*/",
        "/*autodef*/",
    ]
    for i in range(n):
        lines.append(
            f"sub #(.W({8 + (i % 4) * 8}), .DEPTH({2 + (i % 3)})) u_sub_{i} (/*AUTOINST*/);"
        )
    lines.append("endmodule")
    top = work / "top.v"
    top.write_text("\n".join(lines) + "\n")
    return top


def bench_python(top: Path, work: Path, runs: int) -> list[float]:
    from verilog_tooling.inst import main as inst_main

    times = []
    for r in range(runs):
        out = work / f"out_{r}.v"
        t0 = time.monotonic()
        inst_main(["aall", "-i", str(top), "-o", str(out), "-y", str(work)])
        times.append(time.monotonic() - t0)
    return times


def bench_emacs(top: Path, work: Path) -> "float | None":
    vm = Path("/tmp/verilog-mode.el")
    if not vm.is_file():
        print("  (downloading verilog-mode.el to /tmp)")
        subprocess.run(
            ["curl", "-sL", "--max-time", "60", "-o", str(vm),
             "https://raw.githubusercontent.com/veripool/verilog-mode/master/verilog-mode.el"],
            check=True,
        )
    src = work / "top_emacs.v"
    src.write_text(top.read_text())
    elisp = work / "bench.el"
    elisp.write_text(
        f'(require \'verilog-mode "{vm}")\n'
        f'(setq verilog-library-directories \'("{work}"))\n'
        "(setq verilog-auto-inst-param-value t)\n"
        "(let ((t0 (float-time)))\n"
        f'  (find-file "{src}")\n'
        "  (verilog-mode)\n"
        "  (verilog-auto)\n"
        "  (save-buffer)\n"
        '  (message "ELAPSED %.3f" (- (float-time) t0)))\n'
    )
    proc = subprocess.run(
        ["emacs", "--batch", "-Q", "-l", str(elisp)],
        capture_output=True, text=True, cwd=work,
    )
    m = re.search(r"ELAPSED ([0-9.]+)", proc.stderr + proc.stdout)
    return float(m.group(1)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=400, help="instance count (default 400)")
    ap.add_argument("--runs", type=int, default=3, help="timed runs (default 3)")
    ap.add_argument("--emacs", action="store_true", help="also time emacs verilog-mode")
    ap.add_argument("--verbose", action="store_true", help="keep the aall per-step logs")
    args = ap.parse_args()
    if not args.verbose:
        os.environ.setdefault("VERILOG_TOOLING_QUIET", "1")

    with tempfile.TemporaryDirectory(prefix="vt_bench_") as td:
        work = Path(td)
        top = gen_case(work, args.n)
        print(f"case: {args.n} instances, aall pipeline")
        times = bench_python(top, work, args.runs)
        for i, t in enumerate(times, 1):
            print(f"  python run {i}: {t:.2f}s")
        med = sorted(times)[len(times) // 2]
        print(f"  python median: {med:.2f}s")
        if args.emacs:
            em = bench_emacs(top, work)
            print(f"  emacs verilog-auto: {em:.2f}s" if em is not None
                  else "  emacs: unavailable or failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
